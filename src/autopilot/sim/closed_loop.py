"""Closed-loop simulation of the predicted-track follower.

Each simulated surfacing is handed to the real
``PredictedTrackFollower``; the waypoint is parsed back out of the
``goto_l{N}.ma`` it emits, and the glider flies one fixed-heading
dive toward it (overshooting close waypoints) plus a random surfacing
offset of up to 250 m per hour underwater.  Outputs land under
``sim_output/``.

Run with: uv run autopilot-sim --follow-hours 72 [--synthetic] [--config ...]
"""

import argparse
import math
import random
from datetime import timedelta
from pathlib import Path
from queue import Queue
from typing import Any

import yaml
from sfmc_api import SensorReading, SurfacingEvent, dddmm_to_decimal

from autopilot.follower import PredictedTrackFollower, distance_m
from autopilot.sim.mock_data import (
    GLIDER_NAME,
    GLIDER_START,
    M_PER_DEG_LAT,
    TRUTH_START,
    dialog_block,
    load_real_truth,
    make_truth,
    step,
    truth_velocity,
    write_predictions,
)

GLIDER_SPEED = 0.25  # m/s through water
MAX_OFFSET_M_PER_H = 250.0  # random surfacing offset per hour underwater
INTERVAL_H = 3.0
SEED = 1  # fix for a reproducible run; change for a different realization

# Defaults are anchored to the repo root so the CLI works from any cwd
# (dev-only data: the MAT tracks and sample config are not installed).
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MAT = str(
    REPO_ROOT / "20180724_20180806/Floats/"
    "MIT_RIOT_Traj_2018072200_2018072400_2018080600_0200m.mat"
)
DEFAULT_CONFIG = REPO_ROOT / "osu999_config.yaml"


def parse_goto_waypoint(ma_content: str) -> tuple[float, float]:
    """Extract the first (lon, lat) in decimal degrees from a goto .ma file."""
    in_wpts = False
    for line in ma_content.splitlines():
        if line.startswith("<start:waypoints>"):
            in_wpts = True
        elif line.startswith("<end:waypoints>"):
            break
        elif in_wpts:
            lon_ddmm, lat_ddmm = (float(v) for v in line.split())
            return dddmm_to_decimal(lon_ddmm), dddmm_to_decimal(lat_ddmm)
    raise ValueError("No waypoint found in .ma content")


