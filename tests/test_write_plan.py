"""The plan builders and the write client's own guards."""

from __future__ import annotations

from decimal import Decimal

import pytest

from metaads_reporting.write import adsets, ads, campaigns
from metaads_reporting.write.client import (
    SETTABLE_STATUSES,
    WRITABLE_FIELDS,
    GuardrailViolation,
    MutatingMetaAdsClient,
    MutationError,
    check_status,
    check_writable,
)
from metaads_reporting.write.lookup import AmbiguousMatch, NotFound, match_by_name
from tests.fakes import account, ad, ad_set, campaign
from tests.graphsim import GraphSim

CAMPAIGN_ID = "120200000000001"
ADSET_ID = "120200000000002"
AD_ID = "120200000000003"


@pytest.fixture
def client(monkeypatch):
    sim = GraphSim(
        account=account(),
        objects={
            CAMPAIGN_ID: campaign(CAMPAIGN_ID, daily_budget="25000"),
            ADSET_ID: ad_set(ADSET_ID, daily_budget=None),
            AD_ID: ad(AD_ID),
        },
        edges={
            "act_1234567890": {
                "campaigns": [CAMPAIGN_ID],
                "adsets": [ADSET_ID],
                "ads": [AD_ID],
            }
        },
    )
    import metaads_reporting.graph as graph

    monkeypatch.setattr(graph.requests, "Session", lambda: sim)
    built = MutatingMetaAdsClient.from_env()
    built._sim = sim
    return built


# --- the field allowlist ----------------------------------------------------


def test_only_allowlisted_fields_are_writable():
    """Meta's update endpoint takes the ENTIRE node -- targeting, billing event,
    special ad categories, the lot. Which of those this code may change is a
    question worth answering explicitly."""
    check_writable("campaign", {"status": "PAUSED"})
    with pytest.raises(MutationError) as caught:
        check_writable("campaign", {"special_ad_categories": "[]"})
    assert "not in the allowlist" in str(caught.value)


def test_targeting_is_not_writable_anywhere():
    for entity in WRITABLE_FIELDS:
        with pytest.raises(MutationError):
            check_writable(entity, {"targeting": "{}"})


def test_an_unknown_entity_type_is_refused():
    with pytest.raises(MutationError):
        check_writable("adaccount", {"spend_cap": 1})


def test_an_ad_exposes_status_name_and_creative_and_nothing_else():
    """`creative` repoints an existing ad at a different creative -- the only
    way to change what a live ad says, because a creative is immutable once
    attached. The copy itself is still not editable in place: you build a new
    creative (see write/creatives.py) and point at it."""
    assert WRITABLE_FIELDS["ad"] == frozenset({"status", "name", "creative"})


def test_an_ad_still_cannot_have_its_targeting_or_parent_changed():
    for forbidden in ("targeting", "adset_id", "campaign_id", "bid_amount"):
        with pytest.raises(MutationError):
            check_writable("ad", {forbidden: "x"})


# --- the status guard -------------------------------------------------------


def test_only_active_and_paused_are_settable():
    assert SETTABLE_STATUSES == {"ACTIVE", "PAUSED"}
    assert check_status("active") == "ACTIVE"
    assert check_status(" paused ") == "PAUSED"


@pytest.mark.parametrize("status", ["DELETED", "ARCHIVED"])
def test_the_irreversible_statuses_are_refused_below_the_tool_layer(status):
    """Which is why the check lives here: the refusal holds for every caller,
    including scripts/manage.py."""
    with pytest.raises(MutationError) as caught:
        check_status(status)
    assert "PAUSED" in str(caught.value)


def test_nonsense_is_refused():
    with pytest.raises(MutationError):
        check_status("ENABLED")  # that is Google's word


# --- budget level -----------------------------------------------------------


def test_budget_level_reads_which_level_holds_the_budget():
    assert campaigns.budget_level({"daily_budget": "5000"}) == "campaign"
    assert campaigns.budget_level({"lifetime_budget": "100000"}) == "campaign"
    assert campaigns.budget_level({"id": "1"}) == "adset"


def test_budget_field_names_which_one_is_in_use():
    assert campaigns.budget_field({"daily_budget": "1"}) == "daily_budget"
    assert campaigns.budget_field({"lifetime_budget": "1"}) == "lifetime_budget"
    assert campaigns.budget_field({}) is None


def test_a_lifetime_budget_refuses_a_daily_change(client):
    """Switching between them also requires changing the schedule."""
    client._sim.objects[CAMPAIGN_ID].pop("daily_budget")
    client._sim.objects[CAMPAIGN_ID]["lifetime_budget"] = "100000"
    with pytest.raises(MutationError) as caught:
        campaigns.plan_set_daily_budget(client, campaign_id=CAMPAIGN_ID, amount="300")
    assert "LIFETIME budget" in str(caught.value)


