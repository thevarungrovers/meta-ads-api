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


def two_accounts(monkeypatch, **extra):
    tools, _, sim = two_accounts_with_state(monkeypatch, **extra)
    return tools, sim


def two_accounts_with_state(monkeypatch, **extra):
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
                       "max_projected_delta_per_day_total": big,
                       # The totals are in CAD: a JPY change is counted only
                       # through a configured rate (made up for the test).
                       "currency_rates": (("JPY", Decimal("0.01")),), **extra})
    registrar, state = build_tools(sim, limits=limits, monkeypatch=monkeypatch)
    return registrar.tools, state, sim


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


# --- the totals are in ONE currency ----------------------------------------------


def test_a_change_in_another_currency_with_no_rate_is_refused(monkeypatch):
    from tests.kindkit import failed

    tools, sim = two_accounts(monkeypatch, currency_rates=())
    preview = tools["preview_ad_set_daily_budget"](ad_set_id=JPY_ADSET, new_daily_budget="3500")
    assert failed(preview, "cap_currency"), preview.checks
    line = next(c for c in preview.checks if " cap_currency:" in c)
    assert "CAD" in line and "JPY" in line and "currency_rates" in line, line


def test_a_change_in_another_currency_is_counted_at_the_configured_rate(monkeypatch):
    tools, sim = two_accounts(monkeypatch)
    preview = tools["preview_ad_set_status"](ad_set_id=JPY_ADSET, new_status="ACTIVE")
    assert not preview.blocked, [c for c in preview.checks if not c.startswith("OK")]
    assert preview.projected_daily_spend_delta == "3000.00"
    assert any("30.00 CAD" in w for w in preview.warnings), preview.warnings
    result = tools["apply_ad_set_status"](preview_token=preview.preview_token)
    from metaads_mcp import ledger

    assert ledger.find_entry(result.entry_id)["projected_daily_spend_delta"] == "30.00", (
        "the ledger feeds the daily total, so it holds the cap currency")


def test_the_cap_currency_is_the_first_allowed_one_unless_configured():
    from metaads_mcp.guardrails import cap_currency

    assert cap_currency(Limits(**{**DEFAULTS, "allowed_currencies": ("USD", "CAD")})) == "USD"
    assert cap_currency(Limits(**{**DEFAULTS, "allowed_currencies": ("USD", "CAD"),
                                  "cap_currency": "CAD"})) == "CAD"


def test_currency_rates_and_cap_currency_load_from_the_scope(tmp_path):
    import pytest

    from metaads_mcp.guardrails import GuardrailConfigError, load_limits

    path = tmp_path / "guardrails.toml"
    path.write_text('[scope]\nallowed_currencies = ["CAD", "JPY"]\ncap_currency = "cad"\n'
                    '[scope.currency_rates]\nJPY = "0.0091"\n')
    limits = load_limits(path)
    assert limits.cap_currency == "CAD" and dict(limits.currency_rates) == {"JPY": Decimal("0.0091")}
    path.write_text('[scope]\n[scope.currency_rates]\nJPY = "-1"\n')
    with pytest.raises(GuardrailConfigError):
        load_limits(path)
    path.write_text('[scope]\nallowed_currencies = ["CAD"]\ncap_currency = "USD"\n')
    with pytest.raises(GuardrailConfigError):
        load_limits(path)


# --- spend-restoring changes check the currency is allowed ------------------------


def _usd(sim):
    sim.account["currency"] = "USD"
    return sim


def test_a_status_activation_checks_the_currency(monkeypatch):
    from tests.kindkit import failed, rig
    from tests.test_mcp_write_path import CAMPAIGN_ID

    sim = _usd(make_sim())
    sim.objects[CAMPAIGN_ID]["status"] = "PAUSED"
    tools, _ = rig(monkeypatch, sim)
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="ACTIVE")
    assert failed(preview, "currency_allowed"), preview.checks


def test_a_targeting_widening_checks_the_currency(monkeypatch):
    from tests.kindkit import failed, rig
    from tests.test_kinds_targeting import BASE, sim as targeting_sim
    from tests.test_mcp_write_path import ADSET_ID

    sim = _usd(targeting_sim())
    sim.objects[ADSET_ID]["targeting"] = {**BASE, "genders": [1]}
    tools, _ = rig(monkeypatch, sim)
    preview = tools["preview_ad_set_targeting"](ad_set_id=ADSET_ID, genders=[])
    assert failed(preview, "currency_allowed"), preview.checks


def test_a_schedule_extension_checks_the_currency(monkeypatch):
    import datetime as dt

    from tests.kindkit import failed, rig
    from tests.test_kinds_bidding import abo_sim
    from tests.test_mcp_write_path import ADSET_ID

    def at(days):
        return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).replace(microsecond=0).isoformat()

    sim = _usd(abo_sim(start_time="2026-01-01T00:00:00+0000", end_time=at(5)))
    tools, _ = rig(monkeypatch, sim)
    preview = tools["preview_ad_set_schedule"](ad_set_id=ADSET_ID, end_time=at(30))
    assert failed(preview, "currency_allowed"), preview.checks


