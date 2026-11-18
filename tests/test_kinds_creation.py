"""Creating campaigns and ad sets, and copying them -- always PAUSED."""

from __future__ import annotations

import json

import pytest

from metaads_mcp.changes.creation import CreateCampaign
from tests.kindkit import failed, preview_ok, rig
from tests.test_mcp_write_path import AD_ID, ADSET_ID, CAMPAIGN_ID, make_sim

TARGETING = {"geo_locations": {"countries": ["CA"]}, "age_min": 18}


def test_special_ad_categories_is_required_with_no_default():
    import inspect

    parameter = inspect.signature(CreateCampaign.propose).parameters["special_ad_categories"]
    assert parameter.default is inspect.Parameter.empty


def test_a_cbo_campaign_is_created_paused_and_declares_its_categories(monkeypatch):
    s = make_sim()
    tools, state = rig(monkeypatch, s)
    preview = preview_ok(tools, "create_campaign", s, name="Spring launch", objective="OUTCOME_SALES",
                         special_ad_categories=[], budget_level="campaign", daily_budget="40.00")
    assert "special_ad_categories: []" in preview.after
    assert preview.apply_weight == 3 and preview.projected_daily_spend_delta == "0.00"
    result = tools["apply_create_campaign"](preview_token=preview.preview_token)
    created = next(c for c in s.created if c["edge"] == "campaigns")["payload"]
    assert created["status"] == "PAUSED" and created["daily_budget"] == 4000
    assert json.loads(created["special_ad_categories"]) == []
    assert state.counters.applies == 3
    rows = tools["list_my_changes"]()
    assert rows[-1]["entity_id"] == result.entity_id != "(new)"


def test_an_abo_campaign_sets_budget_sharing_explicitly(monkeypatch):
    s = make_sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "create_campaign", s, name="x", objective="OUTCOME_TRAFFIC",
                         special_ad_categories=["NONE"], budget_level="adset")
    tools["apply_create_campaign"](preview_token=preview.preview_token)
    created = next(c for c in s.created if c["edge"] == "campaigns")["payload"]
    assert created["is_adset_budget_sharing_enabled"] == "false"
    with pytest.raises(Exception):
        tools["preview_create_campaign"](name="x", objective="OUTCOME_TRAFFIC",
                                         special_ad_categories=[], budget_level="adset",
                                         daily_budget="10.00")


def test_a_campaign_budget_over_the_ceiling_fails(monkeypatch):
    tools, _ = rig(monkeypatch, make_sim())
    preview = tools["preview_create_campaign"](name="x", objective="OUTCOME_SALES",
                                               special_ad_categories=[], budget_level="campaign",
                                               daily_budget="900.00")
    assert failed(preview, "max_daily_budget")


def test_reverting_a_created_campaign_pauses_it_never_deletes(monkeypatch):
    s = make_sim()
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_create_campaign"](name="x", objective="OUTCOME_SALES",
                                               special_ad_categories=[], budget_level="campaign",
                                               daily_budget="10.00")
    result = tools["apply_create_campaign"](preview_token=preview.preview_token)
    s.objects[result.entity_id]["status"] = "ACTIVE"
    tools["revert_change"](entry_id=result.entry_id)
    assert s.objects[result.entity_id]["status"] == "PAUSED"
    assert "DELETE" not in s.methods


def test_an_ad_set_goes_through_the_targeting_validator(monkeypatch):
    s = make_sim()
    s.objects[CAMPAIGN_ID].pop("daily_budget")
    s.objects[CAMPAIGN_ID]["special_ad_categories"] = ["EMPLOYMENT"]
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_create_ad_set"](
        campaign_id=CAMPAIGN_ID, name="x", optimization_goal="LINK_CLICKS",
        targeting={**TARGETING, "age_min": 30}, daily_budget="20.00")
    assert failed(preview, "special_ad_categories")


def test_an_ad_set_under_cbo_takes_no_budget_and_is_created_paused(monkeypatch):
    s = make_sim()
    tools, _ = rig(monkeypatch, s)
    with pytest.raises(Exception):
        tools["preview_create_ad_set"](campaign_id=CAMPAIGN_ID, name="x",
                                       optimization_goal="LINK_CLICKS", targeting=TARGETING,
                                       daily_budget="20.00")
    preview = preview_ok(tools, "create_ad_set", s, campaign_id=CAMPAIGN_ID, name="x",
                         optimization_goal="LINK_CLICKS", targeting=TARGETING)
    tools["apply_create_ad_set"](preview_token=preview.preview_token)
    created = next(c for c in s.created if c["edge"] == "adsets")["payload"]
    assert created["status"] == "PAUSED" and "daily_budget" not in created


def test_duplicate_ad_set_lists_children_and_copies_paused(monkeypatch):
    s = make_sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "duplicate_ad_set", s, ad_set_id=ADSET_ID)
    assert any(AD_ID in c for c in preview.checks)
    result = tools["apply_duplicate_ad_set"](preview_token=preview.preview_token)
    copy = s.objects[result.entity_id]
    assert copy["status"] == "PAUSED"
    assert f"{result.entity_id}_{AD_ID}" in s.objects
    from metaads_mcp import ledger

    entry = ledger.find_entry(result.entry_id)
    assert entry["inverse"]["children"] == [f"{result.entity_id}_{AD_ID}"]


def test_duplicate_campaign_copies_paused(monkeypatch):
    s = make_sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "duplicate_campaign", s, campaign_id=CAMPAIGN_ID)
    result = tools["apply_duplicate_campaign"](preview_token=preview.preview_token)
    assert s.objects[result.entity_id]["status"] == "PAUSED"
    sent = next(c for c in s.created if c["edge"] == "copies")["payload"]
    assert sent["status_option"] == "PAUSED"
