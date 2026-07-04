"""Minimal demo follower: send a new waypoint each time the glider surfaces.

Runs under the ``sfmc-follow`` framework from the sfmc-api package.
The framework watches the glider's dialog stream, detects each
surfacing (GPS fix + sensor block), and calls ``on_surfacing`` with
the parsed telemetry.  This follower then:

1. Logs where the glider surfaced.
2. Picks the next waypoint from a track defined in the YAML config.
3. Generates a ``goto_l{N}.ma`` file and queues it for upload to the
   glider's ``to-glider`` folder (or prints it in --dry-run mode).

Waypoint logic is deliberately simple: the config defines an ordered
list of waypoints, and each surfacing advances to the next one once
the glider is within ``arrival_radius_m`` of the current target.
"""

import logging
import math

from sfmc_api import BaseFollower, SurfacingEvent, generate_goto_ma

# The sfmc-follow framework only configures its own loggers, so give
# this module's logger a handler or its messages are dropped.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("sfmc.demo_follower")

EARTH_RADIUS_M = 6_371_000.0


def distance_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in metres between two decimal-degree points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


class TrackFollower(BaseFollower):
    """Step the glider through a fixed list of waypoints, one per arrival."""

    def __init__(self, config, queue_in, queue_out):
        super().__init__(config, queue_in, queue_out)
        # List of [lon, lat] pairs in decimal degrees, in visit order.
        self.track = [tuple(wpt) for wpt in config["track"]]
        self.arrival_radius_m = float(config.get("arrival_radius_m", 500.0))
        self.sequence_number = int(config.get("sequence_number", 10))
        self.target_index = 0

    def on_surfacing(self, event: SurfacingEvent) -> None:
        if event.gps_lat is None or event.gps_lon is None:
            logger.warning("Surfacing without a GPS fix, skipping")
            return

        target_lon, target_lat = self.track[self.target_index]
        dist = distance_m(event.gps_lon, event.gps_lat, target_lon, target_lat)
        logger.info(
            "%s surfaced at %.4f, %.4f (%.0f m from waypoint %d of %d)",
            event.vehicle_name,
            event.gps_lat,
            event.gps_lon,
            dist,
            self.target_index + 1,
            len(self.track),
        )

        # Advance to the next waypoint once this one is reached.
        if dist <= self.arrival_radius_m and self.target_index < len(self.track) - 1:
            self.target_index += 1
            target_lon, target_lat = self.track[self.target_index]
            logger.info(
                "Waypoint reached; advancing to waypoint %d: %.4f, %.4f",
                self.target_index + 1,
                target_lat,
                target_lon,
            )

        filename, content = generate_goto_ma(
            waypoints=[(target_lon, target_lat)],
            sequence_number=self.sequence_number,
        )
        self.send_files(to_glider={filename: content})
        logger.info("Queued %s -> waypoint %.4f, %.4f", filename, target_lat, target_lon)
