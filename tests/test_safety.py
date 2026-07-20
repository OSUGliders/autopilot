"""Unit tests for the geofence and waypoint-validation pipeline."""

import pytest

from autopilot.safety import Geofence, check_next_waypoint, check_waypoint

FENCE_PATH = "boundaries/RIOT_boundary.geojson"
DEEP_INSIDE = (-120.07, 33.58)  # (lon, lat), >10 km from the boundary

# Two points inside the fence whose connecting leg crosses the southern
# notch (the fence is concave there).
NOTCH_WEST = (-120.0, 33.30)
NOTCH_EAST = (-119.25, 33.28)

INSIDE = (-120.0, 33.6)
OUTSIDE_NORTH = (-120.0, 34.2)


@pytest.fixture(scope="module")
def fence() -> Geofence:
    return Geofence.from_geojson(FENCE_PATH, margin_km=2.0)


def test_interior_point_inside_buffered(fence):
    assert fence.contains_buffered(*DEEP_INSIDE)
    assert fence.boundary_distance_km(*DEEP_INSIDE) > 10


def test_outside_point_rejected(fence):
    assert not fence.contains(*OUTSIDE_NORTH)
    assert not fence.contains_buffered(*OUTSIDE_NORTH)


def test_margin_band(fence):
    """A point inside the fence but within the margin fails the buffered check."""
    # Walk north from an inside point until just inside the raw boundary.
    lon, lat = -120.0, 33.6
    while fence.contains(lon, lat + 0.001):
        lat += 0.001
    assert fence.contains(lon, lat)
    assert not fence.contains_buffered(lon, lat)


def test_concave_leg_rejected(fence):
    assert fence.contains(*NOTCH_WEST) and fence.contains(*NOTCH_EAST)
    assert not fence.leg_inside(*NOTCH_WEST, *NOTCH_EAST)


def test_excessive_margin_raises():
    with pytest.raises(ValueError):
        Geofence.from_geojson(FENCE_PATH, margin_km=500.0)


# ── check_waypoint pipeline ─────────────────────────────────────────


def _check(fence, waypoint, age_h=1.0, glider=INSIDE, max_jump=30.0):
    return check_waypoint(fence, glider[0], glider[1], waypoint, age_h, 9.0, max_jump)


def test_ok(fence):
    v = _check(fence, (-120.05, 33.62))
    assert v.ok and v.reason == "OK"


def test_no_prediction(fence):
    assert _check(fence, None).reason == "NO_PREDICTION"


def test_stale(fence):
    assert _check(fence, (-120.05, 33.62), age_h=12.0).reason == "STALE"


def test_fence_waypoint(fence):
    assert _check(fence, OUTSIDE_NORTH).reason == "FENCE_WAYPOINT"


def test_fence_leg(fence):
    v = _check(fence, NOTCH_EAST, glider=NOTCH_WEST, max_jump=100.0)
    assert v.reason == "FENCE_LEG"


def test_jump(fence):
    v = _check(fence, (-119.30, 33.35), max_jump=30.0)  # ~70 km away
    assert v.reason == "JUMP"


def test_glider_outside(fence):
    v = _check(fence, (-120.05, 33.62), glider=OUTSIDE_NORTH)
    assert v.reason == "GLIDER_OUTSIDE"


def test_no_fence_still_checks_staleness():
    v = check_waypoint(None, *INSIDE, (-120.05, 33.62), 12.0, 9.0, 30.0)
    assert v.reason == "STALE"


# ── check_next_waypoint (later waypoints of a goto list) ────────────


def test_next_waypoint_ok(fence):
    assert check_next_waypoint(fence, INSIDE, (-120.05, 33.62)).ok


def test_next_waypoint_outside_fence(fence):
    v = check_next_waypoint(fence, INSIDE, OUTSIDE_NORTH)
    assert v.reason == "FENCE_WAYPOINT"


def test_next_waypoint_concave_leg(fence):
    v = check_next_waypoint(fence, NOTCH_WEST, NOTCH_EAST)
    assert v.reason == "FENCE_LEG"


def test_next_waypoint_no_fence():
    assert check_next_waypoint(None, INSIDE, OUTSIDE_NORTH).ok
