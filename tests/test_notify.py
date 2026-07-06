"""Unit tests for the FALLBACK notification state machine."""

from datetime import UTC, datetime, timedelta

from autopilot.notify import Notifier

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
CONFIG = {"from": "autopilot@test", "to": ["pilot@test"], "reminder_h": 6}


def make_notifier(sent):
    n = Notifier(CONFIG)
    n._send = lambda subject, body: sent.append((subject, body)) or True
    return n


def test_transitions():
    sent = []
    n = make_notifier(sent)

    n.update(T0, "osu684", ok=True)
    assert not sent, "no email while healthy"

    n.update(T0 + timedelta(hours=3), "osu684", False, "STALE", "prediction 13h old")
    assert len(sent) == 1 and "FALLBACK (STALE)" in sent[0][0]

    # Still in fallback, before the reminder interval: silent.
    n.update(T0 + timedelta(hours=6), "osu684", False, "STALE", "prediction 16h old")
    assert len(sent) == 1

    # Reminder due after reminder_h.
    n.update(T0 + timedelta(hours=10), "osu684", False, "STALE", "prediction 20h old")
    assert len(sent) == 2 and "still in FALLBACK" in sent[1][0]

    n.update(T0 + timedelta(hours=12), "osu684", ok=True)
    assert len(sent) == 3 and "recovered" in sent[2][0]

    n.update(T0 + timedelta(hours=15), "osu684", ok=True)
    assert len(sent) == 3, "no email while healthy"


def test_failed_send_retries_next_surfacing():
    sent = []
    n = Notifier(CONFIG)
    n._send = lambda subject, body: False  # transport down
    n.update(T0, "osu684", False, "NO_PREDICTION", "no usable prediction file")

    n._send = lambda subject, body: sent.append((subject, body)) or True
    n.update(T0 + timedelta(hours=1), "osu684", False, "NO_PREDICTION", "")
    assert len(sent) == 1, "unsent entry email retried as reminder"


def test_position_in_body():
    sent = []
    n = make_notifier(sent)
    n.update(T0, "osu684", False, "FENCE_WAYPOINT", "outside", (33.13, -117.70))
    assert "33.1300, -117.7000" in sent[0][1]
