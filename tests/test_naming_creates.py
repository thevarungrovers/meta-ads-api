"""Everything the agent CREATES is named with its prefix, for all four
identity combinations. Placeholders only: AB / max."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from metaads_reporting.naming import Identity
from tests.kindkit import as_identity, failed, preview_ok, rig
from tests.test_mcp_write_path import ADSET_ID, CAMPAIGN_ID, make_sim

COMBOS = [
    pytest.param(Identity("AB", "max"), "AB-max-", id="both"),
    pytest.param(Identity("AB", ""), "AB-", id="owner-only"),
    pytest.param(Identity("", "max"), "max-", id="agent-only"),
    pytest.param(Identity(), "", id="neither"),
]
TARGETING = {"geo_locations": {"countries": ["CA"]}, "age_min": 18}


def _sent(sim, edge):
    return next(c for c in sim.created if c["edge"] == edge)["payload"]


@pytest.mark.parametrize("identity, prefix", COMBOS)
def test_a_campaign_is_sent_with_the_prefix(monkeypatch, identity, prefix):
    s = make_sim()
    tools, state = rig(monkeypatch, s)
    as_identity(state, identity)
    preview = preview_ok(tools, "create_campaign", s, name="spring_promo", objective="OUTCOME_SALES",
                         special_ad_categories=[], budget_level="adset")
    assert preview.entity_name == f"{prefix}spring_promo"
    assert preview.naming[0]["final"] == f"{prefix}spring_promo"
    assert preview.naming[0]["prefix"] == prefix
    validated = next(v for v in s.validated if v.get("edge") == "campaigns")["payload"]
    assert validated["name"] == f"{prefix}spring_promo", "the validate_only request carries it too"
    tools["apply_create_campaign"](preview_token=preview.preview_token)
    assert _sent(s, "campaigns")["name"] == f"{prefix}spring_promo"


@pytest.mark.parametrize("identity, prefix", COMBOS)
def test_an_ad_set_is_sent_with_the_prefix(monkeypatch, identity, prefix):
    s = make_sim()
    tools, state = rig(monkeypatch, s)
    as_identity(state, identity)
    preview = preview_ok(tools, "create_ad_set", s, campaign_id=CAMPAIGN_ID, name="Broad CA",
                         optimization_goal="LINK_CLICKS", targeting=TARGETING)
    tools["apply_create_ad_set"](preview_token=preview.preview_token)
    assert _sent(s, "adsets")["name"] == f"{prefix}Broad CA"


def test_an_ad_set_with_an_empty_name_is_refused(monkeypatch):
    s = make_sim()
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_create_ad_set"](campaign_id=CAMPAIGN_ID, name="  ",
                                             optimization_goal="LINK_CLICKS", targeting=TARGETING)
    assert failed(preview, "naming"), preview.checks


def test_a_name_over_the_limit_is_refused_counting_the_prefix(monkeypatch):
    s = make_sim()
    tools, _ = rig(monkeypatch, s)
    ok = tools["preview_create_campaign"](name="x" * 393, objective="OUTCOME_SALES",
                                          special_ad_categories=[], budget_level="adset")
    assert not failed(ok, "name_length") and len(ok.entity_name) == 400
    asked = len(s.validated)
    preview = tools["preview_create_campaign"](name="x" * 394, objective="OUTCOME_SALES",
                                               special_ad_categories=[], budget_level="adset")
    assert failed(preview, "name_length"), preview.checks
    assert any("at most 393" in c for c in preview.checks)
    assert len(s.validated) == asked, "a refused name is never sent, not even validate_only"


def test_another_agents_prefix_is_refused(monkeypatch):
    tools, _ = rig(monkeypatch, make_sim())
    preview = tools["preview_create_campaign"](name="CD-bot-launch", objective="OUTCOME_SALES",
                                               special_ad_categories=[], budget_level="adset")
    assert failed(preview, "naming") and preview.blocked


def test_a_malformed_identity_blocks_the_create_and_names_the_key(monkeypatch):
    tools, state = rig(monkeypatch, make_sim())
    as_identity(state, Identity.from_values("AB", "max-1"))
    preview = tools["preview_create_campaign"](name="spring", objective="OUTCOME_SALES",
                                               special_ad_categories=[], budget_level="adset")
    assert failed(preview, "agent_identity")
    assert any("META_ADS_MCP_AGENT_NAME" in c for c in preview.checks)


def test_a_duplicate_campaign_name_only_warns(monkeypatch):
    s = make_sim()
    s.objects[CAMPAIGN_ID]["name"] = "AB-max-spring"
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_create_campaign"](name="spring", objective="OUTCOME_SALES",
                                               special_ad_categories=[], budget_level="adset")
    assert not preview.blocked, preview.checks
    assert any(c.startswith("WARN name_is_free") for c in preview.checks), preview.checks


def test_the_preview_says_when_the_name_changes(monkeypatch):
    tools, _ = rig(monkeypatch, make_sim())
    preview = tools["preview_create_campaign"](name="spring", objective="OUTCOME_SALES",
                                               special_ad_categories=[], budget_level="adset")
    assert 'the name will be "AB-max-spring" (prefix added)' in preview.warnings


# --- ads, creatives: the top-level name only, never the headline ------------------


@pytest.mark.parametrize("identity, prefix", COMBOS)
def test_create_ad_stamps_the_ad_name(monkeypatch, identity, prefix):
    from tests.test_kinds_ads import sim as ads_sim
    from tests.test_mcp_creative_path import ADSET_ID as AD_ADSET, CREATIVE_ID

    s = ads_sim()
    tools, state = rig(monkeypatch, s)
    as_identity(state, identity)
    preview = preview_ok(tools, "create_ad", s, ad_set_id=AD_ADSET, creative_id=CREATIVE_ID, name="New ad")
    tools["apply_create_ad"](preview_token=preview.preview_token)
    assert _sent(s, "ads")["name"] == f"{prefix}New ad"


@pytest.mark.parametrize("identity, prefix", COMBOS)
def test_create_creative_stamps_the_top_level_name_and_leaves_the_headline(monkeypatch, identity, prefix):
    from tests.test_kinds_ads import sim as ads_sim

    s = ads_sim()
    tools, state = rig(monkeypatch, s)
    as_identity(state, identity)
    preview = tools["preview_create_creative"](name="Spring", link="https://example.com/spring",
                                               headline="Fresh greens", image_hash="oldhash")
    assert not preview.blocked, preview.checks
    tools["apply_create_creative"](preview_token=preview.preview_token)
    sent = _sent(s, "adcreatives")
    assert sent["name"] == f"{prefix}Spring"
    assert json.loads(sent["object_story_spec"])["link_data"]["name"] == "Fresh greens", \
        "the headline viewers see is never stamped"


def test_url_tags_with_a_name_macro_warn(monkeypatch):
    from tests.test_kinds_ads import sim as ads_sim

    tools, _ = rig(monkeypatch, ads_sim())
    preview = tools["preview_create_creative"](name="Spring", link="https://example.com/spring",
                                               image_hash="oldhash",
                                               url_tags="utm_campaign={{campaign.name}}&utm_source=meta")
    assert any("{{campaign.name}}" in w and "prefix" in w for w in preview.warnings), preview.warnings


# --- audiences, studies, labels ------------------------------------------------------


@pytest.mark.parametrize("identity, prefix", COMBOS)
def test_an_audience_is_named_with_the_prefix(monkeypatch, identity, prefix):
    from tests.test_kinds_audiences import sim_with_sources

    s = sim_with_sources()
    tools, state = rig(monkeypatch, s)
    as_identity(state, identity)
    preview = preview_ok(tools, "create_audience", s, kind="website", name="Visitors 30d",
                         pixel_id="PX1", retention_days=30)
    tools["apply_create_audience"](preview_token=preview.preview_token)
    assert _sent(s, "customaudiences")["name"] == f"{prefix}Visitors 30d"


def test_a_study_is_named_with_the_prefix_and_its_cells_are_not(monkeypatch):
    from tests.test_kinds_studies import CELLS, future, sim as study_sim

    s = study_sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "create_ab_test", s, name="Audience test", variable="audience",
                         cells=CELLS, start_time=future(1), end_time=future(15))
    tools["apply_create_ab_test"](preview_token=preview.preview_token)
    sent = _sent(s, "ad_studies")
    assert sent["name"] == "AB-max-Audience test"
    assert [c["name"] for c in json.loads(sent["cells"])] == ["A", "B"]


@pytest.mark.parametrize("identity, prefix", COMBOS)
def test_a_label_is_created_with_the_prefix(monkeypatch, identity, prefix):
    s = make_sim()
    tools, state = rig(monkeypatch, s)
    as_identity(state, identity)
    preview = preview_ok(tools, "low_risk_change", s, entity_type="account", entity_id="x",
                         field="label_create", value="spring")
    tools["apply_low_risk_change"](preview_token=preview.preview_token)
    assert _sent(s, "adlabels")["name"] == f"{prefix}spring"