# --- plans ------------------------------------------------------------------


def test_a_status_plan_carries_the_payload_and_the_diff(client):
    plan = campaigns.plan_set_status(client, campaign_id=CAMPAIGN_ID, status="PAUSED")
    assert plan.payload == {"status": "PAUSED"}
    assert plan.node_id == CAMPAIGN_ID
    assert plan.changes[0].before == "ACTIVE"
    assert plan.changes[0].after == "PAUSED"
    assert "ACTIVE" in plan.render() and "PAUSED" in plan.render()


def test_a_no_op_is_refused_rather_than_sent(client):
    with pytest.raises(MutationError) as caught:
        campaigns.plan_set_status(client, campaign_id=CAMPAIGN_ID, status="ACTIVE")
    assert "already ACTIVE" in str(caught.value)


def test_a_budget_plan_converts_to_minor_units_in_the_payload(client):
    plan = campaigns.plan_set_daily_budget(client, campaign_id=CAMPAIGN_ID, amount="300")
    assert plan.payload == {"daily_budget": 30000}
    assert plan.changes[0].before == "250.00 CAD"
    assert plan.changes[0].after == "300.00 CAD"


def test_a_campaign_budget_plan_says_it_moves_every_ad_set(client):
    plan = campaigns.plan_set_daily_budget(client, campaign_id=CAMPAIGN_ID, amount="300")
    assert any("EVERY ad set" in w for w in plan.warnings)


def test_a_big_budget_jump_warns_about_the_learning_phase(client):
    client._sim.objects[ADSET_ID]["daily_budget"] = "5000"
    plan = adsets.plan_set_daily_budget(client, ad_set_id=ADSET_ID, amount="200")
    assert any("learning phase" in w for w in plan.warnings)


def test_a_budget_cut_warns_about_stopping_delivery_today(client):
    client._sim.objects[ADSET_ID]["daily_budget"] = "5000"
    plan = adsets.plan_set_daily_budget(client, ad_set_id=ADSET_ID, amount="10")
    assert any("part-way through today" in w for w in plan.warnings)


def test_the_typo_guardrail_catches_minor_units(client):
    """50 entered as 5000 is a hundredfold error and indistinguishable from a
    real value once it reaches the API."""
    with pytest.raises(GuardrailViolation) as caught:
        campaigns.plan_set_daily_budget(
            client, campaign_id=CAMPAIGN_ID, amount="500000"
        )
    assert "NOT minor units" in str(caught.value)


def test_the_typo_guardrail_can_be_overridden_deliberately(client):
    plan = campaigns.plan_set_daily_budget(
        client, campaign_id=CAMPAIGN_ID, amount="50000", override_guardrail=True
    )
    assert plan.payload["daily_budget"] == 5000000


def test_a_bid_plan_is_refused_under_a_strategy_that_ignores_it(client):
    with pytest.raises(MutationError) as caught:
        adsets.plan_set_bid_amount(client, ad_set_id=ADSET_ID, amount="3.50")
    assert "does not use bid_amount" in str(caught.value)


def test_a_bid_plan_says_whether_the_amount_is_a_cap_or_a_target(client):
    client._sim.objects[ADSET_ID].update(
        bid_strategy="LOWEST_COST_WITH_BID_CAP", bid_amount="300"
    )
    plan = adsets.plan_set_bid_amount(client, ad_set_id=ADSET_ID, amount="4.00")
    assert any("CAP" in w for w in plan.warnings)

    client._sim.objects[ADSET_ID]["bid_strategy"] = "COST_CAP"
    plan = adsets.plan_set_bid_amount(client, ad_set_id=ADSET_ID, amount="5.00")
    assert any("CAP" in w for w in plan.warnings)


def test_an_ad_set_under_a_campaign_budget_refuses_its_own(client):
    with pytest.raises(MutationError) as caught:
        adsets.plan_set_daily_budget(client, ad_set_id=ADSET_ID, amount="50")
    assert "CAMPAIGN holds it" in str(caught.value)


def test_activating_an_ad_set_under_a_paused_campaign_warns_it_changes_nothing(client):
    client._sim.objects[ADSET_ID]["status"] = "PAUSED"
    client._sim.objects[ADSET_ID]["effective_status"] = "CAMPAIGN_PAUSED"
    plan = adsets.plan_set_status(client, ad_set_id=ADSET_ID, status="ACTIVE")
    assert any("parent CAMPAIGN is paused" in w for w in plan.warnings)


