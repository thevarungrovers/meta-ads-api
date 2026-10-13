"""Report definitions.

A report is declarative: an insights ``level``, a field list and a default
window. It holds no client and makes no request, so a report can be inspected,
diffed and checked offline -- ``tests/test_reports.py`` resolves every field
name against the known insights vocabulary, which catches a typo without a
single API call.

Add a report by defining one in a module here and listing it in
:data:`REGISTRY`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..fields import unknown_money_fields
from ..insights import (
    DEFAULT_ATTRIBUTION_WINDOWS,
    Filter,
    InsightsError,
    InsightsQuery,
    validate_level,
    validate_preset,
)


@dataclass(frozen=True)
class Report:
    """A named, read-only insights report.

    ``fields`` order is the output column order.
    """

    name: str
    description: str
    level: str
    fields: tuple[str, ...]
    default_preset: str = "last_30d"
    #: 1 for a row per day. None collapses the window into one summary row --
    #: which is right for a ranking and wrong for a trend, so each report says.
    time_increment: int | None = 1
    breakdowns: tuple[str, ...] = ()
    sort: tuple[str, ...] = ()
    attribution_windows: tuple[str, ...] = DEFAULT_ATTRIBUTION_WINDOWS

    def __post_init__(self) -> None:
        if not self.fields:
            raise InsightsError(f"Report {self.name!r} selects no fields.")
        validate_level(self.level)
        validate_preset(self.default_preset)
        # Fail at import time rather than emitting a column that is silently
        # off by a factor of a hundred, in either direction.
        unruled = unknown_money_fields(self.fields)
        if unruled:
            raise InsightsError(
                f"Report {self.name!r} selects monetary field(s) with no unit "
                f"ruling in metaads_reporting.fields: {', '.join(unruled)}. "
                f"Add them to MINOR_UNIT_FIELDS or MAJOR_UNIT_FIELDS."
            )

    @property
    def columns(self) -> tuple[str, ...]:
        """Output column names, including the two Meta adds to every row."""
        return tuple(self.fields) + ("date_start", "date_stop")

    def build(
        self,
        *,
        preset: str | None = None,
        since: str | None = None,
        until: str | None = None,
        filtering: tuple[Filter, ...] = (),
        time_increment: int | None = -1,
        limit: int = 500,
    ) -> InsightsQuery:
        """Assemble this report's :class:`~metaads_reporting.insights.InsightsQuery`.

        ``time_increment=-1`` is the sentinel for "use the report's own", so a
        caller can still pass ``None`` to mean "one summary row" -- which is a
        real and different answer.
        """
        if since or until:
            if preset:
                raise InsightsError(
                    "Give either a preset or a since/until range, not both."
                )
            window = {"since": since, "until": until}
        else:
            window = {"date_preset": preset or self.default_preset}

        return InsightsQuery(
            fields=self.fields,
            level=self.level,
            time_increment=(
                self.time_increment if time_increment == -1 else time_increment
            ),
            breakdowns=self.breakdowns,
            filtering=tuple(filtering),
            attribution_windows=self.attribution_windows,
            sort=self.sort,
            limit=limit,
            **window,
        )


from .accounts import ACCOUNT  # noqa: E402  (import after Report is defined)
from .ads import ADS, CREATIVE_FATIGUE  # noqa: E402
from .adsets import ADSETS  # noqa: E402
from .campaigns import CAMPAIGNS  # noqa: E402

REGISTRY: dict[str, Report] = {
    report.name: report
    for report in (ACCOUNT, CAMPAIGNS, ADSETS, ADS, CREATIVE_FATIGUE)
}


def report_names() -> list[str]:
    return sorted(REGISTRY)


def get_report(name: str) -> Report:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"Unknown report {name!r}. Available: {', '.join(report_names())}."
        ) from None


__all__ = ["Report", "REGISTRY", "get_report", "report_names"]
