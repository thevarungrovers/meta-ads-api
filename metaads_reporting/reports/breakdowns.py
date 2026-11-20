"""Reports broken down by WHO and WHERE: placement, demographics, geography,
hour of day, and conversions by action type.

Each collapses the window into one row per breakdown value (no daily rows), so
the shares are comparable. Reach is left out: it cannot be summed across rows.
"""

from __future__ import annotations

from . import Report

_CORE = ("spend", "impressions", "clicks", "ctr", "cpc", "cpm", "actions")

PLACEMENTS = Report(
    name="placements",
    description="Spend and results per placement (publisher_platform x platform_position).",
    level="account",
    fields=_CORE,
    time_increment=None,
    breakdowns=("publisher_platform", "platform_position"),
    sort=("spend_descending",),
)

DEMOGRAPHICS = Report(
    name="demographics",
    description="Spend and results per age band and gender.",
    level="account",
    fields=_CORE,
    time_increment=None,
    breakdowns=("age", "gender"),
    sort=("spend_descending",),
)

GEO = Report(
    name="geo",
    description="Spend and results per region (use run_report breakdowns for country or dma).",
    level="account",
    fields=_CORE,
    time_increment=None,
    breakdowns=("region",),
    sort=("spend_descending",),
)

HOUR_OF_DAY = Report(
    name="hour_of_day",
    description="Spend and results per hour, in the advertiser's (account) time zone.",
    level="account",
    fields=_CORE,
    time_increment=None,
    breakdowns=("hourly_stats_aggregated_by_advertiser_time_zone",),
)

CONVERSIONS_BY_ACTION = Report(
    name="conversions_by_action",
    description="Per campaign: every action type with its count, value and cost.",
    level="campaign",
    fields=(
        "campaign_id",
        "campaign_name",
        "spend",
        "actions",
        "action_values",
        "cost_per_action_type",
    ),
    time_increment=None,
    sort=("spend_descending",),
)
