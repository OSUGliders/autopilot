"""Tests for the real-time localization-feed adapter."""

import math
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from autopilot.follower import PredictedTrackFollower
from autopilot.ingest import (
    build_kmz,
    deployment_files,
    ingest,
    read_tracks,
    write_kmz,
    write_network_link,
    write_track,
)

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


# ── KMZ / Google Earth ──────────────────────────────────────────


def test_build_kmz_default_trackers_excludes_pf():
    """Too many overlapping lines otherwise; ekf/ops/pf_lag2h only --
    the fixture's "pf" (2 hr lag not applied) is not in that set."""
    tracks = read_tracks(FIXTURE)
    xml = build_kmz(tracks).kml()

    assert "<name>ekf</name>" in xml
    assert "<name>ops</name>" in xml
    assert "<name>pf</name>" not in xml


def test_build_kmz_trackers_none_includes_everything():
    tracks = read_tracks(FIXTURE)
    xml = build_kmz(tracks, trackers=None).kml()

    for tracker in ("ekf", "ops", "pf"):
        assert f"<name>{tracker}</name>" in xml


def test_build_kmz_custom_trackers_filter():
    tracks = read_tracks(FIXTURE)
    xml = build_kmz(tracks, trackers=("pf",)).kml()

    assert "<name>pf</name>" in xml
    assert "<name>ekf</name>" not in xml
    assert "<name>ops</name>" not in xml


def test_build_kmz_folder_structure_and_recency_marker():
    tracks = read_tracks(FIXTURE)
    kml = build_kmz(tracks)
    xml = kml.kml()

    # Folder nesting: deployment > float > tracker.
    assert "<name>A1</name>" in xml
    assert "<name>em10962</name>" in xml
    assert "<name>mlf95</name>" in xml
    assert "<name>ekf</name>" in xml
    # Most-recent marker per tracker, plus observed/forecast lines.
    assert "ekf (most recent)" in xml
    assert "ekf observed" in xml
    assert "ekf forecast" in xml


def test_build_kmz_track_style_sets_normal_and_highlight():
    """A gx:Track's StyleMap left with only normalstyle set makes
    Google Earth fall back to its default pushpin for the highlight
    pair, showing up stacked on the intended circle. Both pairs must
    resolve to a real style with the circle icon."""
    import re

    tracks = read_tracks(FIXTURE)
    xml = build_kmz(tracks).kml()

    # Every StyleMap's normal and highlight pair must each point at a
    # Style that actually sets the circle icon (not an empty fallback).
    style_by_id = dict(re.findall(r'<Style id="(\d+)">(.*?)</Style>', xml, re.S))
    for pair_style_id in re.findall(r"<styleUrl>#(\d+)</styleUrl>", xml):
        style_body = style_by_id.get(pair_style_id)
        if style_body is None:
            continue  # a Point's own direct style, not part of a StyleMap
        if "<IconStyle" in style_body:
            assert "placemark_circle.png" in style_body


def test_build_kmz_recency_marker_includes_asset_id():
    tracks = read_tracks(FIXTURE)
    xml = build_kmz(tracks).kml()

    start = xml.index("<name>ekf (most recent)</name>")
    description = xml[start : start + 300]
    assert "Asset: em10962" in description


def test_build_kmz_forecast_points_get_clickable_markers():
    """Each forecast row gets its own small Placemark (separate from
    the forecast gx:Track) with lead time, absolute time, and lat/lon
    -- clicking the track itself shows nothing per-point."""
    tracks = read_tracks(FIXTURE)
    xml = build_kmz(tracks).kml()

    # Real fixture: em10962/ekf has 2 prediction rows, +2h and +4h from
    # the 19:18:49 last estimate (see test_read_tracks_keeps_only_latest_segment).
    assert "<name>ekf +2.0h</name>" in xml
    assert "<name>ekf +4.0h</name>" in xml

    start = xml.index("<name>ekf +2.0h</name>")
    description = xml[start : start + 300]
    assert "Asset: em10962" in description
    assert "Predicted: 2026-08-07 21:18 UTC" in description
    assert "+2.0 h from last estimate" in description
    assert "33.6142, -120.5135" in description


def test_build_kmz_colors_by_drifter_not_tracker():
    """em10962 has 3 trackers, mlf95 has 1 -> if colored per drifter,
    exactly 2 distinct full-alpha colors appear (one per float); the
    old per-tracker coloring would show up to 4."""
    import re

    tracks = read_tracks(FIXTURE)
    xml = build_kmz(tracks, trackers=None).kml()  # every tracker included

    full_alpha_colors = set(re.findall(r"<color>ff([0-9a-f]{6})</color>", xml))
    assert len(full_alpha_colors) == 2


def test_drifter_colors_deterministic_and_in_range():
    from autopilot.ingest import _drifter_colors

    colors = _drifter_colors(["b_float", "a_float", "c_float"])

    assert set(colors) == {"a_float", "b_float", "c_float"}
    assert all(0 <= v <= 255 for rgb in colors.values() for v in rgb)
    assert colors["a_float"] != colors["c_float"]  # opposite ends of the map


def test_drifter_colors_single_float_does_not_crash():
    from autopilot.ingest import _drifter_colors

    colors = _drifter_colors(["only_float"])

    assert "only_float" in colors


def test_write_kmz_produces_a_valid_zip_with_doc_kml(tmp_path):
    tracks = read_tracks(FIXTURE)

    path = write_kmz(tracks, tmp_path)

    assert path == tmp_path / "tracks.kmz"
    with zipfile.ZipFile(path) as z:
        assert "doc.kml" in z.namelist()


def test_write_kmz_empty_tracks_writes_nothing(tmp_path):
    assert write_kmz({}, tmp_path) is None
    assert not (tmp_path / "tracks.kmz").exists()


def test_write_network_link_points_at_kmz_url(tmp_path):
    path = write_network_link(
        "https://raw.githubusercontent.com/org/repo/main/", tmp_path
    )

    assert path == tmp_path / "live_tracks.kml"
    text = path.read_text()
    assert "https://raw.githubusercontent.com/org/repo/main/tracks.kmz" in text
    assert "onInterval" in text


def test_ingest_writes_kmz_and_network_link_when_requested(tmp_path):
    loc = tmp_path / "localization"
    loc.mkdir()
    shutil.copy(FIXTURE, loc / "A1_float_tracks_latest.csv")
    predictions = tmp_path / "predictions"
    kml_dir = tmp_path / "kml"

    ingest(loc, predictions, kml_dir=kml_dir, kml_base_url="https://example.com/live")

    assert (kml_dir / "tracks.kmz").is_file()
    assert (kml_dir / "live_tracks.kml").is_file()


def test_ingest_skips_kml_entirely_by_default(tmp_path):
    loc = tmp_path / "localization"
    loc.mkdir()
    shutil.copy(FIXTURE, loc / "A1_float_tracks_latest.csv")

    ingest(loc, tmp_path / "predictions")  # no kml_dir

    assert not (tmp_path / "tracks.kmz").exists()


def test_ingest_kmz_without_base_url_skips_network_link(tmp_path):
    loc = tmp_path / "localization"
    loc.mkdir()
    shutil.copy(FIXTURE, loc / "A1_float_tracks_latest.csv")
    kml_dir = tmp_path / "kml"

    ingest(loc, tmp_path / "predictions", kml_dir=kml_dir)  # no base url

    assert (kml_dir / "tracks.kmz").is_file()
    assert not (kml_dir / "live_tracks.kml").exists()
