"""Campaign reads and writes.

THE ONE THING TO UNDERSTAND ABOUT A META CAMPAIGN BUDGET: it may not be on the
campaign at all.

Meta puts the budget at exactly ONE of two levels, never both:

* **Campaign budget optimization** (Advantage campaign budget, CBO) -- the
  campaign holds ``daily_budget`` and distributes it across its ad sets, which
  hold none. Changing it moves spend for EVERY ad set underneath.
* **Ad set budgets** (ABO) -- each ad set holds its own and the campaign holds
  none.

Writing to the wrong level is refused by the API, but with a message about an
invalid parameter that does not say "the budget lives downstairs". So
:func:`budget_level` answers the question first, and the plan functions refuse
with an explanation rather than letting Meta refuse with a riddle.

The second trap is ``daily_budget`` versus ``lifetime_budget``. An object has
one or the other, never both, and switching between them needs the schedule
changed in the same request. This package does not switch: it changes the one
that is already there and refuses the other.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..currency import format_money, from_minor_units, to_minor_units
from .client import MutatingMetaAdsClient, MutationError, check_status
from .lookup import Candidate, NotFound, match_by_name
from .plan import FieldChange, PlannedChange

ENTITY = "campaign"
EDGE = "campaigns"

#: Everything a preview or a plan needs off a campaign. The Graph API returns
#: ONLY what is named here -- there is no default field set -- so a field left
#: off reads as absent rather than erroring.
FIELDS = (
    "id",
    "name",
    "status",
    "effective_status",
    "objective",
    "buying_type",
    "bid_strategy",
    "daily_budget",
    "lifetime_budget",
    "budget_remaining",
    "spend_cap",
    "start_time",
    "stop_time",
    "special_ad_categories",
    "account_id",
)


def budget_level(campaign: dict[str, Any]) -> str:
    """``"campaign"`` when this campaign holds the budget, else ``"adset"``.

    >>> budget_level({"daily_budget": "5000"})
    'campaign'
    >>> budget_level({"id": "1"})
    'adset'
    """
    if campaign.get("daily_budget") or campaign.get("lifetime_budget"):
        return "campaign"
    return "adset"


def budget_field(obj: dict[str, Any]) -> str | None:
    """Which budget field this object actually uses, or None if neither.

    >>> budget_field({"lifetime_budget": "100000"})
    'lifetime_budget'
    >>> budget_field({}) is None
    True
    """
    if obj.get("daily_budget"):
        return "daily_budget"
    if obj.get("lifetime_budget"):
        return "lifetime_budget"
    return None


def find(
    client: MutatingMetaAdsClient,
    *,
    campaign_id: str | None = None,
    name: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Fetch one campaign by id or name. Raises if not exactly one."""
    if (campaign_id is None) == (name is None):
        raise MutationError("Give exactly one of campaign_id or name.")

    if campaign_id is not None:
        try:
            return client.node(str(campaign_id), FIELDS)
        except Exception as exc:
            raise NotFound(
                f"No campaign {campaign_id} this token can see. On Meta a "
                f"missing object and a missing permission are the same error, "
                f"so check the id AND that the System User is assigned to the "
                f"account."
            ) from exc

    rows = search(client, None, account_id=account_id)
    return match_by_name(rows, name or "", entity=ENTITY, to_candidate=as_candidate)


def as_candidate(row: dict[str, Any]) -> Candidate:
    return Candidate(
        id=str(row.get("id")),
        name=row.get("name") or "(unnamed)",
        status=row.get("status") or "",
        context=row.get("objective") or "",
    )


