"""LAN-only web dashboard for the glider autopilot fleet.

Read-only by default: per-glider service state, effective config,
prediction freshness, the latest surfacing plot, and a log tail — all
derived from the same files and systemd units the VM deployment
already uses (``/srv/autopilot`` conventions; see the README).

Controls are opt-in: when the ``AUTOPILOT_WEB_PASSKEY`` environment
variable is set, each glider page gains an on/off toggle
(``systemctl enable/disable --now autopilot@<glider>``, via the
sudo-permitted root helper ``autopilot-toggle``) and a
tracking-target selector (rewrites ``predictions_dir`` in the glider's
config, preserving comments).  There are no user accounts or sessions:
every change requires typing the shared passkey into that form, and
every attempt — allowed or denied — is appended to ``audit.log``.

Run with: autopilot-web [--host 127.0.0.1] [--port 8080] [--base-dir .]
"""

from __future__ import annotations

import argparse
import hmac
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml
from flask import (
    Flask,
    abort,
    redirect,
    render_template_string,
    request,
    send_file,
    url_for,
)

GLIDER_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
LOG_TAIL_LINES = 40


# ── systemd (module-level so tests can monkeypatch) ─────────────────


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", *args], capture_output=True, text=True, timeout=10
    )


#: Root-owned helper (deploy/autopilot-toggle) that validates its
#: arguments itself: modern sudo forbids wildcards in sudoers command
#: arguments, so the sudoers rule allows exactly this path, bare.
TOGGLE_HELPER = "/usr/local/sbin/autopilot-toggle"


def _sudo_toggle(action: str, glider: str) -> subprocess.CompletedProcess:
    # -n: never prompt; fails loudly if the sudoers rule is missing.
    return subprocess.run(
        ["sudo", "-n", TOGGLE_HELPER, action, glider],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _unit_state(glider: str) -> tuple[str, str]:
    """(active, enabled) for autopilot@<glider>; 'unavailable' off-VM."""
    try:
        active = _systemctl("is-active", f"autopilot@{glider}").stdout.strip()
        enabled = _systemctl("is-enabled", f"autopilot@{glider}").stdout.strip()
        return active or "unknown", enabled or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable", "unavailable"


# ── Filesystem helpers ──────────────────────────────────────────────


def gliders(base: Path) -> list[str]:
    """Glider names, from the <name>_config.yaml naming convention."""
    return sorted(
        p.name.removesuffix("_config.yaml")
        for p in base.glob("*_config.yaml")
        if GLIDER_NAME_RE.fullmatch(p.name.removesuffix("_config.yaml"))
    )


def load_config(base: Path, glider: str) -> dict:
    with open(base / f"{glider}_config.yaml") as f:
        config = yaml.safe_load(f) or {}
    return config if isinstance(config, dict) else {}


def _file_time(path: Path) -> datetime | None:
    """Timestamp from a drifter_YYYYmmddTHHMM.csv-style filename."""
    try:
        return datetime.strptime(path.stem.rsplit("_", 1)[-1], "%Y%m%dT%H%M").replace(
            tzinfo=UTC
        )
    except ValueError:
        return None


def prediction_status(base: Path, config: dict, now: datetime) -> dict:
    """Which prediction file is in force, how old, and what's queued."""
    pred_dir = base / str(config.get("predictions_dir", "predictions"))
    pattern = str(config.get("pattern", "drifter_*.csv"))
    max_age_h = float(config.get("max_prediction_age_h", 9.0))
    timed = sorted(
        (t, p) for p in pred_dir.glob(pattern) if (t := _file_time(p)) is not None
    )
    current = [(t, p) for t, p in timed if t <= now]
    status = {
        "dir": str(pred_dir),
        "count": len(timed),
        "current": None,
        "age_h": None,
        "stale": False,
        "future": len(timed) - len(current),
    }
    if current:
        t, p = current[-1]
        age_h = (now - t).total_seconds() / 3600
        status.update(current=p.name, age_h=age_h, stale=age_h > max_age_h)
    return status


def latest_plot(base: Path, config: dict, glider: str) -> Path | None:
    plot_dir = base / str(config.get("plot_dir", "plots"))
    plots = sorted(plot_dir.glob(f"{glider}_*.png"))
    return plots[-1] if plots else None


def tracks_plot(base: Path, config: dict) -> Path | None:
    """Comparison plot (all tracking methods) for the asset behind the
    current target, if autopilot-ingest-predictions produced one."""
    path = base / str(config.get("predictions_dir", "predictions")) / "tracks.png"
    return path if path.is_file() else None


def tail_log(base: Path, glider: str, lines: int = LOG_TAIL_LINES) -> str:
    path = base / "logs" / f"{glider}.log"
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 64 * 1024))
            text = f.read().decode(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def last_state(log_text: str) -> str:
    """Most recent piloting outcome visible in the log tail."""
    for line in reversed(log_text.splitlines()):
        if "FALLBACK" in line:
            return "FALLBACK"
        if "Queued goto" in line:
            return "TRACKING"
    return ""


def target_options(base: Path, config: dict) -> list[str]:
    """Candidate predictions_dir values: predictions/ and its subdirs."""
    pattern = str(config.get("pattern", "drifter_*.csv"))
    root = base / "predictions"
    candidates = [root, *sorted(d for d in root.glob("*") if d.is_dir())]
    options = [str(d.relative_to(base)) for d in candidates if any(d.glob(pattern))]
    current = str(config.get("predictions_dir", ""))
    if current and current not in options:
        options.insert(0, current)
    return options


def _rewrite_config_line(lines: list[str], key: str, value: object) -> None:
    """Rewrite *key*'s line in *lines* in place if present, else append it."""
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}\s*:", line):
            lines[i] = f"{key}: {value}\n"
            break
    else:
        lines.append(f"{key}: {value}\n")


