"""Ad-level performance, and the fatigue view.

Two reports off the same level, because they answer different questions and
want different shapes.

``ads`` is a daily trend. ``creative_fatigue`` is deliberately NOT daily: it
collapses the window into one row per ad so ``frequency`` means something.
Frequency is impressions over REACH, and reach cannot be summed across days --
the same person reached on Monday and Tuesday is one person, and adding the
rows counts them twice. A daily frequency column therefore looks flat and
reassuring on exactly the ad that is burning out.
"""

from __future__ import annotations

from . import Report

ADS = Report(
    name="ads",
    description="Daily ad performance, with its ad set and campaign.",
    level="ad",
    fields=(
        "campaign_id",
        "campaign_name",
        "adset_id",
        "adset_name",
        "ad_id",
        "ad_name",
        "spend",
        "impressions",
        "clicks",
        "ctr",
        "cpc",
        "cpm",
        "actions",
    ),
    time_increment=1,
    sort=("spend_descending",),
)

CREATIVE_FATIGUE = Report(
    name="creative_fatigue",
    description=(
        "One row per ad over the whole window: frequency, reach and CTR, for "
        "spotting a creative that has worn out."
    ),
    level="ad",
    fields=(
        "campaign_name",
        "adset_name",
        "ad_id",
        "ad_name",
        "spend",
        "impressions",
        "reach",
        "frequency",
        "clicks",
        "ctr",
        "cpm",
        "actions",
    ),
    default_preset="last_30d",
    # One row per ad for the whole window. See the module docstring: a daily
    # frequency is not a frequency.
    time_increment=None,
    sort=("spend_descending",),
)
