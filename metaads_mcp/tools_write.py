"""Write tools: a ``preview_*`` and an ``apply_*`` for each change.

SEPARATE TOOLS, NOT A ``dry_run`` PARAMETER. This is the most consequential
choice here. Claude Code's permission rules key on the tool NAME, so separate
names let you permanently allowlist every ``preview_*`` while never
allowlisting an ``apply_*``. With a flag, one "always allow" clicked during a
harmless preview would silently authorise every future real write -- exactly
the failure this is guarding against.

Every ``apply_*`` demands the token its own preview minted. That buys three
things at once: a readable preview always sits directly above the approval
prompt, an apply cannot be called cold, and the recorded ``before`` is
re-checked against the entity's CURRENT value, so a change someone made in Ads
Manager in between is caught rather than silently overwritten. That last one
earns its keep on Meta, where Ads Manager is usually open in a browser tab
while somebody is talking to an agent about the same account.

``destructiveHint`` is set only where the change moves money or stops delivery.
Marking a reversible pause destructive would train someone to click through the
loud prompts, which is how loud prompts stop working.

NOT EXPOSED, deliberately:
  - setting any status to DELETED or ARCHIVED. Both are irreversible through
    the API, and PAUSED achieves the same outcome reversibly. The refusal lives
    in ``metaads_reporting.write.client.check_status``, below the tool layer,
    so it holds for every caller.
  - creating campaigns and ad sets, and deleting anything. A campaign create
    carries an objective, a budget and a special-ad-category declaration that
    is a legal statement a human answers for. The mutating client has no delete
    method at all. (Ads ARE created here -- always PAUSED -- by
    preview_duplicate_ad.)
  - editing a creative in place. A Meta creative is immutable once attached,
    so changing what an ad says is a new creative: preview_duplicate_ad puts it
    on a new PAUSED ad, preview_update_ad_copy repoints the existing ad at it.
  - targeting, optimization goal, billing event, schedule, special ad
    categories. None is on the write client's field allowlist.

THE SEQUENCE lives once, in :mod:`metaads_mcp.changes.engine`; each change is
a :class:`~metaads_mcp.changes.base.ChangeKind` in :mod:`metaads_mcp.changes`.
This module only wires them up.

Enforcement is structural: there is no generic ``update(node, fields)``
passthrough, so an unregistered operation is unreachable rather than merely
undocumented. ``tests/test_mcp_surface.py`` pins the registered tool names.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from metaads_mcp import ledger
from metaads_mcp.changes import all_kinds
from metaads_mcp.changes.base import (  # noqa: F401 - re-exported for callers
    APPLY_REVERSIBLE,
    APPLY_SPENDS,
    PREVIEW_SAFE,
    SETTABLE_STATUSES,
    parse_money,
)
from metaads_mcp.changes.context import WriteContext
from metaads_mcp.changes.engine import Engine, register_pair
# One mapper for read and write tools -- see metaads_mcp/errors.py.
from metaads_mcp.errors import refusals_reach_the_caller  # noqa: F401
from metaads_mcp.guardrails import read_only_mode
from metaads_mcp.previews import ApplyResult
from metaads_reporting.write import MutatingMetaAdsClient
from metaads_reporting.write.plan import execute, execute_chain

def parse_decimal(raw: str, name: str, currency: str = ""):
    """Kept for callers of the old name; see changes.base.parse_money."""
    return parse_money(raw, name)


def register(mcp, state) -> None:
    """Attach every write tool to ``mcp``."""

    # Read-only mode: the preview_* tools still register, because a preview
    # writes nothing and being able to say "here is exactly what I would
    # change" is most of the value even when the agent cannot be the one to do
    # it. Every apply_* and revert_change is simply never handed to the
    # registrar, so they are absent from tools/list and unreachable over MCP --
    # not refused at call time, ABSENT. See guardrails.read_only_mode().
    read_only = read_only_mode()

    def apply_tool(**kwargs):
        """`mcp.tool(...)` for a write tool, or a no-op when read-only.

        Returning the bare function leaves it defined and callable in-process,
        which the tests rely on, while registering nothing -- so nothing
        crosses the MCP boundary.
        """
        if read_only:
            return lambda fn: fn
        return mcp.tool(**kwargs)

    write_client = MutatingMetaAdsClient.from_env()
    # The second naming guard, below every tool: with a prefix configured, a
    # named create or a rename without it is refused before it is sent.
    write_client.required_name_prefix = state.identity.prefix
    state.write_client = write_client
    ctx = WriteContext(state, write_client, execute, execute_chain)
    engine = Engine(ctx)
    state.engine = engine

    for kind in all_kinds():
        register_pair(
            mcp, engine, kind, refusals=refusals_reach_the_caller, apply_tool=apply_tool
        )

    from metaads_mcp.tools_read import READ_ONLY

    @mcp.tool(title="Compare the ledger with the live account", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def reconcile_ledger(
        limit: Annotated[int, Field(description="Most recent entries to check", ge=1, le=500)] = 100,
    ) -> dict:
        """Check every applied, unreverted change against the account as it is now.

        For each entry, reads the live value and compares it with what the
        change left behind. `drift` lists entries whose live value differs --
        somebody changed it in Ads Manager since, or the object moved under it
        (a campaign whose budget went to its ad sets). Reverting one of those
        would be refused as stale; a human should close it out.

        Also lists `unknown_outcome` (an intent with no outcome: the write left
        this process and nothing recorded how it ended) and `pending_review`
        (an ad change whose verification said review was still pending -- call
        verify_ad on it). Reads only.
        """
        reverted = ledger.reverted_entry_ids()
        rows = ledger.entries()[-limit:]
        drift, unknown, pending, ok, unchecked = [], [], [], 0, []
        for entry in rows:
            summary = {
                "entry_id": entry["entry_id"],
                "at": entry["at"],
                "tool": entry["tool"],
                "entity": f"{entry['entity_type']} {entry['entity_name']}",
                "entity_id": entry["entity_id"],
                "field": entry["field"],
                "after": entry["after"],
            }
            if entry.get("error") is None and not entry.get("applied") and entry.get("tool") != "revert_change":
                if entry["entry_id"] not in reverted:
                    unknown.append(summary)
                continue
            if not entry.get("applied") or entry["entry_id"] in reverted:
                continue
            if entry.get("tool") == "revert_change" and not (
                isinstance(entry.get("inverse"), dict) and entry["inverse"].get("revert_row")
            ):
                # Written before revert rows recorded what they replaced: its
                # inverse is the ORIGINAL's, so the live comparison would lie.
                unchecked.append({**summary, "reason": "a revert written before reverts recorded their own values"})
                continue
            verification = entry.get("verification") or {}
            if (verification.get("review") or {}).get("state") == "pending":
                pending.append({**summary, "ad_id": verification.get("ad_id")})
            try:
                kind = engine.kind_for_entry(entry)
                live = kind.current_for_entry(ctx, entry)
            except Exception as exc:  # noqa: BLE001 - reported per entry
                unchecked.append({**summary, "reason": str(exc).splitlines()[0]})
                continue
            if live is None:
                unchecked.append({**summary, "reason": "this kind has no live value to compare"})
            elif live != entry["after"]:
                drift.append({**summary, "live": live})
            else:
                ok += 1
        return {
            "checked": len(rows),
            "matching": ok,
            "drift": drift,
            "unknown_outcome": unknown,
            "pending_review": pending,
            "unchecked": unchecked,
            "note": (
                "Drift is not an error to fix blindly: the live value is what the "
                "account does now. Show the human, and let them decide whether to "
                "change it back with a preview_* tool or leave it."
            ),
        }

    @apply_tool(
        title="Revert a change by its ledger entry id", annotations=APPLY_REVERSIBLE
    )
    @refusals_reach_the_caller
    def revert_change(
        entry_id: Annotated[str, Field(description="From list_my_changes (or a batch_id)")],
    ) -> ApplyResult:
        """Put a value back to what it was before an earlier change.

        Reverting is itself a change and is itself recorded, so the ledger
        stays a complete history rather than a list of things that are still
        true. Reverting something this server CREATED pauses it -- nothing is
        ever deleted -- and an audience cannot be reverted at all.
        """
        return engine.revert(entry_id)
