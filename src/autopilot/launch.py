"""Launcher that fills in sfmc-follow CLI flags from the follower config.

``sfmc-follow`` takes the SFMC hostname and notify-email settings only
as CLI flags — it never looks inside ``--config`` for them. Keeping
those in the systemd unit works, but a value that changes per-glider
(which server it's registered on) or that pilots may want to tweak
without root/`daemon-reload` (the notify list, the sender address)
ends up baked into a root-owned unit file instead of the glider's own
config, which pilots can already edit.

This wraps ``sfmc-follow``'s own CLI: for each mapped config key, if
the corresponding flag wasn't already given explicitly, its config
value is appended to argv before ``sfmc-follow`` parses it. An
explicit flag on the command line always wins over the config, for
one-off overrides (e.g. testing a normally-gliderfmc0 config against
gliderfmc1, or a manual `--notify-email` for a single run).
"""

from __future__ import annotations

import sys

import yaml

# Single-valued: config key -> CLI flag.
_SINGLE_VALUE_KEYS = {
    "sfmc_hostname": "--hostname",
    "notify_from": "--notify-from",
}
# Repeatable: config key (a list, or a single string) -> CLI flag.
_REPEATABLE_KEYS = {
    "notify_email": "--notify-email",
}


def _config_path(argv: list[str]) -> str | None:
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
    return None


def _has_flag(argv: list[str], flag: str) -> bool:
    return flag in argv or any(a.startswith(f"{flag}=") for a in argv)


def _load_config(argv: list[str]) -> dict:
    path = _config_path(argv)
    if path is None:
        return {}
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _augment_argv(argv: list[str]) -> list[str]:
    """Fill in mapped flags from the config, skipping any given explicitly."""
    config = _load_config(argv)
    if not config:
        return argv

    extra: list[str] = []
    for key, flag in _SINGLE_VALUE_KEYS.items():
        if _has_flag(argv, flag):
            continue
        value = config.get(key)
        if value:
            extra += [flag, str(value)]

    for key, flag in _REPEATABLE_KEYS.items():
        if _has_flag(argv, flag):
            continue
        values = config.get(key)
        if not values:
            continue
        if isinstance(values, str):
            values = [values]
        for value in values:
            extra += [flag, str(value)]

    return [*argv, *extra] if extra else argv


def main() -> None:
    sys.argv[1:] = _augment_argv(sys.argv[1:])
    from sfmc_api.follow_glider import main as sfmc_follow_main

    sfmc_follow_main()


if __name__ == "__main__":
    main()
