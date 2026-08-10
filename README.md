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
- `osu999_config.yaml` — runtime config (waypoint lead times, fence,
  safe point, thresholds, paths).
- `tests/` — geofence unit tests + closed-loop regressions against a
  recorded float-6000 track fixture (`uv run pytest`).
- `examples/simple_demo/` — minimal first example (fixed waypoint
  list, replayed mock dialog).
- `deploy/autopilot@.service` — templated systemd unit, one instance
  per glider (see Deploy on the VM, below).
- `src/autopilot/launch.py` — `autopilot-follow`, a thin wrapper
  around `sfmc-follow` that fills in `--hostname`, `--notify-email`,
  and `--notify-from` from the glider's own config (see Deploy on the
  VM, below), keeping them out of the shared systemd unit.
- `src/autopilot/web.py` — `autopilot-web`, the LAN-only dashboard:
  per-glider status, prediction freshness, latest plot, log tail;
  optional passkey-gated on/off + target controls (see Web
  dashboard, below).
- `src/autopilot/ingest.py` — `autopilot-ingest-predictions`, converts
  the real-time localization feed into prediction files (see
  Real-time predictions, below).

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

## Real-time predictions

`autopilot-ingest-predictions` converts the real-time localization
feed — one wide `<deployment>_float_tracks_latest.csv` per deployment
(e.g. `A1_...`, then `B1_...` for the next), with `estimate`
(observed) and `prediction` (forecast) rows across several parallel
tracking methods (`ekf`, `pf`, `pf_lag2h`, `batch`, `ops`) — into the
follower's plain `drifter_*.csv` format:

```sh
uv run autopilot-ingest-predictions --localization-dir localization --predictions-dir predictions
```

Every `(deployment, float, tracker)` combination gets its own
directory — `predictions/<deployment>_<float>_<tracker>/` — so a pilot
picks both the platform and the tracking method via the web
dashboard's target selector, with no changes there. Only the latest
`segment` per `(float, tracker)` is used (a segment restart means a
real position gap, never interpolated across), and the same physical
float redeployed under a new deployment id gets its own directory
rather than colliding with the old one.

**The output filename is anchored on the data, not on when this ran**:
it's the latest `estimate` row's own timestamp, not ingest wall-clock
time. Stamping with wall-clock time would silently defeat the
follower's staleness check — a feed that stopped updating would still
produce a "fresh"-looking file every cycle, and FALLBACK would never
fire. A `(float, tracker)` with no estimate rows (prediction-only) or
a deployment file that fails to parse is simply skipped that cycle —
previously written predictions are never touched, so the follower
degrades to flying on the last good file exactly as it already does
for any other stale-prediction case.

It also renders one **comparison plot per asset** — every tracker
overlaid, solid for observed and dashed for forecast — and copies it
as `tracks.png` into all of that asset's tracker directories, so the
web dashboard shows it under "Compare tracking methods" on a glider's
page for whichever target is currently selected, no matter which
tracker. A plotting failure never blocks the CSV writes.

In production this runs as the second `ExecStart=` line in
`deploy/autopilot-rsync-predictions.service`, right after the rsync
pull (see Deploy on the VM, below).

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
file at each surfacing and applies changed `predictions_dir`,
`waypoint_lead_h`, `target_radius_km`, `num_legs_to_run`,
`max_prediction_age_h`, `max_waypoint_jump_km`, `fallback_reminder_h`,
`plot_bounds`, and `pattern` without a restart, logging each change —
so switching which drifter a glider tracks, or retargeting it to a
different prediction directory, is a config edit, not a restart.
`predictions_dir` is still required at startup, and a reload that
drops or blanks it keeps flying on the previous target rather than
breaking. Everything else (fence, safe point, `sequence_number`,
plot/archive paths) still requires a restart, and a broken edit keeps
the previous settings rather than stopping the follower.

