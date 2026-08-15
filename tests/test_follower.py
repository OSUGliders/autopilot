"""Unit tests for PredictedTrackFollower helpers.

The duplicate-timestamp case was found by replaying real deployment
logs: repeated rows in a prediction file made end-extrapolation divide
by zero.
"""

import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import pytest
import yaml

from autopilot.follower import M_PER_DEG_LAT, PredictedTrackFollower, offset_position
from autopilot.safety import Verdict

T0 = datetime(2026, 3, 22, 0, 0, tzinfo=UTC)


def test_read_track_dedups_timestamps(tmp_path):
    rows = ["time,latitude,longitude"]
    rows += [f"{(T0).isoformat()},33.00,-117.70"] * 3  # duplicated start
    rows += [f"{(T0 + timedelta(hours=2)).isoformat()},33.10,-117.60"]
    rows += [f"{(T0 + timedelta(hours=4)).isoformat()},33.20,-117.50"] * 2
    path = tmp_path / "drifter_20260322T0000.csv"
    path.write_text("\n".join(rows) + "\n")

    track = PredictedTrackFollower._read_track(path)
    times = [t for t, _, _ in track]
    assert len(times) == len(set(times)) == 3

    # Extrapolation past either end must not divide by zero.
    lat, lon, extrapolated = PredictedTrackFollower._position_at(
        track, T0 + timedelta(hours=6)
    )
    assert extrapolated
    assert lat > 33.20


def test_predictions_dir_required():
    import pytest

    with pytest.raises(ValueError, match="predictions_dir"):
        PredictedTrackFollower({}, Queue(), Queue())


def test_read_track_assumes_utc_for_naive_times(tmp_path):
    path = tmp_path / "drifter_20260322T0000.csv"
    path.write_text(
        "time,latitude,longitude\n"
        "2026-03-22T00:00:00,33.00,-117.70\n"  # no UTC offset
        "2026-03-22T02:00:00+00:00,33.10,-117.60\n"
    )
    track = PredictedTrackFollower._read_track(path)
    assert len(track) == 2  # mixed naive/aware must not raise on sort
    assert all(t.tzinfo is not None for t, _, _ in track)


def test_read_track_drops_non_finite_rows(tmp_path):
    path = tmp_path / "drifter_20260322T0000.csv"
    path.write_text(
        "time,latitude,longitude\n"
        "2026-03-22T00:00:00+00:00,33.00,-117.70\n"
        "2026-03-22T02:00:00+00:00,nan,-117.60\n"
        "2026-03-22T04:00:00+00:00,33.20,inf\n"
        "2026-03-22T06:00:00+00:00,33.30,-117.40\n"
    )
    track = PredictedTrackFollower._read_track(path)
    assert [lat for _, lat, _ in track] == [33.00, 33.30]


def test_corrupt_prediction_degrades_to_fallback(tmp_path):
    """A file that cannot be parsed must yield FALLBACK + notification,
    not an exception that loses the surfacing entirely."""
    predictions = tmp_path / "predictions"
    predictions.mkdir()
    (predictions / "drifter_20260322T0000.csv").write_text(
        "time,latitude,longitude\ngarbage,not-a-float,??\n"
    )
    queue_out: Queue = Queue()
    follower = PredictedTrackFollower(
        {
            "predictions_dir": str(predictions),
            "plot_dir": str(tmp_path / "plots"),
            "archive_dir": str(tmp_path / "archive"),
        },
        Queue(),
        queue_out,
    )
    event = SimpleNamespace(
        vehicle_name="osu999",
        timestamp=datetime(2026, 3, 22, 1, 0, tzinfo=UTC),
        gps_lat=33.1,
        gps_lon=-117.7,
    )

    follower.on_surfacing(event)  # must not raise

    assert queue_out.empty(), "no goto may be sent from a corrupt prediction"
    assert follower._in_fallback, "pilot notification path must have fired"


# ── Live config reload ──────────────────────────────────────────


