"""Audience reads, targeting search and reach estimates."""

from __future__ import annotations

import json

from tests.harness import build_tools
from tests.test_mcp_write_path import ADSET_ID, make_sim


def sim_with_audiences():
    sim = make_sim()
    sim.objects["AUD_READY"] = {
        "id": "AUD_READY", "name": "Site visitors 30d", "subtype": "WEBSITE",
        "approximate_count_lower_bound": 10000, "approximate_count_upper_bound": 12000,
        "delivery_status": {"code": 200, "description": "ready"},
        "operation_status": {"code": 200, "description": "Normal"},
        "retention_days": 30,
    }
    sim.objects["AUD_NEW"] = {
        "id": "AUD_NEW", "name": "Engagers", "subtype": "ENGAGEMENT",
        "delivery_status": {"code": 200, "description": "ready"},
        "operation_status": {"code": 441, "description": "We're finding people"},
    }
    sim.objects["AUD_TINY"] = {
        "id": "AUD_TINY", "name": "Tiny", "subtype": "WEBSITE",
        "delivery_status": {"code": 300, "description": "This audience is too small"},
    }
    sim.objects[ADSET_ID]["targeting"] = {"custom_audiences": [{"id": "AUD_READY"}]}
    sim.edges["act_1234567890"]["customaudiences"] = ["AUD_READY", "AUD_NEW", "AUD_TINY"]
    return sim


def test_list_audiences_reports_usability_and_use(monkeypatch):
    registrar, _ = build_tools(sim_with_audiences(), monkeypatch=monkeypatch)
    out = registrar.tools["list_audiences"](kind="custom")
    rows = {r["audience_id"]: r for r in out["custom"]}
    assert rows["AUD_READY"]["usable"] is True and rows["AUD_READY"]["used_by_ad_sets"] == 1
    assert rows["AUD_NEW"]["usable"] is True and rows["AUD_NEW"]["usable_state"] == "populating"
    assert rows["AUD_TINY"]["usable"] is False


def test_unused_only_hides_audiences_an_ad_set_uses(monkeypatch):
    registrar, _ = build_tools(sim_with_audiences(), monkeypatch=monkeypatch)
    out = registrar.tools["list_audiences"](kind="custom", unused_only=True)
    assert {r["audience_id"] for r in out["custom"]} == {"AUD_NEW", "AUD_TINY"}


def test_get_audience_names_the_ad_sets_using_it(monkeypatch):
    registrar, _ = build_tools(sim_with_audiences(), monkeypatch=monkeypatch)
    out = registrar.tools["get_audience"](audience_id="AUD_READY")
    assert out["approx_size"] == "10000-12000"
    assert out["used_by"] == [{"ad_set_id": ADSET_ID, "name": "Broad 25-54", "mode": "include",
                               "effective_status": "ACTIVE"}]


def test_search_targeting_maps_types_to_metas_search(monkeypatch):
    sim = make_sim()
    seen = []

    def answer(params):
        seen.append(dict(params))
        return {"data": [{"id": "6003", "name": "Cooking", "audience_size_lower_bound": 1,
                          "audience_size_upper_bound": 2, "path": ["Interests", "Cooking"]}]}

    sim.canned["search"] = answer
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    out = registrar.tools["search_targeting"](type="interest", q="cook")
    assert out["rows"][0]["id"] == "6003" and seen[-1]["type"] == "adinterest"
    registrar.tools["search_targeting"](type="geo", q="Springfield", location_types=["city"])
    assert seen[-1]["type"] == "adgeolocation"
    assert json.loads(seen[-1]["location_types"]) == ["city"]
    registrar.tools["search_targeting"](type="behaviors")
    assert seen[-1]["class"] == "behaviors"


def test_estimate_reach_reads_the_bounds(monkeypatch):
    sim = make_sim()
    sim.canned["delivery_estimate"] = {"data": [{
        "estimate_mau_lower_bound": 90000, "estimate_mau_upper_bound": 110000,
        "estimate_ready": True}]}
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    out = registrar.tools["estimate_reach"](targeting={"geo_locations": {"countries": ["CA"]}})
    assert out["midpoint"] == 100000 and out["ready"] is True
