"""ad_set_targeting: full merged spec, exact revert, audiences, overlap, rules."""

from __future__ import annotations

import json

import pytest

from metaads_mcp.changes.targeting import apply_edits, fingerprint
from tests.kindkit import failed, preview_ok, rig
from tests.test_mcp_write_path import ADSET_ID, CAMPAIGN_ID, make_sim

BASE = {
    "geo_locations": {"countries": ["CA"]},
    "age_min": 25,
    "age_max": 54,
    "flexible_spec": [{"interests": [{"id": "6001", "name": "Cooking"}]}],
}

SIZES = {"A": 1000, "B": 1000, "C": 1000}


def estimator(params):
    """Midpoint reach for a spec: audiences by SIZES, A and B overlap by 50%."""
    spec = json.loads(params["targeting_spec"])
    include = sorted(a["id"] for a in spec.get("custom_audiences") or [])
    exclude = {a["id"] for a in spec.get("excluded_custom_audiences") or []}
    if include == ["A", "B"]:
        size = 1500
    elif include:
        size = sum(SIZES.get(a, 1000) for a in include)
    else:
        size = 100000 if spec.get("age_min", 18) <= 25 else 80000
        if spec.get("geo_locations", {}).get("countries") == ["CA", "US"]:
            size = 400000
    if "SWALLOW" in exclude:
        size = 0
    return {"data": [{"estimate_mau_lower_bound": size, "estimate_mau_upper_bound": size,
                      "estimate_ready": True}]}


def sim(**campaign):
    s = make_sim()
    s.objects[ADSET_ID]["targeting"] = json.loads(json.dumps(BASE))
    s.objects[CAMPAIGN_ID].update(campaign)
    for aid in ("A", "B", "C", "SWALLOW"):
        s.objects[aid] = {"id": aid, "name": f"Aud {aid}", "subtype": "WEBSITE",
                          "delivery_status": {"code": 200}, "operation_status": {"code": 200}}
    s.objects["TINY"] = {"id": "TINY", "name": "Tiny", "delivery_status": {"code": 300,
                         "description": "too small"}}
    s.objects["LAL"] = {"id": "LAL", "name": "LAL 1%", "subtype": "LOOKALIKE",
                        "delivery_status": {"code": 200}}
    s.objects["SAVED1"] = {"id": "SAVED1", "name": "Springfield 30+",
                           "targeting": {"geo_locations": {"countries": ["US"]}, "age_min": 30}}
    s.canned["delivery_estimate"] = estimator
    return s


def test_an_edit_sends_the_full_merged_spec_and_reverts_exactly(monkeypatch):
    s = sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "ad_set_targeting", s, ad_set_id=ADSET_ID, audiences_include=["A"])
    result = tools["apply_ad_set_targeting"](preview_token=preview.preview_token)
    sent = json.loads(s.objects[ADSET_ID]["targeting"])
    assert sent["age_min"] == 25 and sent["flexible_spec"] == BASE["flexible_spec"]
    assert sent["custom_audiences"] == [{"id": "A"}]

    tools["revert_change"](entry_id=result.entry_id)
    assert json.loads(s.objects[ADSET_ID]["targeting"]) == BASE, "detaching is the undo"


def test_a_targeting_change_made_elsewhere_is_caught(monkeypatch):
    s = sim()
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_ad_set_targeting"](ad_set_id=ADSET_ID, age_min=30)
    s.objects[ADSET_ID]["targeting"] = {**BASE, "age_max": 60}
    with pytest.raises(Exception) as caught:
        tools["apply_ad_set_targeting"](preview_token=preview.preview_token)
    assert "has changed since the preview" in str(caught.value)


def test_an_unusable_audience_is_refused(monkeypatch):
    tools, _ = rig(monkeypatch, sim())
    preview = tools["preview_ad_set_targeting"](ad_set_id=ADSET_ID, audiences_include=["TINY"])
    assert failed(preview, "audience_TINY")


def test_the_audience_count_is_capped(monkeypatch):
    tools, _ = rig(monkeypatch, sim(), max_audiences_per_ad_set=2)
    preview = tools["preview_ad_set_targeting"](ad_set_id=ADSET_ID, audiences_include=["A", "B", "C"])
    assert failed(preview, "max_audiences_per_ad_set")


