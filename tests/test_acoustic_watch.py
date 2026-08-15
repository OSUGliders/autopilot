"""Tests for the acoustic-data watcher.

``check_variable`` is tested against a real .tbd + .cac pair
(downloaded from osu685 on gliderfmc1 and verified by hand — see
tests/fixtures/) rather than a synthetic file, so a real dbdreader
parsing regression would actually be caught. ``scan_once``'s
orchestration (file discovery, dedup, Slack dispatch) is tested with
a monkeypatched ``check_variable``, since the transition logic itself
is already covered exhaustively in test_acoustic_ledger.py.

Known real values in the fixture (see the sha in git history for how
these were obtained): sci_generic_a = [20628., 20628.];
sci_m_present_time has 34 samples spanning ~607.13 s (~10.12 min).

A second real fixture, a .sbd (flight) file from osusim, confirms the
watcher is genuinely format-agnostic, not just tbd-shaped: same
dbdreader.DBD API, no sci_generic_* variables at all (those are
science-computer-only), but real flight variables instead --
m_gps_lat/m_gps_lon (50 samples: GPS-fix rate) and
m_present_time/m_present_secs_into_mission (1763 samples: a real
duration reference for .sbd rate checks).
"""

import shutil
from pathlib import Path

import pytest

from autopilot.acoustic import watch

FIXTURES = Path(__file__).parent / "fixtures"
REAL_TBD = FIXTURES / "osu685-2026-172-0-324.tbd"
REAL_SBD = FIXTURES / "osusim-2026-191-0-922.sbd"


# ── check_variable: real file, real cache ────────────────────────


def test_presence_check_counts_all_samples():
    result = watch.check_variable(REAL_TBD, FIXTURES, "sci_generic_a")
    assert result.count == 2
    assert result.ok


def test_equals_counts_only_matching_samples():
    result = watch.check_variable(REAL_TBD, FIXTURES, "sci_generic_a", equals=20628.0)
    assert result.count == 2  # both real samples equal this value
    assert result.ok


def test_equals_value_not_present_gives_zero_count():
    result = watch.check_variable(REAL_TBD, FIXTURES, "sci_generic_a", equals=99999.0)
    assert result.count == 0
    assert not result.ok  # below default min_count=1


def test_min_count_threshold_respected():
    result = watch.check_variable(REAL_TBD, FIXTURES, "sci_generic_a", min_count=3)
    assert result.count == 2
    assert not result.ok  # 2 < 3


def test_rate_computed_from_duration_variable():
    result = watch.check_variable(
        REAL_TBD, FIXTURES, "sci_generic_a", duration_variable="sci_m_present_time"
    )
    assert result.duration_minutes == pytest.approx(10.1188, abs=1e-3)
    assert result.rate_per_minute == pytest.approx(2 / 10.1188, abs=1e-3)


def test_rate_threshold_below_passes_ok():
    result = watch.check_variable(
        REAL_TBD,
        FIXTURES,
        "sci_generic_a",
        duration_variable="sci_m_present_time",
        min_rate_per_minute=0.1,  # actual rate ~0.198/min
    )
    assert result.ok


def test_rate_threshold_above_fails():
    result = watch.check_variable(
        REAL_TBD,
        FIXTURES,
        "sci_generic_a",
        duration_variable="sci_m_present_time",
        min_rate_per_minute=0.5,  # actual rate ~0.198/min
    )
    assert not result.ok


def test_rate_threshold_sets_expected_count():
    result = watch.check_variable(
        REAL_TBD,
        FIXTURES,
        "sci_generic_a",
        duration_variable="sci_m_present_time",
        min_rate_per_minute=0.1,
    )
    assert result.expected_count == pytest.approx(0.1 * result.duration_minutes)
    assert "expected >= 1.0" in result.announce_text()


def test_no_rate_threshold_leaves_expected_count_none():
    result = watch.check_variable(REAL_TBD, FIXTURES, "sci_generic_a")
    assert result.expected_count is None
    assert result.announce_text() == "2 time(s)"


