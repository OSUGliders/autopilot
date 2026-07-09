"""Tests for the time-shifted live-drifter prediction generator."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from scipy.io import savemat

from autopilot.sim.live_drifter import load_track, rank_floats, write_shifted

ANCHOR = datetime(2026, 7, 9, 18, 0, tzinfo=UTC)


@pytest.fixture
def mat(tmp_path):
    """Two floats, hourly for 60 h: #1 near the glider, #2 a degree north."""
    hours = np.arange(0.0, 61.0)
    days = 737000.0 + hours / 24.0
    lat1 = 33.5 + 0.005 * hours
    lon1 = -119.7 - 0.005 * hours
    path = tmp_path / "floats.mat"
    savemat(
        path,
        {
            "TIME": np.column_stack([days, days]),
            "LAT": np.column_stack([lat1, lat1 + 1.0]),
            "LON": np.column_stack([lon1, lon1]),
        },
    )
    return path


def test_rank_floats_orders_by_distance(mat):
    ranked = rank_floats(mat, glider_lat=33.5, glider_lon=-119.7, n=2)
    assert [fid for _, fid in ranked] == [1, 2]
    assert ranked[0][0] < 1.0 < ranked[1][0]


def test_write_shifted_layout_and_alignment(mat, tmp_path):
    t_h, lat, lon = load_track(mat, float_id=1)
    paths = write_shifted(t_h, lat, lon, ANCHOR, tmp_path / "predictions")

    # 6-hourly creation times spanning the 60 h track: 0, 6, ..., 60.
    assert len(paths) == 11
    assert paths[0].name == "drifter_20260709T1800.csv"
    assert paths[-1].name == "drifter_20260712T0600.csv"

    # First file: no rows before the anchor; row 0 is the track start.
    rows = paths[0].read_text().splitlines()
    assert rows[0] == "time,latitude,longitude"
    t0, la0, lo0 = rows[1].split(",")
    assert datetime.fromisoformat(t0) == ANCHOR
    assert float(la0) == pytest.approx(33.5)
    assert float(lo0) == pytest.approx(-119.7)

    # Mid-track file: full 24 h hindcast + 12 h forecast in 2 h steps.
    mid = paths[5].read_text().splitlines()[1:]
    assert len(mid) == (24 + 12) // 2 + 1
    last_t = datetime.fromisoformat(mid[-1].split(",")[0])
    assert last_t == ANCHOR + timedelta(hours=30 + 12)