def test_overlap_over_the_threshold_warns(monkeypatch):
    tools, _ = rig(monkeypatch, sim(), overlap_warn_pct=40.0)
    preview = tools["preview_ad_set_targeting"](ad_set_id=ADSET_ID, audiences_include=["A", "B"])
    assert any("A and B overlap by about 50%" in w for w in preview.warnings), preview.warnings


def test_an_exclusion_that_swallows_everything_fails(monkeypatch):
    tools, _ = rig(monkeypatch, sim())
    preview = tools["preview_ad_set_targeting"](
        ad_set_id=ADSET_ID, audiences_include=["A"], audiences_exclude=["SWALLOW"])
    assert failed(preview, "exclusion_swallow")


def test_a_big_reach_move_fails(monkeypatch):
    tools, _ = rig(monkeypatch, sim(), max_reach_change_pct=50.0)
    preview = tools["preview_ad_set_targeting"](ad_set_id=ADSET_ID,
                                                geo_include={"countries": ["US"]})
    assert failed(preview, "max_reach_change_pct")


@pytest.mark.parametrize("edit, phrase", [
    ({"age_min": 30}, "age narrowed"),
    ({"genders": [2]}, "gender"),
    ({"geo_include": {"zips": [{"key": "US:00000"}]}}, "ZIP"),
    ({"geo_include": {"cities": [{"key": "123", "radius": 5, "distance_unit": "mile"}]}}, "radius"),
    ({"audiences_include": ["LAL"]}, "lookalike"),
    ({"detailed_exclude": {"interests": [{"id": "6002", "name": "x"}]}}, "exclusions"),
])
def test_special_ad_categories_refuse_restricted_targeting(monkeypatch, edit, phrase):
    s = sim(special_ad_categories=["HOUSING"])
    s.objects[ADSET_ID]["targeting"] = {"geo_locations": {"countries": ["CA"]}}
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_ad_set_targeting"](ad_set_id=ADSET_ID, **edit)
    assert failed(preview, "special_ad_categories"), preview.checks
    assert any(phrase in c for c in preview.checks)


def test_a_saved_audience_is_copied_into_the_ad_set(monkeypatch):
    s = sim()
    tools, _ = rig(monkeypatch, s, max_reach_change_pct=500.0)
    preview = preview_ok(tools, "ad_set_targeting", s, ad_set_id=ADSET_ID,
                         saved_audience_id="SAVED1")
    assert any("Springfield 30+" in w for w in preview.warnings)
    tools["apply_ad_set_targeting"](preview_token=preview.preview_token)
    assert json.loads(s.objects[ADSET_ID]["targeting"])["geo_locations"] == {"countries": ["US"]}


def test_advantage_audience_says_audiences_become_suggestions(monkeypatch):
    tools, _ = rig(monkeypatch, sim())
    preview = tools["preview_ad_set_targeting"](ad_set_id=ADSET_ID, advantage_audience=True)
    assert any("suggestions" in w for w in preview.warnings)


def test_remove_detaches_from_include_and_exclude():
    spec = {**BASE, "custom_audiences": [{"id": "A"}], "excluded_custom_audiences": [{"id": "B"}]}
    new = apply_edits(spec, {"audiences_remove": ["A", "B"]})
    assert "custom_audiences" not in new and "excluded_custom_audiences" not in new
    assert fingerprint(new) == fingerprint(BASE)


def test_the_fingerprint_ignores_keys_meta_adds_on_read():
    read_back = {**BASE, "geo_locations": {"countries": ["CA"], "location_types": ["home"]},
                 "brand_safety_content_filter_levels": ["FACEBOOK_STANDARD"]}
    assert fingerprint(read_back) == fingerprint(BASE)


# --- placements and other targeting the edit did not touch -------------------


def _targeting(s):
    from metaads_mcp.changes.common import as_obj

    return as_obj(s.objects[ADSET_ID]["targeting"])


def test_a_placement_change_made_after_the_preview_is_not_overwritten(monkeypatch):
    s = sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "ad_set_targeting", s, ad_set_id=ADSET_ID, audiences_include=["A"])
    s.objects[ADSET_ID]["targeting"] = {**BASE, "publisher_platforms": ["facebook"],
                                        "facebook_positions": ["feed"]}
    try:
        tools["apply_ad_set_targeting"](preview_token=preview.preview_token)
    except Exception as exc:  # noqa: BLE001 - refusing is one right answer
        assert "has changed since the preview" in str(exc)
    after = _targeting(s)
    assert after.get("publisher_platforms") == ["facebook"], "placements were silently undone"
    assert after.get("facebook_positions") == ["feed"]


