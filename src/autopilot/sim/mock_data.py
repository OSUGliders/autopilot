"""Building blocks for simulated scenarios.

Drifter truth tracks (synthetic curve or MIT RIOT MAT files),
prediction files (24 h hindcast + 12 h velocity extrapolation, 2-h
steps), Slocum dialog snippets, and small kinematics helpers — used
by ``autopilot.sim.closed_loop`` and the tests.

``uv run autopilot-mock-data`` writes a synthetic open-loop demo set:
``predictions/`` plus ``predicted_dialog.log`` (a glider pursuing the
drifter, for sfmc-follow --replay).
"""

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sfmc_api import decimal_to_dddmm

M_PER_DEG_LAT = 111320.0

TRUTH_START = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)  # 24 h before first prediction
TRUTH_HOURS = 48
STEP_H = 0.5

DRIFTER_START = (44.60, -124.50)  # lat, lon
DRIFTER_SPEED = 0.15  # m/s
GLIDER_START = (44.56, -124.50)  # ~4.4 km south of the drifter
GLIDER_SPEED = 0.35  # m/s
GLIDER_NAME = "osu999"

PREDICTION_TIMES_H = [24, 30, 36]  # hours after TRUTH_START, 6 h apart
SURFACING_TIMES_H = [24 + 3 * i for i in range(9)]  # every 3 h for 24 h


def step(lat: float, lon: float, v_east: float, v_north: float, dt_s: float):
    lat2 = lat + v_north * dt_s / M_PER_DEG_LAT
    lon2 = lon + v_east * dt_s / (M_PER_DEG_LAT * math.cos(math.radians(lat)))
    return lat2, lon2


def make_truth(hours: float = TRUTH_HOURS) -> dict[float, tuple[float, float]]:
    """True drifter track: heading rotates 90 degrees (NW to SW) over *hours*."""
    lat, lon = DRIFTER_START
    truth = {0.0: (lat, lon)}
    n = int(hours / STEP_H)
    for i in range(n):
        heading = math.radians(315.0 - 90.0 * (i * STEP_H) / hours)
        v_east = DRIFTER_SPEED * math.sin(heading)
        v_north = DRIFTER_SPEED * math.cos(heading)
        lat, lon = step(lat, lon, v_east, v_north, STEP_H * 3600)
        truth[(i + 1) * STEP_H] = (lat, lon)
    return truth


def load_real_truth(
    mat_path: str,
    float_id: int = 6000,
    start_h: float = 0.0,
    hours: float = TRUTH_HOURS,
) -> dict[float, tuple[float, float]]:
    """Truth track from a simulated float (MAT file), same shape as make_truth().

    Reads LAT/LON/TIME for *float_id* (1-based, as in drifter_tracks.ipynb),
    takes the *hours*-long window beginning *start_h* hours into the record,
    and resamples it onto the 0.5-h truth grid starting at TRUTH_START (the
    original simulation dates are irrelevant here).
    """
    import numpy as np
    from scipy.io import loadmat

    fl = loadmat(mat_path, squeeze_me=True)
    col = float_id - 1
    t_h = (fl["TIME"][:, col] - fl["TIME"][0, col]) * 24.0 - start_h
    if t_h[-1] < hours:
        raise ValueError(f"Track too short: {t_h[-1]:.0f} h available after start_h")
    grid = np.arange(0.0, hours + STEP_H / 2, STEP_H)
    lat = np.interp(grid, t_h, fl["LAT"][:, col])
    lon = np.interp(grid, t_h, fl["LON"][:, col])
    return {float(h): (float(la), float(lo)) for h, la, lo in zip(grid, lat, lon)}


def truth_velocity(
    truth: dict[float, tuple[float, float]], t_h: float
) -> tuple[float, float]:
    """(v_east, v_north) in m/s by finite difference of the truth track."""
    h0 = max(0.0, t_h - STEP_H)
    h1 = min(max(truth), t_h + STEP_H)
    (lat0, lon0), (lat1, lon1) = truth[h0], truth[h1]
    dt = (h1 - h0) * 3600
    v_north = (lat1 - lat0) * M_PER_DEG_LAT / dt
    v_east = (lon1 - lon0) * M_PER_DEG_LAT * math.cos(math.radians(lat0)) / dt
    return v_east, v_north


