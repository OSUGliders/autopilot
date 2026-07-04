"""Closed-loop regression tests against the recorded float-6000 track.

These lock in the behavior validated interactively: normal tracking
stays within the target radius with zero fallbacks, and a drifter
leaving the geofence forces the safe-point fallback while the glider
stays inside the fence.
"""

import csv
from pathlib import Path

import pytest

from autopilot.safety import Geofence
from autopilot.sim.closed_loop import run_sim
from autopilot.sim.mock_data import M_PER_DEG_LAT

FIXTURE = Path(__file__).parent / "fixtures" / "float6000_truth.csv"
SAFE_POINT = (-120.07, 33.58)  # (lon, lat), matches the deployed config


@pytest.fixture(scope="module")
def truth() -> dict[float, tuple[float, float]]:
    with open(FIXTURE, newline="") as f:
        return {
            float(row["hours"]): (float(row["latitude"]), float(row["longitude"]))
            for row in csv.DictReader(f)
        }


def glider_start(truth):
    """3 km south of the drifter at the first surfacing (hour 24)."""
    d_lat, d_lon = truth[24.0]
    return (d_lat - 3000.0 / M_PER_DEG_LAT, d_lon)


def commanded_safe_point(waypoints):
    return [w for w in waypoints if abs(w[0] - SAFE_POINT[0]) < 1e-3
            and abs(w[1] - SAFE_POINT[1]) < 1e-3]


def test_normal_tracking_72h(truth, tmp_path):
    """Healthy scenario: no fallbacks, station kept within the target."""
    result = run_sim(truth, glider_start(truth), follow_hours=72, out_root=tmp_path)

    assert not commanded_safe_point(result["waypoints"])

    seps = result["separations"]
    settled = seps[len(seps) // 2:]
    target = result["config"]["target_radius_km"]
    assert sum(settled) / len(settled) < 2.5
    assert all(s <= target for s in settled)

    # One goto file archived per surfacing.
    archived = list(Path(result["config"]["archive_dir"]).glob("goto_l*.ma"))
    assert len(archived) == len(seps)


def test_fence_fallback_120h(truth, tmp_path):
    """The drifter exits the fence: fallback fires, glider stays inside."""
    result = run_sim(truth, glider_start(truth), follow_hours=120, out_root=tmp_path)

    assert commanded_safe_point(result["waypoints"]), "expected safe-point fallback"

    fence = Geofence.from_geojson("boundaries/RIOT_boundary.geojson", 2.0)
    assert all(fence.contains(lon, lat) for lat, lon in result["glider_track"])

    # The last commanded waypoint of the run is the safe point (the
    # drifter has left the fence for good by then), and the archived
    # .ma on disk agrees.
    assert commanded_safe_point([result["waypoints"][-1]])
    last_ma = sorted(Path(result["config"]["archive_dir"]).glob("goto_l*.ma"))[-1]
    assert "-12004.2000\t3334.8000" in last_ma.read_text()
