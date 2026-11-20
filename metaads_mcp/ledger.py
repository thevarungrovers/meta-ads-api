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
changes_since = logdb.changes_since
batch_entries = logdb.batch_changes
set_verification = logdb.set_verification
amend = logdb.amend_change


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
    inverse: dict | None = None,
    reverts: str | None = None,
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
        inverse=inverse,
        reverts=reverts,
    )


def write_outcome(
    *,
    entry_id: str,
    ok: bool,
    resource_name: str | None = None,
    error: str | None = None,
    reverts: str | None = None,
    verification: dict | None = None,
) -> None:
    """Record how it went, and which earlier entry this one reverses."""
    logdb.complete_change(
        entry_id=entry_id,
        ok=ok,
        resource_name=resource_name,
        error=error,
        reverts=reverts,
        verification=verification,
    )


#: Kinds whose applied rows mean "this server made that object".
CREATING_KINDS = frozenset({
    "create_campaign", "create_ad_set", "create_ad", "duplicate_ad",
    "duplicate_ad_set", "duplicate_campaign",
})


def created_ids(entries_list: list[dict] | None = None) -> set[str]:
    """Every object id the ledger says this server created: a create's id,
    a copy's id and its children, and anything a copy's follow-up renamed."""
    out: set[str] = set()
    for entry in entries_list if entries_list is not None else entries():
        if not entry.get("applied"):
            continue
        inverse = entry.get("inverse") if isinstance(entry.get("inverse"), dict) else {}
        if inverse.get("kind") in CREATING_KINDS and entry.get("tool", "").startswith("apply_"):
            for key in ("created_id", "created_ad_id"):
                if inverse.get(key):
                    out.add(str(inverse[key]))
            out.update(str(c) for c in inverse.get("children") or [] if c)
            if entry.get("entity_id") not in (None, "", "(new)"):
                out.add(str(entry["entity_id"]))
        elif inverse.get("follows"):
            out.add(str(entry.get("entity_id")))
    return out


def created_by_us(entity_id: str) -> bool:
    """Whether the ledger shows this server created ``entity_id``."""
    return str(entity_id) in created_ids()