def test_missing_duration_variable_falls_back_to_min_count():
    result = watch.check_variable(
        REAL_TBD,
        FIXTURES,
        "sci_generic_a",
        duration_variable="not_a_real_sensor_name",
        min_rate_per_minute=100,  # would fail if a rate were computed
        min_count=1,
    )
    assert result.rate_per_minute is None
    assert result.ok  # falls back to count (2) >= min_count (1)


def test_missing_cache_file_returns_none(tmp_path):
    """No .cac available -> unreadable, not "below threshold"; the
    caller must retry rather than count this as a failing file."""
    assert watch.check_variable(REAL_TBD, tmp_path, "sci_generic_a") is None


def test_unknown_variable_name_returns_none():
    """A variable dbdreader doesn't recognize at all raises inside
    dbdreader -- the file opened fine, but nothing could be read from
    it, so this must degrade to None (retry), not a false "below
    threshold" reading that would count toward an alert streak."""
    assert watch.check_variable(REAL_TBD, FIXTURES, "not_a_real_sensor_name") is None


# ── check_variable: real .sbd file, proving format-generality ───


def test_sbd_presence_check():
    """Same function, a completely different Slocum file type (flight,
    not science) -- proves the watcher isn't secretly tbd-shaped."""
    result = watch.check_variable(REAL_SBD, FIXTURES, "m_gps_lat")
    assert result.count == 50
    assert result.ok


def test_sbd_has_no_sci_generic_variables():
    """sci_generic_a is a globally-known Slocum sensor name, just not
    one this flight file's sensor config includes -- dbdreader
    degrades that gracefully to an empty result (with a warning),
    unlike a completely unknown name (see
    test_unknown_variable_name_returns_none), which raises. Confirms
    the two file types are genuinely different variable universes:
    this reads as a clean "no data" (count 0), not an error."""
    result = watch.check_variable(REAL_SBD, FIXTURES, "sci_generic_a")
    assert result.count == 0
    assert not result.ok


def test_sbd_rate_check_with_real_duration_variable():
    result = watch.check_variable(
        REAL_SBD, FIXTURES, "m_gps_lat", duration_variable="m_present_time"
    )
    assert result.duration_minutes is not None
    assert result.rate_per_minute == pytest.approx(50 / result.duration_minutes)


def test_scan_respects_sbd_extension(input_dir, monkeypatch):
    shutil.copy(REAL_SBD, input_dir / "osusim-2026-191-0-922.sbd")
    calls = []
    monkeypatch.setattr(
        watch,
        "check_variable",
        lambda path, *a, **k: (
            calls.append(path.name)
            or watch.CheckResult(
                count=50, duration_minutes=None, rate_per_minute=None, ok=True
            )
        ),
    )

    state: dict = {}
    watch.scan_once(
        input_dir,
        FIXTURES,
        "m_gps_lat",
        {},
        state,
        "osusim",
        watch.dinkum_name_re("sbd"),
        2,
        None,
    )

    assert calls == ["osusim-2026-191-0-922.sbd"]


# ── scan_once: file discovery, dedup, dispatch ───────────────────


@pytest.fixture
def input_dir(tmp_path):
    d = tmp_path / "raw"
    d.mkdir()
    return d


def test_scan_respects_extension(input_dir, monkeypatch):
    shutil.copy(REAL_TBD, input_dir / "osu685-2026-172-0-324.sbd")  # wrong extension
    monkeypatch.setattr(
        watch, "check_variable", lambda *a, **k: pytest.fail("should not run")
    )

    state: dict = {}
    watch.scan_once(
        input_dir,
        FIXTURES,
        "sci_generic_a",
        {},
        state,
        "osu685",
        watch.dinkum_name_re("tbd"),
        2,
        None,
    )

    assert state == {}


def test_scan_ignores_raw_numeric_names(input_dir, monkeypatch):
    shutil.copy(REAL_TBD, input_dir / "03060070.tbd")  # raw glider-numeric form
    monkeypatch.setattr(
        watch, "check_variable", lambda *a, **k: pytest.fail("should not run")
    )

    state: dict = {}
    watch.scan_once(
        input_dir,
        FIXTURES,
        "sci_generic_a",
        {},
        state,
        "osu685",
        watch.dinkum_name_re("tbd"),
        2,
        None,
    )

    assert state == {}


