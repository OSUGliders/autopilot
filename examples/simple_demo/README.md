# simple_demo

A simple `sfmc-follow` follower, kept as a learning
example. It predates the autopilot (`src/autopilot/`) and shows
the bare plugin pattern without predictions, geofencing, or plotting:
surface → pick the next waypoint from a fixed list in the config →
emit a `goto_l{N}.ma` file.

- `demo_follower.py` — the follower: steps through `track` in the
  config, advancing when the glider surfaces within `arrival_radius_m`
  of the current target.
- `demo_config.yaml` — three waypoints.
- `sample_dialog.log` — prescribed Iridium dialog with three
  surfacings, for replay (a real one comes from
  `sfmc-monitor-glider`).

Run it (from the repo root):

```sh
uv run sfmc-follow --glider osu999 \
    --follower examples/simple_demo/demo_follower.py \
    --config examples/simple_demo/demo_config.yaml \
    --replay examples/simple_demo/sample_dialog.log --dry-run
```

`--dry-run` prints the .ma files instead of uploading. Not used by the
autopilot package or its tests — safe to ignore or delete.