def test_a_disapproved_ad_warns_that_activating_does_not_appeal(client):
    client._sim.objects[AD_ID]["status"] = "PAUSED"
    client._sim.objects[AD_ID]["effective_status"] = "DISAPPROVED"
    plan = ads.plan_set_status(client, ad_id=AD_ID, status="ACTIVE")
    assert any("does not appeal" in w for w in plan.warnings)


# --- find and lookup --------------------------------------------------------


def test_find_requires_exactly_one_of_id_or_name(client):
    with pytest.raises(MutationError):
        campaigns.find(client)
    with pytest.raises(MutationError):
        campaigns.find(client, campaign_id="1", name="x")


def test_a_missing_object_says_both_possible_causes(client):
    with pytest.raises(NotFound) as caught:
        campaigns.find(client, campaign_id="404404404")
    assert "permission" in str(caught.value).lower()


def test_an_exact_name_match_beats_a_substring_one():
    """"Retargeting" is a substring of "Retargeting - Broad"."""
    rows = [
        {"id": "1", "name": "Retargeting"},
        {"id": "2", "name": "Retargeting - Broad"},
    ]
    found = match_by_name(
        rows, "Retargeting", entity="campaign", to_candidate=campaigns.as_candidate
    )
    assert found["id"] == "1"


def test_name_matching_is_case_insensitive():
    rows = [{"id": "1", "name": "Retargeting"}]
    assert match_by_name(
        rows, "RETARGETING", entity="campaign", to_candidate=campaigns.as_candidate
    )["id"] == "1"


def test_ambiguity_is_refused_with_the_candidates_listed():
    """Picking one for the user is how the wrong campaign gets paused."""
    rows = [
        {"id": "1", "name": "Retargeting - EN", "status": "ACTIVE"},
        {"id": "2", "name": "Retargeting - FR", "status": "ACTIVE"},
    ]
    with pytest.raises(AmbiguousMatch) as caught:
        match_by_name(
            rows, "Retargeting", entity="campaign", to_candidate=campaigns.as_candidate
        )
    assert "1" in str(caught.value) and "2" in str(caught.value)


def test_duplicate_exact_names_are_still_ambiguous():
    """Meta does not require names to be unique, and a bulk duplicate in Ads
    Manager leaves several with exactly the same one."""
    rows = [
        {"id": "1", "name": "Brand", "status": "ACTIVE"},
        {"id": "2", "name": "Brand", "status": "PAUSED"},
    ]
    with pytest.raises(AmbiguousMatch):
        match_by_name(rows, "Brand", entity="campaign", to_candidate=campaigns.as_candidate)


def test_no_match_says_how_matching_works():
    with pytest.raises(NotFound) as caught:
        match_by_name([], "x", entity="campaign", to_candidate=campaigns.as_candidate)
    assert "substring" in str(caught.value)


def test_an_empty_search_is_refused():
    with pytest.raises(MutationError):
        match_by_name([], "  ", entity="campaign", to_candidate=campaigns.as_candidate)


# --- the client's write path ------------------------------------------------


def test_apply_false_sends_metas_own_validate_only(client):
    """Not a local guess: the full server-side check runs and nothing changes."""
    client.update(CAMPAIGN_ID, entity_type="campaign", payload={"status": "PAUSED"})
    assert client._sim.validated and not client._sim.applied
    assert client._sim.objects[CAMPAIGN_ID]["status"] == "ACTIVE"


def test_apply_true_actually_writes(client):
    client.update(
        CAMPAIGN_ID, entity_type="campaign", payload={"status": "PAUSED"}, apply=True
    )
    assert client._sim.applied
    assert client._sim.objects[CAMPAIGN_ID]["status"] == "PAUSED"


def test_an_empty_payload_is_refused(client):
    with pytest.raises(MutationError):
        client.update(CAMPAIGN_ID, entity_type="campaign", payload={})


def test_every_write_is_recorded_in_the_mutation_log(client):
    from metaads_reporting import logdb

    client.update(
        CAMPAIGN_ID, entity_type="campaign", payload={"status": "PAUSED"}, apply=True
    )
    rows = logdb.mutation_rows()
    assert len(rows) == 1
    assert rows[0]["payload"] == {"status": "PAUSED"}
    assert rows[0]["ok"] == 1


def test_a_validate_only_pass_is_logged_too(client):
    """It records what was SENT, including the passes that changed nothing."""
    from metaads_reporting import logdb

    client.update(CAMPAIGN_ID, entity_type="campaign", payload={"status": "PAUSED"})
    assert len(logdb.mutation_rows()) == 1
