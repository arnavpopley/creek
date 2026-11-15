"""PreCompact: log the compaction and refresh state (§7.6).

PreCompact can't change the summary; SessionStart (source "compact") re-injects
the state block afterwards.
"""

from __future__ import annotations

from .. import ledger
from . import locate


def run(payload: dict) -> None:
    cdir, session = locate(payload)
    with ledger.transaction(cdir, session) as tx:
        tx.emit({"type": "compact", "trigger": payload.get("trigger") or "auto"})
    return None
