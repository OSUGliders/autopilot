"""Watch a local mirror of a glider's from-glider files for newly
arrived data, and alert over Slack if a configured variable falls
below an expected count or rate for ``--threshold`` files in a row
(``--threshold 1``: alert on the very first bad file).

General across file type (``.tbd`` science files, ``.sbd`` flight
files — same Slocum binary format either way, dbdreader doesn't care)
and across what "healthy" means for a given variable:

- Presence only (default): the variable has any value at all in the
  file. Good for "is science data arriving in this slot at all."
- A specific value, counted (``--equals``): e.g. counting
  ``sci_generic_k == 10`` "interrogation sent" events, rather than
  every sample of that register.
- A rate (``--duration-variable`` + ``--min-rate-per-minute``): the
  count alone doesn't say whether it's *enough* without knowing how
  much of the file it covers — a sparse variable's own timestamps
  understate that (a few interrogation events don't tell you how long
  the file actually spans), so a second, densely-sampled variable
  (``m_present_time``/``sci_m_present_time``) supplies the file's
  actual duration.

``--min-bytes`` fully exempts files smaller than this from the check
entirely (not read, not counted toward the alert streak, not
announced) -- for real, identifiable no-content files (e.g. small
administrative segments logged while a glider idles at the surface),
never for tolerating routine failures. Real per-glider file sizes
tend to split cleanly enough to find this boundary (see the deploy
unit for how it was picked for osu1267). Once genuinely-empty small
files are filtered out this way, there's no such thing as a
legitimate "quiet period" left to tolerate -- any real-sized file that
still fails is a real problem, which is why ``--threshold 1`` (alert
immediately) is the right choice once this filter is in place.

``--announce`` additionally posts two routine (non-alert) Slack
messages per checked file, in sequence: arrival ("file received, N
bytes") as soon as it's seen, then content ("sci_generic_k == 10: N
time(s) over M min (expected >= E)") once read. Off by default --
useful while watching a check settle in, easy to skip on an
already-trusted one.

This never touches the SFMC API, STOMP, or the network for file
retrieval — it only watches a local directory and reads files with
``dbdreader``, using a manually-maintained repository of ``.cac``
sensor cache files (dbdreader cannot decode a file without the
matching one; there is no SFMC API to fetch these, so the repository
is built up by hand as new sensor configurations are seen).  The
directory is expected to be kept in sync by ``sfmc-pull-new-downloads``
(shipped with sfmc-api) running as its own process — this module is
deliberately just a local-directory watcher, not a second SFMC client.

Deliberately a separate process from ``autopilot-follow`` and
``autopilot-ingest-predictions``: a bug, hang, or crash here cannot
affect waypoint generation. Run one instance per glider, alongside a
matching ``sfmc-pull-new-downloads`` instance watching the same
directory::

    # Simple presence check, science files:
    autopilot-acoustic-watch --glider osu685 --extension tbd \\
        --input-dir /srv/autopilot/acoustic-raw/osu685 \\
        --cache-dir /srv/autopilot/acoustic-cache \\
        --variable sci_generic_a \\
        --ledger /srv/autopilot/acoustic-alert/osu685-tbd.json \\
        --slack-webhook-url-file /etc/autopilot/slack_webhook_url

    # Interrogation rate check, same glider's flight files:
    autopilot-acoustic-watch --glider osu685 --extension sbd \\
        --input-dir /srv/autopilot/acoustic-raw-flight/osu685 \\
        --cache-dir /srv/autopilot/acoustic-cache \\
        --variable sci_generic_k --equals 10 \\
        --duration-variable m_present_time --min-rate-per-minute 0.4 \\
        --ledger /srv/autopilot/acoustic-alert/osu685-sbd.json \\
        --slack-webhook-url-file /etc/autopilot/slack_webhook_url
"""

from __future__ import annotations

import argparse
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import dbdreader

from . import ledger as ledger_mod
from .slack import SendFn, make_slack_send

logger = logging.getLogger("autopilot.acoustic")

_VALUE_EPSILON = 1e-6