def test_scan_processes_matching_file_once(input_dir, monkeypatch):
    shutil.copy(REAL_TBD, input_dir / "osu685-2026-172-0-324.tbd")
    calls = []
    monkeypatch.setattr(
        watch,
        "check_variable",
        lambda path, *a, **k: (
            calls.append(path.name)
            or watch.CheckResult(
                count=1, duration_minutes=None, rate_per_minute=None, ok=True
            )
        ),
    )

    state: dict = {}
    pattern = watch.dinkum_name_re("tbd")
    watch.scan_once(
        input_dir, FIXTURES, "sci_generic_a", {}, state, "osu685", pattern, 2, None
    )
    watch.scan_once(
        input_dir, FIXTURES, "sci_generic_a", {}, state, "osu685", pattern, 2, None
    )

    assert calls == ["osu685-2026-172-0-324.tbd"]  # second scan: already processed
    assert state["osu685"].processed == {"osu685-2026-172-0-324.tbd"}


def test_scan_notifies_on_alert_and_recovery(input_dir, monkeypatch):
    names = [f"osu685-2026-172-0-{320 + i}.tbd" for i in range(3)]
    for name in names:
        shutil.copy(REAL_TBD, input_dir / name)
    oks = iter(
        [False, False, True]
    )  # 2 below threshold (crosses threshold=2), then recovers

    def fake_check(path, *a, **k):
        ok = next(oks)
        return watch.CheckResult(
            count=0 if not ok else 5, duration_minutes=None, rate_per_minute=None, ok=ok
        )

    monkeypatch.setattr(watch, "check_variable", fake_check)
    sent: list[tuple[str, str]] = []

    state: dict = {}
    watch.scan_once(
        input_dir,
        FIXTURES,
        "sci_generic_a",
        {},
        state,
        "osu685",
        watch.dinkum_name_re("tbd"),
        threshold=2,
        send=lambda subject, body: sent.append((subject, body)),
    )

    assert len(sent) == 2
    assert "failing" in sent[0][0]
    assert "recovered" in sent[1][0]
    assert not state["osu685"].in_alert


def test_scan_unreadable_file_neither_processed_nor_counted(input_dir, monkeypatch):
    shutil.copy(REAL_TBD, input_dir / "osu685-2026-172-0-324.tbd")
    monkeypatch.setattr(watch, "check_variable", lambda *a, **k: None)

    state: dict = {}
    watch.scan_once(
        input_dir,
        FIXTURES,
        "sci_generic_a",
        {},
        state,
        "osu685",
        watch.dinkum_name_re("tbd"),
        2,
        None,
    )

    assert "osu685" not in state  # never recorded -> retried next scan


def test_scan_slack_failure_does_not_raise(input_dir, monkeypatch):
    shutil.copy(REAL_TBD, input_dir / "osu685-2026-172-0-324.tbd")
    monkeypatch.setattr(
        watch,
        "check_variable",
        lambda *a, **k: watch.CheckResult(
            count=0, duration_minutes=None, rate_per_minute=None, ok=False
        ),
    )

    def broken_send(subject, body):
        raise RuntimeError("webhook down")

    state: dict = {}
    watch.scan_once(
        input_dir,
        FIXTURES,
        "sci_generic_a",
        {},
        state,
        "osu685",
        watch.dinkum_name_re("tbd"),
        threshold=1,
        send=broken_send,
    )

    assert state["osu685"].in_alert  # ledger transition still recorded


# ── scan_once: --announce routine messages ───────────────────────


def test_announce_off_by_default_sends_no_routine_messages(input_dir, monkeypatch):
    shutil.copy(REAL_TBD, input_dir / "osu685-2026-172-0-324.tbd")
    monkeypatch.setattr(
        watch,
        "check_variable",
        lambda *a, **k: watch.CheckResult(
            count=1, duration_minutes=None, rate_per_minute=None, ok=True
        ),
    )
    sent: list[tuple[str, str]] = []

    watch.scan_once(
        input_dir,
        FIXTURES,
        "sci_generic_a",
        {},
        {},
        "osu685",
        watch.dinkum_name_re("tbd"),
        2,
        send=lambda subject, body: sent.append((subject, body)),
        # announce defaults to False
    )

    assert sent == []  # no alert/recovery transition either -> nothing sent


