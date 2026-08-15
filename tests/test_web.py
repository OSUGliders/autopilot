"""Tests for the web dashboard: read-only views, passkey gate, audit."""

import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from autopilot import web

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def base(tmp_path, monkeypatch):
    """A /srv/autopilot-like directory with one glider, osusim."""
    (tmp_path / "osusim_config.yaml").write_text(
        "# comment to preserve\n"
        "predictions_dir: predictions\n"
        "config_file: osusim_config.yaml\n"
        "max_prediction_age_h: 9\n"
    )
    pred = tmp_path / "predictions"
    pred.mkdir()
    (pred / "drifter_20260726T0600.csv").write_text("time,latitude,longitude\n")
    (pred / "drifter_20260726T1800.csv").write_text("time,latitude,longitude\n")
    alt = pred / "6560"
    alt.mkdir()
    (alt / "drifter_20260726T0000.csv").write_text("time,latitude,longitude\n")
    # Off-VM there is no systemd; keep the tests hermetic either way.
    monkeypatch.setattr(web, "_unit_state", lambda glider: ("active", "enabled"))
    return tmp_path


@pytest.fixture
def client(base):
    app = web.create_app(base)
    app.testing = True
    return app.test_client()


def test_index_lists_glider(client):
    page = client.get("/").get_data(as_text=True)
    assert "osusim" in page and "active" in page


def test_glider_page_shows_prediction_and_config(base, client, monkeypatch):
    real = web.prediction_status
    monkeypatch.setattr(web, "prediction_status", lambda b, c, now: real(b, c, NOW))
    page = client.get("/glider/osusim").get_data(as_text=True)
    assert "drifter_20260726T0600.csv" in page  # in force (1800 is future)
    assert "predictions_dir" in page
    assert "Read-only" not in page  # that's the index footer


def test_unknown_glider_404(client):
    assert client.get("/glider/nope").status_code == 404
    assert client.get("/glider/../etc").status_code == 404


def test_tracks_plot_absent_by_default(base, client):
    assert web.tracks_plot(base, web.load_config(base, "osusim")) is None
    assert client.get("/glider/osusim/tracks.png").status_code == 404
    assert "Compare tracking methods" not in client.get("/glider/osusim").get_data(
        as_text=True
    )


def test_tracks_plot_shown_when_ingest_produced_one(base, client):
    (base / "predictions" / "tracks.png").write_bytes(b"not a real png, just bytes")

    assert web.tracks_plot(base, web.load_config(base, "osusim")) is not None
    r = client.get("/glider/osusim/tracks.png")
    assert r.status_code == 200 and r.data == b"not a real png, just bytes"
    assert "Compare tracking methods" in client.get("/glider/osusim").get_data(
        as_text=True
    )


def test_prediction_status_ages(base):
    config = web.load_config(base, "osusim")
    status = web.prediction_status(base, config, NOW)
    assert status["current"] == "drifter_20260726T0600.csv"
    assert status["age_h"] == pytest.approx(6.0)
    assert not status["stale"]
    assert status["future"] == 1

    late = web.prediction_status(base, config, NOW + timedelta(hours=6))
    assert late["current"] == "drifter_20260726T1800.csv"


def test_target_options_and_rewrite_preserves_comments(base):
    config = web.load_config(base, "osusim")
    options = web.target_options(base, config)
    assert options == ["predictions", "predictions/6560"]

    cfg_path = base / "osusim_config.yaml"
    web.set_predictions_dir(cfg_path, "predictions/6560")
    text = cfg_path.read_text()
    assert "# comment to preserve" in text
    assert "predictions_dir: predictions/6560" in text
    assert "config_file: osusim_config.yaml" in text


# ── Waypoint offset ───────────────────────────────────────────────


def test_glider_page_shows_waypoint_offset_default(client):
    page = client.get("/glider/osusim").get_data(as_text=True)
    assert "+0 m north, +0 m east" in page


def test_waypoint_offset_rewrite_preserves_comments(base):
    cfg_path = base / "osusim_config.yaml"
    web.set_waypoint_offset(cfg_path, 250.0, -100.0)
    text = cfg_path.read_text()
    assert "# comment to preserve" in text
    assert "waypoint_offset_north_m: 250.0" in text
    assert "waypoint_offset_east_m: -100.0" in text


def test_offset_change_denied_without_passkey_env(base, client, monkeypatch):
    monkeypatch.delenv("AUTOPILOT_WEB_PASSKEY", raising=False)
    r = client.post(
        "/glider/osusim/offset", data={"north_m": "10", "east_m": "10", "passkey": "x"}
    )
    assert r.status_code == 403


def test_offset_change_requires_valid_numbers(base, client, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WEB_PASSKEY", "sekrit")
    r = client.post(
        "/glider/osusim/offset",
        data={"north_m": "not-a-number", "east_m": "0", "passkey": "sekrit"},
    )
    assert r.status_code == 400


def test_offset_change_writes_config_and_audits(base, client, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WEB_PASSKEY", "sekrit")
    r = client.post(
        "/glider/osusim/offset",
        data={"north_m": "250", "east_m": "-100", "passkey": "sekrit"},
    )
    assert r.status_code == 302
    text = (base / "osusim_config.yaml").read_text()
    assert "waypoint_offset_north_m: 250.0" in text
    assert "waypoint_offset_east_m: -100.0" in text
    assert "OFFSET osusim -> north=250 east=-100 ok" in (base / "audit.log").read_text()


# ── Passkey gate ────────────────────────────────────────────────


def test_posts_disabled_without_passkey_env(client, monkeypatch):
    monkeypatch.delenv("AUTOPILOT_WEB_PASSKEY", raising=False)
    r = client.post("/glider/osusim/service", data={"action": "off", "passkey": "x"})
    assert r.status_code == 403


def test_wrong_passkey_denied_and_audited(base, client, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WEB_PASSKEY", "sekrit")
    monkeypatch.setattr(
        web, "time", __import__("types").SimpleNamespace(sleep=lambda s: None)
    )
    calls = []
    monkeypatch.setattr(web, "_sudo_toggle", lambda *a: calls.append(a))

    r = client.post(
        "/glider/osusim/service", data={"action": "off", "passkey": "wrong"}
    )

    assert r.status_code == 302 and "wrong+passkey" in r.headers["Location"]
    assert not calls, "the toggle helper must not run on a bad passkey"
    assert "DENIED OFF osusim" in (base / "audit.log").read_text()


def test_correct_passkey_toggles_service(base, client, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WEB_PASSKEY", "sekrit")
    calls = []

    def fake_toggle(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(web, "_sudo_toggle", fake_toggle)

    r = client.post(
        "/glider/osusim/service", data={"action": "on", "passkey": "sekrit"}
    )

    assert r.status_code == 302
    assert calls == [("on", "osusim")]
    assert "ON osusim ok" in (base / "audit.log").read_text()


def test_target_change_requires_valid_option(base, client, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WEB_PASSKEY", "sekrit")
    r = client.post(
        "/glider/osusim/target",
        data={"predictions_dir": "/etc/shadow", "passkey": "sekrit"},
    )
    assert r.status_code == 400


def test_target_change_writes_config_and_audits(base, client, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WEB_PASSKEY", "sekrit")
    r = client.post(
        "/glider/osusim/target",
        data={"predictions_dir": "predictions/6560", "passkey": "sekrit"},
    )
    assert r.status_code == 302
    assert (
        "predictions_dir: predictions/6560" in (base / "osusim_config.yaml").read_text()
    )
    assert "TARGET osusim -> predictions/6560 ok" in (base / "audit.log").read_text()
