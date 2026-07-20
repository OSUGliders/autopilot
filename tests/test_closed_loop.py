"""Closed-loop regression tests against the recorded float-6000 track.

These lock in the behavior validated interactively: normal tracking
stays within the target radius with no fallbacks, and a drifter
leaving the geofence stops goto uploads (the glider loops its last
in-fence waypoint) while the glider stays inside the fence.
"""

import csv
from pathlib import Path

import pytest
import yaml

from autopilot.safety import Geofence
from autopilot.sim.closed_loop import parse_goto_waypoint, run_sim
from autopilot.sim.mock_data import M_PER_DEG_LAT

REPO = Path(__file__).parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "float6000_truth.csv"


@pytest.fixture(scope="module")
def truth() -> dict[float, tuple[float, float]]:
    with open(FIXTURE, newline="") as f:
        return {
            float(row["hours"]): (float(row["latitude"]), float(row["longitude"]))
            for row in csv.DictReader(f)
        }


@pytest.fixture(scope="module")
def fence() -> Geofence:
    return Geofence.from_geojson("boundaries/RIOT_boundary.geojson", 2.0)


def glider_start(truth):
    """3 km south of the drifter at the first surfacing (hour 24)."""
    d_lat, d_lon = truth[24.0]
    return (d_lat - 3000.0 / M_PER_DEG_LAT, d_lon)


def archived_gotos(result):
    return sorted(Path(result["config"]["archive_dir"]).glob("goto_l*.ma"))


def test_normal_tracking_72h(truth, tmp_path):
    """Healthy scenario: a goto every surfacing, station kept."""
    result = run_sim(truth, glider_start(truth), follow_hours=72, out_root=tmp_path)

    # No fallback: one goto file archived per surfacing.
    seps = result["separations"]
    assert len(archived_gotos(result)) == len(seps)

    settled = seps[len(seps) // 2 :]
    target = result["config"]["target_radius_km"]
    assert sum(settled) / len(settled) < 2.5
    assert all(s <= target for s in settled)


def test_fence_fallback_120h(truth, fence, tmp_path):
    """The drifter exits the fence: uploads stop, glider stays inside."""
    result = run_sim(truth, glider_start(truth), follow_hours=120, out_root=tmp_path)

    # FALLBACK now means "no new goto", so some surfacings archive
    # nothing once the drifter's predicted track leaves the fence.
    n_surfacings = len(result["separations"])
    archived = archived_gotos(result)
    assert 0 < len(archived) < n_surfacings

    # Every waypoint ever sent is inside the buffered fence, so the
    # glider looping the last one stays inside the raw fence.
    for ma in archived:
        lon, lat = parse_goto_waypoint(ma.read_text())
        assert fence.contains_buffered(lon, lat)
    assert all(fence.contains(lon, lat) for lat, lon in result["glider_track"])


def test_fence_fallback_safe_point_120h(truth, fence, tmp_path):
    """With safe_point configured, FALLBACK commands it instead."""
    safe_point = [-120.07, 33.58]
    with open(REPO / "osu999_config.yaml") as f:
        config = yaml.safe_load(f)
    config["safe_point"] = safe_point
    for key in ("geofence", "bathymetry"):
        if config.get(key):
            config[key] = str(REPO / config[key])
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(config))

    result = run_sim(
        truth,
        glider_start(truth),
        follow_hours=120,
        config_path=cfg_path,
        out_root=tmp_path,
    )

    # Fallback still uploads: a goto every surfacing, with the safe
    # point commanded whenever the near-term prediction clips the
    # margin band, and nothing sent outside the buffered fence.
    archived = archived_gotos(result)
    assert len(archived) == len(result["separations"])
    first_wpts = [parse_goto_waypoint(ma.read_text()) for ma in archived]
    assert any(
        abs(lon - safe_point[0]) < 1e-3 and abs(lat - safe_point[1]) < 1e-3
        for lon, lat in first_wpts
    ), "expected at least one safe-point goto"
    assert all(fence.contains_buffered(lon, lat) for lon, lat in first_wpts)
    assert all(fence.contains(lon, lat) for lat, lon in result["glider_track"])
