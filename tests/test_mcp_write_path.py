"""preview -> apply, driven end to end against a simulated account.

No network, no credentials, no stdio. The simulator keeps an object store and
really applies a POST, so a second preview sees the first one's change -- which
is what makes the staleness and idempotence assertions here mean something.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from metaads_mcp.guardrails import DEFAULTS, Limits
from metaads_mcp.previews import PreviewExpired, PreviewStale
from tests.fakes import account, ad, ad_set, campaign, error_response, insights_row
from tests.graphsim import GraphSim
from tests.harness import build_tools

CAMPAIGN_ID = "120200000000001"
ADSET_ID = "120200000000002"
AD_ID = "120200000000003"


def make_sim(**overrides) -> GraphSim:
    objects = {
        CAMPAIGN_ID: campaign(CAMPAIGN_ID, daily_budget="25000"),
        ADSET_ID: ad_set(ADSET_ID, daily_budget=None, campaign_id=CAMPAIGN_ID),
        AD_ID: ad(AD_ID),
    }
    objects.update(overrides.pop("objects", {}))
    return GraphSim(
        account=account(**overrides.pop("account", {})),
        objects=objects,
        edges={
            "act_1234567890": {
                "campaigns": [CAMPAIGN_ID],
                "adsets": [ADSET_ID],
                "ads": [AD_ID],
            },
            CAMPAIGN_ID: {"adsets": [ADSET_ID]},
            ADSET_ID: {"ads": [AD_ID]},
        },
        insights=[insights_row()],
        **overrides,
    )


@pytest.fixture
def tools(monkeypatch):
    sim = make_sim()
    registrar, state = build_tools(sim, monkeypatch=monkeypatch)
    return registrar.tools, state, sim


# --- the happy path ---------------------------------------------------------


def test_preview_then_apply_changes_the_account(tools):
    tool, state, sim = tools

    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    assert preview.before == "ACTIVE"
    assert preview.after == "PAUSED"
    assert preview.blocked is False
    assert preview.preview_token

    # A preview must not have changed anything.
    assert sim.applied == []
    assert sim.objects[CAMPAIGN_ID]["status"] == "ACTIVE"

    result = tool["apply_campaign_status"](preview_token=preview.preview_token)
    assert result.applied is True
    assert sim.objects[CAMPAIGN_ID]["status"] == "PAUSED"
    assert len(sim.applied) == 1


def test_the_ledger_records_what_happened(tools):
    tool, state, sim = tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    result = tool["apply_campaign_status"](preview_token=preview.preview_token)

    rows = tool["list_my_changes"]()
    assert len(rows) == 1
    entry = rows[0]
    assert entry["entry_id"] == result.entry_id
    assert entry["applied"] is True
    assert entry["before"] == "ACTIVE" and entry["after"] == "PAUSED"
    assert entry["reverted"] is False


def test_a_budget_change_converts_minor_units_in_both_directions(tools):
    """The preview reads 25000 as 250.00 and the apply sends 300.00 as 30000."""
    tool, state, sim = tools

    preview = tool["preview_campaign_daily_budget"](
        campaign_id=CAMPAIGN_ID, new_daily_budget="300.00"
    )
    assert preview.before == "250.00 CAD"
    assert preview.after == "300.00 CAD"
    assert preview.projected_daily_spend_delta == "50.00"

    tool["apply_campaign_daily_budget"](preview_token=preview.preview_token)
    # Back to minor units on the way out.
    assert sim.objects[CAMPAIGN_ID]["daily_budget"] == "30000"


def test_a_preview_really_validates_against_meta_and_changes_nothing(tools):
    """apply=False is not a local guess: Meta runs the full server-side check."""
    tool, state, sim = tools
    tool["preview_campaign_daily_budget"](
        campaign_id=CAMPAIGN_ID, new_daily_budget="300.00"
    )
    # The preview itself does not send a write at all; the validate_only pass
    # happens on the plan path, which `manage.py` uses. What matters here is
    # that nothing was APPLIED.
    assert sim.applied == []
    assert sim.objects[CAMPAIGN_ID]["daily_budget"] == "25000"


# --- the token contract -----------------------------------------------------


def test_a_token_is_single_use(tools):
    tool, state, sim = tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    tool["apply_campaign_status"](preview_token=preview.preview_token)
    with pytest.raises(Exception) as caught:
        tool["apply_campaign_status"](preview_token=preview.preview_token)
    assert "already used" in str(caught.value)


def test_a_token_is_not_interchangeable_between_tools(tools):
    tool, state, sim = tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    with pytest.raises(Exception) as caught:
        tool["apply_ad_status"](preview_token=preview.preview_token)
    assert "not interchangeable" in str(caught.value)


def test_an_unknown_token_is_refused(tools):
    tool, state, sim = tools
    with pytest.raises(Exception):
        tool["apply_campaign_status"](preview_token="made-up")


def test_a_blocked_preview_cannot_be_applied(monkeypatch):
    """And the token is consumed anyway, so it cannot be retried until some
    unrelated thing changes and it slips through."""
    sim = make_sim()
    tight = Limits(**{**DEFAULTS, "max_daily_budget": Decimal("10.00")})
    registrar, state = build_tools(sim, limits=tight, monkeypatch=monkeypatch)
    tool = registrar.tools

    preview = tool["preview_campaign_daily_budget"](
        campaign_id=CAMPAIGN_ID, new_daily_budget="300.00"
    )
    assert preview.blocked is True
    assert any(c.startswith("FAIL") for c in preview.checks)

    with pytest.raises(Exception) as caught:
        tool["apply_campaign_daily_budget"](preview_token=preview.preview_token)
    assert "failed a guardrail check" in str(caught.value)
    assert sim.applied == []


# --- staleness --------------------------------------------------------------


def test_a_change_made_elsewhere_between_preview_and_apply_is_caught(tools):
    """Ads Manager is usually open in a browser tab while this is running."""
    tool, state, sim = tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")

    # Somebody pauses it in Ads Manager.
    sim.objects[CAMPAIGN_ID]["status"] = "PAUSED"

    with pytest.raises(Exception) as caught:
        tool["apply_campaign_status"](preview_token=preview.preview_token)
    assert "has changed since the preview" in str(caught.value)
    assert sim.applied == []


def test_a_budget_moved_elsewhere_is_caught_too(tools):
    tool, state, sim = tools
    preview = tool["preview_campaign_daily_budget"](
        campaign_id=CAMPAIGN_ID, new_daily_budget="300.00"
    )
    sim.objects[CAMPAIGN_ID]["daily_budget"] = "27500"
    with pytest.raises(Exception) as caught:
        tool["apply_campaign_daily_budget"](preview_token=preview.preview_token)
    assert "has changed since the preview" in str(caught.value)


# --- the kill switch --------------------------------------------------------


def test_the_kill_switch_stops_an_apply_taken_before_it_was_set(tools, monkeypatch, tmp_path):
    """The one moment it has to work: a preview is good for ten minutes, and
    somebody throws the switch from another terminal in between."""
    tool, state, sim = tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    assert preview.blocked is False  # the switch was off when it was minted

    switch = tmp_path / "WRITES_DISABLED"
    switch.write_text("ops paused writes")
    monkeypatch.setattr("metaads_mcp.guardrails.KILL_SWITCH", switch)

    with pytest.raises(Exception) as caught:
        tool["apply_campaign_status"](preview_token=preview.preview_token)
    assert "writes are disabled" in str(caught.value)
    assert sim.applied == []


def test_the_kill_switch_also_blocks_revert(tools, monkeypatch, tmp_path):
    """revert_change does not go through commit(), so its gates are separate --
    which is exactly how they go missing."""
    tool, state, sim = tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    result = tool["apply_campaign_status"](preview_token=preview.preview_token)

    switch = tmp_path / "WRITES_DISABLED"
    switch.write_text("ops paused writes")
    monkeypatch.setattr("metaads_mcp.guardrails.KILL_SWITCH", switch)

    with pytest.raises(Exception) as caught:
        tool["revert_change"](entry_id=result.entry_id)
    assert "writes are disabled" in str(caught.value)


# --- the session budget -----------------------------------------------------


def test_the_session_apply_cap_stops_a_loop(monkeypatch):
    """The cap is per SESSION and spans tools: a loop does not get a fresh
    budget by switching which entity it mangles."""
    sim = make_sim()
    tight = Limits(**{**DEFAULTS, "max_applies_per_session": 1})
    registrar, state = build_tools(sim, limits=tight, monkeypatch=monkeypatch)
    tool = registrar.tools

    first = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    tool["apply_campaign_status"](preview_token=first.preview_token)

    # A different tool, a different entity, and a projected delta of zero -- so
    # nothing but the session counter can refuse it.
    second = tool["preview_ad_status"](ad_id=AD_ID, new_status="PAUSED")
    assert second.blocked is False
    with pytest.raises(Exception) as caught:
        tool["apply_ad_status"](preview_token=second.preview_token)
    assert "session apply limit reached" in str(caught.value)


def test_a_preview_over_the_per_day_delta_ceiling_is_blocked_at_preview_time(
    monkeypatch,
):
    """Blocked at the preview, not at the apply, so the human sees WHY before
    being asked to approve anything."""
    sim = make_sim()
    sim.objects[CAMPAIGN_ID]["status"] = "PAUSED"
    tight = Limits(**{**DEFAULTS, "max_projected_delta_per_day": Decimal("10.00")})
    registrar, _ = build_tools(sim, limits=tight, monkeypatch=monkeypatch)

    preview = registrar.tools["preview_campaign_status"](
        campaign_id=CAMPAIGN_ID, new_status="ACTIVE"
    )
    assert preview.blocked is True
    failed = [c for c in preview.checks if c.startswith("FAIL")]
    assert any("projected_delta_per_day" in c for c in failed)


def test_revert_counts_against_the_apply_cap(monkeypatch):
    """find_entry reaches back over the WHOLE ledger, including earlier
    sessions, so without this a revert loop had no bound at all."""
    sim = make_sim()
    tight = Limits(**{**DEFAULTS, "max_applies_per_session": 1})
    registrar, state = build_tools(sim, limits=tight, monkeypatch=monkeypatch)
    tool = registrar.tools

    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    result = tool["apply_campaign_status"](preview_token=preview.preview_token)

    with pytest.raises(Exception) as caught:
        tool["revert_change"](entry_id=result.entry_id)
    assert "session apply limit reached" in str(caught.value)


# --- revert -----------------------------------------------------------------


def test_revert_puts_the_value_back_and_records_itself(tools):
    tool, state, sim = tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    result = tool["apply_campaign_status"](preview_token=preview.preview_token)
    assert sim.objects[CAMPAIGN_ID]["status"] == "PAUSED"

    undo = tool["revert_change"](entry_id=result.entry_id)
    assert sim.objects[CAMPAIGN_ID]["status"] == "ACTIVE"
    assert undo.before == "PAUSED" and undo.after == "ACTIVE"

    rows = tool["list_my_changes"]()
    assert len(rows) == 2, "the revert is itself a recorded change"
    assert rows[0]["reverted"] is True


def test_a_budget_revert_parses_the_amount_back_out_of_the_stored_string(tools):
    """The ledger stores "250.00 CAD", because a bare 250.00 does not say of what."""
    tool, state, sim = tools
    preview = tool["preview_campaign_daily_budget"](
        campaign_id=CAMPAIGN_ID, new_daily_budget="300.00"
    )
    result = tool["apply_campaign_daily_budget"](preview_token=preview.preview_token)
    assert sim.objects[CAMPAIGN_ID]["daily_budget"] == "30000"

    tool["revert_change"](entry_id=result.entry_id)
    assert sim.objects[CAMPAIGN_ID]["daily_budget"] == "25000"


def test_reverting_twice_is_refused(tools):
    tool, state, sim = tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    result = tool["apply_campaign_status"](preview_token=preview.preview_token)
    tool["revert_change"](entry_id=result.entry_id)
    with pytest.raises(Exception) as caught:
        tool["revert_change"](entry_id=result.entry_id)
    assert "already been reverted" in str(caught.value)


def test_revert_refuses_when_somebody_else_moved_the_value_since(tools):
    """Putting our old value back would discard THEIR change, while the ledger
    recorded it as a tidy undo of ours."""
    tool, state, sim = tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    result = tool["apply_campaign_status"](preview_token=preview.preview_token)

    sim.objects[CAMPAIGN_ID]["status"] = "ACTIVE"  # somebody in Ads Manager

    with pytest.raises(Exception) as caught:
        tool["revert_change"](entry_id=result.entry_id)
    assert "would overwrite that instead of undoing this" in str(caught.value)


def test_reverting_an_unknown_entry_is_refused(tools):
    tool, state, sim = tools
    with pytest.raises(Exception) as caught:
        tool["revert_change"](entry_id="deadbeef")
    assert "No ledger entry" in str(caught.value)


# --- failures -----------------------------------------------------------------


def test_a_failed_write_is_recorded_as_not_applied(tools):
    tool, state, sim = tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")

    # The node read inside apply succeeds; the POST fails.
    def fail_on_post(method, url, params=None, data=None, files=None, timeout=None):
        if method == "POST":
            return error_response(100, message="Invalid parameter")
        return GraphSim.request(sim, method, url, params, data, files, timeout)

    sim_request = sim.request
    calls = {"n": 0}

    def router(method, url, params=None, data=None, files=None, timeout=None):
        if method == "POST":
            calls["n"] += 1
            return error_response(100, message="Invalid parameter")
        return sim_request(method, url, params, data, files, timeout)

    sim.request = router
    with pytest.raises(Exception):
        tool["apply_campaign_status"](preview_token=preview.preview_token)
    sim.request = sim_request

    rows = tool["list_my_changes"]()
    assert rows[-1]["applied"] is False
    assert rows[-1]["error"]


def test_a_response_without_success_is_not_recorded_as_applied(tools, monkeypatch):
    """Meta answering the request but not confirming the change must not be
    written down as a change."""
    tool, state, sim = tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")

    from tests.fakes import FakeResponse

    original = sim.request

    def router(method, url, params=None, data=None, files=None, timeout=None):
        if method == "POST":
            return FakeResponse({"not_success": True})
        return original(method, url, params, data, files, timeout)

    sim.request = router
    with pytest.raises(Exception) as caught:
        tool["apply_campaign_status"](preview_token=preview.preview_token)
    assert "did not confirm the change" in str(caught.value)
    assert tool["list_my_changes"]()[-1]["applied"] is False


# --- Meta-specific refusals -------------------------------------------------


def test_an_ad_set_under_a_campaign_budget_refuses_a_budget_change(tools):
    """The ad set has no budget of its own; setting one would convert the
    campaign off campaign-budget optimization."""
    tool, state, sim = tools
    with pytest.raises(Exception) as caught:
        tool["preview_ad_set_daily_budget"](ad_set_id=ADSET_ID, new_daily_budget="50.00")
    assert "CAMPAIGN holds it" in str(caught.value)
    assert "preview_campaign_daily_budget" in str(caught.value)


def test_a_campaign_without_a_budget_refuses_one(monkeypatch):
    """The mirror image: on an ad-set-budget campaign the budget is one level
    down, and setting one here would redistribute spend across every ad set."""
    sim = make_sim()
    sim.objects[CAMPAIGN_ID].pop("daily_budget")
    sim.objects[ADSET_ID]["daily_budget"] = "5000"
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)

    with pytest.raises(Exception) as caught:
        registrar.tools["preview_campaign_daily_budget"](
            campaign_id=CAMPAIGN_ID, new_daily_budget="300.00"
        )
    assert "does not hold its own budget" in str(caught.value)


def test_a_bid_change_is_refused_under_a_strategy_that_ignores_it(tools):
    """Meta ACCEPTS the write and ignores it, so without this the ledger would
    record a change that never happened."""
    tool, state, sim = tools
    with pytest.raises(Exception) as caught:
        tool["preview_ad_set_bid_amount"](ad_set_id=ADSET_ID, new_bid_amount="3.50")
    message = str(caught.value)
    assert "LOWEST_COST_WITHOUT_CAP" in message
    assert "ACCEPT" in message and "ignore" in message


def test_a_bid_change_works_under_a_strategy_that_uses_it(monkeypatch):
    sim = make_sim()
    sim.objects[ADSET_ID].update(
        bid_strategy="LOWEST_COST_WITH_BID_CAP", bid_amount="300", daily_budget="5000"
    )
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    tool = registrar.tools

    preview = tool["preview_ad_set_bid_amount"](ad_set_id=ADSET_ID, new_bid_amount="4.00")
    assert preview.before == "3.00 CAD"
    assert preview.after == "4.00 CAD"
    assert any("CAP" in w for w in preview.warnings)

    tool["apply_ad_set_bid_amount"](preview_token=preview.preview_token)
    assert sim.objects[ADSET_ID]["bid_amount"] == "400"


@pytest.mark.parametrize("status", ["DELETED", "ARCHIVED"])
def test_irreversible_statuses_are_refused_at_the_tool(tools, status):
    tool, state, sim = tools
    with pytest.raises(Exception) as caught:
        tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status=status)
    assert "irreversible" in str(caught.value)
    assert "PAUSED" in str(caught.value)


def test_minor_units_passed_as_a_budget_are_caught(tools):
    """2500 where 25.00 was meant is a hundredfold error, and the number is
    rendered verbatim in the prompt a human approves."""
    tool, state, sim = tools
    with pytest.raises(Exception) as caught:
        tool["preview_campaign_daily_budget"](
            campaign_id=CAMPAIGN_ID, new_daily_budget="2500000"
        )
    assert "looks like minor units" in str(caught.value)


def test_a_missing_object_says_both_possible_causes(tools):
    tool, state, sim = tools
    with pytest.raises(Exception) as caught:
        tool["preview_campaign_status"](campaign_id="999999999", new_status="PAUSED")
    assert "permission" in str(caught.value).lower()


# --- the preview itself -----------------------------------------------------


def test_a_preview_carries_effective_status_not_just_status(monkeypatch):
    """An ACTIVE ad under a paused campaign is not delivering, and `status`
    alone makes it look like it is."""
    sim = make_sim()
    sim.objects[AD_ID]["effective_status"] = "CAMPAIGN_PAUSED"
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)

    preview = registrar.tools["preview_ad_status"](ad_id=AD_ID, new_status="ACTIVE")
    assert preview.status == "ACTIVE"
    assert preview.effective_status == "CAMPAIGN_PAUSED"
    assert any("parent is paused" in w for w in preview.warnings)


def test_a_disapproved_ad_warns_that_activating_does_nothing(monkeypatch):
    sim = make_sim()
    sim.objects[AD_ID]["effective_status"] = "DISAPPROVED"
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    preview = registrar.tools["preview_ad_status"](ad_id=AD_ID, new_status="ACTIVE")
    assert any("DISAPPROVED" in w for w in preview.warnings)


def test_pausing_projects_a_zero_delta(tools):
    """Pausing cannot increase spend."""
    tool, state, sim = tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    assert preview.projected_daily_spend_delta == "0.00"


def test_activating_is_bounded_by_the_budget_not_past_spend(monkeypatch):
    """A campaign that spent nothing last week can still spend its full budget
    tomorrow."""
    sim = make_sim()
    sim.objects[CAMPAIGN_ID]["status"] = "PAUSED"
    sim.insights = []
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    preview = registrar.tools["preview_campaign_status"](
        campaign_id=CAMPAIGN_ID, new_status="ACTIVE"
    )
    assert preview.projected_daily_spend_delta == "250.00"


def test_a_preview_includes_the_last_seven_days(tools):
    tool, state, sim = tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    assert preview.metrics_7d["spend"] == "123.45"
    assert preview.metrics_7d["frequency"] == "1.25"


def test_a_failed_metrics_lookup_does_not_sink_the_preview(tools):
    """Context, not correctness -- but the failure is named, so nobody reads a
    missing section as 'it spent nothing'."""
    tool, state, sim = tools
    original = sim.request

    def router(method, url, params=None, data=None, files=None, timeout=None):
        if "insights" in url:
            return error_response(1, status=500, message="boom")
        return original(method, url, params, data, files, timeout)

    sim.request = router
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    assert "unavailable" in preview.metrics_7d


def test_every_preview_names_the_currency(tools):
    """A ceiling of 500 means nothing until you know 500 of what."""
    tool, state, sim = tools
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    assert preview.currency == "CAD"