def test_a_revert_keeps_a_placement_change_made_since(monkeypatch):
    s = sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "ad_set_targeting", s, ad_set_id=ADSET_ID, audiences_include=["A"])
    result = tools["apply_ad_set_targeting"](preview_token=preview.preview_token)
    s.objects[ADSET_ID]["targeting"] = {**_targeting(s), "publisher_platforms": ["instagram"]}

    tools["revert_change"](entry_id=result.entry_id)
    after = _targeting(s)
    assert "custom_audiences" not in after, "the attach was undone"
    assert after.get("publisher_platforms") == ["instagram"], "the placement change survived"


def test_a_revert_keeps_an_untouched_dimension_moved_since(monkeypatch):
    s = sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "ad_set_targeting", s, ad_set_id=ADSET_ID, audiences_include=["A"])
    result = tools["apply_ad_set_targeting"](preview_token=preview.preview_token)
    s.objects[ADSET_ID]["targeting"] = {**_targeting(s), "age_max": 60}

    tools["revert_change"](entry_id=result.entry_id)
    after = _targeting(s)
    assert "custom_audiences" not in after and after["age_max"] == 60


def test_a_revert_refuses_when_the_touched_dimension_moved(monkeypatch):
    s = sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "ad_set_targeting", s, ad_set_id=ADSET_ID, audiences_include=["A"])
    result = tools["apply_ad_set_targeting"](preview_token=preview.preview_token)
    s.objects[ADSET_ID]["targeting"] = {**_targeting(s), "custom_audiences": [{"id": "A"}, {"id": "B"}]}
    with pytest.raises(Exception) as caught:
        tools["revert_change"](entry_id=result.entry_id)
    assert "Something changed it since" in str(caught.value)


def test_the_fingerprint_shows_placements():
    assert fingerprint({**BASE, "publisher_platforms": ["facebook"]}) != fingerprint(BASE)


# --- a saved audience and the ad set's placements ---------------------------

MANUAL = {"publisher_platforms": ["facebook"], "facebook_positions": ["feed"],
          "device_platforms": ["mobile"], "user_os": ["iOS"]}


def test_a_saved_audience_keeps_the_ad_sets_placements_and_devices(monkeypatch):
    s = sim()
    s.objects[ADSET_ID]["targeting"] = {**BASE, **MANUAL}
    tools, _ = rig(monkeypatch, s, max_reach_change_pct=500.0)
    preview = preview_ok(tools, "ad_set_targeting", s, ad_set_id=ADSET_ID,
                         saved_audience_id="SAVED1")
    tools["apply_ad_set_targeting"](preview_token=preview.preview_token)
    after = _targeting(s)
    assert after["geo_locations"] == {"countries": ["US"]}
    for key, value in MANUAL.items():
        assert after.get(key) == value, f"{key} was dropped"


def test_saved_placements_that_widen_fail_without_the_opt_in(monkeypatch):
    s = sim()
    s.objects[ADSET_ID]["targeting"] = {**BASE, **MANUAL}
    tools, _ = rig(monkeypatch, s, max_reach_change_pct=500.0)
    preview = tools["preview_ad_set_targeting"](
        ad_set_id=ADSET_ID, saved_audience_id="SAVED1", use_saved_audience_placements=True)
    assert failed(preview, "placements_widen"), preview.checks
    assert "publisher_platforms" in preview.before and "publisher_platforms" not in preview.after

    opted = tools["preview_ad_set_targeting"](
        ad_set_id=ADSET_ID, saved_audience_id="SAVED1", use_saved_audience_placements=True,
        allow_wider_placements=True)
    assert not failed(opted, "placements_widen"), opted.checks


# --- an estimate that is not there, on an edit that can widen ----------------

from tests.fakes import FakeResponse  # noqa: E402


def _broken(kind):
    def answer(params):
        if kind == "rate_limited":
            return FakeResponse({"error": {"message": "User request limit reached",
                                           "code": 17}}, status=400)
        if kind == "not_ready":
            return {"data": [{"estimate_mau_lower_bound": 1000, "estimate_mau_upper_bound": 1000,
                              "estimate_ready": False}]}
        if kind == "zero":
            return {"data": [{"estimate_mau_lower_bound": 0, "estimate_mau_upper_bound": 0,
                              "estimate_ready": True}]}
        raise RuntimeError("estimate down")
    return answer