def dinkum_name_re(extension: str) -> re.Pattern[str]:
    """SFMC's Dinkum-renamed form, e.g. ``osu685-2026-172-0-324.tbd``.

    The raw glider-numeric name (e.g. ``03060070.tbd``) is
    deliberately not matched: sfmc-pull-new-downloads downloads both
    when both are listed, and checking the same transfer twice under
    two names would double every count. See sfmc-pull-new-downloads's
    own docstring for why both copies exist at all (the rename lands
    on a variable delay).
    """
    return re.compile(
        rf"^[A-Za-z0-9]+-\d{{4}}-\d{{3}}-\d+-\d+\.{re.escape(extension)}$"
    )


@dataclass
class CheckResult:
    """One variable's summary for one file.

    ``rate_per_minute`` is ``None`` whenever ``duration_variable``
    wasn't configured, or the file didn't have at least two samples of
    it to establish a span — callers fall back to ``min_count`` in
    that case. ``expected_count`` (``min_rate_per_minute`` scaled by
    this file's own duration) is the same configured floor ``ok`` was
    judged against, phrased as a count instead of a rate — only set
    when both a duration and a rate threshold are available.
    """

    count: int
    duration_minutes: float | None
    rate_per_minute: float | None
    ok: bool
    expected_count: float | None = None

    def describe(self) -> str:
        if self.rate_per_minute is not None:
            return f"{self.count} in {self.duration_minutes:.1f} min ({self.rate_per_minute:.2f}/min)"
        return f"{self.count}"

    def announce_text(self) -> str:
        """Human-readable count summary for a routine Slack post."""
        if self.expected_count is not None:
            return (
                f"{self.count} time(s) over {self.duration_minutes:.1f} min "
                f"(expected >= {self.expected_count:.1f})"
            )
        if self.duration_minutes is not None:
            return f"{self.count} time(s) over {self.duration_minutes:.1f} min"
        return f"{self.count} time(s)"


def check_variable(
    path: Path,
    cache_dir: Path,
    variable: str,
    *,
    equals: float | None = None,
    duration_variable: str | None = None,
    min_count: int = 1,
    min_rate_per_minute: float | None = None,
) -> CheckResult | None:
    """Read *variable* from *path* and summarize it as a count/rate.

    Returns ``None`` if the file itself could not be opened at all
    (most likely a missing or mismatched ``.cac`` file) — a different
    claim than "no/insufficient data," so the caller retries it on the
    next scan rather than counting it as either an empty or a healthy
    file. A variable name dbdreader doesn't recognize at all behaves
    the same way (see the module docstring for the distinction from a
    known variable that simply wasn't transmitted this segment).
    """
    try:
        dbd = dbdreader.DBD(str(path), cacheDir=str(cache_dir))
    except Exception:
        logger.exception("Could not open %s (missing/mismatched .cac file?)", path.name)
        return None

    try:
        _times, values = dbd.get(variable, check_for_invalid_parameters=True)
    except Exception:
        logger.exception("Could not read %r from %s", variable, path.name)
        return None

    if equals is None:
        count = len(values)
    else:
        count = int((abs(values - equals) < _VALUE_EPSILON).sum()) if len(values) else 0

    duration_minutes = None
    if duration_variable is not None:
        try:
            dtimes, dvalues = dbd.get(
                duration_variable, check_for_invalid_parameters=True
            )
        except Exception:
            logger.exception(
                "Could not read duration variable %r from %s",
                duration_variable,
                path.name,
            )
        else:
            if len(dvalues) >= 2:
                duration_minutes = float(dtimes.max() - dtimes.min()) / 60.0

    rate_per_minute = None
    if duration_minutes and duration_minutes > 0:
        rate_per_minute = count / duration_minutes

    expected_count = None
    if rate_per_minute is not None and min_rate_per_minute is not None:
        ok = rate_per_minute >= min_rate_per_minute
        expected_count = min_rate_per_minute * duration_minutes
    else:
        ok = count >= min_count

    return CheckResult(count, duration_minutes, rate_per_minute, ok, expected_count)


def _notify(
    send: SendFn, glider: str, filename: str, transition: str, streak: int
) -> None:
    try:
        if transition == "alert":
            send(
                f"{glider}: data check failing",
                f"Below the configured threshold for {streak} consecutive "
                f"file(s), most recently `{filename}`.",
            )
        else:
            send(
                f"{glider}: data check recovered",
                f"`{filename}` is back above threshold.",
            )
    except Exception:
        # A Slack outage must not stop the watcher, and must not lose
        # the ledger transition that already happened -- it just means
        # this particular message never arrives.  The next real
        # transition still fires normally.
        logger.exception("Slack delivery failed for %s (%s)", glider, transition)