Email alerts use sfmc-api's notification system: give it at least one
recipient (`--notify-email ADDR`, repeatable, or `notify_email:` in
the config — see `autopilot-follow`, below) to enable both the
framework's sustained SFMC-disconnect alerts (`--notify-after`,
`--notify-repeat`) and this follower's FALLBACK entry/reminder/recovery
emails (`fallback_reminder_h` in the config), plus a one-off
"autopilot started" email on every service start/restart — a live
test of the whole delivery path without waiting for a real FALLBACK.
SMTP defaults to localhost:25; see `sfmc-follow --help` for the
`--smtp-*` options (CLI-only, no config equivalent yet).
**Also set a `notify_from`/`--notify-from`** address your relay will
actually deliver — the default (`sfmc-follow@<hostname>`) is often an
unregistered mailbox that gets silently dropped downstream. Verify
delivery end-to-end with `uv run python examples/send_test_email.py
you@example.edu --from your-notify-from@example.edu`.

## Deploy on the VM

Code and operational state are split into two directories, so a
`git pull` can never touch predictions, plots, or logs:

- `/opt/autopilot` — the git checkout and its `.venv` (`uv sync`
  after cloning). Read-only in day-to-day operation; only touched to
  update the code.
- `/srv/autopilot` — per-glider config YAML, and the
  `predictions/`, `plots/`, `goto_archive/`, `logs/`, `boundaries/`
  directories the follower reads and writes at runtime. This is the
  systemd units' `WorkingDirectory`, since the follower resolves the
  config's relative paths (`predictions_dir`, `plot_dir`, etc.)
  against the current directory in live mode, not the config file's
  location.
- `/etc/autopilot/credentials.json` — SFMC credentials, `660`
  `autopilot:glider_pilots`.

A dedicated `autopilot` system user owns and runs the services. Pilots
who need to edit configs belong to the `glider_pilots` group, which
owns `/srv/autopilot`; the directories are setgid with a default ACL
(`setfacl -R -d -m g:glider_pilots:rwX /srv/autopilot`) so new files
inherit group-write instead of landing root- or single-user-owned.

### autopilot-follow: config-driven CLI flags

`deploy/autopilot@.service` runs `autopilot-follow`, not
`sfmc-follow` directly — a thin wrapper that fills in a few
`sfmc-follow` flags from the glider's own `--config` YAML, so they
don't need to live in the (root-owned, shared-across-gliders) unit
file:

| Config key (launcher-only) | CLI flag         |
|-----------------------------|------------------|
| `sfmc_hostname`             | `--hostname`     |
| `notify_email` (str or list)| `--notify-email` |
| `notify_from`               | `--notify-from`  |

An explicit flag on the command line always wins over the config
value — useful for a one-off override (e.g. testing a normally
gliderfmc0 config against gliderfmc1, or a manual `--notify-email` for
a single run). These are startup-time values, like `--hostname`
itself: changing them still needs a restart, unlike the `HOT_KEYS`
config fields above.

`sfmc_hostname` matters once more than one server is in play:
`credentials.json` uses **per-host** entries, so one file holds
credentials for as many servers as needed —

```json
{
  "gliderfmc1.ceoas.oregonstate.edu": { "apiCredentials": { "clientId": "...", "secret": "..." } },
  "gliderfmc0.ceoas.oregonstate.edu": { "apiCredentials": { "clientId": "...", "secret": "..." } }
}
```

— and with only one host in the file it's used automatically, but
once a second is added, every follower must say which one it means.

### systemd

A single templated unit, `deploy/autopilot@.service`, covers every
glider — `%i` is substituted with the instance name at every point it
appears (`--glider`, the config path, the log path):

```ini
[Unit]
Description=Glider autopilot follower for %i
After=network-online.target
Wants=network-online.target

[Service]
User=autopilot
Group=glider_pilots
UMask=0002
WorkingDirectory=/srv/autopilot
ExecStart=/opt/autopilot/.venv/bin/sfmc-follow --glider %i \
    --follower /opt/autopilot/src/autopilot/follower.py \
    --config /srv/autopilot/%i_config.yaml \
    --credentials /etc/autopilot/credentials.json \
    --logfile /srv/autopilot/logs/%i.log \
    --notify-email your.email@oregonstate.edu \
    --notify-from glider-autopilot@oregonstate.edu
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

`--notify-from` matters — see above. Install it once:

```sh
sudo cp /opt/autopilot/deploy/autopilot@.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Tracking a glider then requires only a config file at the convention
the template expects — `/srv/autopilot/<glider>_config.yaml` — and:

```sh
sudo systemctl enable --now autopilot@osusim    # turn autopilot ON for osusim
sudo systemctl disable --now autopilot@osusim   # turn it OFF
```

Each instance (`autopilot@osusim`, `autopilot@osu684`, ...) is an
independent unit — starting, stopping, or restarting one never touches
another. If migrating from a hand-written `autopilot-<glider>.service`
predating this template, `disable --now` the old unit first so two
processes never subscribe to the same glider's dialog at once.

After any change — a config edit to a key that isn't in `HOT_KEYS`
(fence, safe point, `sequence_number`, paths), or a code update
(below) — restart the instance:

```sh
sudo systemctl restart autopilot@osusim
systemctl status autopilot@osusim     # confirm it came back up
journalctl -u autopilot@osusim -f     # live tail: framework + follower
```

`--logfile` (above) captures the same lines to a rotating file, but
only while the glider is at the surface — long journal silence while
it's underwater is normal; `systemctl is-active` plus a journal tail
is the real liveness check.

A config edit to a `HOT_KEYS` field (`waypoint_lead_h`,
`max_waypoint_jump_km`, etc., when `config_file:` is set in the YAML)
does **not** need a restart — it's picked up at the next surfacing.

### Web dashboard

`autopilot-web` serves a LAN-only status page over the same files and
units: per glider, the service state, last piloting outcome
(TRACKING/FALLBACK from the log), which prediction file is in force
and its age, the full config, the latest surfacing plot, and a log
tail. It discovers gliders from `<name>_config.yaml` in its
`--base-dir` — no configuration of its own.

There are no user accounts. With no passkey configured the dashboard
is strictly read-only. To enable the two controls — autopilot on/off
(`systemctl enable/disable --now autopilot@<glider>`) and tracking
target (rewrites `predictions_dir` in the glider's YAML, offering
`predictions/` and its subdirectories) — set a shared passkey; it
must be typed into every change, and every attempt (allowed or
denied) is appended to `/srv/autopilot/audit.log` with timestamp and
client address.

Setup:

```sh
# 1. The service (read-only until step 2 and 3):
sudo cp /opt/autopilot/deploy/autopilot-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now autopilot-web
# now at http://gliderpilot.ceoas.oregonstate.edu:8080 (LAN only —
# keep the port firewalled from off-campus)

# 2. The shared passkey (enables the controls):
echo 'AUTOPILOT_WEB_PASSKEY=choose-a-long-phrase' | sudo tee /etc/autopilot/web.env
sudo chmod 600 /etc/autopilot/web.env
sudo systemctl restart autopilot-web

# 3. Let the dashboard user toggle glider units (and nothing else).
#    Modern sudo forbids wildcards in command arguments, so the rule
#    allows exactly one root-owned helper, which validates the glider
#    name and action itself:
sudo install -m 0755 -o root /opt/autopilot/deploy/autopilot-toggle /usr/local/sbin/
visudo -cf /opt/autopilot/deploy/sudoers-autopilot-web   # syntax check first!
sudo install -m 0440 /opt/autopilot/deploy/sudoers-autopilot-web \
    /etc/sudoers.d/autopilot-web
```

Target changes apply at the next surfacing when the glider's config
has `config_file:` set (live reload); otherwise the page reminds you
a restart is needed. Turning a glider on/off uses
`enable/disable --now`, so the choice also survives VM reboots.

### Updating the code

`/opt/autopilot` is an ordinary git checkout:

```sh
cd /opt/autopilot
git pull
uv sync                                        # this repo's own deps
uv lock --upgrade-package sfmc-api && uv sync  # only if sfmc-api itself
                                                # was updated upstream —
                                                # git pull alone won't
                                                # fetch its new commits
for u in $(systemctl list-units 'autopilot@*' --plain --no-legend | cut -d' ' -f1); do
    sudo systemctl restart "$u"                # every enabled glider instance
done
sudo systemctl restart autopilot-web           # if the dashboard is installed
```

`/srv/autopilot` is untouched by any of this — configs, predictions,
plots, and logs all survive a code update.