@pytest.mark.parametrize("broken", ["error", "rate_limited", "not_ready", "zero"])
@pytest.mark.parametrize("edit", [
    {"geo_include": {"countries": ["US"]}},
    {"age_min": 18},
    {"genders": []},
    {"advantage_audience": True},
    {"detailed_remove": ["6001"]},
])
def test_a_widening_edit_without_an_estimate_fails_closed(monkeypatch, broken, edit):
    s = sim()
    s.objects[ADSET_ID]["targeting"] = {**BASE, "genders": [1]}
    s.canned["delivery_estimate"] = _broken(broken)
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_ad_set_targeting"](ad_set_id=ADSET_ID, **edit)
    assert failed(preview, "max_reach_change_pct"), preview.checks
    assert any("retry" in c.lower() for c in preview.checks if c.startswith("FAIL"))


def test_removing_an_audience_or_exclusion_without_an_estimate_fails_closed(monkeypatch):
    s = sim()
    s.objects[ADSET_ID]["targeting"] = {**BASE, "custom_audiences": [{"id": "A"}],
                                        "excluded_custom_audiences": [{"id": "B"}]}
    s.canned["delivery_estimate"] = _broken("error")
    tools, _ = rig(monkeypatch, s)
    for edit in ({"audiences_remove": ["A"]}, {"audiences_remove": ["B"]}):
        preview = tools["preview_ad_set_targeting"](ad_set_id=ADSET_ID, **edit)
        assert failed(preview, "max_reach_change_pct"), (edit, preview.checks)


def test_a_narrowing_edit_without_an_estimate_still_previews(monkeypatch):
    s = sim()
    s.canned["delivery_estimate"] = _broken("error")
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_ad_set_targeting"](ad_set_id=ADSET_ID, age_min=30)
    assert not preview.blocked, preview.checks


def test_a_widening_edit_projects_the_unused_budget_like_an_activation(monkeypatch):
    s = sim()
    s.objects[ADSET_ID]["targeting"] = {**BASE, "genders": [1]}
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_ad_set_targeting"](ad_set_id=ADSET_ID, genders=[])
    # The campaign's 250.00 budget, less the 123.45 / 7 it spends a day.
    assert preview.projected_daily_spend_delta == "232.36", preview
    assert any("projected_delta_per_day" in c and "SKIP" in c for c in preview.checks), preview.checks
    assert "Advantage+" in (preview.note or "")


# --- legacy rows (no touched) and a revert that did not land -----------------


def _legacy_row(s, before_spec, after_spec):
    """A row written before rows named their dimensions: the whole previous
    spec, nothing it sent, before/after spelled without placements."""
    from metaads_reporting import logdb

    s.objects[ADSET_ID]["targeting"] = json.loads(json.dumps(after_spec))
    logdb.insert_change(
        entry_id="legacytg", tool="apply_ad_set_targeting", account_id="1234567890",
        entity_type="ad set", entity_id=ADSET_ID, entity_name="Example ad set",
        field_name="targeting", before=fingerprint(before_spec, where=False),
        after=fingerprint(after_spec, where=False), projected_delta="0",
        inverse={"kind": "ad_set_targeting", "weight": 1,
                 "previous": {"targeting": json.loads(json.dumps(before_spec))}},
    )
    logdb.complete_change(entry_id="legacytg", ok=True)
    return "legacytg"


@pytest.mark.parametrize("added", [
    {"custom_audiences": [{"id": "A"}]},
    {"excluded_custom_audiences": [{"id": "B"}]},
    {"locales": [6]},
    {"exclusions": {"interests": [{"id": "7001"}]}},
])
def test_a_legacy_revert_removes_keys_the_original_edit_added(monkeypatch, added):
    s = sim()
    tools, _ = rig(monkeypatch, s)
    entry = _legacy_row(s, BASE, {**BASE, **added})
    tools["revert_change"](entry_id=entry)
    after = _targeting(s)
    for key in added:
        assert key not in after, f"{key} the original edit added survived the revert"
    assert after == BASE


def test_a_legacy_revert_removes_an_added_flexible_spec(monkeypatch):
    s = sim()
    before = {k: v for k, v in BASE.items() if k != "flexible_spec"}
    tools, _ = rig(monkeypatch, s)
    entry = _legacy_row(s, before, BASE)
    tools["revert_change"](entry_id=entry)
    assert "flexible_spec" not in _targeting(s)


