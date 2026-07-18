"""Follow a drifting float using externally supplied track predictions.

Assumed prediction workflow: every 6 hours a shore-side system writes
``predictions/drifter_YYYYmmddTHHMM.csv`` (creation time in the name)
containing the drifter's track from 24 h before to 12 h after creation
time, in 2-hour steps.  Rows are ``time,latitude,longitude`` with ISO
UTC timestamps; rows after the creation time are predictions.

At each surfacing this follower:

1. Selects the newest prediction file created at or before the
   surfacing time (from the glider's clock, so replay works too).
   Between prediction updates it keeps flying on the older file.
2. Interpolates the drifter's predicted position at the time the
   glider will *arrive* there: transit time = separation distance /
   through-water speed, with one refinement pass.
3. Sends that single point as ``goto_l{N}.ma``.

A single waypoint at the predicted arrival position is the simplest
behaviour that keeps the glider near the drifter (goal: within 4 km).
A pattern (diamond, zigzag) can be added later by offsetting several
waypoints around the same arrival point.
"""

import bisect
import csv
import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # follower runs in a background thread; no GUI
import matplotlib.pyplot as plt
import yaml
from sfmc_api import BaseFollower, SurfacingEvent, generate_goto_ma

from autopilot.safety import Geofence, check_waypoint

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("sfmc.predicted_track")

M_PER_DEG_LAT = 111320.0
MAX_TRANSIT_S = 12 * 3600.0  # cap extrapolation of arrival time


