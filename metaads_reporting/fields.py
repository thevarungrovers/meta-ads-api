"""Field extraction and the money ruling.

Two jobs: pull a dotted path out of a Graph API object, and decide what unit a
monetary field is in.

**The money rule is not derivable from the name, and it points two ways.**

1. **Node fields are MINOR UNITS.** ``daily_budget``, ``lifetime_budget``,
   ``bid_amount``, ``budget_remaining``, ``spend_cap``, ``amount_spent`` on a
   campaign, ad set or account are integers in the account currency's minor
   unit -- ``5000`` is fifty dollars. See :mod:`metaads_reporting.currency`.
2. **Insights fields are MAJOR UNITS, already.** ``spend``, ``cpc``, ``cpm``,
   ``cpp`` and the ``cost_per_*`` family come off the ``/insights`` edge as
   decimal STRINGS in account currency -- ``"12.34"`` is twelve thirty-four.
   Dividing one of those by 100 is an error in the opposite direction from
   failing to divide a budget, and both are silent.

So the two sets are listed explicitly, and :func:`unknown_money_fields` flags a
selected field this module has no ruling on, so a report fails at import rather
than emitting a column that is wrong by a factor of a hundred.

A third trap, not about units at all: ``status`` and ``effective_status`` are
different fields and the difference matters constantly. ``status`` is what
somebody set on THIS object. ``effective_status`` folds in the parents and ad
review -- an ACTIVE ad inside a PAUSED campaign has
``effective_status: CAMPAIGN_PAUSED`` and is not delivering. Reporting only
``status`` makes a dead ad look live.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

from .currency import from_minor_units

#: Node fields denominated in the account currency's MINOR unit. Every entry is
#: an integer field on a campaign, ad set, ad or ad account object.
MINOR_UNIT_FIELDS = frozenset(
    {
        "daily_budget",
        "lifetime_budget",
        "budget_remaining",
        "bid_amount",
        "spend_cap",
        "min_daily_budget",
        "daily_spend_limit",
        "lifetime_spend_limit",
        "amount_spent",
        "balance",
        "daily_budget_min",
        "adset_budget_remaining",
    }
)

#: SCALAR insights fields already in MAJOR units, as decimal strings. Listed
#: explicitly so nobody "fixes" one into the set above.
#:
#: Scalar is the operative word -- see :data:`NESTED_INSIGHTS_FIELDS`. A
#: list-valued field in here gets parsed as a number, fails, and becomes None
#: without raising.
MAJOR_UNIT_FIELDS = frozenset(
    {
        "spend",
        "social_spend",
        "cpc",
        "cpm",
        "cpp",
        "cost_per_inline_link_click",
        "cost_per_inline_post_engagement",
        "cost_per_unique_click",
        "cost_per_unique_inline_link_click",
        "cost_per_thruplay",
        "cost_per_15_sec_video_view",
        "cost_per_2_sec_continuous_video_view",
        "cost_per_estimated_ad_recallers",
    }
)

#: Insights fields that are LISTS of ``{action_type, value, <window>...}``, not
#: numbers. They carry money (``action_values``, ``cost_per_action_type``) or a
#: ratio (``purchase_roas``), already in major units, one entry per action type.
#:
#: THEY MUST NOT GO THROUGH THE SCALAR MONEY PATH. ``Decimal(str([...]))``
#: raises, :func:`as_decimal` swallows it, and the column silently becomes None
#: -- which is how a live account returned
#: ``[{"action_type": "omni_purchase", "value": "22252.538168", ...}]`` and the
#: report printed nothing. Pass them through whole and let the caller pick an
#: action type out with :func:`action_value`.
#:
#: Each entry also carries a per-window breakdown -- ``1d_view`` and
#: ``7d_click`` alongside the combined ``value`` -- which is lost the moment
#: this is flattened to one number, and is the only way to separate
#: view-through from click-through after the fact.
NESTED_INSIGHTS_FIELDS = frozenset(
    {
        "actions",
        "action_values",
        "cost_per_action_type",
        "cost_per_conversion",
        "cost_per_unique_action_type",
        "purchase_roas",
        "website_purchase_roas",
        "video_play_actions",
        "video_thruplay_watched_actions",
        "outbound_clicks",
        "unique_actions",
    }
)

#: Substrings that make a field name look monetary. Used only to decide whether
#: an unruled field is worth refusing over.
_MONEY_HINTS = ("budget", "spend", "bid", "cost", "cpc", "cpm", "cpp", "revenue",
                "balance", "price", "roas", "value")

#: Names containing a money hint that are definitely NOT amounts. Without this
#: list, perfectly ordinary fields get flagged and every report refuses to load.
_NOT_MONEY = frozenset(
    {
        "bid_strategy",
        "bid_constraints",
        "bid_adjustments",
        "bid_info",
        "budget_rebalance_flag",
        "is_budget_schedule_enabled",
        "budget_remaining_percent",
        "lifetime_budget_min",
        "spend_cap_action",
        "value_per_conversion_ratio",
        "cost_per_action_type_label",
    }
)

#: The statuses a human sets, and the two this toolchain will ever set.
#: DELETED and ARCHIVED are omitted deliberately -- see tools_write.
SETTABLE_STATUSES = ("ACTIVE", "PAUSED")

#: Every value `status` can hold. ARCHIVED and DELETED are read-only here.
ALL_STATUSES = ("ACTIVE", "PAUSED", "DELETED", "ARCHIVED")

#: effective_status values that mean "not delivering, and not because of this
#: object's own status". Worth naming in a preview: pausing something that is
#: already dark for another reason changes nothing a human would notice.
NOT_DELIVERING_FOR_ANOTHER_REASON = frozenset(
    {
        "CAMPAIGN_PAUSED",
        "ADSET_PAUSED",
        "DISAPPROVED",
        "PENDING_REVIEW",
        "PENDING_BILLING_INFO",
        "IN_PROCESS",
        "WITH_ISSUES",
        "PREAPPROVED",
    }
)


class FieldPathError(KeyError):
    """Raised when a dotted path does not exist on the object."""


_MISSING = object()


def leaf_name(path: str) -> str:
    """``'campaign.daily_budget'`` -> ``'daily_budget'``."""
    return path.rsplit(".", 1)[-1]


def is_minor_unit_field(path: str) -> bool:
    """Whether the field at ``path`` is an integer in the currency's minor unit.

    >>> is_minor_unit_field("daily_budget")
    True
    >>> is_minor_unit_field("adset.bid_amount")
    True
    >>> is_minor_unit_field("spend")
    False
    >>> is_minor_unit_field("bid_strategy")
    False
    """
    return leaf_name(path) in MINOR_UNIT_FIELDS


def is_major_unit_field(path: str) -> bool:
    """Whether the field at ``path`` is a SCALAR already in account currency.

    >>> is_major_unit_field("spend")
    True
    >>> is_major_unit_field("daily_budget")
    False
    >>> is_major_unit_field("purchase_roas")
    False
    """
    return leaf_name(path) in MAJOR_UNIT_FIELDS


def is_nested_field(path: str) -> bool:
    """Whether the field at ``path`` is a list of per-action-type entries.

    >>> is_nested_field("purchase_roas")
    True
    >>> is_nested_field("actions")
    True
    >>> is_nested_field("spend")
    False
    """
    return leaf_name(path) in NESTED_INSIGHTS_FIELDS


def unknown_money_fields(paths: Iterable[str]) -> tuple[str, ...]:
    """Selected fields that look monetary but have no ruling in this module.

    Used by the report layer to fail loudly rather than emit a column that is
    silently off by a factor of a hundred, in one direction or the other.

    >>> unknown_money_fields(["spend", "daily_budget", "bid_strategy"])
    ()
    >>> unknown_money_fields(["cost_per_wombat"])
    ('cost_per_wombat',)
    """
    unknown = []
    for path in paths:
        name = leaf_name(path)
        if name in _NOT_MONEY:
            continue
        if not any(hint in name for hint in _MONEY_HINTS):
            continue
        if name in MINOR_UNIT_FIELDS or name in MAJOR_UNIT_FIELDS:
            continue
        if name in NESTED_INSIGHTS_FIELDS:
            continue
        unknown.append(path)
    return tuple(unknown)


def get_path(obj: Any, path: str, default: Any = _MISSING) -> Any:
    """Read a dotted path out of nested Graph API JSON.

    The Graph API nests whenever a field expansion is asked for --
    ``fields=campaign{name}`` puts the name at ``campaign.name`` -- so a flat
    ``obj["name"]`` is right for the object itself and wrong for anything
    expanded.

    >>> get_path({"campaign": {"name": "Brand"}}, "campaign.name")
    'Brand'
    >>> get_path({"id": "1"}, "nope", default=None) is None
    True
    """
    current: Any = obj
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if default is not _MISSING:
            return default
        raise FieldPathError(
            f"{path!r} is not present on this object (stopped at {part!r}). "
            f"Did the request ask for it in `fields`? The Graph API returns "
            f"ONLY the fields named, never a default set."
        )
    return current


def as_decimal(value: Any) -> Decimal | None:
    """A Graph API number or decimal string as a :class:`~decimal.Decimal`.

    Meta sends insight numbers as strings -- ``"12.34"``, ``"0"`` -- so every
    arithmetic use has to parse first. Returning None for an unparseable value
    rather than raising, because one odd row should not fail a whole report.

    >>> as_decimal("12.34")
    Decimal('12.34')
    >>> as_decimal(None) is None
    True
    >>> as_decimal("not a number") is None
    True
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def money_value(obj: Any, path: str, currency: str) -> Decimal | None:
    """The amount at ``path``, in major units, whatever unit it was stored in.

    The single function that gets the direction right. Minor-unit node fields
    are divided; insights fields are parsed and left alone.
    """
    raw = get_path(obj, path, default=None)
    if raw is None or raw == "":
        return None
    if is_minor_unit_field(path):
        return from_minor_units(raw, currency)
    return as_decimal(raw)