def set_predictions_dir(config_path: Path, new: str) -> None:
    """Rewrite only the predictions_dir line, preserving all comments."""
    lines = config_path.read_text().splitlines(keepends=True)
    _rewrite_config_line(lines, "predictions_dir", new)
    config_path.write_text("".join(lines))


def set_waypoint_offset(config_path: Path, north_m: float, east_m: float) -> None:
    """Rewrite the waypoint offset lines, preserving all comments."""
    lines = config_path.read_text().splitlines(keepends=True)
    _rewrite_config_line(lines, "waypoint_offset_north_m", north_m)
    _rewrite_config_line(lines, "waypoint_offset_east_m", east_m)
    config_path.write_text("".join(lines))


def waypoint_offset(config: dict) -> dict:
    """Current offset, tolerant of a missing/non-numeric config value."""

    def _float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    return {
        "north_m": _float(config.get("waypoint_offset_north_m", 0.0)),
        "east_m": _float(config.get("waypoint_offset_east_m", 0.0)),
    }


# ── Passkey + audit ─────────────────────────────────────────────────


def controls_enabled() -> bool:
    return bool(os.environ.get("AUTOPILOT_WEB_PASSKEY"))


def passkey_ok(supplied: str) -> bool:
    expected = os.environ.get("AUTOPILOT_WEB_PASSKEY", "")
    return bool(expected) and hmac.compare_digest(supplied.encode(), expected.encode())


def audit(base: Path, addr: str, message: str) -> None:
    line = f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {addr} {message}\n"
    with open(base / "audit.log", "a") as f:
        f.write(line)


# ── Templates ───────────────────────────────────────────────────────

STYLE = """
body { font-family: system-ui, sans-serif; margin: 2em auto; max-width: 60em;
       padding: 0 1em; }
table { border-collapse: collapse; }
th, td { text-align: left; padding: 0.3em 1em 0.3em 0; vertical-align: top; }
th { border-bottom: 1px solid #999; }
.ok { color: #1a7f37; } .bad { color: #c00; font-weight: bold; }
.muted { color: #777; font-size: 0.9em; }
img.plot { max-width: 100%; border: 1px solid #ccc; }
pre { background: #f6f6f6; padding: 0.7em; overflow-x: auto; font-size: 0.8em; }
form.control { margin-bottom: 0.8em; }
input[type=password] { width: 8em; }
.msg { background: #fffbe6; border: 1px solid #e6d87a; padding: 0.5em 1em; }
"""

INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>Glider autopilot</title><style>{{ style }}</style></head><body>
<h1>Glider autopilot</h1>
<table>
<tr><th>Glider</th><th>Service</th><th>State</th><th>Prediction in force</th></tr>
{% for g in rows %}
<tr>
<td><a href="{{ url_for('glider_page', name=g.name) }}">{{ g.name }}</a></td>
<td class="{{ 'ok' if g.active == 'active' else 'bad' }}">{{ g.active }}
    <span class="muted">({{ g.enabled }})</span></td>
<td class="{{ 'bad' if g.state == 'FALLBACK' else 'ok' }}">{{ g.state or '—' }}</td>
<td class="{{ 'bad' if g.pred.stale else '' }}">
    {% if g.pred.current %}{{ g.pred.current }} ({{ '%.1f' % g.pred.age_h }} h old)
    {% else %}none{% endif %}</td>
