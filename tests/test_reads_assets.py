"""Assets, diagnostics, rules, studies, catalogs and the activity log."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.fakes import error_response
from tests.harness import build_tools
from tests.test_mcp_write_path import make_sim

ACT = "act_1234567890"


def tools(monkeypatch, sim):
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    return registrar.tools


def test_an_enabled_money_rule_is_flagged(monkeypatch):
    sim = make_sim()
    sim.objects["R1"] = {"id": "R1", "name": "Raise budget", "status": "ENABLED",
                         "execution_spec": {"execution_type": "CHANGE_BUDGET"}}
    sim.objects["R2"] = {"id": "R2", "name": "Pause losers", "status": "ENABLED",
                         "execution_spec": {"execution_type": "PAUSE"}}
    sim.edges[ACT]["adrules_library"] = ["R1", "R2"]
    out = tools(monkeypatch, sim)["list_ad_rules"]()
    flags = {r["id"]: r["warning"] for r in out["rows"]}
    assert flags["R1"] and flags["R2"] is None


def test_pixel_health_counts_events_by_name(monkeypatch):
    sim = make_sim()
    sim.objects["PX"] = {"id": "PX", "name": "Main", "last_fired_time": "2026-11-17T10:00:00+0000"}
    sim.canned["PX/stats"] = lambda params: {"data": [{"data": [
        {"value": "PageView", "count": 120}, {"value": "Purchase", "count": 3}]}]}
    out = tools(monkeypatch, sim)["get_pixel_health"](pixel_id="PX")
    assert out["by_event"]["PageView"] == 120 and out["events_seen"] == ["PageView", "Purchase"]


def test_recent_changes_asks_for_a_closed_window(monkeypatch):
    sim = make_sim()
    tools(monkeypatch, sim)["list_recent_changes"](days=3)
    call = next(c for c in sim.calls if c["url"].endswith("/activities"))
    assert int(call["payload"]["until"]) - int(call["payload"]["since"]) == 3 * 86400


def test_lead_forms_without_permission_say_so(monkeypatch):
    sim = make_sim()
    sim.canned["PAGE1/leadgen_forms"] = error_response(200, message="Requires leads_retrieval")
    with pytest.raises(ToolError) as caught:
        tools(monkeypatch, sim)["list_lead_forms"](page_id="PAGE1")
    assert str(caught.value).startswith("PERMISSION")


def test_catalogs_need_a_business_id(monkeypatch):
    with pytest.raises(ToolError) as caught:
        tools(monkeypatch, make_sim())["list_catalogs"]()
    assert "META_ADS_BUSINESS_ID" in str(caught.value)


def test_the_ad_library_wants_a_country(monkeypatch):
    with pytest.raises(ToolError):
        tools(monkeypatch, make_sim())["search_ad_library"](q="x", country="Canada")


def test_lists_say_when_they_are_cut_off(monkeypatch):
    sim = make_sim()
    for i in range(5):
        sim.objects[f"L{i}"] = {"id": f"L{i}", "name": f"label {i}"}
    sim.edges[ACT]["adlabels"] = [f"L{i}" for i in range(5)]
    out = tools(monkeypatch, sim)["list_labels"](limit=3)
    assert out["count"] == 3 and out["truncated"] is True


def test_identities_survive_one_refused_edge(monkeypatch):
    sim = make_sim()
    sim.objects["P1"] = {"id": "P1", "name": "Example Co"}
    sim.edges[ACT]["promote_pages"] = ["P1"]
    sim.canned[f"{ACT}/instagram_accounts"] = error_response(10, message="no")
    out = tools(monkeypatch, sim)["list_connected_identities"]()
    assert out["pages"] == [{"id": "P1", "name": "Example Co"}]
    assert "unavailable" in out["instagram_accounts"]