def reloading_follower(tmp_path):
    """A follower whose config names its own path (reload enabled)."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "predictions_dir": "predictions",
                "max_waypoint_jump_km": 30.0,
                "config_file": str(cfg_path),
            }
        )
    )
    config = yaml.safe_load(cfg_path.read_text())
    return PredictedTrackFollower(config, Queue(), Queue()), cfg_path


def rewrite(cfg_path, text):
    """Overwrite the config, forcing a visibly newer mtime."""
    mtime = cfg_path.stat().st_mtime
    cfg_path.write_text(text)
    os.utime(cfg_path, (mtime + 10, mtime + 10))


def test_reload_applies_hot_threshold(tmp_path):
    follower, cfg_path = reloading_follower(tmp_path)
    assert follower.max_jump_km == 30.0

    rewrite(cfg_path, cfg_path.read_text().replace("30.0", "12.0"))
    follower._maybe_reload()
    assert follower.max_jump_km == 12.0

    # Unchanged file: nothing to do.
    follower._maybe_reload()
    assert follower.max_jump_km == 12.0


def test_reload_switches_predictions_dir(tmp_path):
    follower, cfg_path = reloading_follower(tmp_path)
    assert follower.predictions_dir == Path("predictions")

    new = yaml.safe_load(cfg_path.read_text())
    new["predictions_dir"] = "predictions/6560"
    rewrite(cfg_path, yaml.safe_dump(new))
    follower._maybe_reload()
    assert follower.predictions_dir == Path("predictions/6560")


def test_reload_keeps_predictions_dir_if_blanked(tmp_path, caplog):
    follower, cfg_path = reloading_follower(tmp_path)
    new = yaml.safe_load(cfg_path.read_text())
    del new["predictions_dir"]
    with caplog.at_level(logging.WARNING, logger="sfmc.predicted_track"):
        rewrite(cfg_path, yaml.safe_dump(new))
        follower._maybe_reload()
    assert follower.predictions_dir == Path("predictions")
    assert any("predictions_dir missing" in r.message for r in caplog.records)


def test_reload_keeps_settings_on_broken_yaml(tmp_path):
    follower, cfg_path = reloading_follower(tmp_path)
    rewrite(cfg_path, "predictions_dir: [unclosed\n")
    follower._maybe_reload()
    assert follower.max_jump_km == 30.0


def test_reload_skips_invalid_value_applies_the_rest(tmp_path):
    """One bad value must not raise, block other keys, or half-apply."""
    follower, cfg_path = reloading_follower(tmp_path)
    new = yaml.safe_load(cfg_path.read_text())
    new["max_waypoint_jump_km"] = 12.0
    new["num_legs_to_run"] = "forever"  # int() raises
    rewrite(cfg_path, yaml.safe_dump(new))

    follower._maybe_reload()  # must not raise

    assert follower.max_jump_km == 12.0  # good key applied
    assert follower.num_legs_to_run == -1  # bad key kept at old value


def test_reload_keeps_settings_on_non_mapping_yaml(tmp_path):
    follower, cfg_path = reloading_follower(tmp_path)
    rewrite(cfg_path, "- not\n- a\n- mapping\n")
    follower._maybe_reload()  # must not raise
    assert follower.max_jump_km == 30.0


def test_reload_warns_on_restart_only_key(tmp_path, caplog):
    follower, cfg_path = reloading_follower(tmp_path)
    rewrite(cfg_path, cfg_path.read_text() + "sequence_number: 7\n")
    with caplog.at_level(logging.WARNING, logger="sfmc.predicted_track"):
        follower._maybe_reload()
    assert follower.sequence_number == 10  # startup value kept
    assert any("requires a restart" in r.message for r in caplog.records)


def test_adopts_framework_log_handlers(tmp_path):
    """With sfmc-follow's loggers present, our log lands in its file."""
    from autopilot import follower as follower_mod

    log_path = tmp_path / "osu999.log"
    framework = logging.getLogger("sfmc.osu999.FOLLOW")
    framework.setLevel(logging.INFO)  # as sfmc-follow's setup_logging does
    handler = logging.FileHandler(log_path)
    framework.addHandler(handler)
    try:
        PredictedTrackFollower({"predictions_dir": "p"}, Queue(), Queue())
    finally:
        framework.removeHandler(handler)
        follower_mod.logger.handlers.clear()
        follower_mod.logger.propagate = True
        handler.close()
    assert "Loaded config" in log_path.read_text()


def test_set_notifier_sends_startup_confirmation():
    follower = PredictedTrackFollower({"predictions_dir": "p"}, Queue(), Queue())
    calls = []
    follower.notify = lambda key, summary, detail, *, min_gap_seconds: calls.append(
        (key, summary, detail, min_gap_seconds)
    )

    follower.set_notifier(object())  # any sentinel; BaseFollower just stores it

    assert len(calls) == 1
    key, summary, detail, gap = calls[0]
    assert key == "startup" and gap == 0.0
    assert "sequence_number" in detail


# ── Waypoint offset ───────────────────────────────────────────────


def test_offset_position_shifts_north_and_east():
    lat, lon = offset_position(30.0, -120.0, north_m=M_PER_DEG_LAT, east_m=0.0)
    assert lat == pytest.approx(31.0)
    assert lon == pytest.approx(-120.0)

    lat, lon = offset_position(0.0, 0.0, north_m=0.0, east_m=M_PER_DEG_LAT)
    assert lat == pytest.approx(0.0)
    assert lon == pytest.approx(1.0)  # cos(0) == 1 at the equator


