"""Pre-generate time-shifted drifter predictions for a live glider test.

Queries SFMC for the glider's last valid GPS fix, aligns the start of a
simulated float track (MAT file) with that time, and writes the whole
deployment's worth of 6-hourly prediction files in one shot.  The
follower only reads the newest file created at or before each surfacing,
so the future-dated files lie dormant until the wall clock reaches them.

Usage:
    autopilot-live-drifter FLOATS.mat --glider osusim --list 10
    autopilot-live-drifter FLOATS.mat --glider osusim --float-id 2000 \
        --outdir /srv/autopilot/predictions
"""

import argparse
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from scipy.io import loadmat

from autopilot.follower import distance_m

STEP_H = 2  # spacing of rows within a file
EVERY_H = 6  # spacing of file creation times
HINDCAST_H = 24
FORECAST_H = 12


def load_track(
    mat_path: str | Path, float_id: int, start_h: float = 0.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(t_h, lat, lon) for one float, hours rebased to zero at *start_h*."""
    fl = loadmat(mat_path, squeeze_me=True)
    col = float_id - 1  # 1-based ids, as in drifter_tracks.ipynb
    t_h = (fl["TIME"][:, col] - fl["TIME"][0, col]) * 24.0
    lat, lon = fl["LAT"][:, col], fl["LON"][:, col]
    ok = (t_h >= start_h) & np.isfinite(lat) & np.isfinite(lon)
    if ok.sum() < 2:
        raise SystemExit(f"Float {float_id}: no track after start_h={start_h}")
    return t_h[ok] - start_h, lat[ok], lon[ok]


def rank_floats(
    mat_path: str | Path, glider_lat: float, glider_lon: float, n: int
) -> list[tuple[float, int]]:
    """The n floats nearest the glider at their track start: (km, id)."""
    fl = loadmat(mat_path, squeeze_me=True)
    dists = [
        (distance_m(glider_lon, glider_lat, lon0, lat0) / 1000, col + 1)
        for col, (lat0, lon0) in enumerate(zip(fl["LAT"][0, :], fl["LON"][0, :]))
        if math.isfinite(lat0) and math.isfinite(lon0)
    ]
    return sorted(dists)[:n]


def glider_fix(
    glider: str, credentials: str | None = None, host: str | None = None
) -> tuple[datetime, float, float]:
    """(time, lat, lon) of the glider's last valid GPS fix, from SFMC."""
    from sfmc_api import SFMCClient
    from sfmc_api.coordinates import dddmm_to_decimal

    with SFMCClient(host=host, config_path=credentials) as client:
        details = client.get_active_deployment_details(glider)
    d = details.get("data", details)
    when = d.get("gpsValidDateTime") or d.get("gpsDateTime")
    if not when or d.get("gpsValidLat") is None:
        raise SystemExit(f"No valid GPS fix in active deployment for {glider}")
    t = datetime.strptime(when, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return t, dddmm_to_decimal(d["gpsValidLat"]), dddmm_to_decimal(d["gpsValidLon"])


def write_shifted(
    t_h: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    anchor: datetime,
    outdir: str | Path,
) -> list[Path]:
    """Write all prediction files, track hour 0 mapped to *anchor*."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    end_h = int(t_h[-1])
    paths = []
    for c_h in range(0, end_h + 1, EVERY_H):
        created = anchor + timedelta(hours=c_h)
        rows = []
        for h in range(c_h - HINDCAST_H, c_h + FORECAST_H + 1, STEP_H):
            if 0 <= h <= end_h:
                la = float(np.interp(h, t_h, lat))
                lo = float(np.interp(h, t_h, lon))
                rows.append((anchor + timedelta(hours=h), la, lo))
        path = outdir / f"drifter_{created:%Y%m%dT%H%M}.csv"
        with open(path, "w") as f:
            f.write("time,latitude,longitude\n")
            for t, la, lo in rows:
                f.write(f"{t:%Y-%m-%dT%H:%M:%S}+00:00,{la:.6f},{lo:.6f}\n")
        paths.append(path)
    return paths


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("mat", help="float trajectory MAT file (TIME/LAT/LON)")
    ap.add_argument("--glider", required=True, help="registered glider name")
    ap.add_argument(
        "--float-id", type=int, default=None, help="float to replay (1-based)"
    )
    ap.add_argument(
        "--list",
        type=int,
        default=None,
        metavar="N",
        help="rank the N floats nearest the glider, then exit",
    )
    ap.add_argument(
        "--start-h",
        type=float,
        default=0.0,
        help="skip this many hours into the float track (default: 0)",
    )
    ap.add_argument("--outdir", default="predictions", help="prediction directory")
    ap.add_argument("--credentials", default=None, help="SFMC credentials JSON")
    ap.add_argument(
        "--host", default=None, help="SFMC hostname (multi-host credentials)"
    )
    args = ap.parse_args()

    fix_time, g_lat, g_lon = glider_fix(args.glider, args.credentials, args.host)
    print(
        f"{args.glider} last fix: {fix_time:%Y-%m-%d %H:%M} UTC at {g_lat:.4f}, {g_lon:.4f}"
    )

    if args.list:
        for km, fid in rank_floats(args.mat, g_lat, g_lon, args.list):
            print(f"  float {fid:5d}  starts {km:6.1f} km from glider")
        return
    if args.float_id is None:
        ap.error("--float-id is required (use --list N to rank candidates)")

    t_h, lat, lon = load_track(args.mat, args.float_id, args.start_h)
    anchor = fix_time.replace(minute=0, second=0, microsecond=0)
    sep_km = distance_m(g_lon, g_lat, float(lon[0]), float(lat[0])) / 1000
    print(
        f"float {args.float_id} starts {sep_km:.1f} km from glider; "
        f"{t_h[-1]:.0f} h of track from {anchor:%Y-%m-%d %H:%M} UTC"
    )
    if sep_km > 30:
        print("WARNING: exceeds the 30 km max_waypoint_jump_km — expect FALLBACK")

    paths = write_shifted(t_h, lat, lon, anchor, args.outdir)
    print(f"Wrote {len(paths)} prediction files to {Path(args.outdir).resolve()}")
    print(f"Last file: {paths[-1].name} (predictions run out after that + 12 h)")


if __name__ == "__main__":
    main()