</tr>
{% endfor %}
</table>
<p class="muted">{{ 'Passkey controls enabled.' if controls else 'Read-only.' }}
Refreshes every 60 s.</p>
</body></html>"""

GLIDER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="120">
<title>{{ name }} — autopilot</title><style>{{ style }}</style></head><body>
<p><a href="{{ url_for('index') }}">&larr; all gliders</a></p>
<h1>{{ name }}
<span class="{{ 'ok' if active == 'active' else 'bad' }}">{{ active }}</span>
<span class="muted">({{ enabled }} at boot)</span>
{% if state %}<span class="{{ 'bad' if state == 'FALLBACK' else 'ok' }}">
    — {{ state }}</span>{% endif %}</h1>
{% if msg %}<p class="msg">{{ msg }}</p>{% endif %}

{% if controls %}
<h2>Controls</h2>
<form class="control" method="post"
      action="{{ url_for('service_action', name=name) }}">
  <input type="hidden" name="action" value="{{ 'off' if active == 'active' else 'on' }}">
  <button>Turn autopilot {{ 'OFF' if active == 'active' else 'ON' }}</button>
  passkey <input type="password" name="passkey" required>
</form>
<form class="control" method="post"
      action="{{ url_for('target_action', name=name) }}">
  target <select name="predictions_dir">
  {% for opt in targets %}
    <option value="{{ opt }}" {{ 'selected' if opt == pred.dir_rel }}>{{ opt }}</option>
  {% endfor %}
  </select>
  <button>Set target</button>
  passkey <input type="password" name="passkey" required>
</form>
<p class="muted">Target changes {{ 'apply at the next surfacing (live reload
is on).' if hot_reload else 'need a service restart (set config_file: in the
YAML to enable live reload).' }}</p>
<form class="control" method="post"
      action="{{ url_for('offset_action', name=name) }}">
  offset north <input type="number" step="any" name="north_m" value="{{ offset.north_m }}"> m,
  east <input type="number" step="any" name="east_m" value="{{ offset.east_m }}"> m
  <button>Set offset</button>
  passkey <input type="password" name="passkey" required>
</form>
<p class="muted">Negative values are south / west. Offset changes
{{ 'apply at the next surfacing (live reload is on).' if hot_reload else
'need a service restart (set config_file: in the YAML to enable live
reload).' }}</p>
{% endif %}

<h2>Prediction</h2>
<p class="{{ 'bad' if pred.stale else '' }}">
{% if pred.current %}
  In force: {{ pred.current }} ({{ '%.1f' % pred.age_h }} h old{{ ', STALE' if pred.stale }})
{% else %}No usable prediction file{% endif %}
— {{ pred.count }} file(s) in {{ pred.dir }}, {{ pred.future }} future-dated.</p>
<p>Waypoint offset: <b>{{ '%+.0f' % offset.north_m }} m north, {{ '%+.0f' % offset.east_m }} m east</b>
of the predicted target (negative = south / west).</p>

{% if tracks_plot_mtime %}
<h3>Compare tracking methods</h3>
<img class="plot" src="{{ url_for('tracks_image', name=name) }}?t={{ tracks_plot_mtime }}">
{% endif %}

<h2>Config ({{ name }}_config.yaml)</h2>
<table>
{% for key, value in config_rows %}
<tr><td class="muted">{{ key }}</td><td>{{ value }}</td></tr>
{% endfor %}
</table>

{% if plot_mtime %}
<h2>Latest plot</h2>
<img class="plot" src="{{ url_for('plot_image', name=name) }}?t={{ plot_mtime }}">
{% endif %}

<h2>Log tail</h2>
<pre>{{ log_tail or '(no log file)' }}</pre>
</body></html>"""


# ── App ─────────────────────────────────────────────────────────────


