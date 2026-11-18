"""What each placement shows: text cut-offs, image shapes, video lengths.

THE NUMBERS ARE META'S ADS GUIDE, AND THEY MOVE. Source: the Meta Ads Guide
(facebook.com/business/ads-guide), per placement, as understood on
2026-11-18. Re-check them against the guide before relying on one for a
decision; every check built on this table is a WARN, except an image below the
hard minimum width, which Meta itself refuses.

Two vocabularies meet here:

* ``targeting.publisher_platforms`` + ``*_positions`` -- where an AD SET runs
  (``facebook``/``feed``, ``instagram``/``story`` ...);
* ``ad_format`` -- what the ``/previews`` edges take
  (``MOBILE_FEED_STANDARD``, ``INSTAGRAM_STORY`` ...).

:data:`POSITION_FORMATS` maps the first to the second.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Below this width an image FAILs the preview. Deliberately conservative: the
#: guide's per-placement minimums (500-600px) are recommendations Meta warns
#: on, and its absolute refusal threshold is not published per placement, so
#: only an image no placement could use blocks. Everything between this and
#: the recommended width is a WARN.
HARD_MIN_WIDTH = 200

#: (publisher_platform, position) -> the ad_format that previews it.
POSITION_FORMATS: dict[tuple[str, str], str] = {
    ("facebook", "feed"): "MOBILE_FEED_STANDARD",
    ("facebook", "story"): "FACEBOOK_STORY_MOBILE",
    ("facebook", "facebook_reels"): "FACEBOOK_REELS_MOBILE",
    ("facebook", "marketplace"): "MARKETPLACE_MOBILE",
    ("facebook", "right_hand_column"): "RIGHT_COLUMN_STANDARD",
    ("instagram", "stream"): "INSTAGRAM_STANDARD",
    ("instagram", "story"): "INSTAGRAM_STORY",
    ("instagram", "reels"): "INSTAGRAM_REELS",
    ("instagram", "explore"): "INSTAGRAM_EXPLORE_CONTEXTUAL",
    ("messenger", "messenger_home"): "MESSENGER_MOBILE_INBOX_MEDIA",
    ("messenger", "story"): "MESSENGER_MOBILE_STORY_MEDIA",
    ("audience_network", "classic"): "MOBILE_INTERSTITIAL",
}

#: The formats worth previewing when an ad set uses Advantage+ placements (no
#: publisher_platforms at all) and no spend breakdown says which matter. Most
#: delivery lands in the first few.
DEFAULT_FORMAT_ORDER: tuple[str, ...] = (
    "MOBILE_FEED_STANDARD",
    "INSTAGRAM_STANDARD",
    "INSTAGRAM_STORY",
    "INSTAGRAM_REELS",
    "FACEBOOK_REELS_MOBILE",
    "FACEBOOK_STORY_MOBILE",
    "DESKTOP_FEED_STANDARD",
    "INSTAGRAM_EXPLORE_CONTEXTUAL",
    "MARKETPLACE_MOBILE",
    "MESSENGER_MOBILE_INBOX_MEDIA",
)


@dataclass(frozen=True)
class PlacementSpec:
    """What one ad_format shows before it truncates, crops or refuses."""

    label: str
    #: Characters of primary text shown before "See more".
    primary_text_visible: int
    #: Headline length before it is cut (None: not shown in this placement).
    headline_max: int | None
    description_max: int | None
    #: Width/height ratios that fill the placement without cropping, inclusive.
    min_ratio: float
    max_ratio: float
    recommended_min_width: int
    #: Below this Meta refuses the image outright.
    hard_min_width: int
    #: Longest video the placement plays, in seconds (None: no stated limit).
    video_max_seconds: int | None


_FEED = dict(min_ratio=0.8, max_ratio=1.91, recommended_min_width=600, hard_min_width=HARD_MIN_WIDTH)
_VERTICAL = dict(min_ratio=0.5, max_ratio=0.6, recommended_min_width=600, hard_min_width=HARD_MIN_WIDTH)

SPECS: dict[str, PlacementSpec] = {
    "MOBILE_FEED_STANDARD": PlacementSpec(
        "Facebook feed", 125, 40, 30, video_max_seconds=241 * 60, **_FEED
    ),
    "DESKTOP_FEED_STANDARD": PlacementSpec(
        "Facebook feed (desktop)", 125, 40, 30, video_max_seconds=241 * 60, **_FEED
    ),
    "MARKETPLACE_MOBILE": PlacementSpec(
        "Facebook Marketplace", 125, 40, None, video_max_seconds=241 * 60, **_FEED
    ),
    "RIGHT_COLUMN_STANDARD": PlacementSpec(
        "Facebook right column", 125, 40, None, video_max_seconds=None,
        min_ratio=1.0, max_ratio=1.91, recommended_min_width=1080, hard_min_width=HARD_MIN_WIDTH,
    ),
    "INSTAGRAM_STANDARD": PlacementSpec(
        "Instagram feed", 125, None, None, video_max_seconds=60 * 60, **_FEED
    ),
    "INSTAGRAM_EXPLORE_CONTEXTUAL": PlacementSpec(
        "Instagram Explore", 125, None, None, video_max_seconds=60 * 60, **_FEED
    ),
    "MESSENGER_MOBILE_INBOX_MEDIA": PlacementSpec(
        "Messenger inbox", 125, 40, None, video_max_seconds=None,
        min_ratio=1.0, max_ratio=1.91, recommended_min_width=1080, hard_min_width=HARD_MIN_WIDTH,
    ),
    "INSTAGRAM_STORY": PlacementSpec(
        "Instagram Stories", 72, 40, None, video_max_seconds=60, **_VERTICAL
    ),
    "INSTAGRAM_REELS": PlacementSpec(
        "Instagram Reels", 72, 40, None, video_max_seconds=15 * 60, **_VERTICAL
    ),
    "FACEBOOK_STORY_MOBILE": PlacementSpec(
        "Facebook Stories", 72, 40, None, video_max_seconds=120, **_VERTICAL
    ),
    "FACEBOOK_REELS_MOBILE": PlacementSpec(
        "Facebook Reels", 72, 40, None, video_max_seconds=90, **_VERTICAL
    ),
    "MESSENGER_MOBILE_STORY_MEDIA": PlacementSpec(
        "Messenger Stories", 72, 40, None, video_max_seconds=120, **_VERTICAL
    ),
    "MOBILE_INTERSTITIAL": PlacementSpec(
        "Audience Network", 125, 40, None, video_max_seconds=120,
        min_ratio=0.5, max_ratio=1.91, recommended_min_width=1080, hard_min_width=HARD_MIN_WIDTH,
    ),
}


def spec_for(ad_format: str) -> PlacementSpec | None:
    return SPECS.get(ad_format)


def formats_for_targeting(targeting: dict | None) -> list[str]:
    """The ad_formats an ad set's targeting actually places ads in.

    No ``publisher_platforms`` means Advantage+ placements: everywhere, so the
    default order. Platforms without positions mean every position on them.

    >>> formats_for_targeting({"publisher_platforms": ["instagram"], "instagram_positions": ["story"]})
    ['INSTAGRAM_STORY']
    >>> formats_for_targeting({})[:2]
    ['MOBILE_FEED_STANDARD', 'INSTAGRAM_STANDARD']
    """
    targeting = targeting or {}
    platforms = targeting.get("publisher_platforms") or []
    if not platforms:
        return list(DEFAULT_FORMAT_ORDER)
    found: list[str] = []
    for platform in platforms:
        positions = targeting.get(f"{platform}_positions")
        for (p, position), ad_format in POSITION_FORMATS.items():
            if p != platform:
                continue
            if positions and position not in positions:
                continue
            if ad_format not in found:
                found.append(ad_format)
    order = {f: i for i, f in enumerate(DEFAULT_FORMAT_ORDER)}
    return sorted(found, key=lambda f: order.get(f, len(order)))


def format_for_breakdown(platform: str | None, position: str | None) -> str | None:
    """The ad_format for one insights row broken down by placement."""
    if not platform or not position:
        return None
    return POSITION_FORMATS.get((platform, position))
