"""Audiences: saved targeting specs, and lookalikes derived from them.

TWO KINDS ARE SUPPORTED AND ONE IS STRUCTURALLY REFUSED.

``saved_audience``   a reusable targeting spec -- geo, age, interests. No
                     customer data is involved at all.
``lookalike``        derived from a source Meta ALREADY HOLDS (a pixel
                     audience, an existing custom audience, page engagement).
                     Nothing new is uploaded; Meta expands a population it
                     already has.

``custom audience from a customer list`` is NOT here, and its absence is
enforced rather than merely undocumented. Building one means uploading hashed
emails and phone numbers of real customers to Meta: personal data leaving the
company under Meta's Customer List terms, needing a lawful basis, and very hard
to take back once sent. :func:`check_no_customer_data` refuses any payload
carrying the fields that would do it, and it runs on every call rather than
only the ones that look suspicious.

The awkward part, and the reason the guard is explicit: a LOOKALIKE is created
on the very same ``customaudiences`` edge as a customer-list audience. The only
thing separating "derive from what Meta has" from "upload our customer list" is
the ``subtype`` field and whether a ``data`` payload is attached. That is far
too thin a line to leave to a convention, so both halves are checked here.
"""

from __future__ import annotations

import json
from typing import Any

from ..account_id import account_path
from .client import MutatingMetaAdsClient, MutationError
from .plan import FieldChange, PlannedChange

SAVED_EDGE = "saved_audiences"
CUSTOM_EDGE = "customaudiences"

#: The only subtype this package will create on the customaudiences edge.
ALLOWED_CUSTOM_SUBTYPE = "LOOKALIKE"

#: Fields that carry, or point at, personal data. Any one of them present means
#: the call is building a customer-list audience, whatever its subtype says.
CUSTOMER_DATA_FIELDS = frozenset(
    {
        "data",
        "schema",
        "payload",
        "users",
        "customer_file_source",
        "data_source",
        "is_raw",
        "app_ids",
        "pixel_id",
    }
)

#: Lookalike ratios Meta accepts: 1% to 20% of a country's population.
MIN_RATIO, MAX_RATIO = 0.01, 0.20


class CustomerDataRefused(MutationError):
    """A payload would have sent personal data to Meta.

    Its own type so it cannot be swallowed by a generic handler, and so the
    refusal reads as a policy decision rather than a validation error.
    """


def check_no_customer_data(payload: dict[str, Any]) -> None:
    """Refuse a payload that would upload personal data. Runs on every call.

    >>> check_no_customer_data({"name": "LAL 1%", "subtype": "LOOKALIKE"})
    >>> check_no_customer_data({"schema": ["EMAIL_SHA256"], "data": [["..."]]})
    Traceback (most recent call last):
    ...
    metaads_reporting.write.audiences.CustomerDataRefused: ...
    """
    found = sorted(set(payload) & CUSTOMER_DATA_FIELDS)
    if found:
        raise CustomerDataRefused(
            f"Refusing to send {', '.join(found)}: those fields build a custom "
            f"audience from a customer list, which uploads personal data to "
            f"Meta. This package creates saved audiences and lookalikes only. "
            f"If you genuinely need a customer-list audience, do it in Ads "
            f"Manager, where the data terms are presented and accepted by a "
            f"person."
        )


def check_subtype(subtype: str) -> str:
    """Only LOOKALIKE may be created on the customaudiences edge."""
    wanted = (subtype or "").strip().upper()
    if wanted != ALLOWED_CUSTOM_SUBTYPE:
        raise CustomerDataRefused(
            f"subtype {wanted or '(none)'} is not available here. The "
            f"customaudiences edge creates both lookalikes and customer-list "
            f"audiences, and only {ALLOWED_CUSTOM_SUBTYPE} is permitted -- the "
            f"other kind uploads personal data."
        )
    return wanted


FIELDS = (
    "id",
    "name",
    "description",
    "subtype",
    "approximate_count_lower_bound",
    "approximate_count_upper_bound",
    "operation_status",
    "time_created",
    "delivery_status",
)

#: Probed against the live API, not taken from the docs: `approximate_count`
#: exists on a CUSTOM audience and NOT on a saved one, and asking for it fails
#: the whole request with "(#100) Tried accessing nonexisting field". A saved
#: audience is a targeting spec, so `sentence_lines` -- Meta's own plain-English
#: rendering of it -- is the useful thing instead.
SAVED_FIELDS = (
    "id",
    "name",
    "description",
    "targeting",
    "sentence_lines",
    "run_status",
    "time_created",
)