def create_app(base_dir: str | Path) -> Flask:
    app = Flask(__name__)
    base = Path(base_dir).resolve()

    def _known(name: str) -> None:
        if (
            not GLIDER_NAME_RE.fullmatch(name)
            or not (base / f"{name}_config.yaml").is_file()
        ):
            abort(404)

    def _gate(name: str, action: str) -> None:
        """403/redirect unless controls are on and the passkey matches."""
        if not controls_enabled():
            abort(403, "controls are disabled (no passkey configured)")
        if not passkey_ok(request.form.get("passkey", "")):
            audit(base, request.remote_addr or "?", f"DENIED {action} {name}")
            time.sleep(0.5)  # slow down guessing
            abort(redirect(url_for("glider_page", name=name, msg="wrong passkey")))

    @app.get("/")
    def index():
        now = datetime.now(UTC)
        rows = []
        for name in gliders(base):
            try:
                config = load_config(base, name)
            except Exception:
                config = {}
            active, enabled = _unit_state(name)
            rows.append(
                {
                    "name": name,
                    "active": active,
                    "enabled": enabled,
                    "state": last_state(tail_log(base, name)),
                    "pred": prediction_status(base, config, now),
                }
            )
        return render_template_string(
            INDEX_HTML, style=STYLE, rows=rows, controls=controls_enabled()
        )

    @app.get("/glider/<name>")
    def glider_page(name):
        _known(name)
        try:
            config = load_config(base, name)
            config_error = None
        except Exception as exc:
            config, config_error = {}, str(exc)
        active, enabled = _unit_state(name)
        pred = prediction_status(base, config, datetime.now(UTC))
        pred["dir_rel"] = str(config.get("predictions_dir", ""))
        plot = latest_plot(base, config, name)
        tplot = tracks_plot(base, config)
        log_text = tail_log(base, name)
        config_rows = (
            [("(config unreadable)", config_error)]
            if config_error
            else [(k, config[k]) for k in sorted(config)]
        )
        return render_template_string(
            GLIDER_HTML,
            style=STYLE,
            name=name,
            active=active,
            enabled=enabled,
            state=last_state(log_text),
            msg=request.args.get("msg"),
            controls=controls_enabled(),
            targets=target_options(base, config),
            hot_reload=bool(config.get("config_file")),
            pred=pred,
            offset=waypoint_offset(config),
            config_rows=config_rows,
            plot_mtime=int(plot.stat().st_mtime) if plot else None,
            tracks_plot_mtime=int(tplot.stat().st_mtime) if tplot else None,
            log_tail=log_text,
        )

    @app.get("/glider/<name>/plot.png")
    def plot_image(name):
        _known(name)
        plot = latest_plot(base, load_config(base, name), name)
        if plot is None:
            abort(404)
        return send_file(plot, max_age=0)

    @app.get("/glider/<name>/tracks.png")
    def tracks_image(name):
        _known(name)
        plot = tracks_plot(base, load_config(base, name))
        if plot is None:
            abort(404)
        return send_file(plot, max_age=0)

    @app.post("/glider/<name>/service")
    def service_action(name):
        _known(name)
        action = request.form.get("action", "")
        if action not in ("on", "off"):
            abort(400)
        _gate(name, action.upper())
        result = _sudo_toggle(action, name)
        ok = result.returncode == 0
        audit(
            base,
            request.remote_addr or "?",
            f"{action.upper()} {name} {'ok' if ok else 'FAILED'}",
        )
        msg = (
            f"autopilot turned {action}"
            if ok
            else f"toggle failed: {result.stderr.strip() or result.returncode}"
        )
        return redirect(url_for("glider_page", name=name, msg=msg))

    @app.post("/glider/<name>/target")
    def target_action(name):
        _known(name)
        new = request.form.get("predictions_dir", "")
        config = load_config(base, name)
        if new not in target_options(base, config):
            abort(400)
        _gate(name, f"TARGET={new}")
        set_predictions_dir(base / f"{name}_config.yaml", new)
        audit(base, request.remote_addr or "?", f"TARGET {name} -> {new} ok")
        applies = (
            "applies at the next surfacing"
            if config.get("config_file")
            else "restart the service to apply"
        )
        return redirect(
            url_for("glider_page", name=name, msg=f"target set to {new} ({applies})")
        )

    @app.post("/glider/<name>/offset")
    def offset_action(name):
        _known(name)
        try:
            north_m = float(request.form.get("north_m", ""))
            east_m = float(request.form.get("east_m", ""))
        except ValueError:
            abort(400)
        _gate(name, f"OFFSET north={north_m:g} east={east_m:g}")
        config = load_config(base, name)
        set_waypoint_offset(base / f"{name}_config.yaml", north_m, east_m)
        audit(
            base,
            request.remote_addr or "?",
            f"OFFSET {name} -> north={north_m:g} east={east_m:g} ok",
        )
        applies = (
            "applies at the next surfacing"
            if config.get("config_file")
            else "restart the service to apply"
        )
        return redirect(
            url_for(
                "glider_page",
                name=name,
                msg=f"offset set to {north_m:g} m north, {east_m:g} m east ({applies})",
            )
        )

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1", help="default: %(default)s")
    ap.add_argument("--port", type=int, default=8080, help="default: %(default)s")
    ap.add_argument(
        "--base-dir",
        default=".",
        help="operational directory holding <glider>_config.yaml files "
        "(default: current directory)",
    )
    args = ap.parse_args()

    from waitress import serve

    mode = "controls enabled" if controls_enabled() else "read-only"
    print(f"autopilot-web on http://{args.host}:{args.port} ({mode})")
    serve(create_app(args.base_dir), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
