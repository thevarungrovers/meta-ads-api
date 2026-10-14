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
  - creating or deleting campaigns, ad sets and ads. A campaign create carries
    an objective, a budget and a special-ad-category declaration that is a
    legal statement; it belongs in ``scripts/manage.py``, where a human answers
    for it. The mutating client has no delete method at all.
  - swapping an ad's creative. A Meta creative is immutable once attached, so
    changing what an ad says is a CREATE, not an edit.
  - targeting, optimization goal, billing event, schedule, special ad
    categories. None is on the write client's field allowlist.

Enforcement is structural: there is no generic ``update(node, fields)``
passthrough, so an unregistered operation is unreachable rather than merely
undocumented. ``tests/test_mcp_surface.py`` pins the registered tool names.
"""

from __future__ import annotations

import functools
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from metaads_mcp import ledger
from metaads_mcp.guardrails import (
    Check,
    WritesDisabled,
    any_failed,
    check_account_in_scope,
    check_campaign_in_scope,
    check_ceiling,
    check_currency,
    check_pct_increase,
    check_projected_delta,
    check_writes_enabled,
    read_only_mode,
    render_checks,
    require_writes_enabled,
)
from metaads_mcp.previews import (
    ApplyResult,
    ChangePreview,
    PreviewExpired,
    PreviewStale,
    pct_change,
    project_bid_change,
    project_budget_change,
    project_pause,
)
from metaads_reporting.currency import CurrencyError, format_money, from_minor_units
from metaads_reporting.graph import GraphError
from metaads_reporting.insights import InsightsQuery, last_n_days
from metaads_reporting.write import (
    MutatingMetaAdsClient,
    adsets,
    ads,
    audiences,
    campaigns,
    creatives,
    media,
)
from metaads_reporting.write.audiences import CustomerDataRefused
from metaads_reporting.write.client import MutationError
from metaads_reporting.write.plan import execute, execute_chain

#: Exceptions that mean "the tool refused on purpose", as opposed to "the tool
#: crashed". The distinction is load-bearing: the MCP runtime carries a
#: ToolError's message through to the caller as
#: "Error executing tool <name>: <message>", but replaces every other exception
#: with a bare "Error executing tool <name>" and keeps the detail server-side.
#: Raising ValueError for a guardrail refusal therefore tells the agent nothing
#: at all -- it cannot see which bound it broke, so it cannot propose something
#: smaller, and will most likely try the same call again.
DELIBERATE_REFUSALS = (
    ValueError,
    LookupError,
    PreviewExpired,
    PreviewStale,
    MutationError,
    CustomerDataRefused,
    WritesDisabled,
    GraphError,
    CurrencyError,
)


def refusals_reach_the_caller(fn):
    """Re-raise a deliberate refusal as ToolError so its message survives."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except DELIBERATE_REFUSALS as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


# All four hints here too -- see the note in tools_read.py for why an omitted
# hint is worse than a wrong one.
#
# A preview reads the account, mints a token in memory and writes nothing, so it
# is safe to allowlist. The token is single-use, but calling a preview twice just
# produces a second token and changes nothing anybody can observe at Meta's end,
# so idempotent_hint stays True.
PREVIEW_SAFE = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

#: destructive_hint is set only where the change moves money or stops delivery.
APPLY_SPENDS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)

#: Pausing one ad, and reverting, are writes but not new risks. Both are
#: trivially reversible and neither can increase spend.
APPLY_REVERSIBLE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

SETTABLE_STATUSES = ("ACTIVE", "PAUSED")


def parse_decimal(raw: str, name: str, currency: str) -> Decimal:
    """Money arrives as a decimal string in MAJOR units, never minor ones.

    The argument is rendered verbatim in the prompt a human approves, so "25.00"
    has to mean twenty-five. Accepting a float here would also let 0.1 + 0.2
    arrive as 0.30000000000000004 in the approval text.

    The upper bound is the point: ``2500`` is what you get typing ``25.00`` as
    the integer Meta actually wants. It is a valid Decimal and a catastrophic
    budget, so it is caught before the ceiling check, where the message can say
    what went wrong rather than just "too big".
    """
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(
            f"{name} must be a decimal string in the account's currency, "
            f'e.g. "25.00" -- got {raw!r}. Never pass minor units.'
        ) from exc
    if value < 0:
        raise ValueError(f"{name} cannot be negative, got {value}.")
    if value > Decimal("100000"):
        raise ValueError(
            f"{name} of {value} looks like minor units, not currency. Meta "
            f"stores budgets as an integer in the currency's minor unit, but "
            f"this tool takes the major-unit amount and converts it. If you "
            f"meant {value / 100}, pass that."
        )
    return value


