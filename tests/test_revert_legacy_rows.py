"""Revert rows written by the double-conversion code.

Before the ledger recorded ``projected_entity``, a revert in a non-cap
currency read the cap-currency figure as the entity's and converted it again:
undoing a JPY 3000 -> 1000 cut was recorded as 0.20 CAD, not 20.00. That row
carries the wrong figure, and reverting it copied the figure on -- into the
new row's ``undoes_projected_entity`` -- so the NEXT re-apply was counted at a
hundredth of its cost. A legacy revert row's projection is now recomputed from
its before/after values, or the revert is refused where it cannot be.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from metaads_mcp import ledger
from metaads_reporting import logdb
from tests.test_multi_account_currency import JPY, JPY_ADSET, two_accounts


def _row(entry_id, *, tool, field, before, after, projected, inverse, reverts=None):
    logdb.insert_change(
        entry_id=entry_id, tool=tool, account_id=JPY, entity_type="ad set",
        entity_id=JPY_ADSET, entity_name="Example ad set", field_name=field,
        before=before, after=after, projected_delta=projected, inverse=inverse,
        reverts=reverts,
    )
    logdb.complete_change(entry_id=entry_id, ok=True)


def _legacy_budget_chain(sim):
    """A legacy cut, and the legacy revert that restored it (0.20: converted twice)."""
    sim.objects[JPY_ADSET]["daily_budget"] = "3000"
    _row("legacycut", tool="apply_ad_set_daily_budget", field="daily_budget",
         before="3000 JPY", after="1000 JPY", projected="-20.00",
         inverse={"kind": "ad_set_daily_budget", "weight": 1, "before_amount": "3000"})
    _row("legacyundo", tool="revert_change", field="daily_budget",
         before="1000 JPY", after="3000 JPY", projected="0.20", reverts="legacycut",
         inverse={"kind": "ad_set_daily_budget", "weight": 1, "revert_row": True,
                  "undoes_projected": "-20.00", "before_amount": "1000"})


def test_reverting_a_legacy_revert_row_does_not_carry_its_wrong_figure_on(monkeypatch):
    tools, sim = two_accounts(monkeypatch)
    _legacy_budget_chain(sim)
    recut = tools["revert_change"](entry_id="legacyundo")
    assert str(sim.objects[JPY_ADSET]["daily_budget"]) == "1000"
    inverse = ledger.find_entry(recut.entry_id)["inverse"]
    # Recomputed from the legacy row's 1000 -> 3000: it restored 2000 JPY.
    assert Decimal(inverse["undoes_projected_entity"]) == Decimal("2000"), inverse
    assert inverse["undoes_projected"] == "20.00", inverse
    restore = tools["revert_change"](entry_id=recut.entry_id)
    assert str(sim.objects[JPY_ADSET]["daily_budget"]) == "3000"
    assert restore.projected_daily_spend_delta == "20.00", "not the 0.20 the legacy row held"


def test_a_legacy_revert_of_a_legacy_revert_is_recomputed_through_its_parent(monkeypatch):
    tools, sim = two_accounts(monkeypatch)
    _legacy_budget_chain(sim)
    sim.objects[JPY_ADSET]["daily_budget"] = "1000"
    # The old code's revert of "legacyundo": undoes_projected copied its 0.20.
    _row("legacyrecut", tool="revert_change", field="daily_budget",
         before="3000 JPY", after="1000 JPY", projected="0.00", reverts="legacyundo",
         inverse={"kind": "ad_set_daily_budget", "weight": 1, "revert_row": True,
                  "undoes_projected": "0.20", "before_amount": "3000"})
    restore = tools["revert_change"](entry_id="legacyrecut")
    assert str(sim.objects[JPY_ADSET]["daily_budget"]) == "3000"
    assert restore.projected_daily_spend_delta == "20.00"


def test_a_legacy_revert_row_that_cannot_be_recomputed_is_refused_where_it_matters(monkeypatch):
    """A bid's projection depends on clicks, not on before/after: the undo
    (which lowers the bid again) goes through, but re-applying its change
    later, at an unknown cost, is refused with a reason."""
    tools, sim = two_accounts(monkeypatch)
    sim.objects[JPY_ADSET]["bid_amount"] = "120"
    _row("legacybid", tool="apply_ad_set_bid_amount", field="bid_amount",
         before="120 JPY", after="100 JPY", projected="-0.50",
         inverse={"kind": "ad_set_bid_amount", "weight": 1, "before_amount": "120"})
    _row("legacybidundo", tool="revert_change", field="bid_amount",
         before="100 JPY", after="120 JPY", projected="0.01", reverts="legacybid",
         inverse={"kind": "ad_set_bid_amount", "weight": 1, "revert_row": True,
                  "undoes_projected": "-0.50", "before_amount": "100"})
    recut = tools["revert_change"](entry_id="legacybidundo")
    assert str(sim.objects[JPY_ADSET]["bid_amount"]) == "100"
    applied = len(sim.applied)
    with pytest.raises(Exception) as caught:
        tools["revert_change"](entry_id=recut.entry_id)
    assert "converted twice" in str(caught.value), caught.value
    assert len(sim.applied) == applied, "nothing was sent"


def test_a_cap_currency_legacy_revert_row_is_left_alone(monkeypatch):
    """No conversion ever happened in the cap currency: nothing to recompute."""
    from tests.harness import build_tools
    from tests.test_mcp_write_path import CAMPAIGN_ID, make_sim

    sim = make_sim()
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    tools = registrar.tools
    preview = tools["preview_campaign_daily_budget"](campaign_id=CAMPAIGN_ID,
                                                     new_daily_budget="200.00")
    entry = tools["apply_campaign_daily_budget"](preview_token=preview.preview_token).entry_id
    first = tools["revert_change"](entry_id=entry)
    second = tools["revert_change"](entry_id=first.entry_id)
    third = tools["revert_change"](entry_id=second.entry_id)
    assert third.projected_daily_spend_delta == "50.00"
