"""Read tools. Safe to allowlist permanently -- none of these can change anything.

They route through :class:`ReadOnlyMetaAdsClient`, which sends only GET. On the
Graph API the HTTP method is the read/write boundary, so a write is unreachable
from here regardless of what is asked for.
"""

from __future__ import annotations

import functools
from typing import Annotated, Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from metaads_reporting.client import ReadOnlyMetaAdsClient
from metaads_reporting.currency import CurrencyError
from metaads_reporting.graph import GraphError
from metaads_reporting.insights import InsightsError, name_contains, summarise
from metaads_reporting.reports import get_report, report_names


def refusals_reach_the_caller(fn):
    """Re-raise a deliberate refusal as ToolError so its message survives.

    The MCP runtime carries a ToolError's text through to the caller but
    replaces every other exception with a bare "Error executing tool <name>".
    An unknown report name should say which names exist, not nothing.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except (ValueError, LookupError, GraphError, CurrencyError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


# ALL FOUR HINTS, ALWAYS, ON EVERY TOOL. A hint left unset is published as
# absent, and a client that cannot see one falls back on the spec's default --
# which for `destructiveHint` is TRUE. So an omitted hint does not mean
# "unknown", it means "assume the worst", and a plain `list_ad_accounts` ends up
# rendered with the same warning as a budget change. Some tool directories
# reject the tool outright. Spell every one of them out, even where the value
# looks obvious.
#
# READ_ONLY   queries the Marketing API: no mutation, same answer twice.
# LOCAL_ONLY  answers from this process and the local ledger and touches no
#             network at all, which is the only difference -- hence
#             open_world_hint=False.
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

LOCAL_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def register(mcp, state) -> None:
    """Attach every read tool to ``mcp``."""

    client = ReadOnlyMetaAdsClient.from_env()
    state.read_client = client
    state.default_account_id = client.settings.account_id or ""

    def money(value: Any, currency: str) -> str | None:
        from metaads_reporting.currency import format_money, from_minor_units

        amount = from_minor_units(value, currency)
        return None if amount is None else format_money(amount, currency)

    @mcp.tool(
        title="List the ad accounts this token can reach", annotations=READ_ONLY
    )
    @refusals_reach_the_caller
    def list_ad_accounts() -> dict[str, Any]:
        """Ad accounts reachable by this token, plus the one this server defaults to.

        For a System User token this is the accounts it has been ASSIGNED in
        Business Settings -- usually far fewer than the business owns.

        The default is the only account writes may touch unless guardrails.toml
        widens the scope.
        """
        return {
            "default_account_id": state.default_account_id,
            "reachable": client.list_ad_accounts(),
            "writes_allowed_on": list(state.limits.allowed_account_ids)
            or [state.default_account_id],
        }

    @mcp.tool(title="Show the ad account's own settings", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def get_account() -> dict[str, Any]:
        """Currency, time zone, status, spend cap and balance.

        Worth calling early. **The currency decides what every budget number in
        this server means** -- Meta stores budgets as an integer in the
        currency's minor unit, so the same `5000` is fifty dollars on a USD
        account and five thousand yen on a JPY one. The time zone matters for
        the same reason: report dates are evaluated in the ACCOUNT's zone, not
        yours.
        """
        summary = client.account_summary()
        summary["rate_limit"] = client.rate_limit()
        return summary

    @mcp.tool(title="Find campaigns by name", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def find_campaigns(
        name_contains_text: Annotated[
            str | None,
            Field(description="Case-insensitive substring of the campaign name"),
        ] = None,
        status: Annotated[
            str | None, Field(description="ACTIVE or PAUSED")
        ] = None,
        limit: Annotated[int, Field(description="Max rows", ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        """Campaigns with their ids, status, objective and budget.

        Start here. Every write tool needs an id from this.

        **Read `budget_level` carefully.** `campaign` means this campaign holds
        the budget and its ad sets do not (Advantage campaign budget); `adset`
        means the opposite. Changing a budget at the wrong level is refused,
        and which level a given campaign uses is not guessable.
        """
        from metaads_reporting.write import campaigns as campaign_writes

        currency = client.currency()
        rows = campaign_writes.search(
            _as_write_shim(client), name_contains_text, limit=limit
        )
        if status:
            wanted = status.upper()
            rows = [r for r in rows if (r.get("status") or "").upper() == wanted]
        return [
            {
                "campaign_id": str(r.get("id")),
                "name": r.get("name"),
                "status": r.get("status"),
                "effective_status": r.get("effective_status"),
                "objective": r.get("objective"),
                "bid_strategy": r.get("bid_strategy"),
                "budget_level": campaign_writes.budget_level(r),
                "daily_budget": money(r.get("daily_budget"), currency),
                "lifetime_budget": money(r.get("lifetime_budget"), currency),
                "currency": currency,
            }
            for r in rows[:limit]
        ]

    @mcp.tool(title="Find ad sets by name", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def find_ad_sets(
        name_contains_text: Annotated[str | None, Field(description="Substring")] = None,
        campaign_id: Annotated[
            str | None, Field(description="Restrict to one campaign")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        """Ad sets with their ids, status, budget, bid and parent campaign.

        Ad set names are unique only WITHIN a campaign -- "Broad" and
        "Lookalike 1%" appear in every campaign an account has -- so pass
        `campaign_id` whenever you know it.

        `bid_amount` is only honoured under `LOWEST_COST_WITH_BID_CAP` and
        `COST_CAP`. Under any other `bid_strategy` Meta accepts a bid write and
        ignores it, so check the strategy before proposing one.
        """
        from metaads_reporting.write import adsets as adset_writes

        currency = client.currency()
        rows = adset_writes.search(
            _as_write_shim(client),
            name_contains_text,
            campaign_id=campaign_id,
            limit=limit,
        )
        return [
            {
                "ad_set_id": str(r.get("id")),
                "name": r.get("name"),
                "status": r.get("status"),
                "effective_status": r.get("effective_status"),
                "campaign_id": str(r.get("campaign_id")),
                "daily_budget": money(r.get("daily_budget"), currency),
                "lifetime_budget": money(r.get("lifetime_budget"), currency),
                "bid_amount": money(r.get("bid_amount"), currency),
                "bid_strategy": r.get("bid_strategy"),
                "optimization_goal": r.get("optimization_goal"),
                "billing_event": r.get("billing_event"),
                "currency": currency,
            }
            for r in rows[:limit]
        ]

    @mcp.tool(title="Find ads by name", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def find_ads(
        name_contains_text: Annotated[str | None, Field(description="Substring")] = None,
        ad_set_id: Annotated[str | None, Field(description="Restrict to one ad set")] = None,
        campaign_id: Annotated[
            str | None, Field(description="Restrict to one campaign")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        """Ads with their ids, status and parents.

        `effective_status` is the field to read, not `status`. `DISAPPROVED`
        and `PENDING_REVIEW` both mean the ad is not running no matter what its
        own status says, and `ADSET_PAUSED` means a parent is off.
        """
        from metaads_reporting.write import ads as ad_writes

        rows = ad_writes.search(
            _as_write_shim(client),
            name_contains_text,
            ad_set_id=ad_set_id,
            campaign_id=campaign_id,
            limit=limit,
        )
        return [
            {
                "ad_id": str(r.get("id")),
                "name": r.get("name"),
                "status": r.get("status"),
                "effective_status": r.get("effective_status"),
                "ad_set_id": str(r.get("adset_id")),
                "campaign_id": str(r.get("campaign_id")),
                "updated_time": r.get("updated_time"),
            }
            for r in rows[:limit]
        ]

    @mcp.tool(title="Run a performance report", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def run_report(
        report: Annotated[
            str, Field(description=f"One of: {', '.join(report_names())}")
        ],
        days: Annotated[
            int | None,
            Field(description="Window of N days ending YESTERDAY", ge=1, le=365),
        ] = 30,
        since: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        until: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        campaign_name: Annotated[
            str | None, Field(description="Narrow to campaigns whose name contains this")
        ] = None,
        node_id: Annotated[
            str | None,
            Field(description="Report on one campaign, ad set or ad id instead of the account"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 200,
    ) -> dict[str, Any]:
        """Account, campaign, ad set or ad performance from the insights edge.

        Today is excluded from `days` on purpose: today's metrics are partial
        all day and drag every average down.

        THREE THINGS TO KNOW ABOUT THESE NUMBERS:

        - **Attribution is 7-day-click plus 1-day-view** unless a report says
          otherwise. The 1-day-view half counts people who SAW an ad and never
          touched it, which is why Meta's conversion count is routinely several
          times larger than any click-based record of the same campaign. They
          are not measuring the same thing, and neither is wrong.
        - **Dates are in the ACCOUNT's time zone.** See `get_account`.
        - **`reach` and `frequency` cannot be summed across rows.** The same
          person reached on two days is one person. The totals below leave them
          out for that reason.
        """
        definition = get_report(report)
        filters = (
            (name_contains("campaign", campaign_name),) if campaign_name else ()
        )
        if since or until:
            query = definition.build(
                since=since, until=until, filtering=filters, limit=limit
            )
        else:
            from metaads_reporting.insights import last_n_days

            window = last_n_days(days or 30)
            query = definition.build(
                since=window["since"],
                until=window["until"],
                filtering=filters,
                limit=limit,
            )

        rows = client.insight_rows(query, node_id=node_id)
        return {
            "report": report,
            "level": definition.level,
            "window": query.describe_window(),
            "attribution_windows": list(query.attribution_windows),
            "currency": client.currency(),
            "row_count": len(rows),
            "totals": summarise(rows),
            "rows": rows,
            "note": (
                "Totals omit reach and frequency: they cannot be summed across "
                "rows. Use the creative_fatigue report, which collapses the "
                "window into one row per ad, when frequency is the question."
            ),
        }

    @mcp.tool(
        title="Show the write guardrails and session budget", annotations=LOCAL_ONLY
    )
    @refusals_reach_the_caller
    def get_guardrails() -> dict[str, Any]:
        """The bounds every apply_* is checked against, and what is left.

        Call this before proposing a change, so you propose something that can
        actually be applied rather than something that will be refused.
        """
        from metaads_mcp.guardrails import read_only_mode, writes_disabled

        read_only = read_only_mode()
        return {
            "limits": state.limits.describe(),
            "session": state.counters.snapshot(),
            "writes_disabled": writes_disabled(),
            "read_only_server": read_only,
            "pending_previews": state.previews.pending_count,
            "note": (
                "THIS SERVER IS READ-ONLY: it published no apply_* tools and no "
                "revert_change, so nothing here can change the account. The "
                "preview_* tools still work -- use them to show a human exactly "
                "what you would change, and let them make it from a writable "
                "client or Ads Manager."
                if read_only
                else "Session counters reset only when the server restarts."
            ),
        }

    @mcp.tool(
        title="List changes made in this account by the server", annotations=LOCAL_ONLY
    )
    @refusals_reach_the_caller
    def list_my_changes(
        limit: Annotated[int, Field(ge=1, le=200)] = 25,
    ) -> list[dict[str, Any]]:
        """Every change this server has applied, newest last, with entry ids.

        Pass an entry_id to revert_change to undo one.

        An entry with `applied: false` and no error is the one worth looking
        at: the write left this process and nothing recorded how it ended, so
        it may have landed at Meta's end. Check the object in Ads Manager
        before doing anything else to it.
        """
        from metaads_mcp import ledger

        reverted = ledger.reverted_entry_ids()
        rows = ledger.entries()[-limit:]
        return [
            {
                "entry_id": e.get("entry_id"),
                "at": e.get("at"),
                "tool": e.get("tool"),
                "entity": f"{e.get('entity_type')} {e.get('entity_name')}",
                "entity_id": e.get("entity_id"),
                "field": e.get("field"),
                "before": e.get("before"),
                "after": e.get("after"),
                "applied": e.get("applied", False),
                "reverted": e.get("entry_id") in reverted,
                "error": e.get("error"),
            }
            for e in rows
        ]


class _WriteShim:
    """Lets the read tools reuse the write package's search/find helpers.

    Those helpers take a :class:`MutatingMetaAdsClient` because that is what
    the write path has, but every one of them used here only ever READS. Rather
    than duplicating the field lists and the effective_status filters in two
    places -- where they would drift -- this hands them the read client behind
    the three attributes they actually touch.

    It exposes no write method at all, so a search helper that grew one would
    fail here loudly rather than quietly gaining the ability to mutate.
    """

    def __init__(self, client: ReadOnlyMetaAdsClient) -> None:
        self.read = client
        self._client = client

    def resolve_account_id(self, override: str | None = None) -> str:
        return self._client.resolve_account_id(override)

    def currency(self, account_id: str | None = None) -> str:
        return self._client.currency(account_id)

    def node(self, node_id: str, fields: tuple[str, ...]) -> dict[str, Any]:
        return self._client.node(node_id, fields)


def _as_write_shim(client: ReadOnlyMetaAdsClient) -> _WriteShim:
    return _WriteShim(client)