def register(mcp, state) -> None:  # noqa: C901 - a flat list of tool definitions
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
    state.write_client = write_client
    default_account = state.default_account_id

    # -- shared helpers ---------------------------------------------------

    def currency_of() -> str:
        return write_client.currency()

    def metrics_7d(node_id: str, level: str) -> dict[str, Any]:
        """Last 7 whole days for one entity, for context in the preview.

        Collapsed into ONE row (no ``time_increment``) rather than summed from
        daily rows, because ``reach`` and ``frequency`` cannot be added across
        days -- the same person reached twice is one person. Asking Meta for
        the aggregate is the only way to get a frequency that means anything.
        """
        window = last_n_days(7)
        query = InsightsQuery(
            fields=(
                "spend",
                "impressions",
                "clicks",
                "reach",
                "frequency",
                "ctr",
                "cpc",
            ),
            level=level,
            since=window["since"],
            until=window["until"],
            time_increment=None,
            limit=5,
        )
        try:
            rows = write_client.read.insight_rows(query, node_id=str(node_id))
        except Exception as exc:  # noqa: BLE001
            # Context, not correctness. A preview whose metrics lookup failed is
            # still worth showing -- with the failure named, so nobody reads a
            # missing section as "it spent nothing".
            return {"unavailable": f"{type(exc).__name__}: {exc}"}
        if not rows:
            return {"note": "no delivery in the last 7 days"}
        row = rows[0]
        return {
            "spend": row.get("spend"),
            "impressions": row.get("impressions"),
            "clicks": row.get("clicks"),
            "reach": row.get("reach"),
            "frequency": row.get("frequency"),
            "ctr": row.get("ctr"),
            "cpc": row.get("cpc"),
            "window": f"{window['since']} to {window['until']}",
        }

    def clicks_7d(metrics: dict[str, Any]) -> int:
        try:
            return int(Decimal(str(metrics.get("clicks") or 0)))
        except (InvalidOperation, ValueError, TypeError):
            return 0

    def build_preview(
        *,
        tool: str,
        entity_type: str,
        entity_id: str,
        entity_name: str,
        path: str,
        field_name: str,
        before: str,
        after: str,
        checks: list[Check],
        projected: Decimal,
        payload: dict[str, Any],
        currency: str,
        status: str | None = None,
        effective_status: str | None = None,
        metrics: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        note: str | None = None,
    ) -> ChangePreview:
        blocked = any_failed(checks)
        token = state.previews.mint(
            tool=tool, payload=payload, projected_delta=projected, blocked=blocked
        )
        return ChangePreview(
            tool=tool,
            preview_token=token,
            expires_in_seconds=600,
            entity_type=entity_type,
            entity_id=str(entity_id),
            entity_name=entity_name,
            path=path,
            field_name=field_name,
            before=before,
            after=after,
            account_id=default_account,
            currency=currency,
            status=status,
            effective_status=effective_status,
            metrics_7d=metrics,
            projected_daily_spend_delta=f"{projected:.2f}",
            checks=render_checks(checks),
            blocked=blocked,
            warnings=warnings or [],
            note=note,
        )

    def commit(
        pending, *, tool: str, mutate, entity: dict[str, Any], current_before: str
    ) -> ApplyResult:
        """Kill switch, staleness check, session check, mutate, ledger."""
        # The kill switch FIRST, re-read here and never cached. The preview ran
        # check_writes_enabled() too, but that was up to ten minutes ago, and
        # the switch is a file precisely so somebody can throw it from another
        # terminal mid-session. Consulting it only at mint time would let every
        # outstanding preview through after it was set -- the one moment it has
        # to work.
        require_writes_enabled()
        payload = pending.payload
        if payload["before"] != current_before:
            raise PreviewStale(
                f"{entity['entity_name']} has changed since the preview: it was "
                f"{payload['before']!r} then and is {current_before!r} now. "
                "Someone edited it in between -- Ads Manager is the usual "
                "culprit. Take a fresh preview."
            )
        blocked = state.counters.would_exceed(pending.projected_delta)
        if blocked:
            raise PreviewExpired(blocked)

        entry_id = ledger.new_entry_id()
        ledger.write_intent(
            entry_id=entry_id,
            tool=tool,
            account_id=default_account,
            entity_type=entity["entity_type"],
            entity_id=str(entity["entity_id"]),
            entity_name=entity["entity_name"],
            field_name=entity["field_name"],
            before=payload["before"],
            after=payload["after"],
            projected_delta=str(pending.projected_delta),
        )
        try:
            result = mutate()
        except Exception as exc:
            ledger.write_outcome(entry_id=entry_id, ok=False, error=str(exc))
            raise
        if not result.success:
            # Meta answers an update with {"success": true}. Anything else means
            # the request was accepted and the change was not made, which must
            # not be recorded as applied.
            message = (
                f"Meta accepted the request but did not confirm the change: "
                f"{result.response!r}. Nothing has been recorded as applied. "
                f"Check the object in Ads Manager before retrying."
            )
            ledger.write_outcome(entry_id=entry_id, ok=False, error=message)
            raise MutationError(message)

        ledger.write_outcome(
            entry_id=entry_id, ok=True, resource_name=str(entity["entity_id"])
        )
        state.counters.record(pending.projected_delta)

        return ApplyResult(
            applied=True,
            entry_id=entry_id,
            entity_type=entity["entity_type"],
            entity_id=str(entity["entity_id"]),
            entity_name=entity["entity_name"],
            field_name=entity["field_name"],
            before=payload["before"],
            after=payload["after"],
            session=state.counters.snapshot(),
        )

    def status_checks(campaign_id: str | None, projected: Decimal) -> list[Check]:
        return [
            check_writes_enabled(),
            check_account_in_scope(state.limits, default_account, default_account),
            check_campaign_in_scope(state.limits, campaign_id),
            check_projected_delta(state.limits, projected),
        ]

    # -- campaign status --------------------------------------------------

    @mcp.tool(title="Preview pausing or activating a campaign", annotations=PREVIEW_SAFE)
    @refusals_reach_the_caller
    def preview_campaign_status(
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        new_status: Annotated[str, Field(description="ACTIVE or PAUSED")],
    ) -> ChangePreview:
        """Show what pausing or activating a campaign would do. Writes nothing.

        This is the widest change available here: it stops or starts every ad
        set and ad underneath.
        """
        wanted = new_status.upper()
        if wanted not in SETTABLE_STATUSES:
            raise ValueError(
                f"new_status must be ACTIVE or PAUSED, got {new_status!r}. "
                "DELETED and ARCHIVED are not available here: both are "
                "irreversible through the API, and PAUSED achieves the same "
                "thing reversibly."
            )
        row = campaigns.find(write_client, campaign_id=campaign_id)
        currency = currency_of()
        before = row.get("status") or ""
        metrics = metrics_7d(str(row["id"]), "campaign")

        if wanted == "PAUSED":
            projected = project_pause()
        else:
            # Re-activating can resume spend up to the budget. On a campaign
            # that holds no budget of its own, the ad sets do -- and this cannot
            # bound what they add up to without reading all of them, so it falls
            # back to the last 7 days' own spend, and the note says so.
            budget = from_minor_units(row.get("daily_budget"), currency)
            projected = budget if budget is not None else Decimal(
                str(metrics.get("spend") or 0)
            ) / 7

        checks = status_checks(str(row["id"]), projected)
        warnings = []
        if before == wanted:
            warnings.append(f"already {wanted}; applying would be a no-op")
        if wanted == "ACTIVE":
            warnings.append(
                "activating resumes delivery immediately for every ACTIVE ad "
                "set and ad underneath"
            )
        level = campaigns.budget_level(row)
        if wanted == "ACTIVE" and level == "adset":
            warnings.append(
                "this campaign's budgets live on its ad sets, so the projected "
                "delta below is an estimate from past spend, not a cap"
            )

        return build_preview(
            tool="apply_campaign_status",
            entity_type="campaign",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=row.get("name") or "",
            field_name="status",
            before=before,
            after=wanted,
            checks=checks,
            projected=projected,
            currency=currency,
            metrics=metrics,
            status=before,
            effective_status=row.get("effective_status"),
            warnings=warnings,
            payload={
                "campaign_id": str(row["id"]),
                "before": before,
                "after": wanted,
                "name": row.get("name") or "",
            },
            note=(
                "Pausing cannot increase spend, so its projected delta is 0. "
                "Activating is bounded by the budget, not by past spend."
            ),
        )

    @apply_tool(
        title="Apply a campaign status change (AFFECTS DELIVERY)",
        annotations=APPLY_SPENDS,
    )
    @refusals_reach_the_caller
    def apply_campaign_status(
        preview_token: Annotated[str, Field(description="From preview_campaign_status")],
    ) -> ApplyResult:
        """Pause or activate the campaign the preview named."""
        pending = state.previews.take(preview_token, "apply_campaign_status")
        payload = pending.payload
        row = campaigns.find(write_client, campaign_id=payload["campaign_id"])
        return commit(
            pending,
            tool="apply_campaign_status",
            current_before=row.get("status") or "",
            entity={
                "entity_type": "campaign",
                "entity_id": payload["campaign_id"],
                "entity_name": payload["name"],
                "field_name": "status",
            },
            mutate=lambda: execute(
                write_client,
                campaigns.plan_set_status(
                    write_client,
                    campaign_id=payload["campaign_id"],
                    status=payload["after"],
                ),
                apply=True,
            ),
        )

    # -- campaign daily budget --------------------------------------------

    @mcp.tool(
        title="Preview a campaign daily budget change", annotations=PREVIEW_SAFE
    )
    @refusals_reach_the_caller
    def preview_campaign_daily_budget(
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        new_daily_budget: Annotated[
            str, Field(description='Decimal string in account currency, e.g. "250.00"')
        ],
    ) -> ChangePreview:
        """Show what changing a CBO campaign's daily budget would do. Writes nothing.

        Only works on a campaign whose `budget_level` is `campaign`. On an
        ad-set-budget campaign the budget is one level down and this refuses,
        because setting one here would convert the campaign to campaign-budget
        optimization and redistribute spend across every ad set under it.

        The delta is EXACT, not an estimate: a daily budget is the cap itself
        moving, not a guess about behaviour.
        """
        currency = currency_of()
        after = parse_decimal(new_daily_budget, "new_daily_budget", currency)
        row = campaigns.find(write_client, campaign_id=campaign_id)

        level = campaigns.budget_level(row)
        if level != "campaign":
            raise ValueError(
                f"Campaign {row.get('name')!r} does not hold its own budget -- "
                f"its ad sets do. Use find_ad_sets to get an ad set id, then "
                f"preview_ad_set_daily_budget. Setting a budget here would "
                f"convert the campaign to campaign-budget optimization and "
                f"redistribute spend across every ad set under it."
            )
        using = campaigns.budget_field(row)
        if using != "daily_budget":
            raise ValueError(
                f"Campaign {row.get('name')!r} is on a LIFETIME budget, not a "
                f"daily one. Switching between them also requires changing the "
                f"schedule, which is not exposed here."
            )

        before = from_minor_units(row.get("daily_budget"), currency)
        metrics = metrics_7d(str(row["id"]), "campaign")
        ad_set_count = len(
            adsets.search(write_client, None, campaign_id=str(row["id"]), limit=200)
        )

        projected = project_budget_change(before, after)
        checks = [
            check_writes_enabled(),
            check_account_in_scope(state.limits, default_account, default_account),
            check_campaign_in_scope(state.limits, str(row["id"])),
            check_currency(state.limits, currency),
            check_ceiling(
                "max_daily_budget", after, state.limits.max_daily_budget, currency
            ),
            check_pct_increase(
                "max_budget_change_pct",
                before,
                after,
                state.limits.max_budget_change_pct,
            ),
            check_projected_delta(state.limits, max(projected, Decimal("0"))),
        ]
        warnings = [
            f"this is a CAMPAIGN budget shared by {ad_set_count} ad set(s) -- "
            f"the change moves spend for all of them, not one"
        ]
        if before and after < before:
            warnings.append(
                "lowering a daily budget can stop delivery part-way through "
                "today if the campaign has already spent more than the new cap"
            )
        if before and after > before * 2:
            warnings.append(
                "more than doubling a budget re-enters the learning phase on "
                "most optimization goals; performance is unstable until it exits"
            )

        return build_preview(
            tool="apply_campaign_daily_budget",
            entity_type="campaign",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=f"{row.get('name')} (campaign budget)",
            field_name="daily_budget",
            before=format_money(before, currency),
            after=format_money(after, currency),
            checks=checks,
            projected=projected,
            currency=currency,
            metrics=metrics,
            status=row.get("status"),
            effective_status=row.get("effective_status"),
            warnings=warnings,
            payload={
                "campaign_id": str(row["id"]),
                "before": format_money(before, currency),
                "after": format_money(after, currency),
                "after_amount": str(after),
                "name": row.get("name") or "",
            },
            note=(
                f"{pct_change(before, after) or 'n/a'} change. Compare the delta "
                "with the last 7 days' spend: if the campaign never reached the "
                "old cap, raising it may change nothing."
            ),
        )

    @apply_tool(
        title="Apply a campaign daily budget change (SPENDS MONEY)",
        annotations=APPLY_SPENDS,
    )
    @refusals_reach_the_caller
    def apply_campaign_daily_budget(
        preview_token: Annotated[
            str, Field(description="From preview_campaign_daily_budget")
        ],
    ) -> ApplyResult:
        """Change the campaign daily budget the preview named."""
        pending = state.previews.take(preview_token, "apply_campaign_daily_budget")
        payload = pending.payload
        currency = currency_of()
        row = campaigns.find(write_client, campaign_id=payload["campaign_id"])
        current = from_minor_units(row.get("daily_budget"), currency)
        return commit(
            pending,
            tool="apply_campaign_daily_budget",
            current_before=format_money(current, currency),
            entity={
                "entity_type": "campaign",
                "entity_id": payload["campaign_id"],
                "entity_name": payload["name"],
                "field_name": "daily_budget",
            },
            mutate=lambda: execute(
                write_client,
                campaigns.plan_set_daily_budget(
                    write_client,
                    campaign_id=payload["campaign_id"],
                    amount=Decimal(payload["after_amount"]),
                ),
                apply=True,
            ),
        )

    # -- ad set status -----------------------------------------------------

    @mcp.tool(title="Preview pausing or activating an ad set", annotations=PREVIEW_SAFE)
    @refusals_reach_the_caller
    def preview_ad_set_status(
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
        new_status: Annotated[str, Field(description="ACTIVE or PAUSED")],
    ) -> ChangePreview:
        """Show what pausing or activating an ad set would do. Writes nothing."""
        wanted = new_status.upper()
        if wanted not in SETTABLE_STATUSES:
            raise ValueError(
                f"new_status must be ACTIVE or PAUSED, got {new_status!r}. "
                "DELETED and ARCHIVED are not available here."
            )
        row = adsets.find(write_client, ad_set_id=ad_set_id)
        currency = currency_of()
        before = row.get("status") or ""
        metrics = metrics_7d(str(row["id"]), "adset")

        if wanted == "PAUSED":
            projected = project_pause()
        else:
            budget = from_minor_units(row.get("daily_budget"), currency)
            projected = budget if budget is not None else Decimal(
                str(metrics.get("spend") or 0)
            ) / 7

        checks = status_checks(str(row.get("campaign_id")), projected)
        warnings = []
        if before == wanted:
            warnings.append(f"already {wanted}; applying would be a no-op")
        effective = row.get("effective_status")
        if wanted == "ACTIVE" and effective == "CAMPAIGN_PAUSED":
            warnings.append(
                "the parent CAMPAIGN is paused, so this changes nothing "
                "visible until that is activated too"
            )
        elif wanted == "ACTIVE":
            warnings.append("activating resumes delivery immediately")
        if from_minor_units(row.get("daily_budget"), currency) is None:
            warnings.append(
                "this ad set has no budget of its own -- its campaign holds it "
                "-- so the projected delta is an estimate from past spend"
            )

        return build_preview(
            tool="apply_ad_set_status",
            entity_type="ad set",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=f"campaign {row.get('campaign_id')} > {row.get('name')}",
            field_name="status",
            before=before,
            after=wanted,
            checks=checks,
            projected=projected,
            currency=currency,
            metrics=metrics,
            status=before,
            effective_status=effective,
            warnings=warnings,
            payload={
                "ad_set_id": str(row["id"]),
                "before": before,
                "after": wanted,
                "name": row.get("name") or "",
            },
            note="Pausing cannot increase spend, so its projected delta is 0.",
        )

    @apply_tool(
        title="Apply an ad set status change (AFFECTS DELIVERY)",
        annotations=APPLY_SPENDS,
    )
    @refusals_reach_the_caller
    def apply_ad_set_status(
        preview_token: Annotated[str, Field(description="From preview_ad_set_status")],
    ) -> ApplyResult:
        """Pause or activate the ad set the preview named."""
        pending = state.previews.take(preview_token, "apply_ad_set_status")
        payload = pending.payload
        row = adsets.find(write_client, ad_set_id=payload["ad_set_id"])
        return commit(
            pending,
            tool="apply_ad_set_status",
            current_before=row.get("status") or "",
            entity={
                "entity_type": "ad set",
                "entity_id": payload["ad_set_id"],
                "entity_name": payload["name"],
                "field_name": "status",
            },
            mutate=lambda: execute(
                write_client,
                adsets.plan_set_status(
                    write_client,
                    ad_set_id=payload["ad_set_id"],
                    status=payload["after"],
                ),
                apply=True,
            ),
        )

    # -- ad set daily budget ----------------------------------------------

    @mcp.tool(title="Preview an ad set daily budget change", annotations=PREVIEW_SAFE)
    @refusals_reach_the_caller
    def preview_ad_set_daily_budget(
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
        new_daily_budget: Annotated[
            str, Field(description='Decimal string in account currency, e.g. "50.00"')
        ],
    ) -> ChangePreview:
        """Show what changing an ad set's daily budget would do. Writes nothing.

        Refuses when the CAMPAIGN holds the budget, because then this ad set has
        none to change.
        """
        currency = currency_of()
        after = parse_decimal(new_daily_budget, "new_daily_budget", currency)
        row = adsets.find(write_client, ad_set_id=ad_set_id)

        using = campaigns.budget_field(row)
        if using is None:
            raise ValueError(
                f"Ad set {row.get('name')!r} has no budget of its own -- its "
                f"CAMPAIGN holds it (Advantage campaign budget). Use "
                f"preview_campaign_daily_budget on campaign "
                f"{row.get('campaign_id')} instead."
            )
        if using != "daily_budget":
            raise ValueError(
                f"Ad set {row.get('name')!r} is on a LIFETIME budget, not a "
                f"daily one. Switching between them also requires changing the "
                f"schedule, which is not exposed here."
            )

        before = from_minor_units(row.get("daily_budget"), currency)
        metrics = metrics_7d(str(row["id"]), "adset")
        projected = project_budget_change(before, after)

        checks = [
            check_writes_enabled(),
            check_account_in_scope(state.limits, default_account, default_account),
            check_campaign_in_scope(state.limits, str(row.get("campaign_id"))),
            check_currency(state.limits, currency),
            check_ceiling(
                "max_daily_budget", after, state.limits.max_daily_budget, currency
            ),
            check_pct_increase(
                "max_budget_change_pct",
                before,
                after,
                state.limits.max_budget_change_pct,
            ),
            check_projected_delta(state.limits, max(projected, Decimal("0"))),
        ]
        warnings = []
        if before and after < before:
            warnings.append(
                "lowering a daily budget can stop delivery part-way through "
                "today if the ad set has already spent more than the new cap"
            )
        if before and after > before * 2:
            warnings.append(
                "more than doubling a budget re-enters the learning phase on "
                "most optimization goals; performance is unstable until it exits"
            )

        return build_preview(
            tool="apply_ad_set_daily_budget",
            entity_type="ad set",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=f"campaign {row.get('campaign_id')} > {row.get('name')}",
            field_name="daily_budget",
            before=format_money(before, currency),
            after=format_money(after, currency),
            checks=checks,
            projected=projected,
            currency=currency,
            metrics=metrics,
            status=row.get("status"),
            effective_status=row.get("effective_status"),
            warnings=warnings,
            payload={
                "ad_set_id": str(row["id"]),
                "before": format_money(before, currency),
                "after": format_money(after, currency),
                "after_amount": str(after),
                "name": row.get("name") or "",
            },
            note=(
                f"{pct_change(before, after) or 'n/a'} change. Compare the delta "
                "with the last 7 days' spend: if the ad set never reached the "
                "old cap, raising it may change nothing."
            ),
        )

    @apply_tool(
        title="Apply an ad set daily budget change (SPENDS MONEY)",
        annotations=APPLY_SPENDS,
    )
    @refusals_reach_the_caller
    def apply_ad_set_daily_budget(
        preview_token: Annotated[
            str, Field(description="From preview_ad_set_daily_budget")
        ],
    ) -> ApplyResult:
        """Change the ad set daily budget the preview named."""
        pending = state.previews.take(preview_token, "apply_ad_set_daily_budget")
        payload = pending.payload
        currency = currency_of()
        row = adsets.find(write_client, ad_set_id=payload["ad_set_id"])
        current = from_minor_units(row.get("daily_budget"), currency)
        return commit(
            pending,
            tool="apply_ad_set_daily_budget",
            current_before=format_money(current, currency),
            entity={
                "entity_type": "ad set",
                "entity_id": payload["ad_set_id"],
                "entity_name": payload["name"],
                "field_name": "daily_budget",
            },
            mutate=lambda: execute(
                write_client,
                adsets.plan_set_daily_budget(
                    write_client,
                    ad_set_id=payload["ad_set_id"],
                    amount=Decimal(payload["after_amount"]),
                ),
                apply=True,
            ),
        )

    # -- ad set bid amount -------------------------------------------------

    @mcp.tool(title="Preview an ad set bid change", annotations=PREVIEW_SAFE)
    @refusals_reach_the_caller
    def preview_ad_set_bid_amount(
        ad_set_id: Annotated[str, Field(description="From find_ad_sets")],
        new_bid_amount: Annotated[
            str, Field(description='Decimal string in account currency, e.g. "3.50"')
        ],
    ) -> ChangePreview:
        """Show what changing an ad set's bid cap or cost target would do.

        Writes nothing. Refuses under a bid strategy that does not use a bid --
        and that refusal is the main reason this tool exists. Under
        `LOWEST_COST_WITHOUT_CAP`, which is Meta's default and what most ad sets
        are on, Meta ACCEPTS a `bid_amount` write and ignores it. Without the
        check, a human would be told their bid moved when nothing happened.
        """
        currency = currency_of()
        after = parse_decimal(new_bid_amount, "new_bid_amount", currency)
        row = adsets.find(write_client, ad_set_id=ad_set_id)
        strategy = row.get("bid_strategy") or "(not set)"

        usable = adsets.STRATEGIES_USING_BID | adsets.STRATEGIES_USING_TARGET
        if strategy not in usable:
            raise ValueError(
                f"Ad set {row.get('name')!r} is on bid strategy {strategy}, "
                f"which does not use bid_amount. Meta would ACCEPT this write "
                f"and ignore it, so it would be reported as applied while "
                f"nothing changed. bid_amount is used under: "
                f"{', '.join(sorted(usable))}."
            )

        before = from_minor_units(row.get("bid_amount"), currency)
        metrics = metrics_7d(str(row["id"]), "adset")
        projected = project_bid_change(before, after, clicks_7d(metrics))

        checks = [
            check_writes_enabled(),
            check_account_in_scope(state.limits, default_account, default_account),
            check_campaign_in_scope(state.limits, str(row.get("campaign_id"))),
            check_currency(state.limits, currency),
            check_ceiling("max_bid_amount", after, state.limits.max_bid_amount, currency),
            check_pct_increase(
                "max_bid_increase_pct", before, after, state.limits.max_bid_increase_pct
            ),
            check_projected_delta(state.limits, max(projected, Decimal("0"))),
        ]

        meaning = (
            "a CAP: delivery will not bid above it"
            if strategy in adsets.STRATEGIES_USING_BID
            else "a TARGET: Meta aims for this as an average and may exceed it "
            "on an individual result"
        )
        warnings = [f"under {strategy} this amount is {meaning}"]
        if before and after < before:
            warnings.append(
                "lowering a bid cap usually reduces delivery volume as well as "
                "cost -- the ad set may simply stop winning auctions"
            )

        return build_preview(
            tool="apply_ad_set_bid_amount",
            entity_type="ad set",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=f"campaign {row.get('campaign_id')} > {row.get('name')}",
            field_name="bid_amount",
            before=format_money(before, currency),
            after=format_money(after, currency),
            checks=checks,
            projected=projected,
            currency=currency,
            metrics=metrics,
            status=row.get("status"),
            effective_status=row.get("effective_status"),
            warnings=warnings,
            payload={
                "ad_set_id": str(row["id"]),
                "before": format_money(before, currency),
                "after": format_money(after, currency),
                "after_amount": str(after),
                "name": row.get("name") or "",
            },
            note=(
                "The projected delta assumes the same click volume at the new "
                "bid. It is a bound to reason with, not a forecast -- and a cap "
                "is not a price, so raising one changes nothing unless the "
                "auction was already pressing against it."
            ),
        )

    @apply_tool(
        title="Apply an ad set bid change (SPENDS MONEY)", annotations=APPLY_SPENDS
    )
    @refusals_reach_the_caller
    def apply_ad_set_bid_amount(
        preview_token: Annotated[
            str, Field(description="From preview_ad_set_bid_amount")
        ],
    ) -> ApplyResult:
        """Change the bid the preview named."""
        pending = state.previews.take(preview_token, "apply_ad_set_bid_amount")
        payload = pending.payload
        currency = currency_of()
        row = adsets.find(write_client, ad_set_id=payload["ad_set_id"])
        current = from_minor_units(row.get("bid_amount"), currency)
        return commit(
            pending,
            tool="apply_ad_set_bid_amount",
            current_before=format_money(current, currency),
            entity={
                "entity_type": "ad set",
                "entity_id": payload["ad_set_id"],
                "entity_name": payload["name"],
                "field_name": "bid_amount",
            },
            mutate=lambda: execute(
                write_client,
                adsets.plan_set_bid_amount(
                    write_client,
                    ad_set_id=payload["ad_set_id"],
                    amount=Decimal(payload["after_amount"]),
                ),
                apply=True,
            ),
        )

    # -- ad status ---------------------------------------------------------

    @mcp.tool(title="Preview pausing or activating one ad", annotations=PREVIEW_SAFE)
    @refusals_reach_the_caller
    def preview_ad_status(
        ad_id: Annotated[str, Field(description="From find_ads")],
        new_status: Annotated[str, Field(description="ACTIVE or PAUSED")],
    ) -> ChangePreview:
        """Show what pausing or activating one ad would do. Writes nothing.

        The narrowest change available, and the usual fix for a worn-out
        creative: pausing one ad leaves the ad set's budget and learning intact
        and is completely reversible.
        """
        wanted = new_status.upper()
        if wanted not in SETTABLE_STATUSES:
            raise ValueError(
                f"new_status must be ACTIVE or PAUSED, got {new_status!r}. "
                "DELETED and ARCHIVED are not available here."
            )
        row = ads.find(write_client, ad_id=ad_id)
        currency = currency_of()
        before = row.get("status") or ""
        metrics = metrics_7d(str(row["id"]), "ad")

        # Pausing one ad cannot raise spend. Activating one cannot either, in
        # any amount this can bound: the ad set's budget is the cap, and it is
        # already being spent by the ad's siblings. The honest answer is 0,
        # with the reason in the note.
        projected = Decimal("0")

        checks = status_checks(str(row.get("campaign_id")), projected)
        warnings = []
        if before == wanted:
            warnings.append(f"already {wanted}; applying would be a no-op")
        effective = row.get("effective_status")
        if wanted == "ACTIVE" and effective in ("CAMPAIGN_PAUSED", "ADSET_PAUSED"):
            warnings.append(
                f"effective_status is {effective}, so a parent is paused and "
                f"this ad will not deliver until that is activated too"
            )
        elif wanted == "ACTIVE" and effective == "DISAPPROVED":
            warnings.append(
                "this ad was DISAPPROVED in review. Activating it does not "
                "appeal the decision and it will not deliver"
            )
        if wanted == "PAUSED":
            warnings.append(
                "pausing the last active ad in an ad set stops that ad set "
                "delivering entirely -- check find_ads for its siblings first"
            )

        return build_preview(
            tool="apply_ad_status",
            entity_type="ad",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=f"ad set {row.get('adset_id')} > {row.get('name')}",
            field_name="status",
            before=before,
            after=wanted,
            checks=checks,
            projected=projected,
            currency=currency,
            metrics=metrics,
            status=before,
            effective_status=effective,
            warnings=warnings,
            payload={
                "ad_id": str(row["id"]),
                "before": before,
                "after": wanted,
                "name": row.get("name") or "",
            },
            note=(
                "The projected delta is 0 in both directions: the ad set's "
                "budget is the cap, and it does not move when one ad inside it "
                "starts or stops. What changes is which creative spends it."
            ),
        )

    @apply_tool(
        title="Apply an ad status change", annotations=APPLY_REVERSIBLE
    )
    @refusals_reach_the_caller
    def apply_ad_status(
        preview_token: Annotated[str, Field(description="From preview_ad_status")],
    ) -> ApplyResult:
        """Pause or activate the ad the preview named."""
        pending = state.previews.take(preview_token, "apply_ad_status")
        payload = pending.payload
        row = ads.find(write_client, ad_id=payload["ad_id"])
        return commit(
            pending,
            tool="apply_ad_status",
            current_before=row.get("status") or "",
            entity={
                "entity_type": "ad",
                "entity_id": payload["ad_id"],
                "entity_name": payload["name"],
                "field_name": "status",
            },
            mutate=lambda: execute(
                write_client,
                ads.plan_set_status(
                    write_client, ad_id=payload["ad_id"], status=payload["after"]
                ),
                apply=True,
            ),
        )

    # -- ad copy and creative ----------------------------------------------
    #
    # A Meta creative is IMMUTABLE once attached, so neither of these edits one.
    # Both build a NEW creative and differ in what they point at it, which is
    # why they are separate tool names rather than one tool with a mode flag:
    # the risk differs, so the permission prompt should too.

    def _copy_args(primary_text, headline, description, link):
        edits = {
            "primary_text": primary_text,
            "headline": headline,
            "description": description,
            "link": link,
        }
        return {k: v for k, v in edits.items() if v is not None}

    def _copy_preview(
        *,
        tool: str,
        plan,
        row: dict[str, Any],
        projected: Decimal,
        note: str,
        payload_extra: dict[str, Any],
    ) -> ChangePreview:
        currency = currency_of()
        checks = status_checks(str(row.get("campaign_id")), projected)
        before = " | ".join(
            f"{c.field}: {c.before!r}" for c in plan.changes if c.before is not None
        ) or "(see metrics)"
        after = " | ".join(f"{c.field}: {c.after!r}" for c in plan.changes)
        return build_preview(
            tool=tool,
            entity_type="ad",
            entity_id=str(row.get("id")),
            entity_name=row.get("name") or "",
            path=f"ad set {row.get('adset_id')} > {row.get('name')}",
            field_name="creative",
            before=before,
            after=after,
            checks=checks,
            projected=projected,
            currency=currency,
            metrics=metrics_7d(str(row["id"]), "ad"),
            status=row.get("status"),
            effective_status=row.get("effective_status"),
            warnings=plan.warnings,
            payload={
                "ad_id": str(row["id"]),
                "before": before,
                "after": after,
                "name": row.get("name") or "",
                **payload_extra,
            },
            note=note,
        )

    @mcp.tool(
        title="Preview duplicating an ad with new copy or media",
        annotations=PREVIEW_SAFE,
    )
    @refusals_reach_the_caller
    def preview_duplicate_ad(
        ad_id: Annotated[str, Field(description="The ad to copy, from find_ads")],
        primary_text: Annotated[
            str | None, Field(description="New body text (Ads Manager: primary text)")
        ] = None,
        headline: Annotated[str | None, Field(description="New headline")] = None,
        description: Annotated[str | None, Field(description="New description")] = None,
        link: Annotated[str | None, Field(description="New destination URL")] = None,
        image_path: Annotated[
            str | None, Field(description="Local image file to upload and use")
        ] = None,
        video_id: Annotated[
            str | None, Field(description="An EXISTING Meta video id to use")
        ] = None,
        new_name: Annotated[str | None, Field(description="Name for the new ad")] = None,
    ) -> ChangePreview:
        """Show a NEW ad built beside an existing one, with changed copy.

        The safe half of the pair. The original is untouched -- it keeps
        delivering, keeps its history and keeps its likes and comments. The new
        ad is created PAUSED, so going live is a separate deliberate step.

        On a multi-locale creative a single value is applied to every locale.
        Pass nothing for a field to leave it as it is.
        """
        plan = ads.plan_duplicate_ad(
            write_client,
            ad_id=ad_id,
            new_name=new_name,
            edits=_copy_args(primary_text, headline, description, link),
            image_path=image_path,
            video_id=video_id,
        )
        row = ads.find(write_client, ad_id=ad_id)
        return _copy_preview(
            tool="apply_duplicate_ad",
            plan=plan,
            row=row,
            # A new PAUSED ad cannot spend: it is not delivering, and its ad
            # set's budget is unchanged either way.
            projected=Decimal("0"),
            note=(
                "The new ad is PAUSED, so the projected delta is 0 and nothing "
                "changes until somebody activates it. The original ad is not "
                "modified."
            ),
            payload_extra={
                "edits": _copy_args(primary_text, headline, description, link),
                "image_path": image_path,
                "video_id": video_id,
                "new_name": new_name,
            },
        )

    @apply_tool(
        title="Create the duplicate ad the preview described",
        annotations=APPLY_REVERSIBLE,
    )
    @refusals_reach_the_caller
    def apply_duplicate_ad(
        preview_token: Annotated[str, Field(description="From preview_duplicate_ad")],
    ) -> ApplyResult:
        """Build the new creative and the new PAUSED ad."""
        pending = state.previews.take(preview_token, "apply_duplicate_ad")
        payload = pending.payload
        row = ads.find(write_client, ad_id=payload["ad_id"])
        return _commit_creative(
            pending,
            tool="apply_duplicate_ad",
            row=row,
            builder=lambda: ads.plan_duplicate_ad(
                write_client,
                ad_id=payload["ad_id"],
                new_name=payload.get("new_name"),
                edits=payload.get("edits") or {},
                image_path=payload.get("image_path"),
                video_id=payload.get("video_id"),
            ),
        )

    @mcp.tool(
        title="Preview changing a live ad's copy or media", annotations=PREVIEW_SAFE
    )
    @refusals_reach_the_caller
    def preview_update_ad_copy(
        ad_id: Annotated[str, Field(description="The ad to change, from find_ads")],
        primary_text: Annotated[str | None, Field(description="New body text")] = None,
        headline: Annotated[str | None, Field(description="New headline")] = None,
        description: Annotated[str | None, Field(description="New description")] = None,
        link: Annotated[str | None, Field(description="New destination URL")] = None,
        image_path: Annotated[
            str | None, Field(description="Local image file to upload and use")
        ] = None,
        video_id: Annotated[
            str | None, Field(description="An EXISTING Meta video id to use")
        ] = None,
    ) -> ChangePreview:
        """Show repointing an EXISTING ad at a new creative. Keeps the ad id.

        **This usually discards social proof.** A new creative means a new
        post, so the likes, comments and shares on the current one do not carry
        over -- the preview reports the exact counts at stake. Prefer
        preview_duplicate_ad when that engagement matters.

        A live ad also re-enters review, and delivery can pause until the new
        copy is approved.
        """
        plan = ads.plan_update_copy(
            write_client,
            ad_id=ad_id,
            edits=_copy_args(primary_text, headline, description, link),
            image_path=image_path,
            video_id=video_id,
        )
        row = ads.find(write_client, ad_id=ad_id)
        return _copy_preview(
            tool="apply_update_ad_copy",
            plan=plan,
            row=row,
            projected=Decimal("0"),
            note=(
                "The ad set's budget does not move, so the projected delta is "
                "0. What changes is what the ad says -- and, usually, the "
                "social proof it had accumulated."
            ),
            payload_extra={
                "edits": _copy_args(primary_text, headline, description, link),
                "image_path": image_path,
                "video_id": video_id,
            },
        )

    @apply_tool(
        title="Apply the ad copy change (REPLACES THE CREATIVE)",
        annotations=APPLY_SPENDS,
    )
    @refusals_reach_the_caller
    def apply_update_ad_copy(
        preview_token: Annotated[str, Field(description="From preview_update_ad_copy")],
    ) -> ApplyResult:
        """Build the new creative and point the existing ad at it."""
        pending = state.previews.take(preview_token, "apply_update_ad_copy")
        payload = pending.payload
        row = ads.find(write_client, ad_id=payload["ad_id"])
        return _commit_creative(
            pending,
            tool="apply_update_ad_copy",
            row=row,
            builder=lambda: ads.plan_update_copy(
                write_client,
                ad_id=payload["ad_id"],
                edits=payload.get("edits") or {},
                image_path=payload.get("image_path"),
                video_id=payload.get("video_id"),
            ),
        )

    def _commit_creative(pending, *, tool: str, row: dict[str, Any], builder):
        """Shared apply path for both creative tools.

        Rebuilds the plan at apply time rather than carrying protos in the
        token: the source ad may have moved, and a plan built now is checked
        against what is there now.
        """
        require_writes_enabled()
        blocked = state.counters.would_exceed(pending.projected_delta)
        if blocked:
            raise PreviewExpired(blocked)

        plan = builder()
        entry_id = ledger.new_entry_id()
        ledger.write_intent(
            entry_id=entry_id,
            tool=tool,
            account_id=default_account,
            entity_type="ad",
            entity_id=str(row.get("id")),
            entity_name=row.get("name") or "",
            field_name="creative",
            before=pending.payload["before"],
            after=pending.payload["after"],
            projected_delta=str(pending.projected_delta),
        )
        try:
            # The image upload happens here, at apply time, because it is a
            # write. The preview described the FILE rather than inventing a
            # hash for it.
            image_path = pending.payload.get("image_path")
            if image_path:
                image = media.inspect_image(image_path)
                image_hash = media.upload_image(write_client, image, apply=True)
                plan.requires.payload = ads._fill_uploaded_image(
                    plan.requires.payload, image_hash
                )
            result, created = execute_chain(write_client, plan, apply=True)
        except Exception as exc:
            ledger.write_outcome(entry_id=entry_id, ok=False, error=str(exc))
            raise
        if not result.success:
            message = (
                f"Meta accepted the request but did not confirm it: "
                f"{result.response!r}. Check Ads Manager before retrying."
            )
            ledger.write_outcome(entry_id=entry_id, ok=False, error=message)
            raise MutationError(message)

        new_id = result.node_id or str(row.get("id"))
        ledger.write_outcome(entry_id=entry_id, ok=True, resource_name=new_id)
        state.counters.record(pending.projected_delta)
        return ApplyResult(
            applied=True,
            entry_id=entry_id,
            entity_type="ad",
            entity_id=new_id,
            entity_name=row.get("name") or "",
            field_name="creative",
            before=pending.payload["before"],
            after=pending.payload["after"],
            session=state.counters.snapshot(),
        )

    # -- audiences ----------------------------------------------------------

    @mcp.tool(title="Preview creating an audience", annotations=PREVIEW_SAFE)
    @refusals_reach_the_caller
    def preview_create_audience(
        kind: Annotated[str, Field(description="'saved' or 'lookalike'")],
        name: Annotated[str, Field(description="Name for the new audience")],
        targeting: Annotated[
            dict[str, Any] | None,
            Field(description="For 'saved': Meta's targeting spec"),
        ] = None,
        origin_audience_id: Annotated[
            str | None, Field(description="For 'lookalike': the source, from list_audiences")
        ] = None,
        country: Annotated[
            str | None, Field(description="For 'lookalike': two-letter code, e.g. CA")
        ] = None,
        ratio: Annotated[
            float, Field(description="For 'lookalike': 0.01 to 0.20", ge=0.01, le=0.20)
        ] = 0.01,
    ) -> ChangePreview:
        """Show a saved targeting spec or a lookalike. Writes nothing.

        **Customer-list audiences are not available here and never will be.**
        Building one uploads hashed customer emails and phone numbers to Meta;
        that is personal data leaving the company, and it belongs in Ads
        Manager where a person accepts the data terms. The attempt is refused
        structurally, not just undocumented.

        A lookalike is derived from a source Meta already holds, so nothing is
        uploaded.
        """
        wanted = (kind or "").strip().lower()
        if wanted == "saved":
            plan = audiences.plan_create_saved(
                write_client, name=name, targeting=targeting or {}
            )
        elif wanted == "lookalike":
            if not origin_audience_id or not country:
                raise ValueError(
                    "A lookalike needs origin_audience_id and country. Use "
                    "list_audiences to find a source."
                )
            plan = audiences.plan_create_lookalike(
                write_client,
                name=name,
                origin_audience_id=origin_audience_id,
                country=country,
                ratio=ratio,
            )
        else:
            raise ValueError(
                f"kind must be 'saved' or 'lookalike', got {kind!r}. A "
                f"customer-list audience is not available here: it would "
                f"upload personal data to Meta."
            )

        checks = [
            check_writes_enabled(),
            check_account_in_scope(state.limits, default_account, default_account),
        ]
        return build_preview(
            tool="apply_create_audience",
            entity_type=plan.entity,
            entity_id="(new)",
            entity_name=name,
            path=f"account {default_account}",
            field_name="audience",
            before="(does not exist)",
            after=" | ".join(f"{c.field}: {c.after}" for c in plan.changes),
            checks=checks,
            projected=Decimal("0"),
            currency=currency_of(),
            warnings=plan.warnings,
            payload={
                "kind": wanted,
                "name": name,
                "targeting": targeting,
                "origin_audience_id": origin_audience_id,
                "country": country,
                "ratio": ratio,
                "before": "(does not exist)",
                "after": name,
            },
            note=(
                "Creating an audience changes no delivery on its own -- it is "
                "available for an ad set to use. Projected delta is 0."
            ),
        )

    @apply_tool(title="Create the audience the preview described", annotations=APPLY_REVERSIBLE)
    @refusals_reach_the_caller
    def apply_create_audience(
        preview_token: Annotated[str, Field(description="From preview_create_audience")],
    ) -> ApplyResult:
        """Create the saved audience or lookalike."""
        pending = state.previews.take(preview_token, "apply_create_audience")
        payload = pending.payload
        require_writes_enabled()
        blocked = state.counters.would_exceed(Decimal("0"))
        if blocked:
            raise PreviewExpired(blocked)

        if payload["kind"] == "saved":
            plan = audiences.plan_create_saved(
                write_client, name=payload["name"], targeting=payload["targeting"] or {}
            )
        else:
            plan = audiences.plan_create_lookalike(
                write_client,
                name=payload["name"],
                origin_audience_id=payload["origin_audience_id"],
                country=payload["country"],
                ratio=payload["ratio"],
            )

        entry_id = ledger.new_entry_id()
        ledger.write_intent(
            entry_id=entry_id,
            tool="apply_create_audience",
            account_id=default_account,
            entity_type=plan.entity,
            entity_id="(new)",
            entity_name=payload["name"],
            field_name="audience",
            before="(does not exist)",
            after=payload["name"],
            projected_delta="0",
        )
        try:
            result = execute(write_client, plan, apply=True)
        except Exception as exc:
            ledger.write_outcome(entry_id=entry_id, ok=False, error=str(exc))
            raise
        ledger.write_outcome(entry_id=entry_id, ok=True, resource_name=result.node_id)
        state.counters.record(Decimal("0"))
        return ApplyResult(
            applied=True,
            entry_id=entry_id,
            entity_type=plan.entity,
            entity_id=result.node_id or "(new)",
            entity_name=payload["name"],
            field_name="audience",
            before="(does not exist)",
            after=payload["name"],
            session=state.counters.snapshot(),
        )

    # -- revert ------------------------------------------------------------

    def _current_value(entry: dict[str, Any]) -> str | None:
        """Today's value for a ledger entry's field, formatted as it was stored.

        Returns None when the field is one this cannot read, so the caller
        skips the staleness check rather than refusing every revert on a field
        nobody has taught it about yet.
        """
        field_name, entity_id = entry["field"], entry["entity_id"]
        entity_type = entry["entity_type"]
        currency = currency_of()

        if field_name == "status":
            if entity_type == "campaign":
                return campaigns.find(write_client, campaign_id=entity_id).get("status")
            if entity_type == "ad set":
                return adsets.find(write_client, ad_set_id=entity_id).get("status")
            if entity_type == "ad":
                return ads.find(write_client, ad_id=entity_id).get("status")
            return None
        if field_name == "daily_budget":
            if entity_type == "campaign":
                row = campaigns.find(write_client, campaign_id=entity_id)
            else:
                row = adsets.find(write_client, ad_set_id=entity_id)
            return format_money(from_minor_units(row.get("daily_budget"), currency), currency)
        if field_name == "bid_amount":
            row = adsets.find(write_client, ad_set_id=entity_id)
            return format_money(from_minor_units(row.get("bid_amount"), currency), currency)
        return None

    @apply_tool(
        title="Revert a change by its ledger entry id", annotations=APPLY_REVERSIBLE
    )
    @refusals_reach_the_caller
    def revert_change(
        entry_id: Annotated[str, Field(description="From list_my_changes")],
    ) -> ApplyResult:
        """Put a value back to what it was before an earlier change.

        Reverting is itself a change and is itself recorded, so the ledger
        stays a complete history rather than a list of things that are still
        true.
        """
        # A revert is a real write, so it answers to the same two gates every
        # apply_* does. It does not go through commit(), which is why they have
        # to be repeated here rather than inherited.
        require_writes_enabled()
        # Zero projected delta: a revert restores a value the account already
        # held, so it cannot push the session's spend projection anywhere. The
        # APPLIES cap still applies, because find_entry reaches back over the
        # whole ledger -- including changes from earlier sessions this counter
        # has never seen -- so without it a revert loop had no bound at all.
        blocked = state.counters.would_exceed(Decimal("0"))
        if blocked:
            raise PreviewExpired(blocked)

        entry = ledger.find_entry(entry_id)
        if entry is None:
            raise ValueError(f"No ledger entry {entry_id!r}. See list_my_changes.")
        if not entry.get("applied"):
            raise ValueError(
                f"Entry {entry_id} was never recorded as applied; there is "
                f"nothing to revert. If it has no error either, the write left "
                f"this process and nothing recorded how it ended -- check the "
                f"object in Ads Manager rather than reverting blind."
            )
        if entry_id in ledger.reverted_entry_ids():
            raise ValueError(f"Entry {entry_id} has already been reverted.")

        field_name = entry["field"]
        before = entry["before"]
        entity_type = entry["entity_type"]

        # STALENESS, the same check commit() does. If the value is no longer
        # what our change left behind, somebody moved it since -- most often a
        # person in Ads Manager -- and putting our old value back would silently
        # discard THEIR change, while the ledger recorded it as a tidy undo of
        # ours.
        current = _current_value(entry)
        if current is not None and current != entry["after"]:
            raise PreviewStale(
                f"{entry['entity_name']} {field_name} is {current!r} now, but "
                f"this entry left it at {entry['after']!r}. Something changed it "
                f"since, so reverting would overwrite that instead of undoing "
                f"this. Look at list_my_changes and Ads Manager before deciding."
            )

        new_entry = ledger.new_entry_id()
        ledger.write_intent(
            entry_id=new_entry,
            tool="revert_change",
            account_id=default_account,
            entity_type=entity_type,
            entity_id=entry["entity_id"],
            entity_name=entry["entity_name"],
            field_name=field_name,
            before=entry["after"],
            after=before,
            projected_delta="0",
        )
        try:
            plan = _revert_plan(entry_id, entity_type, field_name, entry, before)
            result = execute(write_client, plan, apply=True)
            if not result.success:
                raise MutationError(
                    f"Meta accepted the revert but did not confirm it: "
                    f"{result.response!r}. Check the object in Ads Manager."
                )
        except Exception as exc:
            ledger.write_outcome(entry_id=new_entry, ok=False, error=str(exc))
            raise
        ledger.write_outcome(
            entry_id=new_entry,
            ok=True,
            resource_name=entry["entity_id"],
            reverts=entry_id,
        )
        state.counters.record(Decimal("0"))

        return ApplyResult(
            applied=True,
            entry_id=new_entry,
            entity_type=entity_type,
            entity_id=entry["entity_id"],
            entity_name=entry["entity_name"],
            field_name=field_name,
            before=entry["after"],
            after=before,
            session=state.counters.snapshot(),
        )

    def _revert_plan(entry_id, entity_type, field_name, entry, before):
        """Build the plan that puts ``before`` back."""
        node = entry["entity_id"]
        if field_name == "status":
            if entity_type == "campaign":
                return campaigns.plan_set_status(
                    write_client, campaign_id=node, status=before
                )
            if entity_type == "ad set":
                return adsets.plan_set_status(
                    write_client, ad_set_id=node, status=before
                )
            if entity_type == "ad":
                return ads.plan_set_status(write_client, ad_id=node, status=before)
        if field_name == "daily_budget":
            amount = _amount_from_stored(before)
            if entity_type == "campaign":
                return campaigns.plan_set_daily_budget(
                    write_client, campaign_id=node, amount=amount
                )
            return adsets.plan_set_daily_budget(
                write_client, ad_set_id=node, amount=amount
            )
        if field_name == "bid_amount":
            return adsets.plan_set_bid_amount(
                write_client, ad_set_id=node, amount=_amount_from_stored(before)
            )
        raise ValueError(
            f"Cannot revert field {field_name!r} on a {entity_type} "
            f"automatically (entry {entry_id})."
        )


def _amount_from_stored(stored: str) -> Decimal:
    """Pull the number back out of a stored ``"50.00 CAD"`` string.

    The ledger keeps money WITH its currency code, because a bare "50.00" in an
    audit trail does not say 50 of what. Reversing that is this one function,
    rather than a `.split()[0]` repeated at four call sites where one of them
    would eventually get it wrong.
    """
    text = (stored or "").strip()
    if not text or text == "(not set)":
        raise ValueError(
            f"That entry's previous value was {stored!r}, which is not an "
            f"amount this can put back. Set it explicitly instead."
        )
    head = text.split()[0]
    try:
        return Decimal(head)
    except InvalidOperation as exc:
        raise ValueError(
            f"Could not read an amount out of the stored value {stored!r}."
        ) from exc
