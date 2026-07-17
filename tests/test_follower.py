"""Unit tests for PredictedTrackFollower helpers.

The duplicate-timestamp case was found by replaying real deployment
logs: repeated rows in a prediction file made end-extrapolation divide
by zero.
"""

import logging
import os
from datetime import UTC, datetime, timedelta
from queue import Queue

import yaml

from autopilot.follower import PredictedTrackFollower

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


def test_reload_keeps_settings_on_broken_yaml(tmp_path):
    follower, cfg_path = reloading_follower(tmp_path)
    rewrite(cfg_path, "predictions_dir: [unclosed\n")
    follower._maybe_reload()
    assert follower.max_jump_km == 30.0


def test_reload_warns_on_restart_only_key(tmp_path, caplog):
    follower, cfg_path = reloading_follower(tmp_path)
    rewrite(cfg_path, cfg_path.read_text() + "sequence_number: 7\n")
    with caplog.at_level(logging.WARNING, logger="sfmc.predicted_track"):
        follower._maybe_reload()
    assert follower.sequence_number == 10  # startup value kept
    assert any("requires a restart" in r.message for r in caplog.records)