def test_a_tightening_revert_checks_the_currency(monkeypatch):
    import pytest

    from tests.kindkit import rig
    from tests.test_kinds_bidding import abo_sim
    from tests.test_mcp_write_path import ADSET_ID

    sim = abo_sim(bid_strategy="LOWEST_COST_WITH_MIN_ROAS", bid_constraints={"roas_average_floor": 20000})
    tools, _ = rig(monkeypatch, sim)
    preview = tools["preview_ad_set_roas_floor"](ad_set_id=ADSET_ID, new_roas_floor="2.4")
    result = tools["apply_ad_set_roas_floor"](preview_token=preview.preview_token)
    # The account now reads USD (its cached row dropped): restoring spend in a
    # currency outside allowed_currencies is refused, nothing sent.
    _usd(sim)
    import gc

    from metaads_reporting.client import ReadOnlyMetaAdsClient

    for obj in gc.get_objects():
        if isinstance(obj, ReadOnlyMetaAdsClient):
            obj._account_cache.clear()
    applied = len(sim.applied)
    with pytest.raises(Exception) as caught:
        tools["revert_change"](entry_id=result.entry_id)
    assert "USD" in str(caught.value) and "allowed" in str(caught.value)
    assert len(sim.applied) == applied


def test_conversion_to_the_cap_currency_ceils_increases_and_floors_cuts():
    """An increase is never under-counted: ROUND_CEILING for amounts >= 0 and
    ROUND_FLOOR for negatives. Half-even rounding counted 0.005 as nothing."""
    from metaads_mcp.guardrails import to_cap_currency

    limits = Limits(**{**DEFAULTS, "allowed_currencies": ("CAD", "JPY"),
                       "currency_rates": (("JPY", Decimal("0.005")),)})
    assert to_cap_currency(limits, Decimal("1"), "JPY") == Decimal("0.01")
    assert to_cap_currency(limits, Decimal("5"), "JPY") == Decimal("0.03")  # 0.025
    assert to_cap_currency(limits, Decimal("0"), "JPY") == Decimal("0.00")
    assert to_cap_currency(limits, Decimal("-1"), "JPY") == Decimal("-0.01")
    assert to_cap_currency(limits, Decimal("-5"), "JPY") == Decimal("-0.03")


def test_rates_for_a_currency_that_is_not_allowed_are_refused_at_load(tmp_path):
    import pytest

    from metaads_mcp.guardrails import GuardrailConfigError, load_limits

    path = tmp_path / "guardrails.toml"
    path.write_text('[scope]\nallowed_currencies = ["CAD"]\n'
                    '[scope.currency_rates]\nJPY = "0.0091"\n')
    with pytest.raises(GuardrailConfigError) as caught:
        load_limits(path)
    assert "JPY" in str(caught.value) and "allowed_currencies" in str(caught.value)


def test_any_change_that_adds_spend_is_checked_against_allowed_currencies(monkeypatch):
    """Not only activations: a kind that forgets its own currency check and
    projects added spend is still refused in a currency outside the list."""
    from tests.kindkit import failed

    tools, state, _ = two_accounts_with_state(monkeypatch, allowed_currencies=("CAD",))
    kind = state.engine.kinds["ad_set_bid_amount"]
    original = kind.propose

    def forgetful(ctx, **args):
        proposal = original(ctx, **args)
        proposal.checks = [c for c in proposal.checks if c.name != "currency_allowed"]
        return proposal

    monkeypatch.setattr(kind, "propose", forgetful)
    preview = tools["preview_ad_set_bid_amount"](ad_set_id=JPY_ADSET, new_bid_amount="120")
    assert Decimal(preview.projected_daily_spend_delta) > 0
    assert failed(preview, "currency_allowed"), preview.checks


def test_a_cut_or_a_pause_in_a_currency_with_no_rate_is_not_refused(monkeypatch):
    """Nothing to count: a change that can only lower spend must never be
    stranded by the missing conversion."""
    tools, sim = two_accounts(monkeypatch, currency_rates=())
    sim.objects[JPY_ADSET]["status"] = "ACTIVE"
    for name, args in (("ad_set_status", {"new_status": "PAUSED"}),
                       ("ad_set_daily_budget", {"new_daily_budget": "1000"})):
        preview = tools[f"preview_{name}"](ad_set_id=JPY_ADSET, **args)
        assert Decimal(preview.projected_daily_spend_delta) <= 0
        assert not preview.blocked, [c for c in preview.checks if not c.startswith("OK")]
        line = next(c for c in preview.checks if " cap_currency" in c)
        assert line.startswith("SKIP") and "cannot add spend" in line, line
    result = tools["apply_ad_set_daily_budget"](preview_token=preview.preview_token)
    from metaads_mcp import ledger

    assert ledger.find_entry(result.entry_id)["projected_daily_spend_delta"] == "0.00", (
        "an unconverted JPY figure is never written as if it were CAD")
