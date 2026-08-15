"""Small persisted per-glider state for the acoustic-data watcher.

Tracks which science files have already been checked, and a run of
consecutive "no data" results per glider so ``record`` can fire an
alert once that run reaches the caller's configured ``threshold``.
Whether that should be 1 (alert on the very first bad file) or higher
is a real operational question, not something this module decides:
for osu1267, transfer size *does* reliably separate "nothing to
report" (small administrative surface segments) from "something went
wrong" (a real-sized file with no data), so those small files are
filtered out by the caller entirely (see ``mark_processed`` and
``watch.py``'s ``--min-bytes``) and any real file that fails is a
genuine problem worth an immediate alert, not routine noise -- there
turned out to be no such thing as a legitimate quiet period once size
filtering is in place; the "lulls" seen in real history were failures
pilots had been catching by hand. A transition back to a file with
data sends one recovery message.

Pure functions over a plain dict, no I/O beyond the explicit load/save
calls, so the alerting logic is testable without touching a real
ledger file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

Transition = str | None  # "alert" | "recovery" | None


@dataclass
class GliderLedger:
    processed: set[str] = field(default_factory=set)
    consecutive_empty: int = 0
    in_alert: bool = False


def load(path: Path) -> dict[str, GliderLedger]:
    """Load the ledger; an absent or corrupt file starts empty.

    A corrupt ledger must never stop the watcher from starting — the
    worst case of starting empty is a handful of already-seen files
    getting rechecked (harmless, just redundant work), which is far
    better than the service refusing to come up.
    """
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    ledger: dict[str, GliderLedger] = {}
    for glider, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        processed = entry.get("processed", [])
        ledger[glider] = GliderLedger(
            processed=set(processed) if isinstance(processed, list) else set(),
            consecutive_empty=int(entry.get("consecutive_empty", 0) or 0),
            in_alert=bool(entry.get("in_alert", False)),
        )
    return ledger


def save(path: Path, ledger: dict[str, GliderLedger]) -> None:
    """Atomically write the ledger (write-then-rename)."""
    payload = {
        glider: {
            "processed": sorted(entry.processed),
            "consecutive_empty": entry.consecutive_empty,
            "in_alert": entry.in_alert,
        }
        for glider, entry in ledger.items()
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(path)


def mark_processed(ledger: dict[str, GliderLedger], glider: str, filename: str) -> None:
    """Record *filename* as seen without affecting the alert streak.

    For files exempted from the check entirely (e.g. too small to ever
    carry real content) -- unlike ``record``, this must never touch
    ``consecutive_empty``/``in_alert``, since an exempted file says
    nothing about whether real data is flowing.
    """
    ledger.setdefault(glider, GliderLedger()).processed.add(filename)


def record(
    ledger: dict[str, GliderLedger],
    glider: str,
    filename: str,
    has_data: bool,
    threshold: int,
) -> Transition:
    """Update *glider*'s entry for one checked file.

    Mutates *ledger* in place and returns which transition (if any)
    just happened, so the caller decides whether/how to notify — this
    function only tracks state, it never sends anything itself.
    """
    entry = ledger.setdefault(glider, GliderLedger())
    entry.processed.add(filename)
    if has_data:
        entry.consecutive_empty = 0
        if entry.in_alert:
            entry.in_alert = False
            return "recovery"
        return None
    entry.consecutive_empty += 1
    if not entry.in_alert and entry.consecutive_empty >= threshold:
        entry.in_alert = True
        return "alert"
    return None
