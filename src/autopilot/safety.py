"""Shore-side safety checks: geofence and waypoint validation.

The single choke point every candidate waypoint passes through before
being sent to a glider.  Pure geometry — no SFMC or follower imports —
so it can be unit-tested offline.

The geofence is a polygon drawn by the pilot in advance (e.g. in
Google Earth) over water deep enough for safe operations, exported as
GeoJSON.  Checks are done in a local kilometre frame (equirectangular
about the fence centroid; error is metres over a ~100 km region):

- waypoints must lie inside the fence shrunk by ``margin_km``
  (absorbs GPS error, surface drift, and current set);
- the straight glider-to-waypoint leg must stay inside the fence
  (matters for concave fences);
- the glider itself outside the fence is a breach, reported as its
  own verdict.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import shapely
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform, unary_union

KM_PER_DEG_LAT = 111.32


@dataclass
class Verdict:
    """Outcome of the safety checks for one candidate waypoint."""

    ok: bool
    reason: (
        str  # OK, NO_PREDICTION, STALE, GLIDER_OUTSIDE, FENCE_WAYPOINT, FENCE_LEG, JUMP
    )
    detail: str = ""


class Geofence:
    """A permitted-operations polygon with a safety margin."""

    def __init__(self, polygon, margin_km: float) -> None:
        polygon = shapely.force_2d(polygon)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty:
            raise ValueError("Geofence polygon is empty")
        c = polygon.centroid
        self._lon0, self._lat0 = c.x, c.y
        self._cos = math.cos(math.radians(self._lat0))
        self.margin_km = margin_km
        self.polygon = polygon  # lon/lat degrees
        self._poly_km = transform(self._to_km, polygon)
        self._buffered_km = self._poly_km.buffer(-margin_km)
        if self._buffered_km.is_empty:
            raise ValueError(
                f"A margin of {margin_km} km leaves no permitted area inside the fence"
            )
        self.buffered = transform(self._from_km, self._buffered_km)

    @classmethod
    def from_geojson(cls, path: str | Path, margin_km: float) -> Geofence:
        """Load all polygon features from a GeoJSON file (union of them)."""
        with open(path) as f:
            gj = json.load(f)
        features = gj["features"] if gj.get("type") == "FeatureCollection" else [gj]
        geoms = [
            shape(feat["geometry"])
            for feat in features
            if feat["geometry"]["type"] in ("Polygon", "MultiPolygon")
        ]
        if not geoms:
            raise ValueError(f"No polygon features found in {path}")
        return cls(unary_union(geoms), margin_km)

    # ── Local km frame ──────────────────────────────────────────

    def _to_km(self, x, y):
        return (
            (x - self._lon0) * self._cos * KM_PER_DEG_LAT,
            (y - self._lat0) * KM_PER_DEG_LAT,
        )

    def _from_km(self, x, y):
        return (
            x / (self._cos * KM_PER_DEG_LAT) + self._lon0,
            y / KM_PER_DEG_LAT + self._lat0,
        )

    # ── Queries ─────────────────────────────────────────────────

    def contains(self, lon: float, lat: float) -> bool:
        """Inside the raw fence (no margin)?"""
        return self._poly_km.covers(Point(self._to_km(lon, lat)))

    def contains_buffered(self, lon: float, lat: float) -> bool:
        """Inside the fence shrunk by the safety margin?"""
        return self._buffered_km.covers(Point(self._to_km(lon, lat)))

    def leg_inside(self, lon1: float, lat1: float, lon2: float, lat2: float) -> bool:
        """Does the straight leg stay inside the raw fence?"""
        line = LineString([self._to_km(lon1, lat1), self._to_km(lon2, lat2)])
        return self._poly_km.covers(line)

    def boundary_distance_km(self, lon: float, lat: float) -> float:
        """Distance to the fence boundary (positive both sides)."""
        return self._poly_km.boundary.distance(Point(self._to_km(lon, lat)))

    def rings_lonlat(self, buffered: bool = False) -> list[list[tuple[float, float]]]:
        """Exterior ring(s) in lon/lat, for plotting."""
        geom = self.buffered if buffered else self.polygon
        polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
        return [[(x, y) for x, y in p.exterior.coords] for p in polys]


def _distance_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    mean_lat = math.radians((lat1 + lat2) / 2)
    dx = (lon2 - lon1) * KM_PER_DEG_LAT * math.cos(mean_lat)
    dy = (lat2 - lat1) * KM_PER_DEG_LAT
    return math.hypot(dx, dy)


def check_waypoint(
    fence: Geofence | None,
    glider_lon: float,
    glider_lat: float,
    waypoint: tuple[float, float] | None,
    prediction_age_h: float | None,
    max_age_h: float,
    max_jump_km: float,
) -> Verdict:
    """Run the safety pipeline on one candidate waypoint (lon, lat).

    Checks in order: a usable prediction exists and is fresh, the
    glider is inside the fence, the waypoint is inside the buffered
    fence, the leg stays inside the fence, and the commanded jump is
    plausible.  Returns the first failure, or OK.
    """
    if waypoint is None:
        return Verdict(False, "NO_PREDICTION", "no usable prediction file")
    if prediction_age_h is not None and prediction_age_h > max_age_h:
        return Verdict(
            False,
            "STALE",
            f"prediction is {prediction_age_h:.1f} h old (max {max_age_h:.0f} h)",
        )
    wpt_lon, wpt_lat = waypoint
    if fence is not None:
        if not fence.contains(glider_lon, glider_lat):
            return Verdict(
                False,
                "GLIDER_OUTSIDE",
                f"glider at {glider_lat:.4f}, {glider_lon:.4f} is outside the fence",
            )
        if not fence.contains_buffered(wpt_lon, wpt_lat):
            return Verdict(
                False,
                "FENCE_WAYPOINT",
                f"waypoint {wpt_lat:.4f}, {wpt_lon:.4f} is outside the fence "
                f"minus {fence.margin_km:.1f} km margin",
            )
        if not fence.leg_inside(glider_lon, glider_lat, wpt_lon, wpt_lat):
            return Verdict(False, "FENCE_LEG", "leg to waypoint exits the fence")
    jump_km = _distance_km(glider_lon, glider_lat, wpt_lon, wpt_lat)
    if jump_km > max_jump_km:
        return Verdict(
            False,
            "JUMP",
            f"waypoint is {jump_km:.0f} km away (max {max_jump_km:.0f} km)",
        )
    return Verdict(True, "OK")


def check_next_waypoint(
    fence: Geofence | None,
    prev: tuple[float, float],
    waypoint: tuple[float, float],
) -> Verdict:
    """Validate a follow-up waypoint reached from *prev* (both (lon, lat)).

    The first waypoint of a goto list goes through
    :func:`check_waypoint`; each later one only needs to be inside the
    buffered fence with the leg from its predecessor staying inside.
    """
    if fence is None:
        return Verdict(True, "OK")
    wpt_lon, wpt_lat = waypoint
    if not fence.contains_buffered(wpt_lon, wpt_lat):
        return Verdict(
            False,
            "FENCE_WAYPOINT",
            f"waypoint {wpt_lat:.4f}, {wpt_lon:.4f} is outside the fence "
            f"minus {fence.margin_km:.1f} km margin",
        )
    if not fence.leg_inside(prev[0], prev[1], wpt_lon, wpt_lat):
        return Verdict(False, "FENCE_LEG", "leg between waypoints exits the fence")
    return Verdict(True, "OK")
