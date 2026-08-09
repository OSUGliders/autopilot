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

Run with: autopilot-ingest-predictions --localization-dir DIR --predictions-dir DIR
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("autopilot.ingest")

DEPLOYMENT_FILE_RE = re.compile(
    r"^(?P<deployment>[A-Za-z0-9]+)_float_tracks_latest\.csv$"
)
_SAFE = re.compile(r"[^A-Za-z0-9_]+")


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


def write_track(
    predictions_dir: Path,
    deployment: str,
    float_id: str,
    tracker: str,
    rows: list[tuple[datetime, float, float, str]],
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


def ingest(localization_dir: Path, predictions_dir: Path) -> int:
    """Convert every deployment file's tracks into prediction files.

    Returns the number of (deployment, float, tracker) files written.
    """
    written = 0
    for path in deployment_files(localization_dir):
        for (deployment, float_id, tracker), rows in read_tracks(path).items():
            if write_track(predictions_dir, deployment, float_id, tracker, rows):
                written += 1
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
    args = ap.parse_args()

    n = ingest(Path(args.localization_dir), Path(args.predictions_dir))
    print(f"Wrote {n} prediction file(s)")


if __name__ == "__main__":
    main()
