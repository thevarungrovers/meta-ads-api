"""The currency gates on a change that projects nothing.

A projection of 0 is not proof that a change cannot add spend. Adding days
to a daily-budget flight with no unused headroom projected 0 -- the ad set
already spends its budget, which is exactly why the extra days add spend --
and in a currency outside allowed_currencies, with no rate, it passed both
currency gates. The cap_currency refusal is skipped only for a change that
provably cannot add spend; allowed_currencies is checked on every change
that can move money.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tests.kindkit import failed
from tests.test_multi_account_currency import JPY_ADSET, two_accounts


def _at(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).replace(
        microsecond=0).isoformat()


def _no_headroom(monkeypatch):
    from metaads_mcp.changes import common

    monkeypatch.setattr(common, "unused_headroom", lambda ctx, entry: Decimal("0"))


def test_extending_a_flight_with_no_headroom_is_refused_outside_the_currency_scope(monkeypatch):
    tools, sim = two_accounts(monkeypatch, allowed_currencies=("CAD",), currency_rates=())
    sim.objects[JPY_ADSET]["status"] = "ACTIVE"
    sim.objects[JPY_ADSET]["end_time"] = _at(5)
    _no_headroom(monkeypatch)
    preview = tools["preview_ad_set_schedule"](ad_set_id=JPY_ADSET, end_time=_at(60))
    # The added days spend the whole daily budget, not the unused headroom.
    assert preview.projected_daily_spend_delta == "3000.00", preview.projected_daily_spend_delta
    assert preview.blocked
    assert failed(preview, "currency_allowed"), preview.checks
    assert failed(preview, "cap_currency"), preview.checks


def test_an_earlier_start_projects_the_daily_budget(monkeypatch):
    tools, sim = two_accounts(monkeypatch, allowed_currencies=("CAD",), currency_rates=())
    sim.objects[JPY_ADSET].update(start_time=_at(10), end_time=_at(40))
    _no_headroom(monkeypatch)
    preview = tools["preview_ad_set_schedule"](ad_set_id=JPY_ADSET, start_time=_at(2))
    assert preview.projected_daily_spend_delta == "3000.00"
    assert failed(preview, "currency_allowed") and failed(preview, "cap_currency"), preview.checks


def test_a_zero_projection_alone_does_not_skip_the_cap_currency_refusal(monkeypatch):
    """An optimization goal change projects 0 but is not a reduction."""
    tools, sim = two_accounts(monkeypatch, currency_rates=())
    sim.objects[JPY_ADSET]["optimization_goal"] = "LINK_CLICKS"
    preview = tools["preview_ad_set_optimization_goal"](
        ad_set_id=JPY_ADSET, optimization_goal="LANDING_PAGE_VIEWS")
    assert Decimal(preview.projected_daily_spend_delta) == 0
    assert failed(preview, "cap_currency"), preview.checks


def test_allowed_currencies_is_checked_whatever_the_projection(monkeypatch):
    """A change that projects nothing but is not a pause is still checked."""
    tools, sim = two_accounts(monkeypatch, allowed_currencies=("CAD",), currency_rates=())
    sim.objects[JPY_ADSET]["optimization_goal"] = "LINK_CLICKS"
    preview = tools["preview_ad_set_optimization_goal"](
        ad_set_id=JPY_ADSET, optimization_goal="LANDING_PAGE_VIEWS")
    assert Decimal(preview.projected_daily_spend_delta) == 0
    assert failed(preview, "currency_allowed"), preview.checks


# --- pausing is always allowed ------------------------------------------------------
#
# Stopping spend is never blocked: a pause in an account whose currency is not
# in allowed_currencies (and has no rate) passes both currency gates. The
# account scope still decides which accounts may be touched at all.

JPY_AD = "120200000000093"


def _outside_currency_scope(monkeypatch, status="ACTIVE"):
    from tests.fakes import ad

    tools, sim = two_accounts(monkeypatch, allowed_currencies=("CAD",), currency_rates=())
    sim.objects["120200000000091"]["status"] = status
    sim.objects[JPY_ADSET]["status"] = status
    sim.objects[JPY_AD] = ad(JPY_AD, status=status, adset_id=JPY_ADSET,
                             campaign_id="120200000000091", account_id="2222222222")
    sim.edges.setdefault(JPY_ADSET, {})["ads"] = [JPY_AD]
    return tools, sim


def _currency_lines(preview):
    return [c for c in preview.checks if "currency" in c]


@pytest.mark.parametrize("name,args", [
    ("campaign_status", {"campaign_id": "120200000000091"}),
    ("ad_set_status", {"ad_set_id": JPY_ADSET}),
    ("ad_status", {"ad_id": JPY_AD}),
])
def test_pausing_in_a_currency_outside_the_scope_is_allowed(monkeypatch, name, args):
    tools, sim = _outside_currency_scope(monkeypatch)
    preview = tools[f"preview_{name}"](new_status="PAUSED", **args)
    assert not preview.blocked, preview.checks
    assert not failed(preview, "currency_allowed"), _currency_lines(preview)
    assert not failed(preview, "cap_currency"), _currency_lines(preview)
    tools[f"apply_{name}"](preview_token=preview.preview_token)
    entity = next(iter(args.values()))
    assert sim.objects[entity]["status"] == "PAUSED"


def test_a_batch_pause_in_a_currency_outside_the_scope_is_allowed(monkeypatch):
    tools, _ = _outside_currency_scope(monkeypatch)
    preview = tools["preview_multi_ad_status"](ad_set_id=JPY_ADSET, ad_ids=[JPY_AD],
                                               new_status="PAUSED")
    assert not preview.blocked, preview.checks


@pytest.mark.parametrize("name,args", [
    ("campaign_status", {"campaign_id": "120200000000091"}),
    ("ad_set_status", {"ad_set_id": JPY_ADSET}),
    ("ad_status", {"ad_id": JPY_AD}),
])
def test_activating_in_a_currency_outside_the_scope_is_still_refused(monkeypatch, name, args):
    tools, _ = _outside_currency_scope(monkeypatch, status="PAUSED")
    preview = tools[f"preview_{name}"](new_status="ACTIVE", **args)
    assert preview.blocked
    assert failed(preview, "currency_allowed"), _currency_lines(preview)


def test_a_pause_outside_the_account_scope_is_still_refused(monkeypatch):
    tools, sim = two_accounts(monkeypatch, allowed_account_ids=("1234567890",),
                              allowed_currencies=("CAD",), currency_rates=())
    sim.objects[JPY_ADSET]["status"] = "ACTIVE"
    preview = tools["preview_ad_set_status"](ad_set_id=JPY_ADSET, new_status="PAUSED")
    assert preview.blocked
    assert any(c.startswith("FAIL") and "account" in c for c in preview.checks), preview.checks


def test_a_tightening_with_nothing_projected_skips_only_the_rate(monkeypatch):
    tools, sim = two_accounts(monkeypatch, currency_rates=())
    sim.objects[JPY_ADSET].update(status="ACTIVE", end_time=_at(40),
                                  start_time="2026-01-01T00:00:00+0000")
    preview = tools["preview_ad_set_schedule"](ad_set_id=JPY_ADSET, end_time=_at(10))
    assert Decimal(preview.projected_daily_spend_delta) == 0
    line = next(c for c in preview.checks if " cap_currency" in c)
    assert line.startswith("SKIP"), line
    assert not preview.blocked, preview.checks


def test_reverting_a_shortened_daily_flight_projects_the_daily_budget(monkeypatch):
    from tests.kindkit import preview_ok, rig
    from tests.test_kinds_bidding import abo_sim
    from tests.test_mcp_write_path import ADSET_ID

    sim = abo_sim(start_time="2026-01-01T00:00:00+0000", end_time=_at(30))
    tools, _ = rig(monkeypatch, sim)
    _no_headroom(monkeypatch)
    preview = preview_ok(tools, "ad_set_schedule", sim, ad_set_id=ADSET_ID, end_time=_at(10))
    result = tools["apply_ad_set_schedule"](preview_token=preview.preview_token)
    undo = tools["revert_change"](entry_id=result.entry_id)
    assert Decimal(undo.projected_daily_spend_delta) == pytest.approx(Decimal("50.00"))


def test_extending_a_campaign_flight_projects_its_daily_budget(monkeypatch):
    from tests.kindkit import rig
    from tests.test_mcp_write_path import CAMPAIGN_ID, make_sim

    sim = make_sim()
    sim.objects[CAMPAIGN_ID].update(start_time="2026-01-01T00:00:00+0000", stop_time=_at(5))
    tools, _ = rig(monkeypatch, sim)
    _no_headroom(monkeypatch)
    budget = Decimal(str(sim.objects[CAMPAIGN_ID]["daily_budget"])) / 100
    preview = tools["preview_campaign_schedule"](campaign_id=CAMPAIGN_ID, stop_time=_at(30))
    assert Decimal(preview.projected_daily_spend_delta) == budget


# --- "tightens" is not proof of a reduction ---------------------------------------
#
# ``tightens`` says a change's REVERT lifts a restriction. Some kinds set it
# whatever the direction, so it cannot stand in for "cannot add spend": a
# loosened frequency cap or a switch between two restricting bid strategies
# was waved past the missing rate. Only a change that is provably
# one-directional -- the kind computes it from before and after -- skips it.


def _cap_line(preview) -> str:
    return next(c for c in preview.checks if " cap_currency" in c)


def _reach_sim(monkeypatch, **adset):
    tools, sim = two_accounts(monkeypatch, currency_rates=())
    sim.objects[JPY_ADSET].update(status="ACTIVE", optimization_goal="REACH", **adset)
    return tools, sim


def test_loosening_a_frequency_cap_is_refused_without_a_rate(monkeypatch):
    tools, _ = _reach_sim(monkeypatch, frequency_control_specs=[
        {"event": "IMPRESSIONS", "interval_days": 7, "max_frequency": 1}])
    preview = tools["preview_ad_set_frequency_cap"](
        ad_set_id=JPY_ADSET, max_frequency=30, interval_days=1)
    assert preview.blocked and failed(preview, "cap_currency"), preview.checks


def test_a_frequency_cap_loosened_in_one_dimension_is_refused_without_a_rate(monkeypatch):
    tools, _ = _reach_sim(monkeypatch, frequency_control_specs=[
        {"event": "IMPRESSIONS", "interval_days": 7, "max_frequency": 2}])
    preview = tools["preview_ad_set_frequency_cap"](
        ad_set_id=JPY_ADSET, max_frequency=1, interval_days=1)
    assert failed(preview, "cap_currency"), preview.checks


@pytest.mark.parametrize("specs", [
    None,  # a first cap
    [{"event": "IMPRESSIONS", "interval_days": 1, "max_frequency": 30}],
])
def test_a_stricter_frequency_cap_needs_no_rate(monkeypatch, specs):
    tools, _ = _reach_sim(monkeypatch, frequency_control_specs=specs)
    preview = tools["preview_ad_set_frequency_cap"](
        ad_set_id=JPY_ADSET, max_frequency=1, interval_days=7)
    assert not preview.blocked, preview.checks
    assert _cap_line(preview).startswith("SKIP"), preview.checks


def test_switching_min_roas_to_cost_cap_is_refused_without_a_rate(monkeypatch):
    tools, sim = two_accounts(monkeypatch, currency_rates=())
    sim.objects[JPY_ADSET].update(status="ACTIVE", bid_strategy="LOWEST_COST_WITH_MIN_ROAS",
                                  bid_amount=None, bid_constraints={"roas_average_floor": 50000})
    preview = tools["preview_ad_set_bid_strategy"](
        ad_set_id=JPY_ADSET, strategy="COST_CAP", bid_amount="5000")
    assert Decimal(preview.projected_daily_spend_delta) == 0
    assert preview.blocked and failed(preview, "cap_currency"), preview.checks


def test_a_lower_cap_under_the_same_strategy_needs_no_rate(monkeypatch):
    from metaads_mcp.changes import bidding

    # No clicks to project from: the projection is 0, and only the direction
    # proves the change cannot add spend.
    monkeypatch.setattr(bidding, "bid_change_projection", lambda *a, **k: Decimal("0"))
    tools, sim = two_accounts(monkeypatch, currency_rates=())
    sim.objects[JPY_ADSET]["status"] = "ACTIVE"
    preview = tools["preview_ad_set_bid_strategy"](
        ad_set_id=JPY_ADSET, strategy="LOWEST_COST_WITH_BID_CAP", bid_amount="80")
    assert not preview.blocked, preview.checks
    assert _cap_line(preview).startswith("SKIP"), preview.checks


def test_a_raised_roas_floor_needs_no_rate_and_a_lowered_one_does(monkeypatch):
    tools, sim = two_accounts(monkeypatch, currency_rates=())
    sim.objects[JPY_ADSET].update(status="ACTIVE", bid_strategy="LOWEST_COST_WITH_MIN_ROAS",
                                  bid_amount=None, bid_constraints={"roas_average_floor": 20000})
    raised = tools["preview_ad_set_roas_floor"](ad_set_id=JPY_ADSET, new_roas_floor="2.2")
    assert _cap_line(raised).startswith("SKIP"), raised.checks
    raised_by_strategy = tools["preview_ad_set_bid_strategy"](
        ad_set_id=JPY_ADSET, strategy="LOWEST_COST_WITH_MIN_ROAS", roas_floor="2.2")
    assert _cap_line(raised_by_strategy).startswith("SKIP"), raised_by_strategy.checks


def test_narrowing_targeting_only_needs_no_rate_and_a_mixed_edit_does(monkeypatch):
    from tests.kindkit import rig
    from tests.test_kinds_targeting import sim as targeting_sim
    from tests.test_mcp_write_path import ADSET_ID

    s = targeting_sim()
    s.account["currency"] = "JPY"
    tools, _ = rig(monkeypatch, s, allowed_currencies=("CAD", "JPY"), currency_rates=())
    narrowed = tools["preview_ad_set_targeting"](ad_set_id=ADSET_ID, age_min=30)
    assert _cap_line(narrowed).startswith("SKIP"), narrowed.checks
    mixed = tools["preview_ad_set_targeting"](ad_set_id=ADSET_ID, age_min=30, age_max=65)
    assert failed(mixed, "cap_currency"), mixed.checks


def test_only_a_tightening_frequency_cap_is_marked_tightens(monkeypatch):
    """The ledger mark drives what a revert projects: a loosening's revert
    re-tightens, so it is not marked."""
    from metaads_mcp import ledger
    from tests.test_multi_account_currency import two_accounts as rated

    tools, sim = rated(monkeypatch)
    sim.objects[JPY_ADSET].update(status="ACTIVE", optimization_goal="REACH",
                                  frequency_control_specs=[
                                      {"event": "IMPRESSIONS", "interval_days": 7,
                                       "max_frequency": 1}])
    loosen = tools["preview_ad_set_frequency_cap"](
        ad_set_id=JPY_ADSET, max_frequency=3, interval_days=7)
    result = tools["apply_ad_set_frequency_cap"](preview_token=loosen.preview_token)
    assert not ledger.find_entry(result.entry_id)["inverse"].get("tightens")
    tighten = tools["preview_ad_set_frequency_cap"](
        ad_set_id=JPY_ADSET, max_frequency=1, interval_days=7)
    result = tools["apply_ad_set_frequency_cap"](preview_token=tighten.preview_token)
    assert ledger.find_entry(result.entry_id)["inverse"].get("tightens") is True


def test_re_applying_an_extension_recorded_at_zero_projects_the_daily_budget(monkeypatch):
    """An extension row written when extensions recorded the unused headroom
    (often 0): reverting its revert re-extends, and adds a whole day's budget
    on every day it puts back, whatever was stored."""
    from metaads_mcp.changes import bidding
    from tests.kindkit import rig
    from tests.test_kinds_bidding import abo_sim
    from tests.test_mcp_write_path import ADSET_ID

    sim = abo_sim(start_time="2026-01-01T00:00:00+0000", end_time=_at(10))
    tools, _ = rig(monkeypatch, sim)
    _no_headroom(monkeypatch)
    real_bound = bidding._daily_bound_of
    # The old projection: the headroom, 0 here.
    monkeypatch.setattr(bidding, "_daily_bound_of", lambda ctx, row, end: (Decimal("0"), "old"))
    extend = tools["preview_ad_set_schedule"](ad_set_id=ADSET_ID, end_time=_at(30))
    assert Decimal(extend.projected_daily_spend_delta) == 0
    result = tools["apply_ad_set_schedule"](preview_token=extend.preview_token)
    monkeypatch.setattr(bidding, "_daily_bound_of", real_bound)
    undo = tools["revert_change"](entry_id=result.entry_id)
    assert Decimal(undo.projected_daily_spend_delta) == 0, "shortening adds nothing"
    redo = tools["revert_change"](entry_id=undo.entry_id)
    assert Decimal(redo.projected_daily_spend_delta) == pytest.approx(Decimal("50.00"))
