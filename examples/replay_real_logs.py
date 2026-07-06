"""Build a replay scenario from real per-call SFMC network logs.

Stitches the log files chronologically, extracts the glider's real
surfaced positions, smooths them into a "drifter" truth track (no real
drifter existed for these deployments, so the glider follows its own
history), and writes prediction files, a rectangular test fence, and a
follower config.  Prints the sfmc-follow command to run the replay.

Usage:
    uv run python examples/replay_real_logs.py examples_logs/sl684 replay_sl684
"""

import json
import sys
from datetime import timedelta
from pathlib import Path

import yaml
from sfmc_api.dialog_parser import DialogParser


def main() -> None:
    logs = Path(sys.argv[1])
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "replay_" + logs.name)
    out.mkdir(parents=True, exist_ok=True)

    # ── 1. Stitch per-call logs chronologically (names sort by time) ──
    files = sorted(logs.glob("*.log"))
    stitched = out / "dialog.log"
    stitched.write_text("".join(f.read_text(errors="replace") for f in files))
    print(f"stitched {len(files)} files -> {stitched}")

    # ── 2. Extract real surfacing positions ──────────────────────────
    parser = DialogParser()
    fixes = []  # (datetime, lat, lon)
    glider = "unknown"
    for f in files:
        for line in f.read_text(errors="replace").splitlines():
            ev = parser.feed_line(line)
            if ev and ev.timestamp and ev.gps_lat is not None:
                fixes.append((ev.timestamp, ev.gps_lat, ev.gps_lon))
                glider = ev.vehicle_name
    print(
        f"{len(fixes)} GPS fixes from {glider}, "
        f"{fixes[0][0]:%Y-%m-%d %H:%M} .. {fixes[-1][0]:%Y-%m-%d %H:%M}"
    )

    # ── 3. Smooth into a "drifter" truth on a 1-h grid ───────────────
    t0 = fixes[0][0]
    hours = [(t - t0).total_seconds() / 3600 for t, _, _ in fixes]
    lats = [la for _, la, _ in fixes]
    lons = [lo for _, _, lo in fixes]
    span = hours[-1]

    def interp(h, xs):
        for i in range(len(hours) - 1):
            if hours[i] <= h <= hours[i + 1]:
                w = (h - hours[i]) / (hours[i + 1] - hours[i])
                return xs[i] * (1 - w) + xs[i + 1] * w
        return xs[-1]

    grid = [float(i) for i in range(int(span) + 1)]
    truth = {}  # hour -> (lat, lon), 5-h boxcar over the raw fixes
    for h in grid:
        win = [g for g in grid if abs(g - h) <= 2.5]
        truth[h] = (
            sum(interp(g, lats) for g in win) / len(win),
            sum(interp(g, lons) for g in win) / len(win),
        )

    # ── 4. Prediction files: every 6 h, 24 h hindcast + 12 h forecast ─
    pred_dir = out / "predictions"
    pred_dir.mkdir(exist_ok=True)
    n = 0
    h = 6.0
    while h < span:
        created = t0 + timedelta(hours=h)
        rows = ["time,latitude,longitude"]
        for dh in range(-24, 14, 2):
            th = min(max(h + dh, 0.0), span // 1)
            la, lo = truth[float(int(th))]
            rows.append(f"{(t0 + timedelta(hours=th)).isoformat()},{la:.5f},{lo:.5f}")
        (pred_dir / f"drifter_{created:%Y%m%dT%H%M}.csv").write_text(
            "\n".join(rows) + "\n"
        )
        n += 1
        h += 6.0
    print(f"wrote {n} prediction files")

    # ── 5. Rectangular test fence around the operating box ───────────
    lon0, lon1 = min(lons) - 0.12, max(lons) + 0.12
    lat0, lat1 = min(lats) - 0.10, max(lats) + 0.10
    fence = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": f"{logs.name} replay test fence"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [lon0, lat0],
                            [lon1, lat0],
                            [lon1, lat1],
                            [lon0, lat1],
                            [lon0, lat0],
                        ]
                    ],
                },
            }
        ],
    }
    (out / "test_fence.geojson").write_text(json.dumps(fence, indent=1))
    safe = (round((lon0 + lon1) / 2, 3), round((lat0 + lat1) / 2, 3))

    # ── 6. Config ─────────────────────────────────────────────────────
    config = {
        "predictions_dir": str(pred_dir),
        "pattern": "drifter_*.csv",
        "speed_horizontal": 0.25,
        "target_radius_km": 4.0,
        "sequence_number": 10,
        "geofence": str(out / "test_fence.geojson"),
        "fence_margin_km": 2.0,
        "safe_point": list(safe),
        "max_prediction_age_h": 12,
        "max_waypoint_jump_km": 30,
        "plot_dir": str(out / "plots"),
        "archive_dir": str(out / "goto_archive"),
    }
    config_path = out / "replay_config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))

    print(
        f"\nReplay with:\n"
        f"uv run sfmc-follow --glider {glider} "
        f"--follower src/autopilot/follower.py \\\n"
        f"    --config {config_path} --replay {stitched} "
        f"--replay-interval 0 --dry-run"
    )


if __name__ == "__main__":
    main()
