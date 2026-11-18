"""Who an ad set reaches: one kind, many edits, and the whole spec every time.

META REPLACES TARGETING WHOLESALE. An update carrying ``targeting`` with only
the new interest would wipe the geography, the ages, the audiences -- so this
kind reads the full current spec, applies the edits to a copy, and always
sends the FULL merged spec. The previous spec goes into the ledger, so
revert_change puts back exactly what was there: detaching an audience is the
undo for attaching one.

Guardrails (all Checks, so they show in the preview):
  * max_audiences_per_ad_set, include + exclude;
  * an audience Meta cannot deliver to yet is refused; one still populating
    but usable warns;
  * overlap between included audiences, estimated from delivery_estimate
    arithmetic (Meta's overlap tool has no API), WARN over overlap_warn_pct;
  * an exclusion that swallows the included reach: WARN over
    exclusion_swallow_pct, FAIL when nothing would be left;
  * reach before and after: widening beyond max_reach_change_pct FAILs,
    narrowing beyond it WARNs;
  * special ad category rules (metaads_reporting/special_ad_rules.py), with
    Meta's validate_only in the preview as the backstop.
"""

from __future__ import annotations

import copy
import itertools
from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from metaads_mcp.changes.base import APPLY_SPENDS, Proposal
from metaads_mcp.changes.common import NodeFieldKind, ad_set_activation_bound, as_obj, canonical
from metaads_mcp.guardrails import Check
from metaads_reporting import reach, special_ad_rules
from metaads_reporting.audience_status import usability
from metaads_reporting.write.audiences import FIELDS as AUDIENCE_FIELDS
from metaads_reporting.write.audiences import check_no_customer_data

#: Detailed-targeting categories that live in flexible_spec / exclusions.
DETAILED = ("interests", "behaviors", "demographics", "life_events", "industries",
            "income", "family_statuses", "work_employers", "work_positions",
            "education_schools", "education_majors")

#: Pairs checked for overlap at most (each costs three estimate calls).
MAX_OVERLAP_PAIRS = 5

#: WHERE the ads run: placements, then devices and operating systems. Part of
#: the targeting spec (so Meta replaces them with it), but not something this
#: kind edits -- so they must survive every write and every revert it makes.
PLACEMENT_KEYS = ("publisher_platforms", "facebook_positions", "instagram_positions",
                  "messenger_positions", "audience_network_positions")
DEVICE_KEYS = ("device_platforms", "user_os", "user_device", "excluded_user_device",
               "wireless_carrier")
WHERE_KEYS = PLACEMENT_KEYS + DEVICE_KEYS

#: Top-level keys :func:`fingerprint` reads. Any other key is compared raw.
FINGERPRINTED = ("geo_locations", "excluded_geo_locations", "age_min", "age_max", "genders",
                 "locales", "flexible_spec", "exclusions", "custom_audiences",
                 "excluded_custom_audiences", "targeting_automation", *WHERE_KEYS)


def _ids(items) -> list[str]:
    return sorted(str(i.get("id") if isinstance(i, dict) else i) for i in items or [])


def fingerprint(spec: dict[str, Any] | None, *, where: bool = True) -> str:
    """The parts of a targeting spec a human approves, in one spelling.

    What the preview shows as before/after, and the per-dimension staleness key
    for a revert. Meta adds derived keys when it reads targeting back
    (location_types, brand-safety defaults ...), so a revert comparing the raw
    spec would call every targeting change stale the moment it landed. The
    APPLY compares the raw spec instead (both sides are Meta read-backs).

    Placements and devices are included (``where=False`` drops them, for rows
    written before they were): they ride in the same spec, and a before/after
    that hid them would hide the one change a write could make silently.
    """
    import json

    spec = as_obj(spec) or {}
    geo = spec.get("geo_locations") or {}
    xgeo = spec.get("excluded_geo_locations") or {}

    def places(g):
        out = {}
        if g.get("countries"):
            out["countries"] = sorted(g["countries"])
        for key in ("regions", "cities", "zips", "geo_markets", "custom_locations"):
            if g.get(key):
                out[key] = sorted(
                    f"{p.get('key') or p.get('name') or p.get('latitude')}"
                    + (f"@{p.get('radius')}{p.get('distance_unit', '')}" if p.get("radius") else "")
                    for p in g[key]
                )
        return out

    flex = {}
    for block in spec.get("flexible_spec") or []:
        for category, items in block.items():
            flex.setdefault(category, []).extend(_ids(items))
    out = {
        "geo": places(geo),
        "excluded_geo": places(xgeo),
        "age": [spec.get("age_min"), spec.get("age_max")],
        "genders": sorted(spec.get("genders") or []),
        "locales": sorted(spec.get("locales") or []),
        "detailed": {k: sorted(v) for k, v in sorted(flex.items())},
        "exclusions": {k: _ids(v) for k, v in sorted((spec.get("exclusions") or {}).items())},
        "custom_audiences": _ids(spec.get("custom_audiences")),
        "excluded_custom_audiences": _ids(spec.get("excluded_custom_audiences")),
        "advantage_audience": (spec.get("targeting_automation") or {}).get("advantage_audience"),
    }
    if where:
        out["placements"] = {
            k: sorted(str(v) for v in (spec[k] if isinstance(spec[k], list) else [spec[k]]))
            for k in WHERE_KEYS if spec.get(k)
        }
    return json.dumps({k: v for k, v in out.items() if v not in ({}, [], [None, None], None)},
                      sort_keys=True, separators=(",", ":"))


