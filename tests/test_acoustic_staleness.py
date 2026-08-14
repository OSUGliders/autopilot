"""Tests for the tbd/sbd staleness (fully-missing-transfer) detector.

File *content* is irrelevant here -- only names (for the Dinkum
pattern match) and mtimes (for age) matter -- so fixtures are plain
touched files, not the real .tbd/.sbd binaries used in
test_acoustic_watch.py.
"""

import os
import time
from pathlib import Path

import pytest

from autopilot.acoustic import staleness
from autopilot.acoustic.watch import dinkum_name_re

TBD_PATTERN = dinkum_name_re("tbd")
SBD_PATTERN = dinkum_name_re("sbd")


def touch(path: Path, age_minutes: float) -> None:
    path.write_text("")
    mtime = time.time() - age_minutes * 60
    os.utime(path, (mtime, mtime))


# ── newest_mtime ──────────────────────────────────────────────────


def test_newest_mtime_none_for_empty_dir(tmp_path):
    assert staleness.newest_mtime(tmp_path, TBD_PATTERN) is None


def test_newest_mtime_ignores_non_matching_names(tmp_path):
    touch(tmp_path / "03060070.tbd", age_minutes=0)  # raw numeric form, not Dinkum
    assert staleness.newest_mtime(tmp_path, TBD_PATTERN) is None


def test_newest_mtime_picks_the_freshest_match(tmp_path):
    touch(tmp_path / "osu685-2026-172-0-320.tbd", age_minutes=30)
    touch(tmp_path / "osu685-2026-172-0-321.tbd", age_minutes=5)
    newest = staleness.newest_mtime(tmp_path, TBD_PATTERN)
    assert newest == pytest.approx(time.time() - 5 * 60, abs=2)


# ── check: pure state transition ─────────────────────────────────


def test_no_alert_when_primary_fresh():
    state: dict = {}
    transition = staleness.check(
        "osu685",
        state,
        primary_age_minutes=5,
        corroboration_age_minutes=5,
        max_gap_minutes=60,
        corroboration_window_minutes=30,
    )
    assert transition is None
    assert not state["osu685"].in_alert


def test_alert_when_primary_stale_and_corroboration_fresh():
    state: dict = {}
    transition = staleness.check(
        "osu685",
        state,
        primary_age_minutes=120,
        corroboration_age_minutes=5,
        max_gap_minutes=60,
        corroboration_window_minutes=30,
    )
    assert transition == "alert"
    assert state["osu685"].in_alert


def test_no_alert_when_corroboration_also_stale():
    """Nothing arriving at all (comms outage, glider hasn't surfaced)
    must not be reported as a tbd-specific problem."""
    state: dict = {}
    transition = staleness.check(
        "osu685",
        state,
        primary_age_minutes=120,
        corroboration_age_minutes=120,
        max_gap_minutes=60,
        corroboration_window_minutes=30,
    )
    assert transition is None
    assert not state["osu685"].in_alert


def test_no_repeat_alert_while_still_stale():
    state: dict = {}
    staleness.check(
        "osu685",
        state,
        primary_age_minutes=120,
        corroboration_age_minutes=5,
        max_gap_minutes=60,
        corroboration_window_minutes=30,
    )
    transition = staleness.check(
        "osu685",
        state,
        primary_age_minutes=130,
        corroboration_age_minutes=6,
        max_gap_minutes=60,
        corroboration_window_minutes=30,
    )
    assert transition is None
    assert state["osu685"].in_alert


def test_recovery_after_alert():
    state: dict = {}
    staleness.check(
        "osu685",
        state,
        primary_age_minutes=120,
        corroboration_age_minutes=5,
        max_gap_minutes=60,
        corroboration_window_minutes=30,
    )
    transition = staleness.check(
        "osu685",
        state,
        primary_age_minutes=2,
        corroboration_age_minutes=2,
        max_gap_minutes=60,
        corroboration_window_minutes=30,
    )
    assert transition == "recovery"
    assert not state["osu685"].in_alert


