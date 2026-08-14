"""Detect a fully-missing file transfer — not "arrived with bad
content" (that's watch.py's job), but "never arrived at all" for
longer than expected, while the glider is demonstrably still calling
in normally.

Real evidence motivating this (examples_logs/sl1267/logs/): about 22%
of surfacings with a normal, successful .sbd transfer had zero .tbd
activity at all, including sessions where the science computer's own
log shows it running normally — the file was simply never queued for
transfer that call. watch.py can't see this: it only evaluates files
that actually arrive, so a call with no .tbd file at all leaves no
trace for it to react to.

A single missing session isn't itself notable — gaps like this are
common and usually self-resolve next call. What matters is a *primary*
file type (e.g. .tbd) going missing for substantially longer than
expected while a *corroboration* file type (e.g. .sbd) keeps arriving
on schedule — that combination means the glider is still alive and
calling in normally, so the primary type's absence isn't explained by
"hasn't surfaced." Comparing the newest-file age of two already-
mirrored directories is deliberately a plain mtime comparison, not log
parsing: it doesn't care *why* a file didn't show up, only that a lot
of calls' worth of time passed without one while another file kept
arriving on schedule. Requiring both a stale primary *and* a fresh
corroboration also means a real comms outage (nothing arriving at all)
never falsely fires this as a tbd-specific problem — see
test_no_alert_when_corroboration_also_stale.

Same separate-process principle as watch.py: this only reads directory
mtimes and never touches the SFMC API, STOMP, or predictions/.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .slack import SendFn, make_slack_send
from .watch import dinkum_name_re

logger = logging.getLogger("autopilot.acoustic")


def newest_mtime(directory: Path, name_pattern: re.Pattern[str]) -> float | None:
    """Newest mtime among files in *directory* matching *name_pattern*.

    None if the directory has no matching files (yet) — a new or
    not-yet-populated mirror, distinct from a real gap, so callers
    must never alert off it.
    """
    mtimes = [
        p.stat().st_mtime for p in directory.iterdir() if name_pattern.match(p.name)
    ]
    return max(mtimes) if mtimes else None


@dataclass
class StalenessState:
    in_alert: bool = False


def load(path: Path) -> dict[str, StalenessState]:
    """Load per-glider alert state; an absent or corrupt file starts empty."""
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    state: dict[str, StalenessState] = {}
    for glider, entry in raw.items():
        if isinstance(entry, dict):
            state[glider] = StalenessState(in_alert=bool(entry.get("in_alert", False)))
    return state


def save(path: Path, state: dict[str, StalenessState]) -> None:
    """Atomically write state (write-then-rename)."""
    payload = {glider: {"in_alert": entry.in_alert} for glider, entry in state.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(path)


def check(
    glider: str,
    state: dict[str, StalenessState],
    *,
    primary_age_minutes: float | None,
    corroboration_age_minutes: float | None,
    max_gap_minutes: float,
    corroboration_window_minutes: float,
) -> str | None:
    """Pure state transition, mirroring ledger.record's shape.

    ``None`` ages mean "no matching file has ever been seen" (an empty
    or not-yet-populated mirror directory) and never drive a
    transition either way — this is a setup/deployment state, not a
    live signal.

    Returns "alert", "recovery", or None (no transition).
    """
    entry = state.setdefault(glider, StalenessState())

    if primary_age_minutes is None or corroboration_age_minutes is None:
        return None

    stale = primary_age_minutes > max_gap_minutes
    corroborated = corroboration_age_minutes <= corroboration_window_minutes

    if stale and corroborated and not entry.in_alert:
        entry.in_alert = True
        return "alert"
    if not stale and entry.in_alert:
        entry.in_alert = False
        return "recovery"
    return None


def _notify(
    send: SendFn,
    glider: str,
    transition: str,
    primary_age_minutes: float | None,
    max_gap_minutes: float,
) -> None:
    try:
        if transition == "alert":
            send(
                f"{glider}: no data received in over {max_gap_minutes:.0f} min",
                f"Newest matching file is {primary_age_minutes:.0f} min old, but the "
                "corroborating file type is still arriving on schedule -- the glider "
                "is calling in normally, so this isn't explained by a missed surfacing.",
            )
        else:
            send(
                f"{glider}: data resumed",
                "A new file has arrived; staleness alert cleared.",
            )
    except Exception:
        logger.exception("Slack delivery failed for %s (%s)", glider, transition)


def scan_once(
    glider: str,
    primary_dir: Path,
    primary_pattern: re.Pattern[str],
    corroboration_dir: Path,
    corroboration_pattern: re.Pattern[str],
    state: dict[str, StalenessState],
    max_gap_minutes: float,
    corroboration_window_minutes: float,
    send: SendFn | None,
) -> None:
    now = time.time()
    primary_mtime = newest_mtime(primary_dir, primary_pattern)
    corroboration_mtime = newest_mtime(corroboration_dir, corroboration_pattern)
    primary_age = (now - primary_mtime) / 60.0 if primary_mtime is not None else None
    corroboration_age = (
        (now - corroboration_mtime) / 60.0 if corroboration_mtime is not None else None
    )

    transition = check(
        glider,
        state,
        primary_age_minutes=primary_age,
        corroboration_age_minutes=corroboration_age,
        max_gap_minutes=max_gap_minutes,
        corroboration_window_minutes=corroboration_window_minutes,
    )
    if primary_age is not None:
        logger.info(
            "%s: newest matching file %.1f min old%s",
            glider,
            primary_age,
            f" ({transition})" if transition else "",
        )
    if transition and send:
        _notify(send, glider, transition, primary_age, max_gap_minutes)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s  %(message)s")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--glider", required=True, help="registered glider name")
    ap.add_argument(
        "--primary-dir",
        required=True,
        help="mirror directory for the file type that may go missing (e.g. .tbd)",
    )
    ap.add_argument("--primary-extension", default="tbd")
    ap.add_argument(
        "--corroboration-dir",
        required=True,
        help="mirror directory for a file type that reliably keeps arriving (e.g. .sbd)",
    )
    ap.add_argument("--corroboration-extension", default="sbd")
    ap.add_argument(
        "--max-gap-minutes",
        type=float,
        required=True,
        help="alert if the primary type hasn't arrived in longer than this",
    )
    ap.add_argument(
        "--corroboration-window-minutes",
        type=float,
        required=True,
        help="only alert if the corroboration type has arrived within this long "
        "(i.e. the glider is still calling in normally)",
    )
    ap.add_argument("--state", required=True, help="state file path")
    ap.add_argument("--poll-seconds", type=float, default=60.0)
    ap.add_argument(
        "--slack-webhook-url-file",
        default=None,
        help="file containing the Slack webhook URL; omit to log transitions only",
    )
    args = ap.parse_args()

    primary_dir = Path(args.primary_dir)
    corroboration_dir = Path(args.corroboration_dir)
    state_path = Path(args.state)
    state = load(state_path)
    primary_pattern = dinkum_name_re(args.primary_extension)
    corroboration_pattern = dinkum_name_re(args.corroboration_extension)

    send: SendFn | None = None
    if args.slack_webhook_url_file:
        webhook_url = Path(args.slack_webhook_url_file).read_text().strip()
        send = make_slack_send(webhook_url)

    logger.info(
        "Watching %s (*.%s) against %s (*.%s): max gap %.0f min, corroboration window %.0f min",
        primary_dir,
        args.primary_extension,
        corroboration_dir,
        args.corroboration_extension,
        args.max_gap_minutes,
        args.corroboration_window_minutes,
    )
    while True:
        try:
            scan_once(
                args.glider,
                primary_dir,
                primary_pattern,
                corroboration_dir,
                corroboration_pattern,
                state,
                args.max_gap_minutes,
                args.corroboration_window_minutes,
                send,
            )
            save(state_path, state)
        except Exception:
            logger.exception("Scan failed; will retry")
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