def test_announce_sends_one_combined_message(input_dir, monkeypatch):
    shutil.copy(REAL_TBD, input_dir / "osu685-2026-172-0-324.tbd")
    monkeypatch.setattr(
        watch,
        "check_variable",
        lambda *a, **k: watch.CheckResult(
            count=2,
            duration_minutes=10.1,
            rate_per_minute=0.2,
            ok=True,
            expected_count=1.0,
        ),
    )
    sent: list[tuple[str, str]] = []

    watch.scan_once(
        input_dir,
        FIXTURES,
        "sci_generic_a",
        {"equals": 20628.0},
        {},
        "osu685",
        watch.dinkum_name_re("tbd"),
        2,
        send=lambda subject, body: sent.append((subject, body)),
        announce=True,
    )

    assert len(sent) == 1  # size folded into the one content message, no arrival post
    subject, body = sent[0]
    assert (
        subject == "osu685: osu685-2026-172-0-324.tbd"
    )  # no failure -> no emoji prefix
    assert "1313 bytes" in body  # the fixture's real size
    assert "sci_generic_a == 20628" in body
    assert "2 occurrences" in body
    assert "expected >= 1.0" in body


def test_announce_prefixes_failing_file_with_alarm_emoji(input_dir, monkeypatch):
    shutil.copy(REAL_TBD, input_dir / "osu685-2026-172-0-324.tbd")
    monkeypatch.setattr(
        watch,
        "check_variable",
        lambda *a, **k: watch.CheckResult(
            count=0, duration_minutes=None, rate_per_minute=None, ok=False
        ),
    )
    sent: list[tuple[str, str]] = []

    watch.scan_once(
        input_dir,
        FIXTURES,
        "sci_generic_a",
        {},
        {},
        "osu685",
        watch.dinkum_name_re("tbd"),
        2,
        send=lambda subject, body: sent.append((subject, body)),
        announce=True,
    )

    assert sent[0][0] == "🚨 osu685: osu685-2026-172-0-324.tbd"


def test_announce_silent_while_file_stays_unreadable(input_dir, monkeypatch):
    """No arrival message exists to fire independently of the content
    check, so an unreadable file (e.g. missing .cac) produces nothing,
    no matter how many scans retry it -- unlike a repeat-arrival-spam
    risk, there's no separate signal to guard against here."""
    shutil.copy(REAL_TBD, input_dir / "osu685-2026-172-0-324.tbd")
    monkeypatch.setattr(watch, "check_variable", lambda *a, **k: None)
    sent: list[tuple[str, str]] = []
    state: dict = {}

    def scan():
        watch.scan_once(
            input_dir,
            FIXTURES,
            "sci_generic_a",
            {},
            state,
            "osu685",
            watch.dinkum_name_re("tbd"),
            2,
            send=lambda subject, body: sent.append((subject, body)),
            announce=True,
        )

    scan()
    scan()
    scan()
    assert sent == []

    # The .cac shows up; the retried file now reads fine -- exactly one
    # combined message follows.
    monkeypatch.setattr(
        watch,
        "check_variable",
        lambda *a, **k: watch.CheckResult(
            count=2, duration_minutes=None, rate_per_minute=None, ok=True
        ),
    )
    scan()
    assert [subject for subject, _ in sent] == ["osu685: osu685-2026-172-0-324.tbd"]
    assert state["osu685"].processed == {"osu685-2026-172-0-324.tbd"}


def test_announce_survives_slack_failure(input_dir, monkeypatch):
    shutil.copy(REAL_TBD, input_dir / "osu685-2026-172-0-324.tbd")
    monkeypatch.setattr(
        watch,
        "check_variable",
        lambda *a, **k: watch.CheckResult(
            count=1, duration_minutes=None, rate_per_minute=None, ok=True
        ),
    )

    def broken_send(subject, body):
        raise RuntimeError("webhook down")

    state: dict = {}
    watch.scan_once(
        input_dir,
        FIXTURES,
        "sci_generic_a",
        {},
        state,
        "osu685",
        watch.dinkum_name_re("tbd"),
        2,
        send=broken_send,
        announce=True,
    )  # must not raise

    assert state["osu685"].processed == {"osu685-2026-172-0-324.tbd"}