def run_sim(
    truth: dict[float, tuple[float, float]],
    glider_start: tuple[float, float],
    follow_hours: float = 24.0,
    config_path: str | Path = DEFAULT_CONFIG,
    out_root: str | Path = "sim_output",
    seed: int = SEED,
) -> dict[str, Any]:
    """Run one closed-loop scenario; returns per-surfacing results.

    All outputs (predictions, plots, .ma archive, dialog log) are
    written under *out_root*, so tests can point this at a temporary
    directory.

    Returns a dict with ``separations`` (km, one per surfacing, vs the
    true drifter position), ``glider_track`` [(lat, lon)],
    ``waypoints`` [(lon, lat) commanded after each surfacing], and the
    effective follower ``config``.
    """
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    config_path = Path(config_path)
    with open(config_path) as f:
        config = yaml.safe_load(f)
    # Relative paths inside the config resolve against the config file,
    # not the cwd.
    for key in ("geofence", "bathymetry"):
        if config.get(key) and not Path(config[key]).is_absolute():
            config[key] = str(config_path.parent / config[key])
    # Output dirs keep their configured names but are sandboxed under
    # out_root so a sim run can never mix with live operational output.
    for key, default in (
        ("predictions_dir", "predictions"),
        ("plot_dir", "plots"),
        ("archive_dir", "ma_archive"),
    ):
        config[key] = str(out_root / Path(config.get(key, default)).name)

    # First surfacing is 24 h into the truth window (so the first
    # prediction file has a full hindcast); predictions every 6 h after.
    prediction_times = [24 + 6 * k for k in range(int(follow_hours / 6) + 1)]
    n_surfacings = int(follow_hours / INTERVAL_H) + 1
    print(
        f"Following for {follow_hours:.0f} h: {n_surfacings} surfacings, "
        f"{len(prediction_times)} prediction files"
    )
    write_predictions(truth, prediction_times, outdir=config["predictions_dir"])
    # Fixed plot extent for the whole run: full truth track + 6 km margin
    # (in live use this would be set in the config, e.g. the operating area).
    t_lats = [la for la, _ in truth.values()]
    t_lons = [lo for _, lo in truth.values()]
    if config.get("safe_point"):
        t_lons.append(config["safe_point"][0])
        t_lats.append(config["safe_point"][1])
    pad_lat = 6000.0 / M_PER_DEG_LAT
    pad_lon = pad_lat / math.cos(math.radians(sum(t_lats) / len(t_lats)))
    config["plot_bounds"] = [
        min(t_lons) - pad_lon,
        max(t_lons) + pad_lon,
        min(t_lats) - pad_lat,
        max(t_lats) + pad_lat,
    ]

    queue_in: Queue = Queue()
    queue_out: Queue = Queue()
    follower = PredictedTrackFollower(config, queue_in, queue_out)

    rng = random.Random(seed)
    lat, lon = glider_start
    lines: list[str] = []
    separations: list[float] = []
    glider_track: list[tuple[float, float]] = []
    waypoints: list[tuple[float, float]] = []
    waypoint: tuple[float, float] | None = None

    for i in range(n_surfacings):
        t_h = 24.0 + INTERVAL_H * i
        t = TRUTH_START + timedelta(hours=t_h)

        # Record this surfacing in the dialog log.
        v_east, v_north = truth_velocity(truth, t_h)
        mt = int((t_h - 24.0) * 3600 + 5000)
        lines += dialog_block(
            GLIDER_NAME, t, lat, lon, v_east, v_north, mt, 13.2 - 0.02 * i
        )
        glider_track.append((lat, lon))

        # Separation from the *true* drifter position (the score).
        d_lat, d_lon = truth[t_h]
        sep_km = distance_m(lon, lat, d_lon, d_lat) / 1000
        separations.append(sep_km)
        print(
            f"--- surfacing {i + 1}/{n_surfacings} at {t:%dT%H:%M}: "
            f"true separation {sep_km:.1f} km"
        )

        # Hand the surfacing to the real follower and collect its goto file.
        event = SurfacingEvent(
            vehicle_name=GLIDER_NAME,
            timestamp=t,
            mission_time=float(mt),
            gps_lat=lat,
            gps_lon=lon,
            sensors={
                "m_water_vx": SensorReading("m_water_vx", "m/s", v_east, 40.0),
                "m_water_vy": SensorReading("m_water_vy", "m/s", v_north, 40.0),
            },
        )
        follower.on_surfacing(event)
        while not queue_out.empty():
            for _, content in queue_out.get().get("to-glider", {}).items():
                waypoint = parse_goto_waypoint(content)
        if waypoint is not None:
            waypoints.append(waypoint)

        # Fly one dive at a fixed heading toward the commanded waypoint,
        # covering the full dive's travel distance: if the waypoint is
        # closer than that, the glider overshoots it (no stopping or
        # re-steering mid-dive). Keeps the previous waypoint if no new
        # file was produced.
        if waypoint is not None:
            w_lon, w_lat = waypoint
            dx = (w_lon - lon) * M_PER_DEG_LAT * math.cos(math.radians(lat))
            dy = (w_lat - lat) * M_PER_DEG_LAT
            dist = math.hypot(dx, dy)
            if dist > 0:
                dive_time = INTERVAL_H * 3600
                lat, lon = step(
                    lat,
                    lon,
                    dx / dist * GLIDER_SPEED,
                    dy / dist * GLIDER_SPEED,
                    dive_time,
                )

        # Random surfacing offset: up to 250 m per hour underwater.
        r = rng.uniform(0, MAX_OFFSET_M_PER_H * INTERVAL_H)
        theta = rng.uniform(0, 2 * math.pi)
        lat, lon = step(lat, lon, r * math.sin(theta), r * math.cos(theta), 1.0)

    dialog_path = out_root / "closed_loop_dialog.log"
    dialog_path.write_text("\n".join(lines) + "\n")

    settled = separations[len(separations) // 2 :]
    print(f"\nWrote {dialog_path} ({n_surfacings} surfacings)")
    print(
        f"Separation: start {separations[0]:.1f} km, "
        f"final {separations[-1]:.1f} km, "
        f"second-half mean {sum(settled) / len(settled):.1f} km, "
        f"max {max(settled):.1f} km"
    )
    within = sum(1 for s in settled if s <= config["target_radius_km"])
    print(
        f"Second-half surfacings within {config['target_radius_km']:.0f} km: "
        f"{within}/{len(settled)}"
    )

    return {
        "separations": separations,
        "glider_track": glider_track,
        "waypoints": waypoints,
        "config": config,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--synthetic",
        action="store_true",
        help="use the synthetic curving drifter instead of the MAT track",
    )
    ap.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help="follower config YAML (default: %(default)s)",
    )
    ap.add_argument("--mat", default=DEFAULT_MAT, help="float trajectory MAT file")
    ap.add_argument("--float-id", type=int, default=6000, help="float number (1-based)")
    ap.add_argument(
        "--start-hour",
        type=float,
        default=0.0,
        help="hours into the float record to start the truth window",
    )
    ap.add_argument(
        "--follow-hours",
        type=float,
        default=24.0,
        help="duration of following after the first surfacing",
    )
    args = ap.parse_args()

    truth_hours = 24.0 + args.follow_hours
    if args.synthetic:
        truth = make_truth(truth_hours)
        glider_start = GLIDER_START
        print("Scenario: synthetic drifter")
    else:
        truth = load_real_truth(args.mat, args.float_id, args.start_hour, truth_hours)
        # Start the glider 3 km south of the drifter at the first surfacing:
        # tests station keeping against realistic motion, not a long chase.
        d_lat, d_lon = truth[24.0]
        glider_start = (d_lat - 3000.0 / M_PER_DEG_LAT, d_lon)
        print(f"Scenario: simulated float {args.float_id} from {args.mat}")

    run_sim(
        truth, glider_start, follow_hours=args.follow_hours, config_path=args.config
    )


if __name__ == "__main__":
    main()