def object_to_row(
    obj: dict[str, Any], paths: Iterable[str], currency: str
) -> dict[str, Any]:
    """Flatten one Graph object into ``{path: value}`` for the given paths.

    Column order follows ``paths``. Money is converted to major units on the
    way out, so no caller ever handles a raw minor-unit integer.
    """
    row: dict[str, Any] = {}
    for path in paths:
        # Nested FIRST. These carry money but are lists, and routing one
        # through the scalar money path turns it into None without raising.
        if is_nested_field(path):
            row[path] = get_path(obj, path, default=None)
        elif is_minor_unit_field(path) or is_major_unit_field(path):
            amount = money_value(obj, path, currency)
            row[path] = None if amount is None else str(amount)
        else:
            row[path] = get_path(obj, path, default=None)
    return row


def action_value(actions: Any, action_type: str) -> Decimal | None:
    """Pull one action type out of an insights ``actions`` list.

    ``actions`` is a list of ``{"action_type": ..., "value": "12"}``, not a
    mapping, and the entry for a given type is simply absent when there were
    none -- which is not the same as zero and is routinely read as such.

    >>> action_value([{"action_type": "purchase", "value": "3"}], "purchase")
    Decimal('3')
    >>> action_value([], "purchase") is None
    True
    """
    for entry in actions or []:
        if isinstance(entry, dict) and entry.get("action_type") == action_type:
            return as_decimal(entry.get("value"))
    return None
