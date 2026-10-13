"""Daily ad set performance."""

from __future__ import annotations

from . import Report

ADSETS = Report(
    name="adsets",
    description="Daily ad set performance, with its parent campaign.",
    level="adset",
    fields=(
        "campaign_id",
        "campaign_name",
        "adset_id",
        "adset_name",
        "spend",
        "impressions",
        "clicks",
        "reach",
        "frequency",
        "ctr",
        "cpc",
        "cpm",
        "actions",
        "action_values",
    ),
    time_increment=1,
    sort=("spend_descending",),
)