def test_announce_min_bytes_skips_small_files(input_dir, monkeypatch):
    """A small file must still be fully checked and ledgered -- only
    the routine Slack posts are suppressed."""
    small = input_dir / "osu685-2026-172-0-324.tbd"
    shutil.copy(REAL_TBD, small)
    size = small.stat().st_size
    monkeypatch.setattr(
        watch,
        "check_variable",
        lambda *a, **k: watch.CheckResult(
            count=0, duration_minutes=None, rate_per_minute=None, ok=True
        ),
    )
    sent: list[tuple[str, str]] = []
    state: dict = {}

    watch.scan_once(
        input_dir,
        FIXTURES,
        "sci_generic_a",
        {},
        state,
        "osu685",
        watch.dinkum_name_re("tbd"),
        2,
        send=lambda subject, body: sent.append((subject, body)),
        announce=True,
        announce_min_bytes=size + 1,  # just above this file's real size
    )

    assert sent == []  # too small to announce
    assert state["osu685"].processed == {"osu685-2026-172-0-324.tbd"}  # still checked


def test_announce_min_bytes_allows_large_files(input_dir, monkeypatch):
    large = input_dir / "osu685-2026-172-0-324.tbd"
    shutil.copy(REAL_TBD, large)
    size = large.stat().st_size
    monkeypatch.setattr(
        watch,
        "check_variable",
        lambda *a, **k: watch.CheckResult(
            count=1, duration_minutes=None, rate_per_minute=None, ok=True
        ),
    )
    sent: list[tuple[str, str]] = []

    watch.scan_once(
        input_dir,
        FIXTURES,
        "sci_generic_a",
        {},
        {},
        "osu685",
        watch.dinkum_name_re("tbd"),
        2,
        send=lambda subject, body: sent.append((subject, body)),
        announce=True,
        announce_min_bytes=size,  # exactly this file's size -- inclusive
    )

    assert len(sent) == 1


# ── scan_once: --min-bytes exempts small files entirely ──────────


def test_min_bytes_exempts_small_file_from_check_and_streak(input_dir, monkeypatch):
    small = input_dir / "osu685-2026-172-0-324.tbd"
    shutil.copy(REAL_TBD, small)
    size = small.stat().st_size
    monkeypatch.setattr(
        watch, "check_variable", lambda *a, **k: pytest.fail("should not run")
    )
    sent: list[tuple[str, str]] = []
    state: dict = {}

    watch.scan_once(
        input_dir,
        FIXTURES,
        "sci_generic_a",
        {},
        state,
        "osu685",
        watch.dinkum_name_re("tbd"),
        1,  # minimum threshold -- would alert immediately if this counted
        send=lambda subject, body: sent.append((subject, body)),
        announce=True,
        min_bytes=size + 1,
    )

    assert sent == []
    assert state["osu685"].processed == {
        "osu685-2026-172-0-324.tbd"
    }  # skipped, not retried
    assert state["osu685"].consecutive_empty == 0  # exempted, not a failure
    assert not state["osu685"].in_alert


def test_min_bytes_allows_large_files_and_alerts_immediately(input_dir, monkeypatch):
    large = input_dir / "osu685-2026-172-0-324.tbd"
    shutil.copy(REAL_TBD, large)
    size = large.stat().st_size
    monkeypatch.setattr(
        watch,
        "check_variable",
        lambda *a, **k: watch.CheckResult(
            count=0, duration_minutes=None, rate_per_minute=None, ok=False
        ),
    )
    sent: list[tuple[str, str]] = []
    state: dict = {}

    watch.scan_once(
        input_dir,
        FIXTURES,
        "sci_generic_a",
        {},
        state,
        "osu685",
        watch.dinkum_name_re("tbd"),
        1,  # minimum threshold, as recommended once size-filtering is in place
        send=lambda subject, body: sent.append((subject, body)),
        min_bytes=size,  # exactly this file's size -- inclusive, so it's checked
    )

    assert state["osu685"].in_alert
    assert len(sent) == 1
    assert "failing" in sent[0][0]
