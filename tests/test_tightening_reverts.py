"""Undoing a change that TIGHTENED spend is not free: it projects the unused
budget headroom and counts against the daily total."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.kindkit import preview_ok, rig
from tests.test_kinds_bidding import abo_sim
from tests.test_kinds_targeting import sim as targeting_sim
from tests.test_mcp_write_path import ADSET_ID

#: abo_sim's ad set: 50.00/day, less the 123.45 / 7 the fixture spends a day.
HEADROOM = Decimal("50.00") - (Decimal("123.45") / 7)


def _revert(tools, kind, preview):
    result = tools[f"apply_{kind}"](preview_token=preview.preview_token)
    return tools["revert_change"](entry_id=result.entry_id)


def test_reverting_a_roas_floor_raise_projects_the_headroom(monkeypatch):
    sim = abo_sim(bid_strategy="LOWEST_COST_WITH_MIN_ROAS",
                  bid_constraints={"roas_average_floor": 20000})
    tools, _ = rig(monkeypatch, sim)
    preview = preview_ok(tools, "ad_set_roas_floor", sim, ad_set_id=ADSET_ID, new_roas_floor="2.4")
    undo = _revert(tools, "ad_set_roas_floor", preview)
    assert Decimal(undo.projected_daily_spend_delta) == pytest.approx(HEADROOM, abs=Decimal("0.01"))


def test_a_frequency_cap_is_ledgered_as_tightening(monkeypatch):
    sim = abo_sim(optimization_goal="REACH")
    tools, _ = rig(monkeypatch, sim)
    preview = preview_ok(tools, "ad_set_frequency_cap", sim, ad_set_id=ADSET_ID,
                         max_frequency=2, interval_days=7)
    result = tools["apply_ad_set_frequency_cap"](preview_token=preview.preview_token)
    from metaads_mcp import ledger

    entry = ledger.find_entry(result.entry_id)
    assert (entry["inverse"] or {}).get("tightens") is True


def test_reverting_a_bid_cap_strategy_projects_the_headroom(monkeypatch):
    sim = abo_sim()
    tools, _ = rig(monkeypatch, sim)
    preview = preview_ok(tools, "ad_set_bid_strategy", sim, ad_set_id=ADSET_ID,
                         strategy="LOWEST_COST_WITH_BID_CAP", bid_amount="3.00")
    undo = _revert(tools, "ad_set_bid_strategy", preview)
    assert Decimal(undo.projected_daily_spend_delta) == pytest.approx(HEADROOM, abs=Decimal("0.01"))


def test_reverting_narrowed_targeting_projects_the_headroom(monkeypatch):
    s = targeting_sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "ad_set_targeting", s, ad_set_id=ADSET_ID, age_min=30)
    undo = _revert(tools, "ad_set_targeting", preview)
    # The campaign's 250.00 budget, less the 123.45 / 7 it spends a day.
    assert undo.projected_daily_spend_delta == "232.36"


def test_a_tightening_revert_over_the_daily_total_is_refused(monkeypatch):
    sim = abo_sim(bid_strategy="LOWEST_COST_WITH_MIN_ROAS",
                  bid_constraints={"roas_average_floor": 20000})
    tools, _ = rig(monkeypatch, sim, max_projected_delta_per_day_total=Decimal("10.00"))
    preview = preview_ok(tools, "ad_set_roas_floor", sim, ad_set_id=ADSET_ID, new_roas_floor="2.4")
    result = tools["apply_ad_set_roas_floor"](preview_token=preview.preview_token)
    with pytest.raises(Exception) as caught:
        tools["revert_change"](entry_id=result.entry_id)
    assert "daily spend total" in str(caught.value)
