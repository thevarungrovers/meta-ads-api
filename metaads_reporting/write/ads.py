"""Ad reads and writes.

The smallest unit, and the one most worth pausing: on Meta a single worn-out
creative inside an otherwise healthy ad set is the common failure, and pausing
it is both the usual fix and completely reversible.

Only ``status`` and ``name`` are writable here. Changing what an ad SAYS means
swapping its creative, which on Meta is not an edit at all -- a creative is
immutable once attached, so a copy change is a new ad. That is a create, and
creates are not exposed to an agent.
"""

from __future__ import annotations

from typing import Any

from .client import MutatingMetaAdsClient, MutationError, check_status
from .lookup import Candidate, NotFound, match_by_name
from .plan import FieldChange, PlannedChange

ENTITY = "ad"
EDGE = "ads"

FIELDS = (
    "id",
    "name",
    "status",
    "effective_status",
    "adset_id",
    "campaign_id",
    "created_time",
    "updated_time",
)


def find(
    client: MutatingMetaAdsClient,
    *,
    ad_id: str | None = None,
    name: str | None = None,
    ad_set_id: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Fetch one ad by id or name. Raises if not exactly one."""
    if (ad_id is None) == (name is None):
        raise MutationError("Give exactly one of ad_id or name.")

    if ad_id is not None:
        try:
            return client.node(str(ad_id), FIELDS)
        except Exception as exc:
            raise NotFound(
                f"No ad {ad_id} this token can see. On Meta a missing object "
                f"and a missing permission are the same error."
            ) from exc

    rows = search(client, None, ad_set_id=ad_set_id, account_id=account_id)
    return match_by_name(rows, name or "", entity=ENTITY, to_candidate=as_candidate)


def as_candidate(row: dict[str, Any]) -> Candidate:
    return Candidate(
        id=str(row.get("id")),
        name=row.get("name") or "(unnamed)",
        status=row.get("status") or "",
        context=f"ad set {row.get('adset_id')}",
    )


def search(
    client: MutatingMetaAdsClient,
    needle: str | None = None,
    *,
    ad_set_id: str | None = None,
    campaign_id: str | None = None,
    account_id: str | None = None,
    limit: int = 200,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    """Ads in the account, one campaign or one ad set, narrowed by name.

    Ad names are the least unique things in a Meta account -- a bulk duplicate
    leaves a dozen ads called "Creative A - Copy" across different ad sets --
    so scoping to a parent is usually necessary rather than merely tidy.
    """
    from ..account_id import account_path

    if ad_set_id:
        parent = str(ad_set_id)
    elif campaign_id:
        parent = str(campaign_id)
    else:
        parent = account_path(client.resolve_account_id(account_id))

    rows = client.read.edge(
        parent,
        EDGE,
        FIELDS,
        limit=limit,
        max_pages=max_pages,
        effective_status=(
            '["ACTIVE","PAUSED","CAMPAIGN_PAUSED","ADSET_PAUSED",'
            '"PENDING_REVIEW","DISAPPROVED","WITH_ISSUES"]'
        ),
    )
    if needle:
        target = needle.casefold()
        rows = [r for r in rows if target in (r.get("name") or "").casefold()]
    return rows


def plan_set_status(
    client: MutatingMetaAdsClient,
    *,
    status: str,
    ad_id: str | None = None,
    name: str | None = None,
    account_id: str | None = None,
) -> PlannedChange:
    """Pause or activate one ad."""
    wanted = check_status(status)
    row = find(client, ad_id=ad_id, name=name, account_id=account_id)
    before = row.get("status") or ""

    if before == wanted:
        raise MutationError(
            f"Ad {row.get('name')!r} is already {wanted}; nothing to do."
        )

    warnings = []
    effective = row.get("effective_status")
    if wanted == "ACTIVE" and effective in ("CAMPAIGN_PAUSED", "ADSET_PAUSED"):
        warnings.append(
            f"effective_status is {effective}, so a parent is paused and this "
            f"ad will not deliver until that is activated too"
        )
    elif wanted == "ACTIVE" and effective == "DISAPPROVED":
        warnings.append(
            "this ad was DISAPPROVED in review. Activating it does not appeal "
            "the decision and it will not deliver"
        )
    elif wanted == "ACTIVE":
        warnings.append("activating resumes delivery immediately")

    return PlannedChange(
        action="update",
        entity=ENTITY,
        entity_type="ad",
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
    ad_id: str | None = None,
    name: str | None = None,
    account_id: str | None = None,
) -> PlannedChange:
    row = find(client, ad_id=ad_id, name=name, account_id=account_id)
    if row.get("name") == new_name:
        raise MutationError(f"Ad is already named {new_name!r}.")
    return PlannedChange(
        action="update",
        entity=ENTITY,
        entity_type="ad",
        node_id=str(row["id"]),
        label=f"id {row['id']}",
        payload={"name": new_name},
        changes=[FieldChange("name", row.get("name"), new_name)],
    )
