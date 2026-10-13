"""Building a request to the ``/insights`` edge.

Meta has no query language -- there is no GAQL and no SQL, only a bag of query
parameters -- so "construct the request" and "keep it read-only" are different
problems here than on Google. Read-only is guaranteed by the HTTP method (see
:mod:`metaads_reporting.graph`); what this module exists for is the other half:
getting the parameters right, because several of them are wrong in ways that
return a perfectly plausible number.

THE FOUR THAT ACTUALLY BITE:

* **``time_range`` and ``date_preset`` are mutually exclusive, and sending both
  does not error.** One of them silently wins. :class:`InsightsQuery` refuses
  to carry both rather than guessing which.
* **Dates are in the AD ACCOUNT's time zone**, not this machine's and not UTC.
  A report run from Montreal against a US/Pacific account and a locally
  computed "yesterday" is off by a day at the edges, every time.
* **``time_increment`` decides the SHAPE of the answer.** Left off, the whole
  range collapses into ONE row, so a 30-day request returns a single total and
  looks like a bug. ``time_increment=1`` gives a row per day.
* **``level`` decides what the rows are about and does not have to match the
  object you asked.** ``GET /campaign_id/insights?level=ad`` is legal and
  returns one row per ad. Defaulting it is how a campaign report quietly
  becomes an ad report.

Attribution is its own trap and is handled explicitly: see
:data:`DEFAULT_ATTRIBUTION_WINDOWS`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Sequence


class InsightsError(ValueError):
    """Raised when an insights request cannot be built as asked."""


#: What a row is about. Meta accepts these four and nothing else.
LEVELS = ("account", "campaign", "adset", "ad")

#: Meta's own relative ranges. Preferred over computing dates locally where the
#: account's time zone is not known, because these are evaluated at Meta's end
#: in the ACCOUNT's time zone.
DATE_PRESETS = (
    "today",
    "yesterday",
    "this_month",
    "last_month",
    "this_quarter",
    "maximum",
    "last_3d",
    "last_7d",
    "last_14d",
    "last_28d",
    "last_30d",
    "last_90d",
    "last_week_mon_sun",
    "last_week_sun_sat",
    "last_quarter",
    "last_year",
    "this_week_mon_today",
    "this_week_sun_today",
    "this_year",
)

#: Operators the ``filtering`` parameter accepts.
FILTER_OPERATORS = (
    "EQUAL",
    "NOT_EQUAL",
    "GREATER_THAN",
    "GREATER_THAN_OR_EQUAL",
    "LESS_THAN",
    "LESS_THAN_OR_EQUAL",
    "IN_RANGE",
    "NOT_IN_RANGE",
    "CONTAIN",
    "NOT_CONTAIN",
    "IN",
    "NOT_IN",
    "STARTS_WITH",
    "ANY",
    "ALL",
    "AFTER",
    "BEFORE",
    "NONE",
)

#: Attribution windows requested by default: a click within 7 days, or a view
#: within 1 day.
#:
#: NOT an arbitrary choice, and worth stating because it is the single largest
#: source of "Meta says 400 conversions and our database says 90". Meta's
#: default for an account is whatever that account's attribution setting says,
#: and the 1-day-view half in particular counts people who SAW an ad and never
#: touched it. Asking for the windows explicitly means a report says what it
#: counted instead of inheriting a setting nobody on this side can see.
#:
#: To compare against anything click-based -- a UTM-tagged signup table, say --
#: ask for ``("7d_click",)`` alone.
DEFAULT_ATTRIBUTION_WINDOWS = ("7d_click", "1d_view")

#: Breakdowns that cannot be combined with each other.
#:
#: DELIBERATELY SHORT. Meta's full compatibility matrix is large, undocumented
#: in one place, and changes between API versions, so reproducing it here would
#: produce confident refusals of combinations that actually work -- which is
#: worse than no check, because the API's own refusal at least reflects what
#: the API currently does. (``age`` and ``gender`` together, for instance, is
#: one of the most common valid pairs there is.)
#:
#: What is listed is the one rule that has held across versions: the geographic
#: levels are alternatives, not a hierarchy you can ask for at once. Anything
#: else invalid still reaches Meta and is refused there, with a message about
#: an "invalid breakdown combination" that does not say which two are at fault
#: -- see the note in :func:`_check_breakdowns`.
EXCLUSIVE_BREAKDOWNS = (
    frozenset({"country", "region", "dma"}),
)


def validate_level(level: str) -> str:
    """Return a valid level, or raise.

    >>> validate_level("Campaign")
    'campaign'
    """
    value = (level or "").strip().lower()
    if value not in LEVELS:
        raise InsightsError(
            f"{level!r} is not an insights level. Choose one of: "
            f"{', '.join(LEVELS)}."
        )
    return value


def validate_preset(preset: str) -> str:
    """Return a valid ``date_preset``, or raise.

    >>> validate_preset("LAST_7D")
    'last_7d'
    """
    value = (preset or "").strip().lower()
    if value not in DATE_PRESETS:
        raise InsightsError(
            f"{preset!r} is not a Meta date preset. Choose one of: "
            f"{', '.join(DATE_PRESETS)}."
        )
    return value


def time_range(since: date | str, until: date | str) -> dict[str, str]:
    """A ``{"since": ..., "until": ...}`` range, both ends inclusive.

    >>> time_range("2026-09-01", "2026-09-30")
    {'since': '2026-09-01', 'until': '2026-09-30'}
    """
    start = _as_iso(since, "since")
    end = _as_iso(until, "until")
    if start > end:
        raise InsightsError(f"since {start} is after until {end}.")
    return {"since": start, "until": end}


def last_n_days(days: int, *, today: date | None = None) -> dict[str, str]:
    """The ``days`` days ending YESTERDAY, as a ``time_range``.

    Yesterday, not today: today's metrics are partial all day, and a partial
    day silently drags every average down.

    **This computes dates on THIS machine.** Where the account's time zone is
    not known to differ, that is fine; where it is, prefer a ``date_preset``,
    which Meta evaluates in the account's own zone.

    >>> last_n_days(7, today=date(2026, 10, 8))
    {'since': '2026-10-01', 'until': '2026-10-07'}
    """
    if days <= 0:
        raise InsightsError(f"days must be positive, got {days}.")
    anchor = today or date.today()
    end = anchor - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return time_range(start, end)


def _as_iso(value: date | str, label: str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise InsightsError(
            f"{label} must be a date or an ISO YYYY-MM-DD string, got {value!r}."
        ) from exc


@dataclass(frozen=True)
class Filter:
    """One ``filtering`` clause.

    >>> Filter("campaign.name", "CONTAIN", "Brand").as_dict()
    {'field': 'campaign.name', 'operator': 'CONTAIN', 'value': 'Brand'}
    """

    field: str
    operator: str
    value: Any

    def __post_init__(self) -> None:
        if not self.field:
            raise InsightsError("A filter needs a field.")
        if self.operator.upper() not in FILTER_OPERATORS:
            raise InsightsError(
                f"{self.operator!r} is not a filtering operator. Choose one "
                f"of: {', '.join(FILTER_OPERATORS)}."
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "operator": self.operator.upper(),
            "value": self.value,
        }


def name_contains(entity: str, needle: str) -> Filter:
    """A case-insensitive-ish name filter for ``campaign``/``adset``/``ad``.

    ``CONTAIN`` is Meta's substring operator. It is NOT documented as
    case-insensitive and does not behave like Google's ``LIKE``, so the name
    lookups in ``write/lookup.py`` fetch and match in Python instead. This stays
    for narrowing an insights pull, where an exact-case miss costs a retry
    rather than the wrong campaign.

    >>> name_contains("campaign", "Brand").as_dict()["field"]
    'campaign.name'
    """
    if entity not in ("campaign", "adset", "ad"):
        raise InsightsError(
            f"{entity!r} has no name field to filter on; use campaign, adset "
            f"or ad."
        )
    if not needle.strip():
        raise InsightsError("Search text is empty.")
    return Filter(f"{entity}.name", "CONTAIN", needle)


@dataclass(frozen=True)
class InsightsQuery:
    """An ``/insights`` request, assembled from parts.

    >>> q = InsightsQuery(
    ...     fields=("spend", "impressions"),
    ...     level="campaign",
    ...     date_preset="last_7d",
    ...     time_increment=1,
    ... )
    >>> sorted(q.to_params())
    ['action_attribution_windows', 'date_preset', 'fields', 'level', 'limit', 'time_increment']
    """

    fields: tuple[str, ...]
    level: str = "campaign"
    date_preset: str | None = None
    since: str | None = None
    until: str | None = None
    #: 1 for a row per day; None collapses the whole range into one row.
    time_increment: int | None = None
    breakdowns: tuple[str, ...] = ()
    filtering: tuple[Filter, ...] = ()
    attribution_windows: tuple[str, ...] = DEFAULT_ATTRIBUTION_WINDOWS
    sort: tuple[str, ...] = ()
    limit: int = 500

    def __post_init__(self) -> None:
        if not self.fields:
            raise InsightsError("An insights query must select at least one field.")
        validate_level(self.level)
        if self.date_preset and (self.since or self.until):
            # The whole reason this class exists. Meta accepts both and picks
            # one without saying so, which is a report over a window nobody
            # asked for.
            raise InsightsError(
                "Give either a date_preset or a since/until range, not both. "
                "Meta accepts both and silently applies one of them, so a "
                "report would cover a window nobody chose."
            )
        if bool(self.since) != bool(self.until):
            raise InsightsError(
                "A custom range needs BOTH since and until; Meta does not "
                "infer the open end."
            )
        if self.date_preset:
            validate_preset(self.date_preset)
        if self.since:
            time_range(self.since, self.until or self.since)
        if self.time_increment is not None and not (
            1 <= self.time_increment <= 90
        ):
            raise InsightsError(
                f"time_increment must be between 1 and 90 days, got "
                f"{self.time_increment}. Omit it for a single summary row."
            )
        if self.limit <= 0:
            raise InsightsError(f"limit must be positive, got {self.limit}.")
        _check_breakdowns(self.breakdowns)

    def to_params(self) -> dict[str, Any]:
        """The query parameters, with every structured value JSON-encoded.

        ``filtering``, ``time_range`` and the list parameters all go over the
        wire as JSON strings. Passing a Python list to ``requests`` instead
        produces ``fields=spend&fields=impressions``, which Meta reads as the
        LAST one only -- a report with one column and no error.
        """
        params: dict[str, Any] = {
            "fields": ",".join(self.fields),
            "level": validate_level(self.level),
            "limit": self.limit,
        }
        if self.date_preset:
            params["date_preset"] = validate_preset(self.date_preset)
        if self.since and self.until:
            params["time_range"] = json.dumps(time_range(self.since, self.until))
        if self.time_increment is not None:
            params["time_increment"] = self.time_increment
        if self.breakdowns:
            params["breakdowns"] = ",".join(self.breakdowns)
        if self.filtering:
            params["filtering"] = json.dumps(
                [f.as_dict() for f in self.filtering]
            )
        if self.attribution_windows:
            params["action_attribution_windows"] = json.dumps(
                list(self.attribution_windows)
            )
        if self.sort:
            params["sort"] = json.dumps(list(self.sort))
        return params

    def with_filter(self, *filters: Filter) -> "InsightsQuery":
        """A copy with extra filters appended."""
        from dataclasses import replace

        return replace(self, filtering=self.filtering + tuple(filters))

    def describe_window(self) -> str:
        """The date window in words, for a report header or a tool result."""
        if self.since and self.until:
            return f"{self.since} to {self.until} (account time zone)"
        return f"{self.date_preset or 'account default'} (account time zone)"


def _check_breakdowns(breakdowns: Sequence[str]) -> None:
    """Catch the combinations we are sure about; let Meta rule on the rest.

    This is not a full validation and is not trying to be -- see
    :data:`EXCLUSIVE_BREAKDOWNS`. An unlisted invalid pair reaches the API and
    comes back as "invalid breakdown combination", which names neither
    breakdown; when that happens, drop to one breakdown and add the second back.
    """
    if len(set(breakdowns)) != len(breakdowns):
        raise InsightsError(f"Duplicate breakdown in {list(breakdowns)}.")
    for group in EXCLUSIVE_BREAKDOWNS:
        clash = sorted(group.intersection(breakdowns))
        if len(clash) > 1:
            raise InsightsError(
                f"Breakdowns {' and '.join(clash)} cannot be combined: they are "
                f"alternative geographic levels, not a hierarchy. Pick one."
            )


def node_fields(*paths: str) -> str:
    """Join field paths for a plain node read's ``fields`` parameter.

    The Graph API returns ONLY what is asked for -- there is no default field
    set beyond ``id`` -- so a missing field reads as a missing value rather
    than an error.

    >>> node_fields("id", "name", "status")
    'id,name,status'
    """
    flat: list[str] = []
    for path in paths:
        if isinstance(path, str):
            flat.append(path)
        else:  # pragma: no cover - defensive
            flat.extend(path)
    if not flat:
        raise InsightsError("No fields requested.")
    return ",".join(flat)


def summarise(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Totals for an insights result, with ratios RECOMPUTED, not averaged.

    Averaging a column of CPCs or CTRs weights every row equally, which is
    wrong whenever rows differ in volume. Derived figures come from the summed
    numerator and denominator.
    """
    from decimal import Decimal

    from .fields import as_decimal

    totals = {"spend": Decimal(0), "impressions": Decimal(0), "clicks": Decimal(0),
              "reach": Decimal(0)}
    rows = list(rows)
    for row in rows:
        for key in totals:
            value = as_decimal(row.get(key))
            if value is not None:
                totals[key] += value

    out = {key: str(value) for key, value in totals.items()}
    out["rows"] = str(len(rows))
    if totals["impressions"]:
        out["ctr"] = str(
            (totals["clicks"] / totals["impressions"] * 100).quantize(Decimal("0.0001"))
        )
        out["cpm"] = str(
            (totals["spend"] / totals["impressions"] * 1000).quantize(Decimal("0.01"))
        )
    if totals["clicks"]:
        out["cpc"] = str((totals["spend"] / totals["clicks"]).quantize(Decimal("0.0001")))
    # Reach is DELIBERATELY not used to derive a frequency here: reach cannot be
    # summed across rows -- the same person reached on two days is one person,
    # and adding the rows counts them twice. The per-row value is the only one
    # that means anything.
    return out
