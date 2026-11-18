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
    tools, sim = two_accounts(monkeypatch, allowed_currencies=("CAD",), currency_rates=())
    sim.objects[JPY_ADSET]["status"] = "ACTIVE"
    preview = tools["preview_ad_set_status"](ad_set_id=JPY_ADSET, new_status="PAUSED")
    assert Decimal(preview.projected_daily_spend_delta) == 0
    assert failed(preview, "currency_allowed"), preview.checks
    # ... while the missing rate alone never strands it: a pause adds nothing.
    line = next(c for c in preview.checks if " cap_currency" in c)
    assert line.startswith("SKIP"), line


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