def test_a_legacy_revert_keeps_placements(monkeypatch):
    s = sim()
    tools, _ = rig(monkeypatch, s)
    entry = _legacy_row(s, BASE, {**BASE, "custom_audiences": [{"id": "A"}]})
    s.objects[ADSET_ID]["targeting"] = {**_targeting(s), "publisher_platforms": ["facebook"]}
    tools["revert_change"](entry_id=entry)
    after = _targeting(s)
    assert "custom_audiences" not in after and after["publisher_platforms"] == ["facebook"]


def _merging_targeting(s):
    """Meta as feared: an update MERGES the targeting keys instead of replacing."""
    original = s._post

    def post(segments, body):
        node = segments[0]
        before = s.objects.get(node, {}).get("targeting")
        answer = original(segments, body)
        if "targeting" in body and node in s.objects and before is not None:
            from metaads_mcp.changes.common import as_obj

            s.objects[node]["targeting"] = {**as_obj(before), **as_obj(s.objects[node]["targeting"])}
        return answer

    s._post = post


def test_a_targeting_revert_that_did_not_land_is_not_recorded_as_applied(monkeypatch):
    from metaads_mcp import ledger

    s = sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "ad_set_targeting", s, ad_set_id=ADSET_ID, audiences_include=["A"])
    result = tools["apply_ad_set_targeting"](preview_token=preview.preview_token)
    _merging_targeting(s)
    with pytest.raises(Exception) as caught:
        tools["revert_change"](entry_id=result.entry_id)
    assert "did not land" in str(caught.value)
    entry = ledger.entries()[-1]
    assert entry["tool"] == "revert_change" and not entry["applied"] and entry["error"]


# --- widening the classifier did not see (reachable via saved_audience_id) -----

from metaads_mcp.changes.targeting import reach_widening  # noqa: E402


def test_adding_a_location_type_widens():
    before = {"geo_locations": {"countries": ["CA"], "location_types": ["home"]}}
    after = {"geo_locations": {"countries": ["CA"], "location_types": ["home", "travel_in"]}}
    assert reach_widening(before, after)
    assert not reach_widening(after, before)


def test_dropping_location_types_reverts_to_the_wider_default():
    before = {"geo_locations": {"countries": ["CA"], "location_types": ["home"]}}
    after = {"geo_locations": {"countries": ["CA"]}}
    assert reach_widening(before, after), "no location_types means home and recent"


@pytest.mark.parametrize("setting", [{"age": 1}, {"gender": 1}, {"age": 1, "gender": 1}])
def test_age_or_gender_expansion_widens(setting):
    before = {**BASE, "targeting_automation": {"advantage_audience": 0}}
    after = {**BASE, "targeting_automation": {"advantage_audience": 0, "individual_setting": setting}}
    assert reach_widening(before, after)
    assert fingerprint(before) != fingerprint(after)


@pytest.mark.parametrize("key, places", [
    ("country_groups", ["europe"]),
    ("neighborhoods", [{"key": "N1"}]),
    ("metro_areas", [{"key": "M1"}]),
    ("places", [{"key": "P1", "radius": 1, "distance_unit": "kilometer"}]),
    ("electoral_districts", [{"key": "E1"}]),
    ("medium_geo_areas", [{"key": "G1"}]),
])
def test_every_geo_key_is_in_the_fingerprint_and_widens(key, places):
    before = {**BASE}
    after = {**BASE, "geo_locations": {**BASE["geo_locations"], key: places}}
    assert fingerprint(before) != fingerprint(after), key
    assert reach_widening(before, after), key


def test_a_saved_audience_that_widens_only_by_location_type_fails_closed(monkeypatch):
    s = sim()
    s.objects[ADSET_ID]["targeting"] = {**BASE, "geo_locations": {"countries": ["CA"],
                                                                  "location_types": ["home"]}}
    s.objects["SAVED2"] = {"id": "SAVED2", "name": "Wider",
                           "targeting": {**BASE, "geo_locations": {"countries": ["CA"],
                                         "location_types": ["home", "travel_in"]},
                                         "age_min": 30}}
    s.canned["delivery_estimate"] = _broken("error")
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_ad_set_targeting"](ad_set_id=ADSET_ID, saved_audience_id="SAVED2")
    assert failed(preview, "max_reach_change_pct"), preview.checks