def distance_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Flat-earth distance in metres, fine for the few-km scales here."""
    mean_lat = math.radians((lat1 + lat2) / 2)
    dx = (lon2 - lon1) * M_PER_DEG_LAT * math.cos(mean_lat)
    dy = (lat2 - lat1) * M_PER_DEG_LAT
    return math.hypot(dx, dy)


class PredictedTrackFollower(BaseFollower):
    """Chase the drifter's predicted position at glider arrival time."""

    # Config keys re-applied at each surfacing when ``config_file`` is
    # set, so they can be edited without a restart:
    # {yaml key: (attribute, cast, default)}.  Everything else —
    # fence, safe_point, sequence_number, paths — is read once at
    # startup (validation failures must happen there, not at sea).
    HOT_KEYS = {
        "pattern": ("pattern", str, "drifter_*.csv"),
        "speed_horizontal": ("speed", float, 0.35),
        "target_radius_km": ("target_radius_km", float, 4.0),
        # -1 loops around the waypoint forever; -2 stops once reached.
        "num_legs_to_run": ("num_legs_to_run", int, -1),
        "max_prediction_age_h": ("max_age_h", float, 9.0),
        "max_waypoint_jump_km": ("max_jump_km", float, 30.0),
        # Hours between "still in FALLBACK" reminder emails.
        "fallback_reminder_h": ("fallback_reminder_h", float, 6.0),
        # Fixed plot extent [lon_min, lon_max, lat_min, lat_max]; if
        # None, the extent grows to fit the data (never shrinks).
        "plot_bounds": ("plot_bounds", None, None),
    }

    def __init__(self, config, queue_in, queue_out):
        super().__init__(config, queue_in, queue_out)
        self.predictions_dir = Path(config["predictions_dir"])
        self._apply_hot(config)
        self.sequence_number = int(config.get("sequence_number", 10))
        self.plot_dir = Path(config.get("plot_dir", "plots"))
        self.archive_dir = Path(config.get("archive_dir", "ma_archive"))
        self.history: list[tuple[float, float]] = []  # (lat, lon) per surfacing
        self._bounds: list[float] | None = None  # grow-only plot extent
        # Optional GEBCO bathymetry netCDF for depth contours on plots.
        self.bathymetry = config.get("bathymetry")
        self._bathy = None  # lazily opened elevation DataArray (as depth)

        # ── Safety layer ────────────────────────────────────────
        fence_path = config.get("geofence")
        self.fence = (
            Geofence.from_geojson(fence_path, float(config.get("fence_margin_km", 2.0)))
            if fence_path
            else None
        )
        sp = config.get("safe_point")
        self.safe_point = (float(sp[0]), float(sp[1])) if sp else None  # (lon, lat)
        self._in_fallback = False  # edge detection for pilot emails
        # Fail at startup, not at sea.
        if self.fence is not None:
            if self.safe_point is None:
                raise ValueError("geofence configured without a safe_point")
            if not self.fence.contains_buffered(*self.safe_point):
                raise ValueError("safe_point is outside the buffered geofence")

        # Live reload: when the config names its own path, HOT_KEYS are
        # re-read from it at each surfacing.
        self.config_file = config.get("config_file")
        self._config_mtime = self._config_stat()
        if self.config_file is not None and self._config_mtime is None:
            raise ValueError(f"config_file is not readable: {self.config_file}")
        self._log_config("Loaded config")

    # ── Config reload ───────────────────────────────────────────

    def _config_stat(self) -> float | None:
        if self.config_file is None:
            return None
        try:
            return Path(self.config_file).stat().st_mtime
        except OSError:
            return None

    def _apply_hot(self, config: dict) -> list[str]:
        """Set attributes for HOT_KEYS from *config*; list the changes."""
        changes = []
        for key, (attr, cast, default) in self.HOT_KEYS.items():
            value = config.get(key, default)
            if cast is not None and value is not None:
                value = cast(value)
            old = getattr(self, attr, value)
            if value != old:
                changes.append(f"{key}: {old} -> {value}")
            setattr(self, attr, value)
        return changes

    def _log_config(self, heading: str) -> None:
        """Log the effective config (defaults applied) at INFO."""
        eff = {key: getattr(self, attr) for key, (attr, _, _) in self.HOT_KEYS.items()}
        eff.update(
            predictions_dir=self.predictions_dir,
            plot_dir=self.plot_dir,
            archive_dir=self.archive_dir,
            sequence_number=self.sequence_number,
            geofence=self.config.get("geofence"),
            fence_margin_km=float(self.config.get("fence_margin_km", 2.0)),
            safe_point=self.safe_point,
            bathymetry=self.bathymetry,
            config_file=self.config_file,
        )
        lines = "\n".join(f"  {k}: {v}" for k, v in sorted(eff.items()))
        logger.info("%s:\n%s", heading, lines)

    def _maybe_reload(self) -> None:
        """Re-apply HOT_KEYS from config_file if it changed on disk.

        Never raises: a broken edit keeps the current settings and is
        retried at the next surfacing.
        """
        if self.config_file is None:
            return
        mtime = self._config_stat()
        if mtime is None:
            if self._config_mtime is not None:
                logger.error(
                    "Config file %s unreadable; keeping current settings",
                    self.config_file,
                )
                self._config_mtime = None
            return
        if mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        try:
            with open(self.config_file) as f:
                new = yaml.safe_load(f) or {}
        except Exception:
            logger.exception("Config reload failed; keeping current settings")
            return
        for key in sorted(set(self.config) | set(new)):
            if key not in self.HOT_KEYS and self.config.get(key) != new.get(key):
                logger.warning(
                    "Config %r changed on disk but requires a restart to apply", key
                )
        changes = self._apply_hot(new)
        self.config = new
        if changes:
            for change in changes:
                logger.info("Config change: %s", change)
            self._log_config("Config reloaded")

    # ── Prediction file handling ────────────────────────────────

    def _latest_prediction(self, when: datetime) -> tuple[datetime, Path] | None:
        """Newest file whose filename timestamp is at or before *when*."""
        best = None
        for path in self.predictions_dir.glob(self.pattern):
            try:
                created = datetime.strptime(
                    path.stem.rsplit("_", 1)[-1], "%Y%m%dT%H%M"
                ).replace(tzinfo=UTC)
            except ValueError:
                logger.warning("Skipping file with unparseable name: %s", path.name)
                continue
            if created <= when and (best is None or created > best[0]):
                best = (created, path)
        return best

    @staticmethod
    def _read_track(path: Path) -> list[tuple[datetime, float, float]]:
        track = []
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                t = datetime.fromisoformat(row["time"])
                track.append((t, float(row["latitude"]), float(row["longitude"])))
        track.sort()
        # Drop duplicate timestamps (keep the last row): repeated times
        # would make interpolation divide by zero.
        deduped: list[tuple[datetime, float, float]] = []
        for row in track:
            if deduped and deduped[-1][0] == row[0]:
                deduped[-1] = row
            else:
                deduped.append(row)
        return deduped

    @staticmethod
    def _position_at(
        track: list[tuple[datetime, float, float]], when: datetime
    ) -> tuple[float, float, bool]:
        """Linear interpolation along the track; extrapolates at the ends.

        Returns (lat, lon, extrapolated).
        """
        times = [t for t, _, _ in track]
        i = bisect.bisect_left(times, when)
        if 0 < i < len(track):
            (t0, lat0, lon0), (t1, lat1, lon1) = track[i - 1], track[i]
            f = (when - t0) / (t1 - t0)
            return lat0 + f * (lat1 - lat0), lon0 + f * (lon1 - lon0), False
        # Off either end: extrapolate from the nearest pair.
        (t0, lat0, lon0), (t1, lat1, lon1) = (
            (track[0], track[1]) if i == 0 else (track[-2], track[-1])
        )
        f = (when - t0) / (t1 - t0)
        return lat0 + f * (lat1 - lat0), lon0 + f * (lon1 - lon0), True

    # ── Per-surfacing logic ─────────────────────────────────────

    def _compute_candidate(
        self,
        event: SurfacingEvent,
        now: datetime,
        track: list[tuple[datetime, float, float]],
        created: datetime,
        prediction_name: str,
    ) -> tuple[tuple[float, float], tuple[float, float], float, float]:
        """Candidate waypoint from the prediction track.

        Returns (candidate (lon, lat), drifter_now (lat, lon),
        prediction age in hours, transit seconds).
        """
        age_h = (now - created).total_seconds() / 3600
        logger.info("Using %s (prediction age %.1f h)", prediction_name, age_h)

        # Where is the drifter predicted to be right now?
        d_lat, d_lon, _ = self._position_at(track, now)
        sep_km = distance_m(event.gps_lon, event.gps_lat, d_lon, d_lat) / 1000
        status = "within" if sep_km <= self.target_radius_km else "OUTSIDE"
        logger.info(
            "%s at %.4f, %.4f; drifter predicted at %.4f, %.4f; "
            "separation %.1f km (%s %.0f km target)",
            event.vehicle_name,
            event.gps_lat,
            event.gps_lon,
            d_lat,
            d_lon,
            sep_km,
            status,
            self.target_radius_km,
        )

        # Aim where the drifter will be when the glider arrives:
        # estimate transit time, then refine once against the moved target.
        transit = min(sep_km * 1000 / self.speed, MAX_TRANSIT_S)
        wpt_lat, wpt_lon, _ = self._position_at(track, now + timedelta(seconds=transit))
        transit = min(
            distance_m(event.gps_lon, event.gps_lat, wpt_lon, wpt_lat) / self.speed,
            MAX_TRANSIT_S,
        )
        wpt_lat, wpt_lon, extrapolated = self._position_at(
            track, now + timedelta(seconds=transit)
        )
        if extrapolated:
            logger.warning(
                "Arrival time %.1f h ahead is beyond the prediction horizon; "
                "extrapolating past the last track point",
                transit / 3600,
            )
        return (wpt_lon, wpt_lat), (d_lat, d_lon), age_h, transit

    def _notify_fallback(self, now, event, verdict) -> None:
        """Email pilots on FALLBACK entry, reminders, and recovery.

        Delivery goes through :meth:`BaseFollower.notify` (sfmc-api's
        DisconnectNotifier): background sender with retries, enabled by
        the ``--notify-email`` CLI flags, silent in replay mode.  A
        ``min_gap_seconds`` of 0 forces the transition emails through
        the per-key rate limit; reminders reuse the same key so the
        first one is due ``fallback_reminder_h`` after entry.
        """
        position = f"Glider position: {event.gps_lat:.4f}, {event.gps_lon:.4f}"
        if verdict.ok:
            if self._in_fallback:
                self.notify(
                    "fallback",
                    "autopilot recovered",
                    f"{event.vehicle_name} is tracking normally again at "
                    f"{now:%Y-%m-%d %H:%M} UTC.\n{position}",
                    min_gap_seconds=0.0,
                )
            self._in_fallback = False
            return
        if not self._in_fallback:
            summary = f"autopilot FALLBACK ({verdict.reason})"
            detail = (
                f"{event.vehicle_name} could not get a safe waypoint at "
                f"{now:%Y-%m-%d %H:%M} UTC and was sent to the safe point.\n"
                f"Reason: {verdict.reason}: {verdict.detail}\n{position}\n"
                "It will retry at every surfacing; pilot attention required."
            )
            gap = 0.0
        else:
            summary = f"autopilot still in FALLBACK ({verdict.reason})"
            detail = (
                f"{event.vehicle_name} is still holding at the safe point as "
                f"of {now:%Y-%m-%d %H:%M} UTC.\n"
                f"Reason: {verdict.reason}: {verdict.detail}\n{position}"
            )
            gap = self.fallback_reminder_h * 3600.0
        self._in_fallback = True
        self.notify("fallback", summary, detail, min_gap_seconds=gap)

    def on_surfacing(self, event: SurfacingEvent) -> None:
        self._maybe_reload()
        if event.gps_lat is None or event.gps_lon is None:
            logger.warning("Surfacing without a GPS fix, skipping")
            return
        now = event.timestamp or datetime.now(UTC)
        self.history.append((event.gps_lat, event.gps_lon))

        # ── Candidate waypoint from the newest prediction ───────
        candidate: tuple[float, float] | None = None  # (lon, lat)
        track: list[tuple[datetime, float, float]] = []
        created: datetime | None = None
        prediction_name = "none"
        age_h: float | None = None
        drifter_now: tuple[float, float] | None = None
        transit = 0.0

        latest = self._latest_prediction(now)
        if latest is None:
            logger.warning("No prediction file available at %s", now)
        else:
            created, path = latest
            prediction_name = path.name
            track = self._read_track(path)
            if len(track) < 2:
                logger.warning("Prediction file %s has fewer than 2 rows", path.name)
                track = []

        if track:
            try:
                candidate, drifter_now, age_h, transit = self._compute_candidate(
                    event, now, track, created, prediction_name
                )
            except Exception:
                # A bad prediction file must degrade to FALLBACK (the
                # safety gate treats candidate=None as NO_PREDICTION),
                # never lose the surfacing.
                logger.exception("Waypoint computation failed")
                candidate = None

        # ── Safety gate ─────────────────────────────────────────
        verdict = check_waypoint(
            self.fence,
            event.gps_lon,
            event.gps_lat,
            candidate,
            age_h,
            self.max_age_h,
            self.max_jump_km,
        )
        if self.fence is not None:
            logger.info(
                "Distance to fence boundary: %.1f km",
                self.fence.boundary_distance_km(event.gps_lon, event.gps_lat),
            )
        self._notify_fallback(now, event, verdict)
        if verdict.ok:
            state = "NORMAL"
            wpt_lon, wpt_lat = candidate
        else:
            if self.safe_point is None:
                logger.error(
                    "No safe waypoint (%s: %s) and no safe_point configured; "
                    "nothing sent",
                    verdict.reason,
                    verdict.detail,
                )
                return
            state = f"FALLBACK ({verdict.reason})"
            wpt_lon, wpt_lat = self.safe_point
            logger.warning(
                "FALLBACK (%s: %s): commanding safe point %.4f, %.4f — "
                "pilot attention required",
                verdict.reason,
                verdict.detail,
                wpt_lat,
                wpt_lon,
            )

        filename, content = generate_goto_ma(
            waypoints=[(wpt_lon, wpt_lat)],
            sequence_number=self.sequence_number,
            num_legs_to_run=self.num_legs_to_run,
        )
        self.send_files(to_glider={filename: content})
        if verdict.ok:
            logger.info(
                "Queued %s -> %.4f, %.4f (drifter position predicted %.1f h ahead)",
                filename,
                wpt_lat,
                wpt_lon,
                transit / 3600,
            )
        else:
            logger.info(
                "Queued %s -> %.4f, %.4f (safe point)", filename, wpt_lat, wpt_lon
            )

        # Archive a timestamped copy of what was sent (the upload itself
        # uses the regular name, which the glider's mission expects).
        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            stem, suffix = Path(filename).stem, Path(filename).suffix
            archive_path = self.archive_dir / f"{stem}_{now:%Y%m%dT%H%M%S}{suffix}"
            archive_path.write_text(content)
            logger.info("Archived %s", archive_path)
        except Exception:
            logger.exception("Archiving failed")

        try:
            self._save_plot(
                now,
                event,
                track,
                created,
                prediction_name,
                (wpt_lat, wpt_lon),
                drifter_now,
                state,
            )
        except Exception:
            logger.exception("Plotting failed")

    # ── Plotting ────────────────────────────────────────────────

    def _draw_bathymetry(
        self, ax, lon0: float, lon1: float, lat0: float, lat1: float
    ) -> None:
        """Thin depth contours from a GEBCO netCDF, subset to the view.

        GEBCO provides ``elevation`` on ascending ``lat``/``lon``
        coordinates; depth is negative elevation.  Disables itself
        after a failure so one bad file doesn't spam every plot.
        """
        if self.bathymetry is None:
            return
        try:
            import numpy as np
            import xarray as xr

            if self._bathy is None:
                self._bathy = -xr.open_dataset(self.bathymetry).elevation
            b = self._bathy.sel(lon=slice(lon0, lon1), lat=slice(lat0, lat1))
            if min(b.sizes.values()) < 2:
                logger.warning(
                    "Bathymetry %s does not cover the plotted region; disabling",
                    self.bathymetry,
                )
                self.bathymetry = None
                return
            ax.contour(
                b.lon,
                b.lat,
                b,
                levels=np.arange(0, 5000, 100),
                colors="k",
                linewidths=0.25,
            )
        except Exception:
            logger.exception("Bathymetry plotting failed; disabling")
            self.bathymetry = None

    def _save_plot(
        self,
        now: datetime,
        event: SurfacingEvent,
        track: list[tuple[datetime, float, float]],
        created: datetime | None,
        prediction_name: str,
        waypoint: tuple[float, float],
        drifter_now: tuple[float, float] | None,
        state: str = "NORMAL",
    ) -> None:
        """Save a map of glider track, planned waypoint, and drifter track."""
        fig, ax = plt.subplots(figsize=(7, 7))

        r_lat = self.target_radius_km * 1000 / M_PER_DEG_LAT
        wpt_lat, wpt_lon = waypoint

        # Extent first, so bathymetry can be subset to the plotted region.
        if self.plot_bounds is not None:
            lon0, lon1, lat0, lat1 = self.plot_bounds
        else:
            # Grow-only extent: expand to include everything plotted so
            # far, never shrink, so the view doesn't jump between
            # surfacings.
            lons = (
                [lon for _, _, lon in track] + [p[1] for p in self.history] + [wpt_lon]
            )
            lats = (
                [lat for _, lat, _ in track] + [p[0] for p in self.history] + [wpt_lat]
            )
            if drifter_now is not None:
                d_lat, d_lon = drifter_now
                r_lon = r_lat / math.cos(math.radians(d_lat))
                lons += [d_lon - r_lon, d_lon + r_lon]
                lats += [d_lat - r_lat, d_lat + r_lat]
            new = [min(lons), max(lons), min(lats), max(lats)]
            if self._bounds is None:
                self._bounds = new
            else:
                b = self._bounds
                self._bounds = [
                    min(b[0], new[0]),
                    max(b[1], new[1]),
                    min(b[2], new[2]),
                    max(b[3], new[3]),
                ]
            lon0, lon1, lat0, lat1 = self._bounds
        pad_lon = 0.05 * (lon1 - lon0)
        pad_lat = 0.05 * (lat1 - lat0)
        ax.set_xlim(lon0 - pad_lon, lon1 + pad_lon)
        ax.set_ylim(lat0 - pad_lat, lat1 + pad_lat)

        self._draw_bathymetry(
            ax, lon0 - pad_lon, lon1 + pad_lon, lat0 - pad_lat, lat1 + pad_lat
        )

        # Geofence and safe point.
        if self.fence is not None:
            for i, ring in enumerate(self.fence.rings_lonlat()):
                ax.plot(
                    *zip(*ring),
                    "-",
                    color="red",
                    lw=1,
                    label="geofence" if i == 0 else None,
                )
            for i, ring in enumerate(self.fence.rings_lonlat(buffered=True)):
                ax.plot(
                    *zip(*ring),
                    "--",
                    color="red",
                    lw=0.7,
                    alpha=0.6,
                    label="fence margin" if i == 0 else None,
                )
        if self.safe_point is not None:
            ax.plot(*self.safe_point, "s", color="tab:green", ms=8, label="safe point")

        # Drifter track: observed up to file creation time, predicted after.
        if track and created is not None:
            obs = [(lon, lat) for t, lat, lon in track if t <= created]
            pred = [(lon, lat) for t, lat, lon in track if t >= created]
            if obs:
                ax.plot(*zip(*obs), "-", color="tab:blue", label="drifter observed")
            if pred:
                ax.plot(*zip(*pred), "--", color="tab:blue", label="drifter predicted")

        if drifter_now is not None:
            d_lat, d_lon = drifter_now
            r_lon = r_lat / math.cos(math.radians(d_lat))
            ax.plot(
                d_lon,
                d_lat,
                "o",
                color="tab:blue",
                ms=8,
                label="drifter (predicted now)",
            )
            # Target-radius circle around the drifter's predicted position.
            th = [i * 2 * math.pi / 100 for i in range(101)]
            ax.plot(
                [d_lon + r_lon * math.cos(a) for a in th],
                [d_lat + r_lat * math.sin(a) for a in th],
                ":",
                color="tab:blue",
                lw=1,
                label=f"{self.target_radius_km:.0f} km target",
            )

        # Glider track and current position.
        lats, lons = zip(*self.history)
        ax.plot(lons, lats, "-", color="tab:orange", label="glider track")
        ax.plot(
            event.gps_lon,
            event.gps_lat,
            "^",
            color="tab:orange",
            ms=10,
            label="glider (this surfacing)",
        )

        # Planned waypoint and the leg to it.
        ax.plot(
            [event.gps_lon, wpt_lon],
            [event.gps_lat, wpt_lat],
            ":",
            color="tab:red",
            lw=1,
        )
        ax.plot(wpt_lon, wpt_lat, "*", color="tab:red", ms=14, label="waypoint")

        ax.set_aspect(1 / math.cos(math.radians(event.gps_lat)))
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        ax.set_title(
            f"{event.vehicle_name}  {now:%Y-%m-%d %H:%M} UTC  —  {state}\n"
            f"prediction: {prediction_name}",
            fontsize=10,
            color="black" if state == "NORMAL" else "red",
        )
        ax.legend(loc="best", fontsize=8)
        ax.grid(alpha=0.3)

        self.plot_dir.mkdir(parents=True, exist_ok=True)
        out = self.plot_dir / f"{event.vehicle_name}_{now:%Y%m%dT%H%M%S}.png"
        fig.savefig(out, dpi=120, bbox_inches="tight")
        plt.close(fig)
        logger.info("Saved plot %s", out)
