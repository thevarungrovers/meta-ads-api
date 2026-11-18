"""Change kinds: every typed write the server offers, on one engine.

See :mod:`.base` for what a kind is and :mod:`.engine` for the one sequence
they all go through. :func:`all_kinds` is the registry tools_write publishes.
"""

from __future__ import annotations


def all_kinds() -> list:
    from metaads_mcp.changes.audiences import CreateAudience
    from metaads_mcp.changes.bidding import BIDDING_KINDS
    from metaads_mcp.changes.creative import DuplicateAd, UpdateAdCopy
    from metaads_mcp.changes.delivery import (
        AdSetBidAmount,
        AdSetDailyBudget,
        AdSetStatus,
        AdStatus,
        CampaignDailyBudget,
        CampaignStatus,
    )

    return [
        CampaignStatus(),
        CampaignDailyBudget(),
        AdSetStatus(),
        AdSetDailyBudget(),
        AdSetBidAmount(),
        AdStatus(),
        DuplicateAd(),
        UpdateAdCopy(),
        CreateAudience(),
        *(kind() for kind in BIDDING_KINDS),
    ]
