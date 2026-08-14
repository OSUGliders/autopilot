"""Acoustic-data delivery alerting for Slocum gliders.

Deliberately separate from the piloting/prediction code
(:mod:`autopilot.follower`, :mod:`autopilot.safety`) — this subpackage
only ever *observes* incoming science files and sends Slack alerts; it
never writes into ``predictions/``, never calls into the follower, and
runs as its own systemd unit(s) so a bug here cannot affect waypoint
generation.  See the README for the full design.
"""
