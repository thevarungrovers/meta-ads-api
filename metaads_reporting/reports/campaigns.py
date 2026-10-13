"""Daily campaign performance.

``campaign_name`` is included deliberately. Insights rows carry ``campaign_id``,
which is the only reliable key, but an id alone is unreadable in a report a
human opens -- and Meta does not join the name on unless it is asked for.
"""

from __future__ import annotations

from . import Report

CAMPAIGNS = Report(
    name="campaigns",
    description="Daily campaign performance: spend, clicks, actions.",
    level="campaign",
    fields=(
        "campaign_id",
        "campaign_name",
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
        "purchase_roas",
    ),
    time_increment=1,
    sort=("spend_descending",),
)
