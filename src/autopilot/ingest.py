"""Turn the real-time localization feed into per-platform prediction files.

The remote pipeline writes one wide CSV per deployment —
``<deployment>_float_tracks_latest.csv`` (e.g. ``A1_float_tracks_latest.csv``,
then ``B1_...`` for the next) — with columns::

    deployment,float,tracker,segment,time,lat,lon,std_east_m,std_north_m,kind,gliders

``float`` is the platform id (may repeat across deployments — the same
physical float redeployed).  ``tracker`` is one of several parallel
localization methods (``ekf``, ``pf``, ``pf_lag2h``, ``batch``,
``ops``); ``kind`` is ``estimate`` (observed) or ``prediction``
(forecast); ``segment`` restarts when the pipeline loses and regains
track (a real position gap, not to be interpolated across).

Since the pilot should be able to choose which tracker to fly on, this
writes every (deployment, float, tracker) combination out as its own
predictions subdirectory — ``predictions/<deployment>_<float>_<tracker>/``
— toggleable via the web dashboard's existing target selector with no
changes there.  Each write regenerates from the full CSV; there is no
incremental state to get out of sync (parsing is a few thousand rows,
milliseconds, so simplicity wins over bookkeeping).

The output filename's timestamp (``drifter_<created>.csv``) is the
latest *observed* (``kind=estimate``) row's own time — not when this
script happened to run.  See :func:`write_track` for why: using
wall-clock time there would silently defeat the follower's staleness
check.

Never deletes: a file that fails to parse, or a (float, tracker) that
has dropped out of this cycle's data, simply isn't written this time —
the follower already turns a stale predictions_dir into FALLBACK plus
a pilot email on its own, so a bad or missing upstream update degrades
safely rather than needing special-casing here.

Also writes one comparison plot per (deployment, float) asset —
every tracker overlaid — as ``tracks.png`` inside each of that asset's
tracker directories.  Duplicating the same image into every tracker
directory (rather than inventing a separate per-asset URL/lookup) means
the web dashboard can show it for whichever target is currently
selected with zero new path parsing: it already has ``predictions_dir``
in hand.  A plotting failure never blocks the CSV writes.

Also writes one combined KMZ (with ``--kml-dir``), covering every
asset found this cycle, for viewing the whole fleet in Google Earth:
Folder per deployment > float > tracker, each with an observed line, a
forecast line, and a bigger marker at the most recent estimate — the
same visual language as the comparison PNG.  With ``--kml-base-url``
also set, a second, tiny "network link" KML is written alongside it;
opened once in Google Earth, that one re-fetches the KMZ on an
interval by itself — see ``deploy/autopilot-publish-kmz`` for how this
gets published somewhere Earth can actually reach it (this module
knows nothing about git/GitHub; it only ever writes local files).
Both are best-effort, like the comparison plot — never allowed to
block the CSV writes.

Run with: autopilot-ingest-predictions --localization-dir DIR --predictions-dir DIR
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import re
import shutil
import zipfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # runs headless (systemd timer / CLI)
import matplotlib.pyplot as plt
import simplekml

logger = logging.getLogger("autopilot.ingest")

DEPLOYMENT_FILE_RE = re.compile(
    r"^(?P<deployment>[A-Za-z0-9]+)_float_tracks_latest\.csv$"
)
_SAFE = re.compile(r"[^A-Za-z0-9_]+")


def _drifter_colors(
    float_ids, cmap_name: str = "autumn"
) -> dict[str, tuple[int, int, int]]:
    """One RGB color per drifter (float id), evenly sampled from a
    matplotlib colormap -- so every tracker for a given drifter draws
    in the same color, and different drifters are visually distinct.
    Sampled fresh from whichever floats are present this cycle (this
    module keeps no state between cycles), so a color can shift
    slightly if the fleet roster changes -- accepted for simplicity.
    """
    cmap = matplotlib.colormaps[cmap_name]
    ordered = sorted(float_ids)
    n = len(ordered)
    colors = {}
    for i, float_id in enumerate(ordered):
        frac = 0.5 if n <= 1 else i / (n - 1)
        r, g, b, _a = cmap(frac)
        colors[float_id] = (round(r * 255), round(g * 255), round(b * 255))
    return colors


def _slug(*parts: str) -> str:
    """Filesystem-safe directory name from arbitrary CSV field values."""
    return "_".join(_SAFE.sub("", p) or "x" for p in parts)


def deployment_files(localization_dir: Path) -> list[Path]:
    """``<deployment>_float_tracks_latest.csv`` files, deployment recognized."""
    found = []
    for path in sorted(localization_dir.glob("*_float_tracks_latest.csv")):
        if DEPLOYMENT_FILE_RE.match(path.name):
            found.append(path)
        else:
            logger.warning("Skipping unrecognized file name: %s", path.name)
    return found


def _parse_time(raw: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


def read_tracks(
    path: Path,
) -> dict[tuple[str, str, str], list[tuple[datetime, float, float, str]]]:
    """(deployment, float, tracker) -> time-sorted (time, lat, lon, kind)
    rows, latest segment only.

    ``kind`` (``estimate``/``prediction``) is kept through to
    :func:`write_track`, which needs it to anchor the output filename
    on the *data's* last observed time — never on ingest wall-clock
    time (see its docstring).

    Bad rows (unparseable time, non-finite coordinates, wrong kind) are
    dropped rather than aborting the whole file — one glitchy row must
    not cost every platform in this deployment its update this cycle.
    """
    m = DEPLOYMENT_FILE_RE.match(path.name)
    deployment = m.group("deployment") if m else path.stem

    # First pass: the newest segment actually present per (float, tracker) —
    # segments are independent per tracker, so this is computed per pair,
    # not assumed synchronized across methods.
    best_segment: dict[tuple[str, str], int] = {}
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    key = (row["float"], row["tracker"])
                    seg = int(row["segment"])
                except (KeyError, ValueError):
                    continue
                if seg > best_segment.get(key, -1):
                    best_segment[key] = seg
    except OSError:
        logger.exception("Cannot read %s", path)
        return {}

    tracks: dict[tuple[str, str, str], list[tuple[datetime, float, float, str]]] = (
        defaultdict(list)
    )
    dropped = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                float_id, tracker, kind = row["float"], row["tracker"], row["kind"]
                if kind not in ("estimate", "prediction"):
                    continue
                if int(row["segment"]) != best_segment.get((float_id, tracker)):
                    continue
                t = _parse_time(row["time"])
                lat, lon = float(row["lat"]), float(row["lon"])
            except (KeyError, ValueError):
                dropped += 1
                continue
            if t is None or not (math.isfinite(lat) and math.isfinite(lon)):
                dropped += 1
                continue
            tracks[(deployment, float_id, tracker)].append((t, lat, lon, kind))
    if dropped:
        logger.warning("Dropped %d unusable row(s) from %s", dropped, path.name)

    for rows in tracks.values():
        rows.sort(key=lambda r: r[0])
    return tracks


#: Default for write_track's max_source_age_h -- see its docstring.
DEFAULT_MAX_SOURCE_AGE_H = 48.0


def write_track(
    predictions_dir: Path,
    deployment: str,
    float_id: str,
    tracker: str,
    rows: list[tuple[datetime, float, float, str]],
    max_source_age_h: float | None = DEFAULT_MAX_SOURCE_AGE_H,
    now: datetime | None = None,
) -> Path | None:
    """Write one drifter_<created>.csv; None (with a warning) if unwritable.

    ``created`` — the filename timestamp the follower measures staleness
    against (``age_h = now - created``, checked against
    ``max_prediction_age_h``) — is the latest ``estimate`` row's own
    time, never ingest wall-clock time.  Stamping with wall-clock time
    would silently defeat staleness detection: if the upstream feed
    stopped updating, every 10-minute ingest cycle would still write a
    filename that looks freshly minted even though the real position
    estimate underneath it is hours old, and FALLBACK would never fire.
    Using the data's own last-estimate time means age_h correctly
    grows for real when the feed actually goes stale.

    ``max_source_age_h`` additionally skips the write entirely once
    ``created`` is already older than this (``None`` disables the
    check). This doesn't change the follower's own behavior -- a
    frozen filename already ages correctly whether or not this keeps
    rewriting it with identical content -- it just stops silently
    re-publishing dead data forever: a clear "not writing" warning in
    ingest's own log points straight at which (deployment, float,
    tracker) feed has actually died, and the comparison plot for an
    asset with every tracker this stale stops being needlessly
    re-rendered too (write_comparison_plot only draws into directories
    write_track actually wrote this cycle).
    """
    if not rows:
        logger.warning(
            "No usable rows for %s/%s/%s; not writing", deployment, float_id, tracker
        )
        return None
    estimate_times = [t for t, _, _, kind in rows if kind == "estimate"]
    if not estimate_times:
        logger.warning(
            "No estimate rows for %s/%s/%s (prediction-only data has no "
            "reliable creation time); not writing",
            deployment,
            float_id,
            tracker,
        )
        return None
    created = max(estimate_times)
    if max_source_age_h is not None:
        age_h = ((now or datetime.now(UTC)) - created).total_seconds() / 3600
        if age_h > max_source_age_h:
            logger.warning(
                "%s/%s/%s: newest estimate is %.1f h old (max %.0f h); not writing",
                deployment,
                float_id,
                tracker,
                age_h,
                max_source_age_h,
            )
            return None
    outdir = predictions_dir / _slug(deployment, float_id, tracker)
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"drifter_{created:%Y%m%dT%H%M}.csv"
    with open(path, "w") as f:
        f.write("time,latitude,longitude\n")
        for t, lat, lon, _kind in rows:
            f.write(
                f"{t.astimezone(UTC):%Y-%m-%dT%H:%M:%S}+00:00,{lat:.6f},{lon:.6f}\n"
            )
    return path


def _plot_comparison(
    deployment: str,
    float_id: str,
    by_tracker: dict[str, list[tuple[datetime, float, float, str]]],
):
    """One figure overlaying every tracker's track: solid = observed,
    dashed = forecast, one legend entry per tracker (color-matched).

    Each tracker's most recent estimate — the one position actually
    worth trusting right now — is drawn as a large, outlined dot;
    earlier estimates are small so the eye goes straight to what's
    current instead of hunting along the line for the last point.

    The title's "as of" time is the newest *estimate* row's own
    timestamp across every tracker shown, not wall-clock render time —
    same anchor as write_track's filename — so a plot rebuilt every
    cycle from data that has actually stopped updating keeps showing
    its true (aging) last-real-position time instead of always looking
    freshly drawn.
    """
    fig, ax = plt.subplots(figsize=(10, 10))
    lats = []
    estimate_times = []
    for tracker in sorted(by_tracker):
        rows = by_tracker[tracker]
        obs = [(lon, lat) for _, lat, lon, kind in rows if kind == "estimate"]
        pred = [(lon, lat) for _, lat, lon, kind in rows if kind == "prediction"]
        lats += [lat for _, lat, _, _ in rows]
        estimate_times += [t for t, _, _, kind in rows if kind == "estimate"]
        color = None
        if obs:
            (line,) = ax.plot(*zip(*obs), "-", lw=1.3, label=tracker)
            color = line.get_color()
            if len(obs) > 1:
                xs, ys = zip(*obs[:-1])
                ax.scatter(xs, ys, s=14, color=color, zorder=3)
            ax.scatter(
                *obs[-1], s=160, color=color, edgecolor="black", linewidth=0.9, zorder=5
            )
        if pred:
            label = None if obs else f"{tracker} (forecast only)"
            ax.plot(
                *zip(*pred), "--", lw=1.3, color=color, marker="o", ms=5, label=label
            )
    if lats:
        ax.set_aspect(1 / math.cos(math.radians(sum(lats) / len(lats))))
    ax.set_xlabel("Longitude", fontsize=12)
    ax.set_ylabel("Latitude", fontsize=12)
    if estimate_times:
        as_of = f"as of {max(estimate_times).astimezone(UTC):%Y-%m-%d %H:%M} UTC"
    else:
        as_of = "no observed positions"
    ax.set_title(
        f"{deployment} / {float_id}  ({as_of})\n"
        "solid = observed (large dot = most recent)   dashed = forecast",
        fontsize=14,
    )
    ax.legend(loc="best", fontsize=10)
    ax.tick_params(labelsize=10)
    ax.grid(alpha=0.3)
    return fig


def write_comparison_plot(
    deployment: str,
    float_id: str,
    by_tracker: dict[str, list[tuple[datetime, float, float, str]]],
    dest_dirs: list[Path],
) -> None:
    """Render once, copy into every already-written tracker directory
    for this asset.  Best-effort: a plotting bug must never block the
    CSV writes that already succeeded this cycle.
    """
    if not dest_dirs:
        return
    try:
        fig = _plot_comparison(deployment, float_id, by_tracker)
        first = dest_dirs[0] / "tracks.png"
        fig.savefig(first, dpi=130, bbox_inches="tight")
        plt.close(fig)
        for d in dest_dirs[1:]:
            shutil.copy(first, d / "tracks.png")
    except Exception:
        logger.exception("Comparison plot failed for %s/%s", deployment, float_id)


# Trackers shown in the KMZ by default: the fleet's every-tracker
# comparison PNG stays as-is (all methods -- picking a tracker to fly
# on is a real decision), but that many overlapping lines is too
# cluttered for a fleet-wide Google Earth view. Override with
# --kml-trackers.
DEFAULT_KML_TRACKERS = ("ekf", "ops", "pf_lag2h")

# A small circle, not Earth's default oversized pushpin.
_CIRCLE_ICON = "http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png"


def _style_track(track, rgb: tuple[int, int, int], width: float, alpha: int) -> None:
    """Thin line only -- no icon of its own.

    A gx:Track has its own position icon, shown at its current point
    even at rest (not just while the time slider is being scrubbed).
    That duplicated the deliberate, informative markers this module
    already places at the same spot -- the "most recent" marker, and
    each forecast point's own marker -- as a second icon stacked on
    top with no description of its own (a gx:Track carries no per-point
    detail to show). Icon scale 0 hides it entirely; the markers
    already carry the information it would have shown.

    A gx:Track's style is a StyleMap (normal/highlight pair), and
    Google Earth falls back to its default yellow pushpin for whichever
    pair is left unset -- setting only normalstyle showed a stray
    default pin stacked on top too. Both pairs get the identical style
    so nothing ever falls back.
    """
    color = simplekml.Color.rgb(*rgb, alpha)
    for style in (track.stylemap.normalstyle, track.stylemap.highlightstyle):
        style.linestyle.color = color
        style.linestyle.width = width
        style.iconstyle.scale = 0
        style.labelstyle.scale = 0


def _style_point(
    pnt, rgb: tuple[int, int, int], scale: float, alpha: int = 255
) -> None:
    """Small circle icon for a plain Placemark; name shows only as the
    info-balloon title on click, not as a permanent map label."""
    pnt.style.iconstyle.icon.href = _CIRCLE_ICON
    pnt.style.iconstyle.scale = scale
    pnt.style.iconstyle.color = simplekml.Color.rgb(*rgb, alpha)
    pnt.style.labelstyle.scale = 0


def build_kmz(
    all_tracks: dict[tuple[str, str, str], list[tuple[datetime, float, float, str]]],
    trackers: tuple[str, ...] | None = DEFAULT_KML_TRACKERS,
    cmap_name: str = "autumn",
) -> simplekml.Kml:
    """One KML Document, Folder per deployment > float > tracker.

    Colored by *drifter* (float id), not tracker: every tracking
    method for a given drifter shares one color (sampled from
    *cmap_name*), so what stands out on a fleet-wide map is which
    physical drifter a track belongs to, not which algorithm produced
    it — the comparison PNG is where method-vs-method matters, and
    that's still colored per tracker there.  A drifter reused across
    deployments keeps the same color (colored by float id alone, not
    (deployment, float)).

    Each track is a ``gx:Track`` — a chronological when/coord sequence,
    not a plain LineString — so Google Earth's time slider can scrub
    through it; a plain LineString carries no per-point time
    information at all.  gx:Track is also far more compact than one
    Placemark per row, which matters here: some tracks run to 1000+
    rows.  Full opacity for observed, lighter for forecast (KML has no
    true dashed line style). The track itself draws as a line only —
    its own position icon is hidden (see :func:`_style_track`) so it
    doesn't duplicate the deliberate markers below.

    A bigger marker sits at the most recent estimate, and forecast
    points additionally each get their own small, individually
    clickable Placemark (a gx:Track has no per-point click target) —
    clicking one shows the asset id, predicted time (as both an
    absolute UTC time and a lead time from the last real estimate), and
    lat/lon.

    *trackers* restricts which tracking methods appear at all (default
    :data:`DEFAULT_KML_TRACKERS`) — with every method included, the
    overlapping lines make a fleet-wide view unreadable.  ``None``
    means no filtering (every tracker present in the data).  This has
    no effect on the CSV predictions or the comparison PNG, which
    still cover every tracker — this only trims what Earth draws.
    """
    kml = simplekml.Kml()
    allowed = set(trackers) if trackers is not None else None

    by_deployment: dict[str, dict[str, dict[str, list]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for (deployment, float_id, tracker), rows in all_tracks.items():
        if allowed is not None and tracker not in allowed:
            continue
        by_deployment[deployment][float_id][tracker] = rows

    drifter_colors = _drifter_colors(
        {float_id for floats in by_deployment.values() for float_id in floats},
        cmap_name,
    )

    for deployment in sorted(by_deployment):
        dep_folder = kml.newfolder(name=deployment)
        for float_id in sorted(by_deployment[deployment]):
            float_folder = dep_folder.newfolder(name=float_id)
            rgb = drifter_colors[float_id]
            for tracker in sorted(by_deployment[deployment][float_id]):
                rows = by_deployment[deployment][float_id][tracker]
                obs = [
                    (t, lat, lon) for t, lat, lon, kind in rows if kind == "estimate"
                ]
                pred = [
                    (t, lat, lon) for t, lat, lon, kind in rows if kind == "prediction"
                ]
                tracker_folder = float_folder.newfolder(name=tracker)
                if obs:
                    trk = tracker_folder.newgxtrack(name=f"{tracker} observed")
                    trk.newwhen(
                        [
                            t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                            for t, _, _ in obs
                        ]
                    )
                    trk.newgxcoord([(lon, lat) for _, lat, lon in obs])
                    _style_track(trk, rgb, width=1.2, alpha=255)
                if pred:
                    label = "forecast" if obs else "forecast only"
                    trk = tracker_folder.newgxtrack(name=f"{tracker} {label}")
                    trk.newwhen(
                        [
                            t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                            for t, _, _ in pred
                        ]
                    )
                    trk.newgxcoord([(lon, lat) for _, lat, lon in pred])
                    _style_track(trk, rgb, width=1.0, alpha=140)
                last_obs = max(obs, key=lambda o: o[0]) if obs else None
                last_t = last_obs[0] if last_obs else None
                if last_obs is not None:
                    _, last_lat, last_lon = last_obs
                    pnt = tracker_folder.newpoint(
                        name=f"{tracker} (most recent)", coords=[(last_lon, last_lat)]
                    )
                    _style_point(pnt, rgb, scale=0.9)
                    last_utc = last_t.astimezone(UTC)
                    pnt.timestamp.when = last_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                    pnt.description = (
                        f"Asset: {float_id}\n"
                        f"Last estimate: {last_utc:%Y-%m-%d %H:%M} UTC\n"
                        f"{last_lat:.4f}, {last_lon:.4f}"
                    )
                # A small, individually clickable marker per forecast
                # point -- separate from the forecast gx:Track (which
                # only shows position, no per-point detail on click).
                # Lead time is measured from the last real estimate
                # ("now" for tracking purposes), not from ingest
                # wall-clock time, matching write_track's own anchor.
                for t, lat, lon in pred:
                    lead_h = (t - last_t).total_seconds() / 3600 if last_t else None
                    t_utc = t.astimezone(UTC)
                    label = f"+{lead_h:.1f}h" if lead_h is not None else "forecast"
                    pnt = tracker_folder.newpoint(
                        name=f"{tracker} {label}", coords=[(lon, lat)]
                    )
                    _style_point(pnt, rgb, scale=0.35, alpha=180)
                    pnt.timestamp.when = t_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                    desc = [
                        f"Asset: {float_id}",
                        f"Predicted: {t_utc:%Y-%m-%d %H:%M} UTC",
                    ]
                    if lead_h is not None:
                        desc.append(f"({lead_h:+.1f} h from last estimate)")
                    desc.append(f"{lat:.4f}, {lon:.4f}")
                    pnt.description = "\n".join(desc)
    return kml


#: Zip entries need *some* date_time; a fixed one (rather than
#: zipfile's default of "now") means two runs over identical track
#: data produce a byte-identical .kmz -- autopilot-publish-kmz commits
#: only when the file's bytes actually changed, and a wall-clock
#: timestamp baked into the zip would make every single run look
#: different even when nothing about the tracks did.
_ZIP_FIXED_DATE = (1980, 1, 1, 0, 0, 0)


def write_kmz(
    all_tracks: dict,
    kml_dir: Path,
    trackers: tuple[str, ...] | None = DEFAULT_KML_TRACKERS,
) -> Path | None:
    """Best-effort: a KMZ bug must never block the CSV writes.

    Writes the zip directly rather than using Kml.savekmz() -- that
    method stamps its one entry with the current wall-clock time,
    which made the file differ on every run even when the underlying
    KML text was identical (see _ZIP_FIXED_DATE).
    """
    if not all_tracks:
        return None
    try:
        kml_dir.mkdir(parents=True, exist_ok=True)
        path = kml_dir / "tracks.kmz"
        kml_bytes = build_kmz(all_tracks, trackers).kml().encode("utf-8")
        info = zipfile.ZipInfo("doc.kml", date_time=_ZIP_FIXED_DATE)
        info.compress_type = zipfile.ZIP_DEFLATED
        with zipfile.ZipFile(path, "w") as kmz:
            kmz.writestr(info, kml_bytes)
        return path
    except Exception:
        logger.exception("KMZ generation failed")
        return None


def write_network_link(base_url: str, kml_dir: Path) -> Path | None:
    """A tiny KML that Google Earth re-fetches on an interval.  Opened
    once, it keeps showing whatever tracks.kmz currently holds without
    any further manual action — see deploy/autopilot-publish-kmz for
    how tracks.kmz actually gets somewhere Earth can reach it.
    """
    try:
        link = simplekml.Kml()
        nl = link.newnetworklink(name="Autopilot live tracks")
        nl.link.href = f"{base_url.rstrip('/')}/tracks.kmz"
        nl.link.refreshmode = simplekml.RefreshMode.oninterval
        nl.link.refreshinterval = 600  # seconds; matches the ingest cadence
        path = kml_dir / "live_tracks.kml"
        link.save(str(path))
        return path
    except Exception:
        logger.exception("Network-link KML generation failed")
        return None


def ingest(
    localization_dir: Path,
    predictions_dir: Path,
    kml_dir: Path | None = None,
    kml_base_url: str | None = None,
    kml_trackers: tuple[str, ...] | None = DEFAULT_KML_TRACKERS,
    max_source_age_h: float | None = DEFAULT_MAX_SOURCE_AGE_H,
    now: datetime | None = None,
) -> int:
    """Convert every deployment file's tracks into prediction files.

    ``now`` is forwarded to :func:`write_track` for the
    ``max_source_age_h`` check (``None``: real wall-clock time) --
    mainly so tests can pin it rather than depend on the real clock.

    Returns the number of (deployment, float, tracker) files written.
    """
    written = 0
    all_tracks: dict[tuple[str, str, str], list] = {}
    for path in deployment_files(localization_dir):
        tracks = read_tracks(path)
        all_tracks.update(tracks)
        by_asset: dict[tuple[str, str], dict[str, list]] = defaultdict(dict)
        written_dirs: dict[tuple[str, str], list[Path]] = defaultdict(list)
        for (deployment, float_id, tracker), rows in tracks.items():
            by_asset[(deployment, float_id)][tracker] = rows
            out = write_track(
                predictions_dir,
                deployment,
                float_id,
                tracker,
                rows,
                max_source_age_h,
                now,
            )
            if out:
                written += 1
                written_dirs[(deployment, float_id)].append(out.parent)
        for (deployment, float_id), by_tracker in by_asset.items():
            write_comparison_plot(
                deployment,
                float_id,
                by_tracker,
                written_dirs.get((deployment, float_id), []),
            )
    if kml_dir is not None:
        write_kmz(all_tracks, kml_dir, kml_trackers)
        if kml_base_url:
            write_network_link(kml_base_url, kml_dir)
    return written


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s  %(message)s")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--localization-dir", default="localization", help="default: %(default)s"
    )
    ap.add_argument(
        "--predictions-dir", default="predictions", help="default: %(default)s"
    )
    ap.add_argument(
        "--kml-dir",
        default=None,
        help="write tracks.kmz (and, with --kml-base-url, live_tracks.kml) here; "
        "omit to skip KML output entirely",
    )
    ap.add_argument(
        "--kml-base-url",
        default=None,
        help="where --kml-dir's contents will be reachable from (e.g. a "
        "raw.githubusercontent.com URL) -- enables live_tracks.kml",
    )
    ap.add_argument(
        "--kml-trackers",
        default=",".join(DEFAULT_KML_TRACKERS),
        help="comma-separated tracker names to draw in the KMZ (default: "
        "%(default)s); 'all' draws every tracker present. Only affects the "
        "KMZ -- predictions and the comparison PNG always cover every tracker.",
    )
    ap.add_argument(
        "--max-source-age-h",
        type=float,
        default=DEFAULT_MAX_SOURCE_AGE_H,
        help="skip (re)writing a (deployment, float, tracker)'s prediction "
        "file once its newest real position is already older than this -- "
        "avoids silently re-publishing dead upstream data forever "
        "(default: %(default)s); 0 or negative disables the check",
    )
    args = ap.parse_args()

    kml_trackers = (
        None if args.kml_trackers == "all" else tuple(args.kml_trackers.split(","))
    )
    max_source_age_h = args.max_source_age_h if args.max_source_age_h > 0 else None
    n = ingest(
        Path(args.localization_dir),
        Path(args.predictions_dir),
        Path(args.kml_dir) if args.kml_dir else None,
        args.kml_base_url,
        kml_trackers,
        max_source_age_h,
    )
    print(f"Wrote {n} prediction file(s)")


if __name__ == "__main__":
    main()