def list_custom(
    client: MutatingMetaAdsClient,
    *,
    account_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Custom and lookalike audiences on the account. Read-only.

    Includes customer-list audiences that already exist -- reading one is not
    the same as creating one, and you need to see them to pick a lookalike
    source.
    """
    target = account_path(client.resolve_account_id(account_id))
    rows = client.read.edge(target, CUSTOM_EDGE, FIELDS, limit=limit, max_items=limit)
    return [
        {
            "audience_id": str(r.get("id")),
            "name": r.get("name"),
            "subtype": r.get("subtype"),
            "approx_size": _approx_size(r),
            "status": (r.get("operation_status") or {}).get("description"),
            "created": r.get("time_created"),
        }
        for r in rows
    ]


def list_saved(
    client: MutatingMetaAdsClient,
    *,
    account_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Saved targeting specs on the account. Read-only."""
    target = account_path(client.resolve_account_id(account_id))
    rows = client.read.edge(
        target, SAVED_EDGE, SAVED_FIELDS, limit=limit, max_items=limit
    )
    return [
        {
            "saved_audience_id": str(r.get("id")),
            "name": r.get("name"),
            "description": r.get("description"),
            # Meta's own plain-English rendering of the targeting spec, which
            # is far more use in a listing than the raw nested JSON.
            "summary": _sentences(r.get("sentence_lines")),
            "status": r.get("run_status"),
            "created": r.get("time_created"),
        }
        for r in rows
    ]


def _sentences(lines: Any) -> list[str]:
    """Flatten `sentence_lines` into readable strings.

    Meta returns [{"content": "Location:", "children": ["Canada"]}, ...].
    """
    out = []
    for line in lines or []:
        if not isinstance(line, dict):
            continue
        content = line.get("content") or ""
        children = ", ".join(str(c) for c in (line.get("children") or []))
        out.append(f"{content} {children}".strip())
    return out


def _approx_size(row: dict[str, Any]) -> str | None:
    """Meta gives a RANGE, not a number, and often neither.

    Reporting the lower bound alone as "the size" overstates precision on a
    figure Meta deliberately fuzzes.
    """
    low = row.get("approximate_count_lower_bound")
    high = row.get("approximate_count_upper_bound")
    if low is None and high is None:
        return None
    if low == high:
        return str(low)
    return f"{low}-{high}"


def plan_create_saved(
    client: MutatingMetaAdsClient,
    *,
    name: str,
    targeting: dict[str, Any],
    description: str | None = None,
    account_id: str | None = None,
) -> PlannedChange:
    """A reusable targeting spec. No customer data is involved.

    ``targeting`` is Meta's targeting spec verbatim -- geo_locations, age_min,
    age_max, interests and so on. It is passed through rather than modelled,
    because the spec is large, changes often, and a partial model would refuse
    valid targeting while pretending to validate it.
    """
    if not name.strip():
        raise MutationError("A saved audience needs a name.")
    if not targeting:
        raise MutationError("A saved audience needs a targeting spec.")
    check_no_customer_data(targeting)

    payload: dict[str, Any] = {"name": name, "targeting": targeting}
    if description:
        payload["description"] = description
    check_no_customer_data(payload)

    return PlannedChange(
        action="create",
        entity="saved audience",
        entity_type="saved_audience",
        node_id="",
        label=name,
        payload=payload,
        changes=[
            FieldChange("name", None, name),
            FieldChange("targeting", None, json.dumps(targeting)[:200]),
        ],
        warnings=[
            "a saved audience is a targeting spec only -- creating it changes "
            "no delivery until an ad set uses it"
        ],
        edge=SAVED_EDGE,
    )


def plan_create_lookalike(
    client: MutatingMetaAdsClient,
    *,
    name: str,
    origin_audience_id: str,
    country: str,
    ratio: float = 0.01,
    account_id: str | None = None,
) -> PlannedChange:
    """A lookalike derived from an audience Meta already holds.

    Nothing new is uploaded: Meta expands a population it has. The origin is
    read first so a typo fails here, with the audience's name in the message,
    rather than at Meta with an id and nothing else.
    """
    if not name.strip():
        raise MutationError("A lookalike needs a name.")
    if not (MIN_RATIO <= ratio <= MAX_RATIO):
        raise MutationError(
            f"ratio must be between {MIN_RATIO} and {MAX_RATIO} "
            f"({int(MIN_RATIO * 100)}%-{int(MAX_RATIO * 100)}% of the "
            f"country's population), got {ratio}."
        )
    code = (country or "").strip().upper()
    if len(code) != 2:
        raise MutationError(
            f"country must be a two-letter code such as CA, got {country!r}."
        )

    try:
        origin = client.read.node(str(origin_audience_id), ("id", "name", "subtype"))
    except Exception as exc:
        raise MutationError(
            f"No audience {origin_audience_id} this token can see. Use "
            f"list_custom to find a source."
        ) from exc

    payload = {
        "name": name,
        "subtype": check_subtype("LOOKALIKE"),
        "origin_audience_id": str(origin_audience_id),
        "lookalike_spec": {"ratio": ratio, "country": code, "type": "custom_ratio"},
    }
    check_no_customer_data(payload)

    return PlannedChange(
        action="create",
        entity="lookalike audience",
        entity_type="custom_audience",
        node_id="",
        label=name,
        payload=payload,
        changes=[
            FieldChange("name", None, name),
            FieldChange("source", None, f"{origin.get('name')} ({origin_audience_id})"),
            FieldChange("ratio", None, f"{ratio:.0%} of {code}"),
        ],
        warnings=[
            "derived from a source Meta already holds -- no customer data is "
            "uploaded",
            "a lookalike takes a few hours to populate and cannot be used "
            "until it does",
            f"a {ratio:.0%} lookalike is {'narrow and close to the source' if ratio <= 0.02 else 'broad; reach rises and similarity falls'}",
        ],
        edge=CUSTOM_EDGE,
    )
