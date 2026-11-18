"""Read tools. Safe to allowlist permanently -- none of these can change anything.

They route through :class:`ReadOnlyMetaAdsClient`, which sends only GET. On the
Graph API the HTTP method is the read/write boundary, so a write is unreachable
from here regardless of what is asked for.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from metaads_mcp.errors import refusals_reach_the_caller
from metaads_reporting.client import ReadOnlyMetaAdsClient
from metaads_reporting.insights import InsightsError, name_contains, summarise
from metaads_reporting.reports import get_report, report_names


#: effective_status values a find_* may filter on -- what is ACTUALLY
#: happening, parents and review folded in, not what somebody set.
EffectiveStatus = Literal[
    "ACTIVE",
    "PAUSED",
    "CAMPAIGN_PAUSED",
    "ADSET_PAUSED",
    "IN_PROCESS",
    "WITH_ISSUES",
    "PENDING_REVIEW",
    "DISAPPROVED",
]

#: The naming filters every find_* / list_* tool takes (metaads_reporting.naming).
OriginFilter = Annotated[
    Literal["any", "agent", "cli", "manual"],
    Field(description="Who the NAME says made it: agent (an OWNER-agent- prefix), cli "
                      "(OWNER-cli-, a human with scripts/manage.py), manual (no prefix)"),
]
AgentFilter = Annotated[str | None, Field(description="Only names with this agent part, e.g. max")]
OwnerFilter = Annotated[str | None, Field(description="Only names with these owner initials, e.g. AB")]
MineFilter = Annotated[bool, Field(description="Only names with THIS server's own prefix")]

Breakdown = Literal[
    "age", "gender", "country", "region", "dma",
    "publisher_platform", "platform_position", "device_platform",
    "impression_device", "hourly_stats_aggregated_by_advertiser_time_zone",
]

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

    def headroom() -> dict[str, Any]:
        """Meta's rate-limit usage as of the last call: every read reports it,
        so an agent sees 80% coming rather than meeting 100% as an error."""
        return {"worst_percent_used": client.rate_limit().get("worst_percent_used")}

    state.headroom = headroom

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
        from metaads_reporting.token_health import token_health

        summary = client.account_summary()
        summary["rate_limit"] = client.rate_limit()
        summary["token"] = token_health(client)
        return summary

    def found(rows, truncated, render, limit) -> dict[str, Any]:
        from metaads_mcp import ledger
        from metaads_reporting.naming import attribution

        made = ledger.created_ids()

        def annotated(r):
            # Who the NAME says made it (a convention), and whether the
            # ledger shows this server did (the proof).
            return {**render(r), **attribution(state.identity, r.get("name")),
                    "ledger_created": str(r.get("id")) in made}

        return {
            "rows": [annotated(r) for r in rows[:limit]],
            "count": min(len(rows), limit),
            "truncated": truncated,
            "rate_limit": headroom(),
        }

    def name_filter(origin="any", agent_name=None, owner=None, mine=False):
        from metaads_reporting.naming import NameFilter

        return NameFilter.build(state.identity, origin=origin, agent_name=agent_name,
                                owner=owner, mine=mine)

    def _search(module, parent, needle, limit, status, names=None):
        from metaads_reporting.write.lookup import edge_search

        if names is None or not names.active:
            return edge_search(
                _as_write_shim(client), parent, module.EDGE, module.FIELDS,
                needle=needle, limit=limit,
                statuses=[status] if status else list(module.DEFAULT_STATUSES),
            )
        # A server-side prefix pre-filter where one exists, then the exact
        # local parse; read deeper, since the local filter drops rows.
        rows, truncated = edge_search(
            _as_write_shim(client), parent, module.EDGE, module.FIELDS,
            needle=needle or names.needle, limit=200,
            statuses=[status] if status else list(module.DEFAULT_STATUSES),
        )
        if needle and names.needle:
            rows = [r for r in rows if names.needle in (r.get("name") or "")]
        kept = [r for r in rows if names.matches(r.get("name"))]
        return kept[:limit], truncated or len(kept) > limit

    def _account() -> str:
        from metaads_reporting.account_id import account_path

        return account_path(client.resolve_account_id())

    @mcp.tool(title="Find campaigns by name", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def find_campaigns(
        name_contains_text: Annotated[
            str | None,
            Field(description="Case-insensitive substring of the campaign name"),
        ] = None,
        status: Annotated[
            EffectiveStatus | None,
            Field(description="Filter on effective_status, e.g. ACTIVE"),
        ] = None,
        limit: Annotated[int, Field(description="Max rows", ge=1, le=200)] = 50,
        origin: OriginFilter = "any",
        agent_name: AgentFilter = None,
        owner: OwnerFilter = None,
        mine: MineFilter = False,
    ) -> dict[str, Any]:
        """Campaigns with their ids, status, objective and budget.

        Start here. Every write tool needs an id from this.

        **Read `budget_level` carefully.** `campaign` means this campaign holds
        the budget and its ad sets do not (Advantage campaign budget); `adset`
        means the opposite. Changing a budget at the wrong level is refused,
        and which level a given campaign uses is not guessable.

        The name match covers the whole account, not just the first page.
        `truncated: true` means more matched than `limit` returned -- narrow
        the search or raise the limit. `status` filters on effective_status,
        which is what is actually delivering.

        Every row says who its NAME says made it (`origin` agent / cli /
        manual, `owner`, `agent`, `base`) and whether the ledger shows this
        server created it (`ledger_created`); filter with origin, agent_name,
        owner or mine.
        """
        from metaads_reporting.write import campaigns as campaign_writes

        names = name_filter(origin, agent_name, owner, mine)
        currency = client.currency()
        rows, truncated = _search(campaign_writes, _account(), name_contains_text, limit, status, names)
        return found(rows, truncated, lambda r: {
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
        }, limit)

    @mcp.tool(title="Find ad sets by name", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def find_ad_sets(
        name_contains_text: Annotated[str | None, Field(description="Substring")] = None,
        campaign_id: Annotated[
            str | None, Field(description="Restrict to one campaign")
        ] = None,
        status: Annotated[
            EffectiveStatus | None, Field(description="Filter on effective_status")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        origin: OriginFilter = "any",
        agent_name: AgentFilter = None,
        owner: OwnerFilter = None,
        mine: MineFilter = False,
    ) -> dict[str, Any]:
        """Ad sets with their ids, status, budget, bid and parent campaign.

        Ad set names are unique only WITHIN a campaign -- "Broad" and
        "Lookalike 1%" appear in every campaign an account has -- so pass
        `campaign_id` whenever you know it.

        `bid_amount` is only honoured under `LOWEST_COST_WITH_BID_CAP` and
        `COST_CAP`. Under any other `bid_strategy` Meta accepts a bid write and
        ignores it, so check the strategy before proposing one.
        """
        from metaads_reporting.write import adsets as adset_writes

        names = name_filter(origin, agent_name, owner, mine)
        parent = str(campaign_id) if campaign_id else _account()
        rows, truncated = _search(adset_writes, parent, name_contains_text, limit, status, names)
        # Each row in ITS account's currency: a campaign_id can name a campaign
        # in another allowed account, and a JPY budget read as CAD is a
        # hundredfold error.
        currencies: dict[str, str] = {}

        def currency_of(r) -> str:
            account = str(r.get("account_id") or "") or None
            key = account or ""
            if key not in currencies:
                currencies[key] = client.currency(account)
            return currencies[key]

        def shape(r):
            currency = currency_of(r)
            return {
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

        return found(rows, truncated, shape, limit)

    @mcp.tool(title="Find ads by name", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def find_ads(
        name_contains_text: Annotated[str | None, Field(description="Substring")] = None,
        ad_set_id: Annotated[str | None, Field(description="Restrict to one ad set")] = None,
        campaign_id: Annotated[
            str | None, Field(description="Restrict to one campaign")
        ] = None,
        status: Annotated[
            EffectiveStatus | None, Field(description="Filter on effective_status")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        origin: OriginFilter = "any",
        agent_name: AgentFilter = None,
        owner: OwnerFilter = None,
        mine: MineFilter = False,
    ) -> dict[str, Any]:
        """Ads with their ids, status and parents.

        `effective_status` is the field to read, not `status`. `DISAPPROVED`
        and `PENDING_REVIEW` both mean the ad is not running no matter what its
        own status says, and `ADSET_PAUSED` means a parent is off.
        """
        from metaads_reporting.write import ads as ad_writes

        names = name_filter(origin, agent_name, owner, mine)
        parent = str(ad_set_id or campaign_id or "") or _account()
        rows, truncated = _search(ad_writes, parent, name_contains_text, limit, status, names)
        return found(rows, truncated, lambda r: {
            "ad_id": str(r.get("id")),
            "name": r.get("name"),
            "status": r.get("status"),
            "effective_status": r.get("effective_status"),
            "ad_set_id": str(r.get("adset_id")),
            "campaign_id": str(r.get("campaign_id")),
            "updated_time": r.get("updated_time"),
        }, limit)

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
        max_rows: Annotated[
            int, Field(description="Most rows to return", ge=1, le=5000)
        ] = 200,
        aggregate: Annotated[
            bool,
            Field(description="True: one row per entity for the whole window. False: one per day"),
        ] = True,
        action_types: Annotated[
            list[str] | None,
            Field(description="Keep only these action types in actions/action_values, e.g. ['purchase']"),
        ] = None,
        breakdowns: Annotated[
            list[Breakdown] | None,
            Field(description="Override the report's breakdowns, e.g. ['age', 'gender']"),
        ] = None,
        run_async: Annotated[
            bool,
            Field(description="Start an async report run instead; poll get_report_run"),
        ] = False,
    ) -> dict[str, Any]:
        """Account, campaign, ad set or ad performance from the insights edge.

        Today is excluded from `days` on purpose: today's metrics are partial
        all day and drag every average down. `days` is evaluated in the
        ACCOUNT's time zone (a Meta date preset where one exists).

        `max_rows` is a real cap on rows returned; `truncated: true` says
        there were more. `aggregate=False` gives a row per day, which on an
        ad-level report is a lot of rows.

        THREE THINGS TO KNOW ABOUT THESE NUMBERS:

        - **Attribution is 7-day-click plus 1-day-view** unless a report says
          otherwise. The 1-day-view half counts people who SAW an ad and never
          touched it, which is why Meta's conversion count is routinely several
          times larger than any click-based record of the same campaign.
        - **Dates are in the ACCOUNT's time zone.** See `get_account`.
        - **`reach` and `frequency` cannot be summed across rows.** The same
          person reached on two days is one person. The totals below leave them
          out for that reason.
        """
        from metaads_reporting.insights import window_for_days

        definition = get_report(report)
        filters = (
            (name_contains("campaign", campaign_name),) if campaign_name else ()
        )
        increment = None if aggregate else 1
        if since or until:
            query = definition.build(
                since=since, until=until, filtering=filters, time_increment=increment,
                breakdowns=tuple(breakdowns) if breakdowns else None,
            )
        else:
            tz = (client.account() or {}).get("timezone_name")
            window = window_for_days(days or 30, tz)
            query = definition.build(
                preset=window.get("date_preset"),
                since=window.get("since"),
                until=window.get("until"),
                filtering=filters,
                time_increment=increment,
                breakdowns=tuple(breakdowns) if breakdowns else None,
            )

        if run_async:
            from metaads_reporting.account_id import account_path
            from metaads_reporting.write import async_insights

            node = node_id or account_path(client.resolve_account_id())
            run_id = async_insights.start(client, node, query.to_params())
            return {
                "report": report,
                "report_run_id": run_id,
                "window": query.describe_window(),
                "next": "call get_report_run with this id until async_status is "
                "'Job Completed', then it returns the rows",
            }

        rows = client.insight_rows(query, node_id=node_id, max_items=max_rows + 1)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        if action_types:
            wanted = set(action_types)
            for row in rows:
                for key in ("actions", "action_values", "cost_per_action_type"):
                    if isinstance(row.get(key), list):
                        row[key] = [
                            e for e in row[key]
                            if isinstance(e, dict) and e.get("action_type") in wanted
                        ]
        return {
            "report": report,
            "level": definition.level,
            "breakdowns": list(query.breakdowns),
            "window": query.describe_window(),
            "aggregate": aggregate,
            "attribution_windows": list(query.attribution_windows),
            "currency": client.currency(),
            "rows_returned": len(rows),
            "truncated": truncated,
            "totals": summarise(rows),
            "rows": rows,
            "rate_limit": headroom(),
            "note": (
                "Totals omit reach and frequency: they cannot be summed across "
                "rows. Use the creative_fatigue report, which collapses the "
                "window into one row per ad, when frequency is the question."
            ),
        }

    @mcp.tool(title="Poll an async report run", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def get_report_run(
        report_run_id: Annotated[str, Field(description="From run_report(run_async=True)")],
        max_rows: Annotated[int, Field(ge=1, le=5000)] = 500,
    ) -> dict[str, Any]:
        """Where an async report is, and its rows once it is done.

        `async_status` reaches "Job Completed" at 100%; "Job Failed" or "Job
        Skipped" means start it again, narrower. Money in the rows is already in
        major units (insights always are).
        """
        from metaads_reporting.write import async_insights

        state_ = async_insights.status(client, report_run_id)
        out: dict[str, Any] = {
            "report_run_id": report_run_id,
            "async_status": state_.get("async_status"),
            "async_percent_completion": state_.get("async_percent_completion"),
            "rate_limit": headroom(),
        }
        if state_.get("async_status") == "Job Completed":
            rows = async_insights.rows(client, report_run_id, max_items=max_rows + 1)
            out.update(rows=rows[:max_rows], rows_returned=min(len(rows), max_rows),
                       truncated=len(rows) > max_rows)
        return out

    @mcp.tool(title="Read an ad's creative and its current copy", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def get_ad_creative(
        ad_id: Annotated[str, Field(description="From find_ads")],
    ) -> dict[str, Any]:
        """What an ad currently says, and what shape its creative is.

        **Read this before proposing any copy change.** The shape decides what
        can be edited and how:

        - `link_data` / `video_data` — one value per field.
        - `asset_feed_spec` — a LIST per field, one entry per locale. A single
          new value is applied to every locale; pass a list to set them
          individually, matching the existing length.
        - `page_only` — a catalog ad. Its copy comes from the product feed, so
          there is nothing on the creative to change.

        `social_proof` is the likes, comments and shares that
        `apply_update_ad_copy` would discard. If it is non-zero, say so before
        proposing that over `apply_duplicate_ad`.
        """
        from metaads_reporting.write import creatives

        ad = _as_write_shim(client).node(
            str(ad_id), ("id", "name", "status", "effective_status", "adset_id",
                         "campaign_id", "creative")
        )
        creative_id = (ad.get("creative") or {}).get("id")
        if not creative_id:
            return {
                "ad_id": str(ad_id),
                "name": ad.get("name"),
                "creative": None,
                "note": "This ad exposes no creative; it may be dynamic or catalog.",
            }
        creative = creatives.read(_as_write_shim(client), creative_id)
        out = creative.describe()
        out.update(
            ad_id=str(ad_id),
            ad_name=ad.get("name"),
            effective_status=ad.get("effective_status"),
            social_proof=creatives.social_proof(_as_write_shim(client), creative),
            rate_limit=headroom(),
        )
        return out

    @mcp.tool(title="List audiences on the account", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def list_audiences(
        kind: Annotated[
            str, Field(description="'custom', 'saved' or 'all'")
        ] = "all",
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        unused_only: Annotated[
            bool,
            Field(description="Only custom audiences no ad set includes or excludes"),
        ] = False,
        origin: OriginFilter = "any",
        agent_name: AgentFilter = None,
        owner: OwnerFilter = None,
        mine: MineFilter = False,
    ) -> dict[str, Any]:
        """Audiences that already exist: sizes, status, usability, and use.

        Reading a customer-list audience is fine and often necessary — you need
        to see one to build a lookalike from it. CREATING one is what is
        refused, because that would upload personal data.
        """
        from metaads_reporting.write import audiences

        from metaads_reporting.naming import attribution

        names = name_filter(origin, agent_name, owner, mine)
        shim = _as_write_shim(client)
        wanted = (kind or "all").strip().lower()
        # A naming filter drops rows locally, so it reads deeper first.
        depth = 200 if names.active else limit + 1

        def named(rows):
            rows = [{**r, **attribution(state.identity, r.get("name"))} for r in rows]
            return [r for r in rows if names.matches(r.get("name"))]

        out: dict[str, Any] = {}
        if wanted in ("custom", "all"):
            custom = named(audiences.list_custom(shim, limit=depth))
            used, complete = state.audience_usage()
            for row in custom:
                row["used_by_ad_sets"] = len(used.get(row["audience_id"], []))
            if unused_only:
                custom = [r for r in custom if not r["used_by_ad_sets"]]
            out["custom"] = custom[:limit]
            out["custom_truncated"] = len(custom) > limit
            out["usage_scan_complete"] = complete
        if wanted in ("saved", "all"):
            out["saved"] = named(audiences.list_saved(shim, limit=depth))[:limit]
        out["rate_limit"] = headroom()
        out["note"] = (
            "`usable` says whether Meta can deliver to a custom audience yet "
            "(None: an unmapped status, quoted verbatim). Audiences are never "
            "deleted here; an unused one changes nothing. A customer-list "
            "audience must be made in Ads Manager, where a person accepts the "
            "data terms."
        )
        return out

    @mcp.tool(
        title="Show the write guardrails and session budget", annotations=LOCAL_ONLY
    )
    @refusals_reach_the_caller
    def get_guardrails() -> dict[str, Any]:
        """The bounds every apply_* is checked against, and what is left.

        Call this before proposing a change, so you propose something that can
        actually be applied rather than something that will be refused.
        """
        from metaads_mcp.guardrails import DailyUsage, read_only_mode, writes_disabled
        from metaads_reporting.token_health import token_health

        read_only = read_only_mode()
        return {
            "limits": state.limits.describe(),
            "session": state.counters.snapshot(),
            "today": DailyUsage(state.limits).snapshot(),
            "token": token_health(client),
            "writes_disabled": writes_disabled(),
            "read_only_server": read_only,
            "pending_previews": state.previews.pending_count,
            # Who names what this server creates, from the repo's .env only.
            # Each part is used only when set; both empty means plain names.
            "identity": state.identity.describe(),
            "note": (
                "THIS SERVER IS READ-ONLY: it published no apply_* tools and no "
                "revert_change, so nothing here can change the account. The "
                "preview_* tools still work -- use them to show a human exactly "
                "what you would change, and let them make it from a writable "
                "client or Ads Manager."
                if read_only
                else "Session counters reset when the server restarts; the "
                "daily ones (`today`) are read from the ledger and reset at "
                "local midnight."
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

    from metaads_mcp import tools_account

    tools_account.register(
        mcp, state, client,
        headroom=headroom, money=money, read_only=READ_ONLY, local_only=LOCAL_ONLY,
    )


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