def _announce_arrival(send: SendFn, glider: str, path: Path, size: int) -> None:
    """Routine "a new file showed up in the mirror" post.

    Fires as soon as the file is seen locally -- this component only
    watches a directory (see the module docstring), so it cannot
    honestly claim to have detected the zmodem transfer itself, only
    that a new mirrored copy has appeared. Independent of whether the
    file can actually be decoded (no cache-file dependency), so it
    still fires even when the content check below can't run.
    """
    try:
        send(f"{glider}: file received", f"`{path.name}` ({size} byte(s))")
    except Exception:
        logger.exception("Slack arrival announcement failed for %s", path.name)


def _announce_content(
    send: SendFn,
    glider: str,
    path: Path,
    variable: str,
    equals: float | None,
    result: CheckResult,
) -> None:
    label = f"{variable} == {equals:g}" if equals is not None else variable
    try:
        send(f"{glider}: {path.name}", f"{label}: {result.announce_text()}")
    except Exception:
        logger.exception("Slack content announcement failed for %s", path.name)


def scan_once(
    input_dir: Path,
    cache_dir: Path,
    variable: str,
    check_kwargs: dict,
    glider_ledger: dict[str, ledger_mod.GliderLedger],
    glider: str,
    name_pattern: re.Pattern[str],
    threshold: int,
    send: SendFn | None,
    announce: bool = False,
    announce_min_bytes: int = 0,
    min_bytes: int = 0,
    announced: set[str] | None = None,
) -> None:
    """Check every not-yet-processed matching file in *input_dir*.

    *min_bytes* fully exempts files smaller than this from the check:
    not read, not counted toward the alert streak, not announced --
    just marked processed and skipped, permanently. This is for real,
    identifiable "nothing to report" files (e.g. small administrative
    segments logged while a glider idles at the surface, which real
    data shows essentially never carry the variable of interest) --
    not for tolerating routine failures. Any file that clears this bar
    and still fails the check is a real problem, not noise.

    When *announce* is set (and *send* is configured), two routine
    Slack posts go out per remaining new file, in sequence: arrival
    (size, as soon as the file is seen) and then content (count/rate,
    once read). These are independent of the alert/recovery mechanism
    below -- routine visibility into what's arriving, not a health
    signal -- so they fire on every checked file, healthy or not.
    *announce_min_bytes* additionally trims routine Slack noise (only)
    among files that still get checked; unlike *min_bytes* it has no
    effect on the alert streak.

    *announced* is the caller-owned set of filenames whose arrival has
    already been posted. It exists because an unreadable file (e.g.
    missing .cac) is deliberately retried every scan without being
    marked processed -- without this memory, its arrival post would
    repeat every poll interval for as long as the file stays
    unreadable. Pass the same set across scans; in-process only is
    fine (one repeat after a service restart is harmless, a message
    every 10 seconds is not).
    """
    if announced is None:
        announced = set()
    already = glider_ledger.get(glider)
    seen = already.processed if already else frozenset()
    for path in sorted(input_dir.iterdir()):
        if not name_pattern.match(path.name) or path.name in seen:
            continue
        size = path.stat().st_size
        if size < min_bytes:
            ledger_mod.mark_processed(glider_ledger, glider, path.name)
            logger.info(
                "%s: %d byte(s), below --min-bytes %d -- skipped",
                path.name,
                size,
                min_bytes,
            )
            continue
        worth_announcing = announce and send and size >= announce_min_bytes
        if worth_announcing and path.name not in announced:
            announced.add(path.name)
            _announce_arrival(send, glider, path, size)
        result = check_variable(path, cache_dir, variable, **check_kwargs)
        if result is None:
            continue  # unreadable; retried next scan, not counted either way
        if worth_announcing:
            _announce_content(
                send, glider, path, variable, check_kwargs.get("equals"), result
            )
        transition = ledger_mod.record(
            glider_ledger, glider, path.name, result.ok, threshold
        )
        logger.info(
            "%s: %s -> %s%s",
            path.name,
            result.describe(),
            "ok" if result.ok else "below threshold",
            f" ({transition})" if transition else "",
        )
        if transition and send:
            streak = (
                glider_ledger[glider].consecutive_empty if transition == "alert" else 0
            )
            _notify(send, glider, path.name, transition, streak)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s  %(message)s")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--glider", required=True, help="registered glider name")
    ap.add_argument(
        "--extension",
        default="tbd",
        help="file extension to watch, without the dot (default: %(default)s)",
    )
    ap.add_argument(
        "--input-dir",
        required=True,
        help="directory sfmc-pull-new-downloads mirrors this glider's files into",
    )
    ap.add_argument(
        "--cache-dir", required=True, help="manually-maintained .cac file repository"
    )
    ap.add_argument("--variable", required=True, help="variable name to check")
    ap.add_argument(
        "--equals",
        type=float,
        default=None,
        help="count only samples equal to this value; omit to count any sample "
        "(a simple presence check)",
    )
    ap.add_argument(
        "--duration-variable",
        default=None,
        help="a densely-sampled variable (e.g. m_present_time) whose own span "
        "gives the file's duration, for a rate check instead of a raw count",
    )
    ap.add_argument(
        "--min-count",
        type=int,
        default=1,
        help="minimum count to pass when no rate is available (default: %(default)s)",
    )
    ap.add_argument(
        "--min-rate-per-minute",
        type=float,
        default=None,
        help="minimum rate to pass, when --duration-variable is set and the file "
        "has enough samples of it; falls back to --min-count otherwise",
    )
    ap.add_argument("--ledger", required=True, help="state file path")
    ap.add_argument(
        "--threshold",
        type=int,
        default=2,
        help="consecutive below-threshold files before alerting (default: %(default)s)",
    )
    ap.add_argument(
        "--poll-seconds",
        type=float,
        default=10.0,
        help="local directory poll interval (default: %(default)s)",
    )
    ap.add_argument(
        "--slack-webhook-url-file",
        default=None,
        help="file containing the Slack webhook URL; omit to log transitions only",
    )
    ap.add_argument(
        "--announce",
        action="store_true",
        help="also post a routine Slack message per new file (arrival size, "
        "then count/rate) -- independent of alert/recovery; noisy on a busy "
        "glider, off by default",
    )
    ap.add_argument(
        "--announce-min-bytes",
        type=int,
        default=0,
        help="skip routine --announce messages (only) for files smaller than "
        "this; the file is still fully checked and still counts toward "
        "alert/recovery regardless (default: %(default)s, i.e. no filtering)",
    )
    ap.add_argument(
        "--min-bytes",
        type=int,
        default=0,
        help="fully exempt files smaller than this from the check -- not "
        "read, not counted toward the alert streak, not announced; for "
        "real, identifiable no-content files (e.g. small administrative "
        "surface segments), not for tolerating routine failures "
        "(default: %(default)s, i.e. no filtering)",
    )
    args = ap.parse_args()

    input_dir = Path(args.input_dir)
    cache_dir = Path(args.cache_dir)
    ledger_path = Path(args.ledger)
    glider_ledger = ledger_mod.load(ledger_path)
    name_pattern = dinkum_name_re(args.extension)
    check_kwargs = dict(
        equals=args.equals,
        duration_variable=args.duration_variable,
        min_count=args.min_count,
        min_rate_per_minute=args.min_rate_per_minute,
    )

    send: SendFn | None = None
    if args.slack_webhook_url_file:
        webhook_url = Path(args.slack_webhook_url_file).read_text().strip()
        send = make_slack_send(webhook_url)

    logger.info(
        "Watching %s for %s (*.%s, variable: %s, threshold: %d)",
        input_dir,
        args.glider,
        args.extension,
        args.variable,
        args.threshold,
    )
    announced: set[str] = set()
    while True:
        try:
            scan_once(
                input_dir,
                cache_dir,
                args.variable,
                check_kwargs,
                glider_ledger,
                args.glider,
                name_pattern,
                args.threshold,
                send,
                args.announce,
                args.announce_min_bytes,
                args.min_bytes,
                announced,
            )
            ledger_mod.save(ledger_path, glider_ledger)
        except Exception:
            # One bad scan (a transient filesystem hiccup, a corrupt
            # file dbdreader chokes on outside the per-file guard
            # above) must not kill a long-running service -- log and
            # retry next cycle, same as every other watcher in this
            # project.
            logger.exception("Scan failed; will retry")
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