def search(
    client: MutatingMetaAdsClient,
    needle: str | None = None,
    *,
    account_id: str | None = None,
    limit: int = 200,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    """Campaigns in the account, optionally narrowed by name. Read-only.

    ARCHIVED and DELETED campaigns are excluded: they cannot be changed, and
    including them makes every name lookup ambiguous against things that no
    longer exist.
    """
    from ..account_id import account_path

    target = account_path(client.resolve_account_id(account_id))
    rows = client.read.edge(
        target,
        EDGE,
        FIELDS,
        limit=limit,
        max_pages=max_pages,
        effective_status='["ACTIVE","PAUSED","IN_PROCESS","WITH_ISSUES"]',
    )
    if needle:
        target_text = needle.casefold()
        rows = [r for r in rows if target_text in (r.get("name") or "").casefold()]
    return rows


def plan_set_status(
    client: MutatingMetaAdsClient,
    *,
    status: str,
    campaign_id: str | None = None,
    name: str | None = None,
    account_id: str | None = None,
) -> PlannedChange:
    """Pause or activate a campaign."""
    wanted = check_status(status)
    row = find(client, campaign_id=campaign_id, name=name, account_id=account_id)
    before = row.get("status") or ""

    if before == wanted:
        raise MutationError(
            f"Campaign {row.get('name')!r} is already {wanted}; nothing to do."
        )

    warnings = []
    if wanted == "ACTIVE":
        warnings.append(
            "activating resumes delivery immediately across every ad set and "
            "ad underneath that is itself ACTIVE"
        )
    effective = row.get("effective_status")
    if wanted == "PAUSED" and effective and effective != "ACTIVE":
        warnings.append(
            f"effective_status is already {effective}, so this campaign may "
            f"not be delivering anyway"
        )

    return PlannedChange(
        action="update",
        entity=ENTITY,
        entity_type="campaign",
        node_id=str(row["id"]),
        label=f"{row.get('name')} (id {row['id']})",
        payload={"status": wanted},
        changes=[FieldChange("status", before, wanted)],
        warnings=warnings,
    )


def plan_rename(
    client: MutatingMetaAdsClient,
    *,
    new_name: str,
    campaign_id: str | None = None,
    name: str | None = None,
    account_id: str | None = None,
) -> PlannedChange:
    row = find(client, campaign_id=campaign_id, name=name, account_id=account_id)
    if row.get("name") == new_name:
        raise MutationError(f"Campaign is already named {new_name!r}.")
    return PlannedChange(
        action="update",
        entity=ENTITY,
        entity_type="campaign",
        node_id=str(row["id"]),
        label=f"id {row['id']}",
        payload={"name": new_name},
        changes=[FieldChange("name", row.get("name"), new_name)],
    )


def plan_set_daily_budget(
    client: MutatingMetaAdsClient,
    *,
    amount: Decimal | str | float,
    campaign_id: str | None = None,
    name: str | None = None,
    account_id: str | None = None,
    override_guardrail: bool = False,
) -> PlannedChange:
    """Change a CBO campaign's daily budget.

    Refuses when the campaign does not hold the budget, which is the common
    case: on an ABO campaign the budgets are one level down, and setting one
    here would either be rejected by Meta or -- worse -- accepted and silently
    convert the campaign to CBO, redistributing spend across every ad set.
    """
    row = find(client, campaign_id=campaign_id, name=name, account_id=account_id)
    currency = client.currency(account_id)
    client.check_daily_budget(float(amount), override=override_guardrail)

    level = budget_level(row)
    if level != "campaign":
        raise MutationError(
            f"Campaign {row.get('name')!r} does not hold its own budget -- its "
            f"ad sets do (this is an ad-set-budget campaign, not an Advantage "
            f"campaign budget one). Change the ad set's daily_budget instead. "
            f"Setting one here would convert the campaign to a campaign budget "
            f"and redistribute spend across every ad set under it."
        )
    using = budget_field(row)
    if using != "daily_budget":
        raise MutationError(
            f"Campaign {row.get('name')!r} is on a LIFETIME budget, not a daily "
            f"one. Switching between them also requires changing the schedule, "
            f"which this tool does not do -- make that change in Ads Manager."
        )

    before = from_minor_units(row.get("daily_budget"), currency)
    after = Decimal(str(amount))
    if before == after:
        raise MutationError(
            f"Campaign {row.get('name')!r} is already at "
            f"{format_money(after, currency)}; nothing to do."
        )

    warnings = [
        "this is a campaign budget: the change moves spend for EVERY ad set "
        "under this campaign, not one of them"
    ]
    if before and after < before:
        warnings.append(
            "lowering a daily budget can stop delivery part-way through today "
            "if the campaign has already spent more than the new cap"
        )

    return PlannedChange(
        action="update",
        entity=ENTITY,
        entity_type="campaign",
        node_id=str(row["id"]),
        label=f"{row.get('name')} (id {row['id']})",
        payload={"daily_budget": to_minor_units(after, currency)},
        changes=[
            FieldChange(
                "daily_budget",
                format_money(before, currency),
                format_money(after, currency),
            )
        ],
        warnings=warnings,
    )
