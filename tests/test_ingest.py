"""Tests for the real-time localization-feed adapter."""

import math
import shutil
from datetime import UTC, datetime
from pathlib import Path

from autopilot.follower import PredictedTrackFollower
from autopilot.ingest import deployment_files, ingest, read_tracks, write_track

FIXTURE = Path(__file__).parent / "fixtures" / "A1_float_tracks_latest.csv"


def test_deployment_files_matches_naming_convention(tmp_path):
    shutil.copy(FIXTURE, tmp_path / "A1_float_tracks_latest.csv")
    (tmp_path / "notes.txt").write_text("not a deployment file")
    (tmp_path / "weird_float_tracks_latest.csv.bak").write_text("")

    found = deployment_files(tmp_path)

    assert [p.name for p in found] == ["A1_float_tracks_latest.csv"]


def test_read_tracks_keeps_only_latest_segment():
    tracks = read_tracks(FIXTURE)
    rows = tracks[("A1", "em10962", "ekf")]

    # Segment 1 (near -119.9) must not appear; only segment 2 (near -120.4+).
    assert all(lon < -120 for _, _, lon, _ in rows)
    assert [(t.isoformat(), kind) for t, _, _, kind in rows] == [
        ("2026-08-07T17:41:21+00:00", "estimate"),
        ("2026-08-07T19:18:49+00:00", "estimate"),
        ("2026-08-07T21:18:49+00:00", "prediction"),
        ("2026-08-07T23:18:49+00:00", "prediction"),
    ]


def test_read_tracks_separates_trackers():
    tracks = read_tracks(FIXTURE)

    assert ("A1", "em10962", "ekf") in tracks
    assert ("A1", "em10962", "ops") in tracks
    assert ("A1", "em10962", "pf") in tracks
    # ekf and pf disagree at the same timestamp -> must not be merged.
    ekf_first = tracks[("A1", "em10962", "ekf")][0]
    pf_first = tracks[("A1", "em10962", "pf")][0]
    assert ekf_first[1:3] != pf_first[1:3]


def test_read_tracks_drops_bad_rows_keeps_good_ones():
    tracks = read_tracks(FIXTURE)
    rows = tracks[("A1", "mlf95", "ekf")]

    # 6 rows in the fixture for this key; bad-timestamp, NaN lat, and
    # inf lon must all be dropped, leaving the 3 good ones.
    assert len(rows) == 3
    assert all(math.isfinite(lat) and math.isfinite(lon) for _, lat, lon, _ in rows)


def test_write_track_anchors_filename_on_last_estimate_not_wall_clock(tmp_path):
    """The filename must reflect how fresh the real position knowledge
    is, not when this script happened to run — otherwise a feed that
    silently stops updating would still look "fresh" every cycle and
    the follower's staleness/FALLBACK check would never fire."""
    rows = [
        (datetime(2026, 8, 7, 17, 41, 21, tzinfo=UTC), 33.623, -120.442, "estimate"),
        (datetime(2026, 8, 7, 19, 18, 49, tzinfo=UTC), 33.620, -120.449, "estimate"),
        (datetime(2026, 8, 7, 21, 18, 49, tzinfo=UTC), 33.614, -120.513, "prediction"),
    ]

    path = write_track(tmp_path, "A1", "em10962", "ekf", rows)

    # Named for the last *estimate*, not the (later) prediction row and
    # not "now" — the write happens well after 19:18:49 in wall time.
    assert path == tmp_path / "A1_em10962_ekf" / "drifter_20260807T1918.csv"
    lines = path.read_text().splitlines()
    assert lines[0] == "time,latitude,longitude"
    assert len(lines) == 4  # header + all 3 rows, estimate and prediction alike

    # Must be readable by the follower's own parser, unchanged.
    track = PredictedTrackFollower._read_track(path)
    assert len(track) == 3
    assert track[0][1:] == (33.623, -120.442)


def test_write_track_empty_rows_writes_nothing(tmp_path):
    assert write_track(tmp_path, "A1", "ghost", "ekf", []) is None
    assert not (tmp_path / "A1_ghost_ekf").exists()