def test_none_ages_never_transition_even_if_would_otherwise_alert():
    """An empty/not-yet-populated mirror directory must not be
    mistaken for a real gap -- this is a deployment/startup state."""
    state: dict = {}
    transition = staleness.check(
        "osu685",
        state,
        primary_age_minutes=None,
        corroboration_age_minutes=5,
        max_gap_minutes=60,
        corroboration_window_minutes=30,
    )
    assert transition is None
    assert not state["osu685"].in_alert


def test_gliders_tracked_independently():
    state: dict = {}
    staleness.check(
        "osu685",
        state,
        primary_age_minutes=120,
        corroboration_age_minutes=5,
        max_gap_minutes=60,
        corroboration_window_minutes=30,
    )
    transition = staleness.check(
        "osu684",
        state,
        primary_age_minutes=5,
        corroboration_age_minutes=5,
        max_gap_minutes=60,
        corroboration_window_minutes=30,
    )
    assert transition is None
    assert state["osu685"].in_alert
    assert not state["osu684"].in_alert


# ── load/save ─────────────────────────────────────────────────────


def test_save_and_load_round_trip(tmp_path):
    state: dict = {}
    staleness.check(
        "osu685",
        state,
        primary_age_minutes=120,
        corroboration_age_minutes=5,
        max_gap_minutes=60,
        corroboration_window_minutes=30,
    )
    path = tmp_path / "state.json"

    staleness.save(path, state)
    loaded = staleness.load(path)

    assert loaded["osu685"].in_alert


def test_load_missing_file_returns_empty():
    assert staleness.load(Path("/nonexistent/state.json")) == {}


def test_load_corrupt_file_returns_empty_not_crash(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json")
    assert staleness.load(path) == {}


# ── scan_once: integration over real directories ─────────────────


def test_scan_once_alerts_and_notifies(tmp_path):
    primary_dir = tmp_path / "tbd"
    corroboration_dir = tmp_path / "sbd"
    primary_dir.mkdir()
    corroboration_dir.mkdir()
    touch(primary_dir / "osu1267-2026-215-0-10.tbd", age_minutes=180)
    touch(corroboration_dir / "osu1267-2026-215-0-12.sbd", age_minutes=3)

    sent = []
    state: dict = {}
    staleness.scan_once(
        "osu1267",
        primary_dir,
        TBD_PATTERN,
        corroboration_dir,
        SBD_PATTERN,
        state,
        max_gap_minutes=60,
        corroboration_window_minutes=30,
        send=lambda subject, body: sent.append((subject, body)),
    )

    assert state["osu1267"].in_alert
    assert len(sent) == 1
    assert "no data received" in sent[0][0]


def test_scan_once_no_alert_when_primary_fresh(tmp_path):
    primary_dir = tmp_path / "tbd"
    corroboration_dir = tmp_path / "sbd"
    primary_dir.mkdir()
    corroboration_dir.mkdir()
    touch(primary_dir / "osu1267-2026-215-0-10.tbd", age_minutes=5)
    touch(corroboration_dir / "osu1267-2026-215-0-12.sbd", age_minutes=3)

    sent = []
    state: dict = {}
    staleness.scan_once(
        "osu1267",
        primary_dir,
        TBD_PATTERN,
        corroboration_dir,
        SBD_PATTERN,
        state,
        max_gap_minutes=60,
        corroboration_window_minutes=30,
        send=lambda subject, body: sent.append((subject, body)),
    )

    assert not state["osu1267"].in_alert
    assert sent == []


def test_scan_once_slack_failure_does_not_raise(tmp_path):
    primary_dir = tmp_path / "tbd"
    corroboration_dir = tmp_path / "sbd"
    primary_dir.mkdir()
    corroboration_dir.mkdir()
    touch(primary_dir / "osu1267-2026-215-0-10.tbd", age_minutes=180)
    touch(corroboration_dir / "osu1267-2026-215-0-12.sbd", age_minutes=3)

    def broken_send(subject, body):
        raise RuntimeError("webhook down")

    state: dict = {}
    staleness.scan_once(
        "osu1267",
        primary_dir,
        TBD_PATTERN,
        corroboration_dir,
        SBD_PATTERN,
        state,
        max_gap_minutes=60,
        corroboration_window_minutes=30,
        send=broken_send,
    )

    assert state["osu1267"].in_alert  # transition still recorded despite Slack failure
