"""One entity in full, ad verification on demand, and preview links."""

from __future__ import annotations

from metaads_mcp import ledger
from tests.harness import build_tools
from tests.test_mcp_creative_path import AD_ID, ADSET_ID, CAMPAIGN_ID, CREATIVE_ID, make_sim


def tools(monkeypatch, sim=None):
    sim = sim or make_sim()
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    return registrar.tools, sim


def test_get_campaign_reports_budget_level_money_and_children(monkeypatch):
    tool, sim = tools(monkeypatch)
    sim.edges[CAMPAIGN_ID] = {"adsets": [ADSET_ID], "ads": [AD_ID]}
    sim.objects[CAMPAIGN_ID]["special_ad_categories"] = []
    out = tool["get_campaign"](campaign_id=CAMPAIGN_ID)
    assert out["budget_level"] == "campaign" and out["daily_budget"] == "250.00 CAD"
    assert out["ad_set_count"] == 1 and out["ad_count"] == 1
    assert out["performance"]["last_7d"]["spend"] == "123.45"


def test_get_ad_set_reads_targeting_and_placements(monkeypatch):
    tool, sim = tools(monkeypatch)
    sim.objects[ADSET_ID]["targeting"] = {
        "geo_locations": {"countries": ["CA"]},
        "publisher_platforms": ["instagram"], "instagram_positions": ["stream"],
    }
    sim.canned[f"{ADSET_ID}/targetingsentencelines"] = {
        "targetingsentencelines": [{"content": "Location:", "children": ["Canada"]}]
    }
    out = tool["get_ad_set"](ad_set_id=ADSET_ID)
    assert out["targeting"]["geo_locations"] == {"countries": ["CA"]}
    assert out["targeting_sentences"] == ["Location: Canada"]
    assert out["preview_formats"] == ["INSTAGRAM_STANDARD"]


def test_get_ad_has_creative_review_and_previews(monkeypatch):
    tool, sim = tools(monkeypatch)
    out = tool["get_ad"](ad_id=AD_ID)
    assert out["creative"]["text"]["headline"] == "old headline"
    assert out["review"]["state"] == "not_flagged"
    assert out["previews"][0]["iframe_url"]


def test_verify_ad_diffs_against_the_ledger_and_updates_it(monkeypatch):
    tool, sim = tools(monkeypatch)
    preview = tool["preview_update_ad_copy"](ad_id=AD_ID, headline="approved")
    result = tool["apply_update_ad_copy"](preview_token=preview.preview_token)

    sim.objects[AD_ID]["effective_status"] = "DISAPPROVED"
    sim.objects[AD_ID]["ad_review_feedback"] = {"global": {"Text": "policy"}}
    out = tool["verify_ad"](ad_id=AD_ID)
    assert out["entry_id"] == result.entry_id
    assert out["review"]["state"] == "disapproved" and out["passed"] is False
    assert any(f["field"] == "headline" and f["ok"] for f in out["fields"])
    stored = ledger.find_entry(result.entry_id)["verification"]
    assert stored["review"]["state"] == "disapproved"
    assert "iframe_url" not in str(stored)


def test_verify_ad_on_an_ad_this_server_never_touched(monkeypatch):
    tool, sim = tools(monkeypatch)
    out = tool["verify_ad"](ad_id=AD_ID)
    assert out["entry_id"] is None and out["fields"] == []
    assert "only the live state" in out["compared_with"]


def test_get_ad_previews_for_a_creative_and_named_formats(monkeypatch):
    tool, sim = tools(monkeypatch)
    out = tool["get_ad_previews"](creative_id=CREATIVE_ID, formats=["INSTAGRAM_STORY"])
    assert [p["ad_format"] for p in out["previews"]] == ["INSTAGRAM_STORY"]
    assert CREATIVE_ID in out["previews"][0]["iframe_url"]