def touched_keys(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """The top-level targeting keys an edit changed: the dimensions it owns."""
    return sorted(k for k in set(before) | set(after)
                  if canonical(before.get(k)) != canonical(after.get(k)))


def dimensions(spec: dict[str, Any], keys, *, sent: dict[str, Any] | None = None) -> str:
    """``spec`` restricted to ``keys``, spelled for comparison.

    Fingerprinted keys compare by fingerprint (ignoring what Meta derives on
    read). Any other key compares raw, but only when it was SENT: a key the
    edit dropped that Meta then re-derives on read is not a change.
    """
    keys = list(keys)
    known = {k: spec[k] for k in keys if k in FINGERPRINTED and k in spec}
    other = {k: spec.get(k) for k in keys
             if k not in FINGERPRINTED and (sent is None or k in sent)}
    return f"{fingerprint(known)} {canonical(other)}"


def with_dimensions(spec: dict[str, Any], keys, values: dict[str, Any]) -> dict[str, Any]:
    """``spec`` with ``keys`` set from ``values`` (absent there: removed)."""
    out = copy.deepcopy(spec)
    for key in keys:
        if key in values:
            out[key] = copy.deepcopy(values[key])
        else:
            out.pop(key, None)
    return out


def placement_widening(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """How ``after`` lets the ads run in MORE places or on more devices.

    A restriction dropped (no publisher_platforms is Advantage+ placements,
    everywhere), or a platform, position, device or OS added to one, widens.
    Removing an excluded device widens too.
    """
    def values(spec, key):
        raw = spec.get(key)
        if raw in (None, "", [], {}):
            return set()
        return {str(v) for v in (raw if isinstance(raw, list) else [raw])}

    reasons = []
    for key in WHERE_KEYS:
        was, now = values(before, key), values(after, key)
        if key == "excluded_user_device":
            if was - now:
                reasons.append(f"{key} no longer excludes {sorted(was - now)}")
            continue
        if was and not now:
            reasons.append(f"{key} restriction {sorted(was)} removed"
                           + (" (Advantage+ placements: everywhere)" if key == "publisher_platforms" else ""))
        elif was and now - was:
            reasons.append(f"{key} adds {sorted(now - was)}")
    return reasons


def _place_tokens(spec: dict[str, Any], key: str) -> set[str]:
    geo = spec.get(key) or {}
    out = set()
    for kind, items in geo.items():
        if kind == "location_types" or not isinstance(items, list):
            continue
        for p in items:
            if isinstance(p, dict):
                ident = p.get("key") or p.get("name") or f"{p.get('latitude')},{p.get('longitude')}"
                out.add(f"{kind}:{ident}@{p.get('radius')}{p.get('distance_unit', '')}")
            else:
                out.add(f"{kind}:{p}")
    return out


def _detailed_tokens(block: dict[str, Any] | None) -> set[str]:
    return {f"{cat}:{i}" for cat, items in (block or {}).items() for i in _ids(items)}


def reach_widening(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Every way ``after`` can reach MORE people than ``before``.

    Structural, so it holds when the reach estimate is not there: a widening
    edit then fails closed instead of passing on a missing number. Errs toward
    "widens" where the arithmetic is not certain (detailed targeting is ORed
    inside a group, so most edits to an existing one can widen).
    """
    reasons: list[str] = []
    if _place_tokens(after, "geo_locations") - _place_tokens(before, "geo_locations"):
        reasons.append("places added")
    if _place_tokens(before, "excluded_geo_locations") - _place_tokens(after, "excluded_geo_locations"):
        reasons.append("excluded places removed")
    if int(after.get("age_min") or 18) < int(before.get("age_min") or 18):
        reasons.append("minimum age lowered")
    if int(after.get("age_max") or 65) > int(before.get("age_max") or 65):
        reasons.append("maximum age raised")
    for key, what in (("genders", "genders"), ("locales", "languages")):
        was = {str(v) for v in before.get(key) or []}
        now = {str(v) for v in after.get(key) or []}
        if was and (not now or now - was):
            reasons.append(f"{what} widened")
    was_flex, now_flex = before.get("flexible_spec") or [], after.get("flexible_spec") or []
    if was_flex and canonical(was_flex) != canonical(now_flex):
        reasons.append("detailed targeting changed (removing or adding to a group can widen)")
    if _detailed_tokens(before.get("exclusions")) - _detailed_tokens(after.get("exclusions")):
        reasons.append("detailed exclusions removed")
    was_in = set(_ids(before.get("custom_audiences")))
    now_in = set(_ids(after.get("custom_audiences")))
    if was_in and (not now_in or now_in - was_in):
        reasons.append("included audiences widened")
    if set(_ids(before.get("excluded_custom_audiences"))) - set(_ids(after.get("excluded_custom_audiences"))):
        reasons.append("excluded audiences removed")
    if ((after.get("targeting_automation") or {}).get("advantage_audience") == 1
            and (before.get("targeting_automation") or {}).get("advantage_audience") != 1):
        reasons.append("Advantage+ audience turned on")
    reasons.extend(placement_widening(before, after))
    others = sorted(k for k in set(before) | set(after) if k not in FINGERPRINTED
                    and canonical(before.get(k)) != canonical(after.get(k)))
    if others:
        reasons.append(f"other targeting keys changed ({', '.join(others)})")
    return reasons


def _merge_places(target: dict, places: dict, *, remove: bool = False) -> None:
    for key, items in (places or {}).items():
        current = list(target.get(key) or [])
        if key == "countries":
            values = [str(v) for v in items]
            current = [c for c in current if c not in values] if remove else sorted(set(current) | set(values))
        else:
            keyed = {str(p.get("key")): p for p in current if isinstance(p, dict)}
            for place in items:
                if remove:
                    keyed.pop(str(place.get("key") if isinstance(place, dict) else place), None)
                else:
                    keyed[str(place["key"])] = dict(place)
            current = list(keyed.values())
        if current:
            target[key] = current
        else:
            target.pop(key, None)


def apply_edits(spec: dict[str, Any], edits: dict[str, Any], saved: dict | None = None) -> dict[str, Any]:
    """The full spec after ``edits``. Never mutates ``spec``."""
    new = copy.deepcopy(as_obj(saved) if saved is not None else spec) or {}
    if edits.get("geo_include"):
        new.setdefault("geo_locations", {})
        _merge_places(new["geo_locations"], edits["geo_include"])
    if edits.get("geo_remove"):
        _merge_places(new.setdefault("geo_locations", {}), edits["geo_remove"], remove=True)
    if edits.get("geo_exclude"):
        _merge_places(new.setdefault("excluded_geo_locations", {}), edits["geo_exclude"])
    for key in ("age_min", "age_max"):
        if edits.get(key) is not None:
            new[key] = int(edits[key])
    if edits.get("genders") is not None:
        if edits["genders"]:
            new["genders"] = sorted({int(g) for g in edits["genders"]})
        else:
            new.pop("genders", None)
    if edits.get("locales") is not None:
        if edits["locales"]:
            new["locales"] = sorted({int(x) for x in edits["locales"]})
        else:
            new.pop("locales", None)
    if edits.get("detailed_add"):
        flex = new.setdefault("flexible_spec", [{}])
        if not flex:
            flex.append({})
        for category, items in edits["detailed_add"].items():
            have = {str(i.get("id")) for i in flex[0].get(category, [])}
            flex[0][category] = flex[0].get(category, []) + [
                dict(i) for i in items if str(i.get("id")) not in have
            ]
    if edits.get("detailed_exclude"):
        exclusions = new.setdefault("exclusions", {})
        for category, items in edits["detailed_exclude"].items():
            have = {str(i.get("id")) for i in exclusions.get(category, [])}
            exclusions[category] = exclusions.get(category, []) + [
                dict(i) for i in items if str(i.get("id")) not in have
            ]
    if edits.get("detailed_remove"):
        gone = {str(i) for i in edits["detailed_remove"]}
        for block in new.get("flexible_spec") or []:
            for category in list(block):
                block[category] = [i for i in block[category] if str(i.get("id")) not in gone]
                if not block[category]:
                    del block[category]
        new["flexible_spec"] = [b for b in new.get("flexible_spec") or [] if b]
        if not new["flexible_spec"]:
            new.pop("flexible_spec")
        for category in list(new.get("exclusions") or {}):
            new["exclusions"][category] = [
                i for i in new["exclusions"][category] if str(i.get("id")) not in gone
            ]
            if not new["exclusions"][category]:
                del new["exclusions"][category]
        if "exclusions" in new and not new["exclusions"]:
            new.pop("exclusions")
    for key, edit in (("custom_audiences", "audiences_include"),
                      ("excluded_custom_audiences", "audiences_exclude")):
        if edits.get(edit):
            have = _ids(new.get(key))
            new[key] = [{"id": i} for i in sorted(set(have) | {str(a) for a in edits[edit]})]
    if edits.get("audiences_remove"):
        gone = {str(a) for a in edits["audiences_remove"]}
        for key in ("custom_audiences", "excluded_custom_audiences"):
            kept = [{"id": i} for i in _ids(new.get(key)) if i not in gone]
            if kept:
                new[key] = kept
            else:
                new.pop(key, None)
    if edits.get("advantage_audience") is not None:
        new.setdefault("targeting_automation", {})["advantage_audience"] = 1 if edits["advantage_audience"] else 0
    return new


def validate_spec(ctx, spec: dict[str, Any], *, campaign: dict | None,
                  lookalike_ids: set[str] = frozenset(), require_geo: bool = True) -> list[Check]:
    """The rules every targeting spec here goes through -- an edit, a saved
    audience, a new ad set -- so creating and editing share one rule set."""
    checks: list[Check] = []
    try:
        check_no_customer_data(spec)
        for value in spec.values():
            if isinstance(value, dict):
                check_no_customer_data(value)
    except Exception as exc:  # noqa: BLE001 - a refusal, reported as a FAIL
        checks.append(Check("no_customer_data", False, str(exc)))
    age_min, age_max = spec.get("age_min"), spec.get("age_max")
    if age_min is not None and not 13 <= int(age_min) <= 65:
        checks.append(Check("age_range", False, f"age_min {age_min} outside 13-65"))
    if age_max is not None and not 13 <= int(age_max) <= 65:
        checks.append(Check("age_range", False, f"age_max {age_max} outside 13-65"))
    if age_min is not None and age_max is not None and int(age_min) > int(age_max):
        checks.append(Check("age_range", False, f"age_min {age_min} above age_max {age_max}"))
    if not (spec.get("geo_locations") or {}):
        checks.append(Check(
            "geo_locations", not require_geo,
            "no geo_locations: an ad set needs them" if require_geo
            else "no geo_locations: Meta will ask for them when an ad set uses this",
            warn=not require_geo,
        ))
    cats = special_ad_rules.categories(campaign)
    broken = special_ad_rules.violations(spec, cats, lookalike_ids=lookalike_ids)
    if cats:
        checks.append(Check(
            "special_ad_categories", not broken,
            (f"{', '.join(cats)}: not allowed -- {'; '.join(broken)}" if broken
             else f"{', '.join(cats)}: no restricted targeting") + f" [{special_ad_rules.SOURCE}]",
        ))
    return checks


class AdSetTargeting(NodeFieldKind):
    name = "ad_set_targeting"
    preview_title = "Preview a targeting change on an ad set (audiences, places, ages, interests)"
    apply_title = "Apply an ad set targeting change (CHANGES WHO SEES THE ADS)"
    apply_annotations = APPLY_SPENDS
    graph_type = "adset"
    entity_type = "ad set"
    read_fields = ("status", "effective_status", "campaign_id", "account_id", "targeting",
                   "optimization_goal", "daily_budget", "lifetime_budget", "budget_remaining",
                   "end_time")
    ledger_keys = (("ad set", "targeting"),)

    def render(self, ctx, row):
        return fingerprint(row.get("targeting"))

    def _spec(self, ctx, entity_id) -> dict[str, Any]:
        return as_obj(self.read_row(ctx, entity_id).get("targeting")) or {}

    def current(self, ctx, payload):
        # The before/after shown is the fingerprint, but the APPLY compares the
        # WHOLE raw spec (both sides are Meta read-backs, so derived keys match):
        # anything moved since the preview -- a placement, a device, a key
        # this server has no name for -- refuses rather than being merged blind.
        spec = self._spec(ctx, payload["entity_id"])
        if canonical(spec) == payload["full_before"]:
            return payload["before"]
        shown = fingerprint(spec)
        return shown if shown != payload["before"] else f"{shown} (other targeting changed)"

    def build(self, ctx, payload):
        # Re-read and merge ONLY the dimensions this edit owns onto what is
        # there now, so nothing else in the spec is sent back from memory.
        spec = with_dimensions(self._spec(ctx, payload["entity_id"]),
                               payload["touched"], payload["sent"])
        return self._plan(payload["entity_id"], payload.get("name", ""),
                          {"targeting": spec}, payload["before"], payload["after"])

    @staticmethod
    def _owned(entry) -> tuple[list[str], dict[str, Any], dict[str, Any] | None]:
        """(dimensions the entry's change owns, their previous values, what it sent).

        A row written before rows named their dimensions holds the WHOLE
        previous spec and nothing it sent: it owns every key but the
        placements and devices, which are left as they are now.
        """
        inverse = entry.get("inverse") or {}
        previous = as_obj((inverse.get("previous") or {}).get("targeting")) or {}
        if inverse.get("touched") is not None:
            return list(inverse["touched"]), previous, inverse.get("sent") or {}
        return [k for k in previous if k not in WHERE_KEYS], previous, None

    def current_for_entry(self, ctx, entry):
        spec = self._spec(ctx, entry["entity_id"])
        touched, _, sent = self._owned(entry)
        if sent is None:
            return fingerprint(spec, where=False)
        if dimensions(spec, touched, sent=sent) == dimensions(sent, touched, sent=sent):
            # Only what this entry changed is compared: a placement or another
            # dimension moved since is kept, not a reason to refuse.
            return entry["after"]
        shown = fingerprint(spec)
        return shown if shown != entry["after"] else f"{shown} (changed since)"

    def revert_row_inverse(self, ctx, entry):
        # The revert replaces the dimensions this entry owns with their
        # previous values; a revert of the revert puts back what is there now.
        touched, previous, _ = self._owned(entry)
        live = self._spec(ctx, entry["entity_id"])
        return {
            "touched": touched,
            "previous": {"targeting": {k: live[k] for k in touched if k in live}},
            "sent": {k: previous[k] for k in touched if k in previous},
        }

    def inverse(self, ctx, entry):
        touched, previous, _ = self._owned(entry)
        if not previous and not touched:
            raise ValueError(
                f"Entry {entry['entry_id']} recorded no previous targeting, so there "
                "is nothing exact to put back. Use preview_ad_set_targeting."
            )
        spec = with_dimensions(self._spec(ctx, entry["entity_id"]), touched, previous)
        return self._plan(entry["entity_id"], entry.get("entity_name") or "",
                          {"targeting": spec}, entry["after"], entry["before"])

    def _estimate(self, ctx, spec, goal):
        try:
            return reach.estimate(ctx.read, spec, goal or "REACH")
        except Exception:  # noqa: BLE001 - estimates are advisory
            return None

    def _estimate_why(self, ctx, spec, goal) -> tuple[dict | None, str]:
        """(a usable estimate or None, why not)."""
        try:
            est = reach.estimate(ctx.read, spec, goal or "REACH")
        except Exception as exc:  # noqa: BLE001 - reported on the check line
            return None, (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        if not est.get("ready"):
            return None, "Meta says the estimate is not ready yet"
        if est.get("midpoint") is None:
            return None, "Meta returned no reach bounds"
        return est, ""

    def _audience_checks(self, ctx, new, current, goal) -> tuple[list[Check], list[str], set[str]]:
        checks: list[Check] = []
        warnings: list[str] = []
        lookalikes: set[str] = set()
        include = _ids(new.get("custom_audiences"))
        exclude = _ids(new.get("excluded_custom_audiences"))
        total = len(include) + len(exclude)
        cap = ctx.limits.max_audiences_per_ad_set
        checks.append(Check("max_audiences_per_ad_set", total <= cap, f"{total} against {cap}"))
        attached_now = set(include + exclude) - set(
            _ids(current.get("custom_audiences")) + _ids(current.get("excluded_custom_audiences")))
        for aid in sorted(set(include + exclude)):
            try:
                row = ctx.read.node(aid, AUDIENCE_FIELDS)
            except Exception as exc:  # noqa: BLE001
                if aid in attached_now:
                    checks.append(Check(f"audience_{aid}", False, f"cannot read it: {str(exc).splitlines()[0]}"))
                continue
            if str(row.get("subtype")) == "LOOKALIKE":
                lookalikes.add(aid)
            if aid not in attached_now:
                continue
            state = usability(row)
            if state["usable"] is False:
                checks.append(Check(f"audience_{aid}", False,
                                    f"{row.get('name')!r} cannot be delivered to: {state['reason']}"))
            elif state["state"] == "populating":
                warnings.append(f"audience {row.get('name')!r} is still populating: {state['reason']}")
            elif state["usable"] is None:
                warnings.append(f"audience {row.get('name')!r} reports an unmapped status: {state['reason']}")
        base = {k: v for k, v in new.items()
                if k not in ("custom_audiences", "excluded_custom_audiences")}

        # Overlap between included audiences.
        pairs = list(itertools.combinations(include, 2))
        for a, b in pairs[:MAX_OVERLAP_PAIRS]:
            est_a = self._estimate(ctx, {**base, "custom_audiences": [{"id": a}]}, goal)
            est_b = self._estimate(ctx, {**base, "custom_audiences": [{"id": b}]}, goal)
            est_ab = self._estimate(ctx, {**base, "custom_audiences": [{"id": a}, {"id": b}]}, goal)
            if not (est_a and est_b and est_ab) or None in (est_a["midpoint"], est_b["midpoint"], est_ab["midpoint"]):
                warnings.append(f"overlap of {a} and {b} could not be estimated")
                continue
            pct = reach.overlap_pct(est_a["midpoint"], est_b["midpoint"], est_ab["midpoint"])
            if pct is not None and pct > ctx.limits.overlap_warn_pct:
                warnings.append(
                    f"audiences {a} and {b} overlap by about {pct:.0f}% of the smaller one "
                    "(estimate from rounded reach bounds); they compete for the same people"
                )
        if len(pairs) > MAX_OVERLAP_PAIRS:
            warnings.append(f"overlap checked for the first {MAX_OVERLAP_PAIRS} of {len(pairs)} pairs only")

        # An exclusion that swallows the included reach.
        if exclude:
            without = self._estimate(ctx, {**base, **({"custom_audiences": [{"id": i} for i in include]} if include else {})}, goal)
            with_ex = self._estimate(ctx, new, goal)
            if without and with_ex and without["midpoint"]:
                left = with_ex["midpoint"] or 0
                removed = max(0.0, 1 - left / without["midpoint"]) * 100
                if left <= 0:
                    checks.append(Check("exclusion_swallow", False,
                                        "the exclusions remove the whole included reach: the ad set would reach no one"))
                elif removed > ctx.limits.exclusion_swallow_pct:
                    warnings.append(f"the exclusions remove about {removed:.0f}% of the included reach (estimate)")
        return checks, warnings, lookalikes

    def propose(
        self,
        ctx,
        ad_set_id: Annotated[str, Field(description="From find_ad_sets; read get_ad_set first")],
        geo_include: Annotated[dict[str, list] | None, Field(description='Add places: {"countries": ["CA"], "regions": [{"key": "..."}], "cities": [{"key": "...", "radius": 25, "distance_unit": "mile"}], "zips": [{"key": "..."}]} -- keys from search_targeting(type="geo")')] = None,
        geo_remove: Annotated[dict[str, list] | None, Field(description="Remove places, same shape (only keys/countries needed)")] = None,
        geo_exclude: Annotated[dict[str, list] | None, Field(description="Exclude places, same shape")] = None,
        age_min: Annotated[int | None, Field(ge=13, le=65)] = None,
        age_max: Annotated[int | None, Field(ge=13, le=65)] = None,
        genders: Annotated[list[int] | None, Field(description="[1] men, [2] women, [] everyone")] = None,
        locales: Annotated[list[int] | None, Field(description="Locale ids from search_targeting(type='locale'); [] clears")] = None,
        detailed_add: Annotated[dict[str, list[dict]] | None, Field(description='{"interests": [{"id": "...", "name": "..."}], "behaviors": [...]} from search_targeting')] = None,
        detailed_exclude: Annotated[dict[str, list[dict]] | None, Field(description="Exclude detailed targeting, same shape")] = None,
        detailed_remove: Annotated[list[str] | None, Field(description="Detailed-targeting ids to drop (included or excluded)")] = None,
        audiences_include: Annotated[list[str] | None, Field(description="Custom/lookalike audience ids to include")] = None,
        audiences_exclude: Annotated[list[str] | None, Field(description="Custom audience ids to exclude")] = None,
        audiences_remove: Annotated[list[str] | None, Field(description="Audience ids to detach (include or exclude)")] = None,
        saved_audience_id: Annotated[str | None, Field(description="Copy this saved audience's targeting into the ad set (replaces the audience part of the spec, then the edits above apply). The ad set's own placements, devices and OS are kept.")] = None,
        advantage_audience: Annotated[bool | None, Field(description="Advantage+ audience on/off")] = None,
        use_saved_audience_placements: Annotated[bool, Field(description="With saved_audience_id: also take the saved audience's placements, devices and OS instead of keeping the ad set's")] = False,
        allow_wider_placements: Annotated[bool, Field(description="Allow a change that lets the ads run in more placements or on more devices; without it such a preview FAILs")] = False,
    ) -> Proposal:
        """Change who an ad set reaches. Writes nothing.

        Reads the full current spec, applies these edits to a copy and sends
        the WHOLE merged spec (Meta replaces targeting wholesale). Reach is
        estimated before and after; included audiences are checked for
        overlap and exclusions for swallowing the reach; special ad
        categories on the campaign are enforced. The previous spec is kept,
        so revert_change restores it exactly -- detaching an audience is the
        undo for attaching one.

        With Advantage+ audience ON, included audiences and interests become
        SUGGESTIONS that Meta may go beyond, not limits.
        """
        row = self.read_row(ctx, ad_set_id)
        current = as_obj(row.get("targeting")) or {}
        saved = None
        saved_name = None
        if saved_audience_id:
            node = ctx.read.node(str(saved_audience_id), ("id", "name", "targeting"))
            saved, saved_name = as_obj(node.get("targeting")) or {}, node.get("name")
            if not use_saved_audience_placements:
                # A saved audience is WHO, not WHERE: copying it must not drop
                # the ad set's manual placements and flip it to Advantage+
                # placements, or its device/OS limits.
                saved = {k: v for k, v in saved.items() if k not in WHERE_KEYS}
                saved.update({k: copy.deepcopy(current[k]) for k in WHERE_KEYS if k in current})
        edits = {
            "geo_include": geo_include, "geo_remove": geo_remove, "geo_exclude": geo_exclude,
            "age_min": age_min, "age_max": age_max, "genders": genders, "locales": locales,
            "detailed_add": detailed_add, "detailed_exclude": detailed_exclude,
            "detailed_remove": detailed_remove, "audiences_include": audiences_include,
            "audiences_exclude": audiences_exclude, "audiences_remove": audiences_remove,
            "advantage_audience": advantage_audience,
        }
        if saved is None and not any(v is not None for v in edits.values()):
            raise ValueError("Nothing to change: give at least one edit.")
        new = apply_edits(current, edits, saved)
        if fingerprint(new) == fingerprint(current):
            raise ValueError("These edits leave the targeting exactly as it is.")
        touched = touched_keys(current, new)
        sent = {k: new[k] for k in touched if k in new}

        campaign = ctx.read.node(str(row.get("campaign_id")), ("id", "special_ad_categories"))
        goal = row.get("optimization_goal")
        audience_checks, warnings, lookalikes = self._audience_checks(ctx, new, current, goal)
        checks = validate_spec(ctx, new, campaign=campaign, lookalike_ids=lookalikes) + audience_checks
        wider = placement_widening(current, new)
        if wider:
            checks.append(Check(
                "placements_widen", allow_wider_placements,
                "; ".join(wider) + (" (allowed: allow_wider_placements)" if allow_wider_placements
                                    else " -- pass allow_wider_placements=True to allow it"),
            ))

        widening = reach_widening(current, new)
        before_est, why_before = self._estimate_why(ctx, current, goal)
        after_est, why_after = self._estimate_why(ctx, new, goal)
        if before_est is not None and not before_est["midpoint"]:
            before_est, why_before = None, "the current reach estimate is 0, so no change can be measured against it"
        limit = ctx.limits.max_reach_change_pct
        grows = bool(widening)
        if before_est and after_est:
            change = (after_est["midpoint"] - before_est["midpoint"]) / before_est["midpoint"] * 100
            # WIDENING past the limit fails: it is the direction that can raise
            # spend. Narrowing past it only warns -- attaching one custom
            # audience routinely narrows reach by 99%, and that is the point.
            detail = (f"estimated reach {before_est['midpoint']:,} -> {after_est['midpoint']:,} "
                      f"({change:+.0f}%) against +{limit:.0f}%")
            checks.append(Check("max_reach_change_pct", change <= limit, detail,
                                warn=change < -limit))
            grows = grows or after_est["midpoint"] > before_est["midpoint"]
        elif widening:
            # FAIL CLOSED: an edit that can widen reach is exactly the one the
            # reach limit exists for, so a missing number cannot pass it.
            checks.append(Check(
                "max_reach_change_pct", False,
                f"reach could not be estimated ({why_before or why_after}) and this edit "
                f"can widen it ({'; '.join(widening)}); a widening edit is not allowed "
                "without an estimate -- retry later",
            ))
        else:
            checks.append(Check("max_reach_change_pct", True,
                                f"reach could not be estimated ({why_before or why_after}); "
                                "this edit only narrows", skipped=True))
        projected = Decimal("0")
        if grows:
            # More people means the budget can be spent where it was not: the
            # ad set's unused headroom, counted like an activation (exempt from
            # the per-change limit, counted in the daily and session totals).
            bound, why = ad_set_activation_bound(ctx, row, ctx.currency_of(row))
            spent = ctx.spend_7d(ctx.metrics_7d(str(row["id"]), "adset")) / 7
            projected = max(bound - spent, Decimal("0")).quantize(Decimal("0.01"))
            warnings.append(
                f"this can widen reach ({'; '.join(widening) or 'estimate grows'}): "
                f"projected at the unused budget headroom, {projected:.2f}/day "
                f"({why}, less the 7-day average spend)"
            )
        if advantage_audience:
            warnings.append(
                "Advantage+ audience ON: included audiences and interests become "
                "suggestions Meta may go beyond, not limits"
            )
        if saved_name:
            warnings.append(f"the targeting is copied from saved audience {saved_name!r}; "
                            "later edits to that saved audience do not follow")
        return Proposal(
            entity_type="ad set",
            entity_id=str(row["id"]),
            entity_name=row.get("name") or "",
            path=f"campaign {row.get('campaign_id')} > {row.get('name')}",
            field_name="targeting",
            before=fingerprint(current),
            after=fingerprint(new),
            checks=checks,
            projected=projected,
            is_activation=grows,
            # Undoing a narrowing widens again: that revert projects the headroom.
            tightens=bool(reach_widening(new, current)),
            payload={"entity_id": str(row["id"]), "name": row.get("name") or "",
                     "fields": {"targeting": new}, "full_before": canonical(current),
                     "touched": touched, "sent": sent},
            status=row.get("status"),
            effective_status=row.get("effective_status"),
            warnings=warnings,
            campaign_id=str(row.get("campaign_id")),
            account_id=row.get("account_id"),
            note=(
                f"Estimated reach: {before_est and before_est.get('midpoint')} -> "
                f"{after_est and after_est.get('midpoint')} (rounded bounds, an estimate). "
                "The estimate excludes Advantage+ audience expansion, which can reach "
                "beyond it. Widening reach is projected at the ad set's unused daily budget."
            ),
        ).with_inverse({
            # Only the dimensions this edit changed: the revert puts THOSE back
            # onto whatever the spec is then, and refuses if they moved.
            "touched": touched,
            "previous": {"targeting": {k: current[k] for k in touched if k in current}},
            "sent": sent,
        })
