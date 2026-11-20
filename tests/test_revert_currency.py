"""A revert is counted in the cap currency exactly ONCE.

The ledger's projected_daily_spend_delta is already in the cap currency. A
revert used to read it back, treat it as the entity's currency and convert it
again, so undoing a JPY 3000 -> 1000 cut (20.00 CAD at the test rate) was
counted as 0.20 CAD. Every revert projection is now in the ENTITY's currency
(recorded as such in the row's inverse) and converted once.
"""

from __future__ import annotations

import json
from decimal import Decimal

from metaads_mcp import ledger
from tests.harness import build_tools
from tests.test_kinds_targeting import BASE, estimator
from tests.test_multi_account_currency import JPY_ADSET, two_accounts
from tests.test_mcp_write_path import CAMPAIGN_ID, make_sim


def _cut_and_chain(tools, depth):
    preview = tools["preview_ad_set_daily_budget"](ad_set_id=JPY_ADSET, new_daily_budget="1000")
    assert not preview.blocked, preview.checks
    entry = tools["apply_ad_set_daily_budget"](preview_token=preview.preview_token).entry_id
    out = []
    for _ in range(depth):
        result = tools["revert_change"](entry_id=entry)
        out.append(result)
        entry = result.entry_id
    return out


def test_reverting_a_jpy_cut_counts_the_restored_spend_once(monkeypatch):
    tools, sim = two_accounts(monkeypatch)
    (undo,) = _cut_and_chain(tools, 1)
    assert str(sim.objects[JPY_ADSET]["daily_budget"]) == "3000"
    # 2000 JPY restored, at 0.01 CAD per JPY: 20.00 CAD -- not 0.20.
    assert undo.projected_daily_spend_delta == "20.00"
    assert ledger.find_entry(undo.entry_id)["projected_daily_spend_delta"] == "20.00"


def test_the_ledger_records_the_projection_in_the_entitys_currency(monkeypatch):
    tools, _ = two_accounts(monkeypatch)
    preview = tools["preview_ad_set_daily_budget"](ad_set_id=JPY_ADSET, new_daily_budget="1000")
    result = tools["apply_ad_set_daily_budget"](preview_token=preview.preview_token)
    entry = ledger.find_entry(result.entry_id)
    assert entry["projected_daily_spend_delta"] == "-20.00", "the totals' currency"
    assert Decimal(entry["inverse"]["projected_entity"]) == Decimal("-2000")


def test_a_jpy_revert_chain_alternates_between_the_cut_and_the_restore(monkeypatch):
    tools, sim = two_accounts(monkeypatch)
    r1, r2, r3, r4 = _cut_and_chain(tools, 4)
    # r1 restores 2000 JPY, r2 re-applies the cut, r3 restores again, r4 cuts.
    assert [r.projected_daily_spend_delta for r in (r1, r2, r3, r4)] == [
        "20.00", "0.00", "20.00", "0.00"]
    assert str(sim.objects[JPY_ADSET]["daily_budget"]) == "1000"


def test_a_jpy_tightening_revert_chain_is_counted_once(monkeypatch):
    tools, sim = two_accounts(monkeypatch)
    sim.objects[JPY_ADSET]["targeting"] = json.loads(json.dumps(BASE))
    sim.canned["delivery_estimate"] = estimator
    preview = tools["preview_ad_set_targeting"](ad_set_id=JPY_ADSET, age_min=30)
    assert not preview.blocked, preview.checks
    entry = tools["apply_ad_set_targeting"](preview_token=preview.preview_token).entry_id
    r1 = tools["revert_change"](entry_id=entry)
    r2 = tools["revert_change"](entry_id=r1.entry_id)
    r3 = tools["revert_change"](entry_id=r2.entry_id)
    lifted = Decimal(r1.projected_daily_spend_delta)
    # The ad set's 3000 JPY budget (less its spend) is around 30 CAD, never 0.30.
    assert Decimal("10") < lifted <= Decimal("30.00"), lifted
    assert r2.projected_daily_spend_delta == "0.00"
    assert Decimal(r3.projected_daily_spend_delta) == lifted, "lifting again, at the same rate"


def test_a_cad_revert_chain_is_unchanged(monkeypatch):
    s = make_sim()
    registrar, _ = build_tools(s, monkeypatch=monkeypatch)
    tools = registrar.tools
    preview = tools["preview_campaign_daily_budget"](campaign_id=CAMPAIGN_ID,
                                                     new_daily_budget="200.00")
    entry = tools["apply_campaign_daily_budget"](preview_token=preview.preview_token).entry_id
    out = []
    for _ in range(3):
        result = tools["revert_change"](entry_id=entry)
        out.append(result.projected_daily_spend_delta)
        entry = result.entry_id
    assert out == ["50.00", "0.00", "50.00"]


def test_a_legacy_row_without_the_entity_projection_is_converted_back(monkeypatch):
    """A row written before the entity projection was recorded holds only the
    cap-currency figure: it is converted back at the rate, then counted once."""
    from metaads_reporting import logdb
    from tests.test_multi_account_currency import JPY

    tools, sim = two_accounts(monkeypatch)
    sim.objects[JPY_ADSET]["daily_budget"] = "1000"
    logdb.insert_change(
        entry_id="legacyjpy", tool="apply_ad_set_daily_budget", account_id=JPY,
        entity_type="ad set", entity_id=JPY_ADSET, entity_name="Example ad set",
        field_name="daily_budget", before="3000 JPY", after="1000 JPY",
        projected_delta="-20.00",
        inverse={"kind": "ad_set_daily_budget", "weight": 1, "before_amount": "3000"},
    )
    logdb.complete_change(entry_id="legacyjpy", ok=True)
    undo = tools["revert_change"](entry_id="legacyjpy")
    assert undo.projected_daily_spend_delta == "20.00"


def test_a_jpy_status_revert_chain_is_counted_once(monkeypatch):
    tools, _ = two_accounts(monkeypatch)
    preview = tools["preview_ad_set_status"](ad_set_id=JPY_ADSET, new_status="ACTIVE")
    entry = tools["apply_ad_set_status"](preview_token=preview.preview_token).entry_id
    r1 = tools["revert_change"](entry_id=entry)   # pauses: nothing restored
    r2 = tools["revert_change"](entry_id=r1.entry_id)  # activates again: 3000 JPY
    assert r1.projected_daily_spend_delta == "0.00"
    assert r2.projected_daily_spend_delta == "30.00"
