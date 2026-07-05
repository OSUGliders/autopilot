"""Unit tests for PredictedTrackFollower helpers.

The duplicate-timestamp case was found by replaying real deployment
logs: repeated rows in a prediction file made end-extrapolation divide
by zero.
"""

from datetime import UTC, datetime, timedelta

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