def write_predictions(
    truth: dict[float, tuple[float, float]],
    times_h: list[int] = PREDICTION_TIMES_H,
    outdir: str | Path = "predictions",
) -> None:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    for t_create in times_h:
        created = TRUTH_START + timedelta(hours=t_create)
        rows = []
        # Hindcast: true positions, 24 h back in 2-h steps.
        for h in range(t_create - 24, t_create + 1, 2):
            lat, lon = truth[float(h)]
            rows.append((TRUTH_START + timedelta(hours=h), lat, lon))
        # Forecast: linear extrapolation of the last 6 h of motion.
        lat_c, lon_c = truth[float(t_create)]
        lat_p, lon_p = truth[float(t_create - 6)]
        dlat_per_h = (lat_c - lat_p) / 6
        dlon_per_h = (lon_c - lon_p) / 6
        for h in range(2, 13, 2):
            rows.append(
                (
                    created + timedelta(hours=h),
                    lat_c + dlat_per_h * h,
                    lon_c + dlon_per_h * h,
                )
            )
        path = outdir / f"drifter_{created:%Y%m%dT%H%M}.csv"
        with open(path, "w") as f:
            f.write("time,latitude,longitude\n")
            for t, lat, lon in rows:
                f.write(f"{t:%Y-%m-%dT%H:%M:%S}+00:00,{lat:.6f},{lon:.6f}\n")
        print(f"Wrote {path} ({len(rows)} rows)")


def drifter_velocity(t_h: float) -> tuple[float, float]:
    """True drifter (v_east, v_north) in m/s at *t_h* hours."""
    heading = math.radians(315.0 - 90.0 * t_h / TRUTH_HOURS)
    return DRIFTER_SPEED * math.sin(heading), DRIFTER_SPEED * math.cos(heading)


def dialog_block(
    name: str,
    t: datetime,
    lat: float,
    lon: float,
    v_east: float,
    v_north: float,
    mt: int,
    battery: float,
) -> list[str]:
    """One surfacing in Slocum dialog format."""
    return [
        "Carrier Detect found",
        f"Vehicle Name: {name}",
        f"Curr Time: {t:%a %b %d %H:%M:%S %Y} MT:  {mt}",
        f"GPS Location:  {decimal_to_dddmm(lat):.3f} N {decimal_to_dddmm(lon):.3f} E"
        f" measured     35.000 secs ago",
        f"sensor:m_water_vx(m/s)={v_east:.6f}           40.000 secs ago",
        f"sensor:m_water_vy(m/s)={v_north:.6f}          40.000 secs ago",
        f"sensor:m_battery(volts)={battery:.4f}           10.000 secs ago",
        "ABORT HISTORY: none",
    ]


def write_dialog(truth: dict[float, tuple[float, float]]) -> None:
    lat, lon = GLIDER_START
    lines = []
    for i, t_h in enumerate(SURFACING_TIMES_H):
        t = TRUTH_START + timedelta(hours=t_h)
        v_east, v_north = drifter_velocity(t_h)
        mt = int((t_h - SURFACING_TIMES_H[0]) * 3600 + 5000)
        lines += dialog_block(GLIDER_NAME, t, lat, lon, v_east, v_north, mt, 13.2 - 0.02 * i)
        # Pursue the drifter's true position at this surfacing for 3 h.
        target_lat, target_lon = truth[float(t_h)]
        dx = (target_lon - lon) * M_PER_DEG_LAT * math.cos(math.radians(lat))
        dy = (target_lat - lat) * M_PER_DEG_LAT
        dist = math.hypot(dx, dy)
        travel = min(dist, GLIDER_SPEED * 3 * 3600)
        if dist > 0:
            lat, lon = step(lat, lon, dx / dist * GLIDER_SPEED, dy / dist * GLIDER_SPEED, travel / GLIDER_SPEED)
    Path("predicted_dialog.log").write_text("\n".join(lines) + "\n")
    print(f"Wrote predicted_dialog.log ({len(SURFACING_TIMES_H)} surfacings)")


def main() -> None:
    """Generate the synthetic open-loop demo data (predictions + dialog log)."""
    truth = make_truth()
    write_predictions(truth)
    write_dialog(truth)


if __name__ == "__main__":
    main()
