"""What the agent changed, and how to undo one.

The storage is SQLite (``logs/meta-ads.db``, see
``metaads_reporting.logdb``). This module is the ledger-shaped face of the
``changes`` table, because ``tools_write`` speaks in intents and outcomes
rather than rows -- and because the entry dict it hands back is a contract
``revert_change`` reads field by field.

Separate from ``metaads_reporting/write/audit.py``, which records every write
the library sends from any caller into the ``mutations`` table. Two tables
rather than one because they answer different questions: this is "what did the
agent do that I might want to undo", that is "what did this package send to
Meta", and a merged table would null out half its columns on every row.

Two phases per change. ``write_intent`` goes in BEFORE the request leaves the
process and ``write_outcome`` after it returns, so an entry with no outcome
means the process died mid-request -- exactly when you need to know what was in
flight. That gap is not hypothetical here: the write path does not retry,
because a POST that timed out may already have applied and nothing on this side
can tell.

``entries()`` comes back OLDEST FIRST, because ``list_my_changes`` slices
``[-limit:]`` off the end to get the most recent.
"""

from __future__ import annotations

from metaads_reporting import logdb

new_entry_id = logdb.new_entry_id
entries = logdb.change_entries
find_entry = logdb.find_change
reverted_entry_ids = logdb.reverted_entry_ids


def write_intent(
    *,
    entry_id: str,
    tool: str,
    account_id: str,
    entity_type: str,
    entity_id: str,
    entity_name: str,
    field_name: str,
    before: str,
    after: str,
    projected_delta: str,
) -> None:
    """Record what we are ABOUT to do. Must be called before the mutation."""
    logdb.insert_change(
        entry_id=entry_id,
        tool=tool,
        account_id=account_id,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        field_name=field_name,
        before=before,
        after=after,
        projected_delta=projected_delta,
    )


def write_outcome(
    *,
    entry_id: str,
    ok: bool,
    resource_name: str | None = None,
    error: str | None = None,
    reverts: str | None = None,
) -> None:
    """Record how it went, and which earlier entry this one reverses."""
    logdb.complete_change(
        entry_id=entry_id,
        ok=ok,
        resource_name=resource_name,
        error=error,
        reverts=reverts,
    )
