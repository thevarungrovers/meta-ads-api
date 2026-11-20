"""A revert chain on a tightening re-reads the headroom on EVERY lifting step.

Odd steps of the chain (R1, R3, R5 ...) lift the restriction the original
added; even ones put it back. A lifting step used to project only what the
step it re-applied had stored -- so with the budget raised 10x outside the
server between R2 and R3, R3 and R5 still projected the old 232.36 while the
live headroom was about 24,980.
"""

from __future__ import annotations

from decimal import Decimal

from tests.kindkit import preview_ok, rig
from tests.test_kinds_targeting import sim as targeting_sim
from tests.test_mcp_write_path import ADSET_ID, CAMPAIGN_ID

BIG = Decimal("100000")
#: The fixture's spend: 123.45 over 7 days.
SPEND = Decimal("123.45") / 7


def _headroom(budget_minor: int) -> str:
    return f"{max(Decimal(budget_minor) / 100 - SPEND, Decimal('0')).quantize(Decimal('0.01'))}"


def _chain(monkeypatch, *, start_budget: str, budgets: dict[int, str]):
    """Narrow targeting, then revert five deep; ``budgets[n]`` is set on the
    campaign (outside the server) just before revert n."""
    s = targeting_sim()
    s.objects[CAMPAIGN_ID]["daily_budget"] = start_budget
    tools, _ = rig(monkeypatch, s, max_projected_session_delta=BIG,
                   max_projected_delta_per_day_total=BIG, max_daily_budget=BIG)
    preview = preview_ok(tools, "ad_set_targeting", s, ad_set_id=ADSET_ID, age_min=30)
    entry = tools["apply_ad_set_targeting"](preview_token=preview.preview_token).entry_id
    out = []
    for n in range(1, 6):
        if n in budgets:
            s.objects[CAMPAIGN_ID]["daily_budget"] = budgets[n]
        result = tools["revert_change"](entry_id=entry)
        out.append(result.projected_daily_spend_delta)
        entry = result.entry_id
    return out


def test_a_five_deep_chain_reads_the_live_headroom_on_each_lift(monkeypatch):
    out = _chain(monkeypatch, start_budget="25000", budgets={3: "2500000"})
    assert out == [_headroom(25000), "0.00", _headroom(2500000), "0.00", _headroom(2500000)]


def test_headroom_falling_between_lifts_never_projects_below_what_was_stored(monkeypatch):
    out = _chain(monkeypatch, start_budget="2500000", budgets={3: "25000", 5: "1000000"})
    # R3 keeps R1's larger stored figure; R5 sees the new budget.
    assert out[0] == _headroom(2500000) and out[1] == "0.00"
    assert out[2] == _headroom(2500000)
    assert out[3] == "0.00"
    assert out[4] == _headroom(2500000)


def test_zero_headroom_at_the_first_lift_does_not_stick(monkeypatch):
    # 10.00/day against 17.64/day spent: nothing to lift at R1.
    out = _chain(monkeypatch, start_budget="1000", budgets={3: "2500000", 5: "5000000"})
    assert out == ["0.00", "0.00", _headroom(2500000), "0.00", _headroom(5000000)]


def test_no_step_of_a_tightening_chain_is_classed_as_only_lowering_spend(monkeypatch):
    """Every step costs an apply against the caps, however deep."""
    from metaads_mcp.changes.engine import Engine
    from metaads_mcp import ledger

    s = targeting_sim()
    tools, state = rig(monkeypatch, s, max_projected_session_delta=BIG,
                       max_projected_delta_per_day_total=BIG)
    preview = preview_ok(tools, "ad_set_targeting", s, ad_set_id=ADSET_ID, age_min=30)
    entry = tools["apply_ad_set_targeting"](preview_token=preview.preview_token).entry_id
    for _ in range(4):
        entry = tools["revert_change"](entry_id=entry).entry_id
        assert Engine._tightening(ledger.find_entry(entry)), entry