def test_offset_position_negative_shifts_south_and_west():
    lat, lon = offset_position(
        30.0, -120.0, north_m=-M_PER_DEG_LAT, east_m=-M_PER_DEG_LAT
    )
    assert lat < 30.0
    assert lon < -120.0


TRACK = [
    (T0, 33.0, -117.7),
    (T0 + timedelta(hours=6), 33.1, -117.6),
]
EVENT_999 = SimpleNamespace(vehicle_name="osu999", gps_lat=33.0, gps_lon=-117.7)


def test_compute_waypoints_no_offset_by_default():
    follower = PredictedTrackFollower(
        {"predictions_dir": "p", "waypoint_lead_h": [3.0]}, Queue(), Queue()
    )
    assert follower.offset_north_m == 0.0
    assert follower.offset_east_m == 0.0

    waypoints, _, _ = follower._compute_waypoints(EVENT_999, T0, TRACK, T0, "x.csv")

    unoffset_lat, unoffset_lon, _ = PredictedTrackFollower._position_at(
        TRACK, T0 + timedelta(hours=3)
    )
    wpt_lon, wpt_lat = waypoints[0]
    assert wpt_lat == pytest.approx(unoffset_lat)
    assert wpt_lon == pytest.approx(unoffset_lon)


def test_compute_waypoints_applies_configured_offset():
    follower = PredictedTrackFollower(
        {
            "predictions_dir": "p",
            "waypoint_lead_h": [3.0],
            "waypoint_offset_north_m": 500.0,
            "waypoint_offset_east_m": -200.0,
        },
        Queue(),
        Queue(),
    )

    waypoints, drifter_now, _ = follower._compute_waypoints(
        EVENT_999, T0, TRACK, T0, "x.csv"
    )

    unoffset_lat, unoffset_lon, _ = PredictedTrackFollower._position_at(
        TRACK, T0 + timedelta(hours=3)
    )
    expected_lat, expected_lon = offset_position(
        unoffset_lat, unoffset_lon, 500.0, -200.0
    )
    wpt_lon, wpt_lat = waypoints[0]
    assert wpt_lat == pytest.approx(expected_lat)
    assert wpt_lon == pytest.approx(expected_lon)
    assert (wpt_lat, wpt_lon) != pytest.approx((unoffset_lat, unoffset_lon))

    # "drifter now" reflects the true predicted position, not the offset waypoint.
    d_lat, d_lon = drifter_now
    true_lat, true_lon, _ = PredictedTrackFollower._position_at(TRACK, T0)
    assert d_lat == pytest.approx(true_lat)
    assert d_lon == pytest.approx(true_lon)


def test_reload_applies_waypoint_offset(tmp_path):
    follower, cfg_path = reloading_follower(tmp_path)
    assert follower.offset_north_m == 0.0
    assert follower.offset_east_m == 0.0

    new = yaml.safe_load(cfg_path.read_text())
    new["waypoint_offset_north_m"] = 250.0
    new["waypoint_offset_east_m"] = -100.0
    rewrite(cfg_path, yaml.safe_dump(new))
    follower._maybe_reload()

    assert follower.offset_north_m == 250.0
    assert follower.offset_east_m == -100.0


# ── FALLBACK notification edges ─────────────────────────────────


def notifying_follower():
    """A follower whose notify() records calls instead of emailing."""
    follower = PredictedTrackFollower({"predictions_dir": "p"}, Queue(), Queue())
    calls = []
    follower.notify = lambda key, summary, detail, *, min_gap_seconds: calls.append(
        (key, summary, min_gap_seconds)
    )
    return follower, calls


EVENT = SimpleNamespace(vehicle_name="osu684", gps_lat=33.13, gps_lon=-117.70)
OK = Verdict(True, "", "")
BAD = Verdict(False, "STALE", "prediction 13h old")


def test_fallback_notify_edges():
    follower, calls = notifying_follower()

    follower._notify_fallback(T0, EVENT, OK)
    assert not calls, "no email while healthy"

    # Entry forces a send (gap 0); reminders defer to the rate limit.
    follower._notify_fallback(T0, EVENT, BAD)
    follower._notify_fallback(T0, EVENT, BAD)
    assert [c[2] for c in calls] == [0.0, follower.fallback_reminder_h * 3600.0]
    assert "FALLBACK (STALE)" in calls[0][1]
    assert "still in FALLBACK" in calls[1][1]

    # Recovery emails once, then healthy surfacings are silent.
    follower._notify_fallback(T0, EVENT, OK)
    follower._notify_fallback(T0, EVENT, OK)
    assert len(calls) == 3
    assert "recovered" in calls[2][1] and calls[2][2] == 0.0
