"""Shore-side autopilot for Slocum gliders following drifting floats.

Core pieces:

- :mod:`autopilot.follower` — the sfmc-follow plugin that turns each
  surfacing into a new waypoint from drifter track predictions.
- :mod:`autopilot.safety` — geofence and waypoint validation; every
  candidate waypoint passes through here before being sent.
- :mod:`autopilot.sim` — simulation machinery (mock predictions,
  closed-loop stepping) used by the test suite and for building
  performance envelopes; not needed in live operation.
"""

__version__ = "0.1.0"