def test_write_track_prediction_only_writes_nothing(tmp_path):
    """No estimate row means no reliable 'as of' time -> don't guess."""
    rows = [
        (datetime(2026, 8, 7, 21, 18, 49, tzinfo=UTC), 33.614, -120.513, "prediction")
    ]
    assert write_track(tmp_path, "A1", "em10962", "ekf", rows) is None


def test_ingest_writes_one_file_per_deployment_float_tracker(tmp_path):
    loc = tmp_path / "localization"
    loc.mkdir()
    shutil.copy(FIXTURE, loc / "A1_float_tracks_latest.csv")
    predictions = tmp_path / "predictions"

    n = ingest(loc, predictions)

    # em10962: ekf, ops, pf; mlf95: ekf -> 4 combinations.
    assert n == 4
    # em10962's three trackers all last observed at 19:18:49 in the fixture.
    for tracker in ("ekf", "ops", "pf"):
        assert (
            predictions / f"A1_em10962_{tracker}" / "drifter_20260807T1918.csv"
        ).exists()
    # mlf95/ekf's last valid estimate (after dropping bad rows) is 11:00.
    assert (predictions / "A1_mlf95_ekf" / "drifter_20260806T1100.csv").exists()


def test_ingest_writes_comparison_plot_into_every_tracker_dir(tmp_path):
    """One tracks.png per asset, duplicated into each of its tracker
    directories so the web app can show it for whichever target is
    currently selected without any new lookup."""
    loc = tmp_path / "localization"
    loc.mkdir()
    shutil.copy(FIXTURE, loc / "A1_float_tracks_latest.csv")
    predictions = tmp_path / "predictions"

    ingest(loc, predictions)

    plots = [
        (predictions / f"A1_em10962_{tracker}" / "tracks.png")
        for tracker in ("ekf", "ops", "pf")
    ]
    assert all(p.is_file() and p.stat().st_size > 0 for p in plots)
    # Identical content copied to each, not independently (re)rendered.
    assert plots[0].read_bytes() == plots[1].read_bytes() == plots[2].read_bytes()
    # mlf95 is a different asset -> a different plot, not em10962's.
    mlf95_plot = predictions / "A1_mlf95_ekf" / "tracks.png"
    assert mlf95_plot.is_file()
    assert mlf95_plot.read_bytes() != plots[0].read_bytes()


def test_ingest_disambiguates_same_float_across_deployments(tmp_path):
    """The same physical float redeployed under a new deployment id
    must not collide with (or overwrite) the earlier deployment's
    predictions directory."""
    loc = tmp_path / "localization"
    loc.mkdir()
    shutil.copy(FIXTURE, loc / "A1_float_tracks_latest.csv")
    (loc / "B1_float_tracks_latest.csv").write_text(
        "deployment,float,tracker,segment,time,lat,lon,std_east_m,std_north_m,kind,gliders\n"
        "B1,em10962,ekf,1,2026-08-09T00:00:00,10.0,-150.0,1.0,1.0,estimate,sl999\n"
    )
    predictions = tmp_path / "predictions"

    ingest(loc, predictions)

    a1 = predictions / "A1_em10962_ekf" / "drifter_20260807T1918.csv"
    b1 = predictions / "B1_em10962_ekf" / "drifter_20260809T0000.csv"
    assert a1.exists() and b1.exists()
    assert "-120.4" in a1.read_text()  # A1's segment-2 track
    assert "-150.0" in b1.read_text()  # B1's independent track


def test_ingest_bad_file_does_not_touch_existing_predictions(tmp_path):
    """A corrupt/unreadable deployment file on one cycle must not
    delete or clobber predictions already written from a good cycle —
    the follower should keep flying on the last good file."""
    loc = tmp_path / "localization"
    loc.mkdir()
    shutil.copy(FIXTURE, loc / "A1_float_tracks_latest.csv")
    predictions = tmp_path / "predictions"
    ingest(loc, predictions)
    good = predictions / "A1_em10962_ekf" / "drifter_20260807T1918.csv"
    assert good.exists()
    original = good.read_text()

    # Next cycle: the deployment file is now unreadable (e.g. a bad sync).
    (loc / "A1_float_tracks_latest.csv").unlink()
    (loc / "A1_float_tracks_latest.csv").mkdir()  # open() will raise

    n = ingest(loc, predictions)

    assert n == 0
    assert good.read_text() == original  # untouched
