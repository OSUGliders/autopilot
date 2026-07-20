# autopilot

Shore-side autopilot for Slocum gliders following drifting floats:
detect each surfacing, predict where the drifter will be when the
glider arrives, validate the waypoint against a geofence, and send it
— built on the `sfmc-follow` framework from
[sfmc-api](https://github.com/mousebrains/SFMC-API-Python).

## Layout

- `src/autopilot/follower.py` — piloting logic (sfmc-follow plugin):
  newest prediction file → drifter positions at the configured lead
  times (default +3 h and +6 h) → `goto_l10.ma` visited in order, plus
  per-surfacing map and timestamped .ma archive.
- `src/autopilot/safety.py` — geofence + waypoint validation: inside
  `boundaries/RIOT_boundary.geojson` minus margin, legs to and between
  waypoints stay inside, prediction fresh, jump plausible. A bad later
  waypoint truncates the goto (pilot warned); a bad first waypoint
  triggers FALLBACK (red-flagged in logs/plots, pilot emailed) —
  commanding the configured `safe_point`, or, when none is set,
  sending no goto so the glider keeps looping its last commanded
  waypoint.
- `src/autopilot/sim/` — simulation machinery: mock/real drifter
  truth (incl. MIT RIOT MAT tracks), 6-hourly prediction files
  (24 h hindcast + 12 h forecast, 2-h steps), closed-loop stepper
  (0.25 m/s, fixed-heading dives with overshoot, random surfacing
  offset ≤ 250 m per hour underwater).
- `osu999_config.yaml` — runtime config (speed, fence,
  safe point, thresholds, paths).
- `tests/` — geofence unit tests + closed-loop regressions against a
  recorded float-6000 track fixture (`uv run pytest`).
- `examples/simple_demo/` — minimal first example (fixed waypoint
  list, replayed mock dialog).

## Simulate

```sh
uv run autopilot-sim --follow-hours 72   # closed loop vs simulated float 6000
# Options: --synthetic, --float-id, --start-hour, --follow-hours
# Writes everything under sim_output/ (predictions, plots, .ma archive, dialog log)

uv run autopilot-mock-data               # synthetic open-loop demo data
uv run sfmc-follow --glider osu999 --follower src/autopilot/follower.py \
    --config osu999_config.yaml --replay predicted_dialog.log --dry-run
```

## Replay real deployment logs

Replays a past deployment's Iridium dialog through the follower as if
it were happening live. The setup script stitches the per-call
network logs (e.g. `examples_logs/sl684/*.log`) in time order, parses
the glider's real surfaced positions, and — since these deployments
had no drifter — smooths that track into a stand-in "drifter",
writing 6-hourly prediction files, a rectangular test fence, and a
config. The replay then feeds the raw dialog to the follower, which
makes its usual waypoint/safety decisions and writes a plot and .ma
file per surfacing.

```sh
uv run python examples/replay_real_logs.py examples_logs/sl684 replay_sl684
uv run sfmc-follow --glider osu684 --follower src/autopilot/follower.py \
    --config replay_sl684/replay_config.yaml --replay replay_sl684/dialog.log \
    --replay-interval 0 --dry-run
# Plots in replay_sl684/plots/, goto files in replay_sl684/goto_archive/
```

`--replay-interval 0` disables the default 10 s pause between
surfacings (keep it to watch the replay unfold).

## Track a replayed drifter with a simulated glider

`autopilot-live-drifter` replays a historical float track (MAT file
with TIME/LAT/LON) against a simulated glider running in real time on
SFMC. It queries SFMC for the glider's last valid GPS fix, shifts the
track so it starts at that time, and writes the whole deployment's
prediction files in one shot — the follower only ever reads the newest
file dated at or before "now", so future-dated files lie dormant until
the wall clock reaches them.

```sh
# Rank the floats starting nearest the glider, pick one:
uv run autopilot-live-drifter Floats/MIT_RIOT_Traj_...mat --glider osusim --list 8

# Generate all prediction files into the follower's predictions_dir:
uv run autopilot-live-drifter Floats/MIT_RIOT_Traj_...mat --glider osusim \
    --float-id 6560 --outdir predictions
```

Then run the follower as usual (below). Don't re-run the generator
mid-test: it re-anchors the track to the latest fix, teleporting the
drifter.

## Run live

```sh
uv run sfmc-follow --glider <name> --follower src/autopilot/follower.py \
    --config osu999_config.yaml
```

Add `--dry-run` first to watch without uploading. The glider's mission
must run `goto_list` with `args_from_file` matching `sequence_number`
in the config (10 → `goto_l10.ma`). Capture a real dialog log with
`uv run sfmc-monitor-glider --glider <name> --logfile dialog.log`.

The effective config is logged at startup. Set `config_file:` in the
YAML (its own path) to enable live reload: the follower re-reads the
file at each surfacing and applies changed thresholds, speed,
`num_legs_to_run`, `plot_bounds`, and `pattern` without a restart,
logging each change. Structural settings (fence, safe point,
`sequence_number`, paths) still require a restart, and a broken edit
keeps the previous settings rather than stopping the follower.

Email alerts use sfmc-api's notification system: add
`--notify-email ADDR` (repeatable, all recipients get every alert) to
the `sfmc-follow` command. That enables both the framework's sustained
SFMC-disconnect alerts (`--notify-after`, `--notify-repeat`) and this
follower's FALLBACK entry/reminder/recovery emails
(`fallback_reminder_h` in the config). SMTP defaults to
localhost:25; see `sfmc-follow --help` for the `--smtp-*` options.
