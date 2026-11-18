"""Money is read and written in the TOUCHED object's account currency.

Scope allows any account in allowed_account_ids, so the default account's
currency is the wrong one for an object elsewhere: a JPY ad set read as CAD
is a hundredfold error in the approval prompt and in the write.
"""

from __future__ import annotations

from decimal import Decimal

from metaads_mcp.guardrails import DEFAULTS, Limits
from tests.fakes import account, ad_set, campaign
from tests.harness import build_tools
from tests.test_mcp_write_path import make_sim

JPY = "2222222222"
JPY_CAMPAIGN = "120200000000091"
JPY_ADSET = "120200000000092"


def two_accounts(monkeypatch):
    sim = make_sim()
    sim.accounts[f"act_{JPY}"] = account(currency="JPY", id=f"act_{JPY}", account_id=JPY)
    sim.objects[JPY_CAMPAIGN] = campaign(JPY_CAMPAIGN, daily_budget=None, account_id=JPY)
    sim.objects[JPY_ADSET] = ad_set(JPY_ADSET, daily_budget="3000", status="PAUSED",
                                    campaign_id=JPY_CAMPAIGN, account_id=JPY,
                                    bid_strategy="LOWEST_COST_WITH_BID_CAP", bid_amount="100")
    sim.edges[JPY_CAMPAIGN] = {"adsets": [JPY_ADSET]}
    big = Decimal("100000")
    limits = Limits(**{**DEFAULTS, "allowed_account_ids": ("1234567890", JPY),
                       "allowed_currencies": ("CAD", "JPY"), "max_daily_budget": big,
                       "max_bid_amount": big, "max_projected_delta_per_day": big,
                       "max_projected_session_delta": big,
                       "max_projected_delta_per_day_total": big})
    registrar, _ = build_tools(sim, limits=limits, monkeypatch=monkeypatch)
    return registrar.tools, sim


def test_a_budget_in_another_account_uses_that_accounts_currency(monkeypatch):
    tools, sim = two_accounts(monkeypatch)
    preview = tools["preview_ad_set_daily_budget"](ad_set_id=JPY_ADSET, new_daily_budget="3500")
    assert preview.currency == "JPY" and preview.account_id == JPY
    assert preview.before.endswith("JPY") and "3000" in preview.before, preview.before
    assert any("currency_allowed" in c and "JPY" in c for c in preview.checks)
    assert not preview.blocked, [c for c in preview.checks if not c.startswith("OK")]
    result = tools["apply_ad_set_daily_budget"](preview_token=preview.preview_token)
    assert str(sim.objects[JPY_ADSET]["daily_budget"]) == "3500", "minor units of JPY, not CAD"
    from metaads_mcp import ledger

    assert ledger.find_entry(result.entry_id)["account_id"] == JPY, "the ledger names the entity's account"


def test_a_bid_in_another_account_uses_that_accounts_currency(monkeypatch):
    tools, sim = two_accounts(monkeypatch)
    preview = tools["preview_ad_set_bid_amount"](ad_set_id=JPY_ADSET, new_bid_amount="120")
    assert "100" in preview.before and preview.before.endswith("JPY"), preview.before
    tools["apply_ad_set_bid_amount"](preview_token=preview.preview_token)
    assert str(sim.objects[JPY_ADSET]["bid_amount"]) == "120"


def test_an_activation_in_another_account_projects_in_its_currency(monkeypatch):
    tools, _ = two_accounts(monkeypatch)
    preview = tools["preview_ad_set_status"](ad_set_id=JPY_ADSET, new_status="ACTIVE")
    assert preview.projected_daily_spend_delta == "3000.00", preview.projected_daily_spend_delta
    assert preview.currency == "JPY"


# --- the read paths use the entity's account currency --------------------------


def test_find_ad_sets_in_another_accounts_campaign_reads_its_currency(monkeypatch):
    tools, _ = two_accounts(monkeypatch)
    out = tools["find_ad_sets"](campaign_id=JPY_CAMPAIGN)
    row = out["rows"][0]
    assert row["currency"] == "JPY", row
    assert row["daily_budget"].startswith("3000") or row["daily_budget"].startswith("3,000"), row


def test_get_campaign_and_get_ad_set_read_their_own_accounts_currency(monkeypatch):
    tools, sim = two_accounts(monkeypatch)
    sim.objects[JPY_CAMPAIGN]["daily_budget"] = "5000"
    campaign = tools["get_campaign"](campaign_id=JPY_CAMPAIGN)
    assert campaign["currency"] == "JPY" and "5000" in campaign["daily_budget"].replace(",", ""), campaign
    adset = tools["get_ad_set"](ad_set_id=JPY_ADSET)
    adset = adset if isinstance(adset, dict) else adset.model_dump()
    assert adset["currency"] == "JPY" and "3000" in adset["daily_budget"].replace(",", ""), adset
