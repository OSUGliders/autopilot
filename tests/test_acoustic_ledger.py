"""Unit tests for the acoustic watcher's pure state-transition logic."""

from pathlib import Path

from autopilot.acoustic import ledger


def test_no_alert_below_threshold():
    state: dict = {}
    assert ledger.record(state, "osu685", "a.tbd", False, threshold=2) is None
    assert not state["osu685"].in_alert
    assert state["osu685"].consecutive_empty == 1


def test_alert_at_threshold():
    state: dict = {}
    ledger.record(state, "osu685", "a.tbd", False, threshold=2)
    transition = ledger.record(state, "osu685", "b.tbd", False, threshold=2)
    assert transition == "alert"
    assert state["osu685"].in_alert


def test_no_repeat_alert_while_still_empty():
    state: dict = {}
    ledger.record(state, "osu685", "a.tbd", False, threshold=1)
    transition = ledger.record(state, "osu685", "b.tbd", False, threshold=1)
    assert transition is None  # already in_alert; not a new transition
    assert state["osu685"].consecutive_empty == 2


def test_recovery_after_alert():
    state: dict = {}
    ledger.record(state, "osu685", "a.tbd", False, threshold=1)
    transition = ledger.record(state, "osu685", "b.tbd", True, threshold=1)
    assert transition == "recovery"
    assert not state["osu685"].in_alert
    assert state["osu685"].consecutive_empty == 0


def test_healthy_file_before_alert_is_not_a_recovery():
    state: dict = {}
    transition = ledger.record(state, "osu685", "a.tbd", True, threshold=1)
    assert transition is None


def test_gliders_tracked_independently():
    state: dict = {}
    ledger.record(state, "osu685", "a.tbd", False, threshold=1)
    transition = ledger.record(state, "osu684", "b.tbd", False, threshold=2)
    assert transition is None  # osu684's own first empty file, below its threshold
    assert state["osu685"].in_alert
    assert not state["osu684"].in_alert


def test_processed_dedup_tracked_per_glider():
    state: dict = {}
    ledger.record(state, "osu685", "a.tbd", True, threshold=2)
    assert state["osu685"].processed == {"a.tbd"}


def test_mark_processed_does_not_affect_alert_streak():
    state: dict = {}
    ledger.record(state, "osu685", "a.tbd", False, threshold=2)  # consecutive_empty=1
    ledger.mark_processed(state, "osu685", "b.tbd")  # exempted small file
    transition = ledger.record(state, "osu685", "c.tbd", False, threshold=2)

    assert state["osu685"].processed == {"a.tbd", "b.tbd", "c.tbd"}
    assert (
        transition == "alert"
    )  # streak: a.tbd, c.tbd -- b.tbd didn't reset or break it
    assert state["osu685"].consecutive_empty == 2


def test_mark_processed_creates_glider_entry():
    state: dict = {}
    ledger.mark_processed(state, "osu685", "a.tbd")
    assert state["osu685"].processed == {"a.tbd"}
    assert state["osu685"].consecutive_empty == 0
    assert not state["osu685"].in_alert


def test_save_and_load_round_trip(tmp_path):
    state: dict = {}
    ledger.record(state, "osu685", "a.tbd", False, threshold=2)
    ledger.record(state, "osu685", "b.tbd", False, threshold=2)
    path = tmp_path / "ledger.json"

    ledger.save(path, state)
    loaded = ledger.load(path)

    assert loaded["osu685"].processed == {"a.tbd", "b.tbd"}
    assert loaded["osu685"].consecutive_empty == 2
    assert loaded["osu685"].in_alert


def test_load_missing_file_returns_empty():
    assert ledger.load(Path("/nonexistent/ledger.json")) == {}


def test_load_corrupt_file_returns_empty_not_crash(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{not valid json")
    assert ledger.load(path) == {}


def test_load_non_object_json_returns_empty(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("[1, 2, 3]")
    assert ledger.load(path) == {}
