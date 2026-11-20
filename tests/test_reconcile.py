"""reconcile_ledger: what the ledger says the account is, against what it is."""

from __future__ import annotations

from metaads_reporting import logdb
from tests.harness import build_tools
from tests.test_mcp_write_path import CAMPAIGN_ID, make_sim


def test_a_matching_entry_is_matching_and_a_moved_one_is_drift(monkeypatch):
    sim = make_sim()
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    tool = registrar.tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    tool["apply_campaign_status"](preview_token=preview.preview_token)
    assert tool["reconcile_ledger"]()["matching"] == 1

    sim.objects[CAMPAIGN_ID]["status"] = "ACTIVE"  # someone in Ads Manager
    out = tool["reconcile_ledger"]()
    assert out["drift"][0]["live"] == "ACTIVE" and out["drift"][0]["after"] == "PAUSED"


def test_a_budget_that_moved_to_the_ad_sets_shows_as_drift(monkeypatch):
    """A legacy row: a campaign budget change whose campaign no longer holds a
    budget at all, so a revert would be refused as stale."""
    sim = make_sim()
    sim.objects[CAMPAIGN_ID].pop("daily_budget")
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    logdb.insert_change(
        entry_id="legacy", tool="apply_campaign_daily_budget", account_id="1234567890",
        entity_type="campaign", entity_id=CAMPAIGN_ID, entity_name="Example campaign",
        field_name="daily_budget", before="50.00 CAD", after="60.00 CAD", projected_delta="10",
    )
    logdb.complete_change(entry_id="legacy", ok=True)
    out = registrar.tools["reconcile_ledger"]()
    assert out["drift"][0]["entry_id"] == "legacy"
    assert out["drift"][0]["live"] == "(not set)"


def test_an_intent_with_no_outcome_is_reported(monkeypatch):
    registrar, _ = build_tools(make_sim(), monkeypatch=monkeypatch)
    logdb.insert_change(
        entry_id="inflight", tool="apply_campaign_status", account_id="1234567890",
        entity_type="campaign", entity_id=CAMPAIGN_ID, entity_name="x",
        field_name="status", before="ACTIVE", after="PAUSED", projected_delta="0",
    )
    out = registrar.tools["reconcile_ledger"]()
    assert [e["entry_id"] for e in out["unknown_outcome"]] == ["inflight"]


def test_an_ad_still_in_review_is_flagged(monkeypatch):
    from tests.test_mcp_creative_path import AD_ID, make_sim as creative_sim

    sim = creative_sim()
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    tool = registrar.tools
    sim.objects[AD_ID]["effective_status"] = "PENDING_REVIEW"
    preview = tool["preview_update_ad_copy"](ad_id=AD_ID, headline="new")
    tool["apply_update_ad_copy"](preview_token=preview.preview_token)
    out = tool["reconcile_ledger"]()
    assert out["pending_review"][0]["ad_id"] == AD_ID
