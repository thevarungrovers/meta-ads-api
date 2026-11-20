"""What targeting a special ad category forbids.

SOURCE: Meta's "Special Ad Categories" help pages and the Marketing API
special_ad_categories reference, as understood on 2026-11-18. Meta has
changed these rules several times (2019 settlement, 2022 Special Ad Audience
retirement); RE-VERIFY before relying on one. Meta's own validate_only, which
every preview sends, is the backstop: anything this table misses, Meta
refuses at the preview.

For HOUSING, EMPLOYMENT and FINANCIAL_PRODUCTS_SERVICES (CREDIT on older
versions), an ad set may not:
  * narrow age below 18-65+,
  * target a gender,
  * target ZIP or postal codes,
  * target a location with a radius under 15 miles (about 24 km),
  * use a lookalike audience,
  * EXCLUDE detailed targeting (interests, behaviours, demographics).

ISSUES_ELECTIONS_POLITICS requires an authorized advertiser and a disclaimer
but does not carry these targeting restrictions; it is listed so a campaign
carrying it is recognised, not refused.
"""

from __future__ import annotations

from typing import Any

RESTRICTED = frozenset({"HOUSING", "EMPLOYMENT", "FINANCIAL_PRODUCTS_SERVICES", "CREDIT"})
RECOGNISED = RESTRICTED | {"ISSUES_ELECTIONS_POLITICS"}

MIN_RADIUS_MILES = 15
KM_PER_MILE = 1.609344
SOURCE = "Meta Special Ad Categories rules, as of 2026-11-18 (re-verify)"


def categories(campaign: dict[str, Any] | None) -> list[str]:
    raw = (campaign or {}).get("special_ad_categories") or []
    return [str(c) for c in raw if c and str(c) != "NONE"]


def violations(spec: dict[str, Any], cats: list[str], *, lookalike_ids: set[str] = frozenset()) -> list[str]:
    """Every way ``spec`` breaks the restricted categories in ``cats``.

    >>> violations({"age_min": 25}, ["HOUSING"])
    ['age narrowed to 25-65 (must be 18-65+)']
    >>> violations({"age_min": 25}, [])
    []
    """
    restricted = sorted(set(cats) & RESTRICTED)
    if not restricted:
        return []
    found = []
    age_min = int(spec.get("age_min") or 18)
    age_max = int(spec.get("age_max") or 65)
    if age_min > 18 or age_max < 65:
        found.append(f"age narrowed to {age_min}-{age_max} (must be 18-65+)")
    if spec.get("genders"):
        found.append("gender targeting")
    geo = spec.get("geo_locations") or {}
    if geo.get("zips"):
        found.append("ZIP / postal code targeting")
    for city in (geo.get("cities") or []) + (geo.get("custom_locations") or []):
        radius = city.get("radius")
        if radius is None:
            continue
        unit = city.get("distance_unit", "mile")
        miles = float(radius) / KM_PER_MILE if unit == "kilometer" else float(radius)
        if miles < MIN_RADIUS_MILES:
            found.append(f"a location radius of {radius} {unit} (minimum {MIN_RADIUS_MILES} miles)")
    if spec.get("exclusions"):
        found.append("detailed targeting exclusions")
    for audience in spec.get("custom_audiences") or []:
        aid = str(audience.get("id") if isinstance(audience, dict) else audience)
        if aid in lookalike_ids:
            found.append(f"lookalike audience {aid}")
    return [f"{v}" for v in found]
