"""The ChangeKind engine: what the port onto one sequence fixed and pins.

The hand-written pairs had drifted. The creative path skipped the staleness
check, the audience path ignored result.success and recorded "(new)" as the
entity, and a creative change could not be reverted at all. Each of those is a
test here, on the simulator, so it cannot quietly come back.
"""

from __future__ import annotations

import json

import pytest

from metaads_mcp.changes import all_kinds
from tests.fakes import FakeResponse
from tests.test_mcp_creative_path import AD_ID, CREATIVE_ID, make_sim
from tests.harness import build_tools


@pytest.fixture
def tools(monkeypatch):
    sim = make_sim()
    registrar, state = build_tools(sim, monkeypatch=monkeypatch)
    return registrar.tools, state, sim


def test_every_kind_has_a_unique_name_and_titles():
    names = [k.name for k in all_kinds()]
    assert len(names) == len(set(names))
    for kind in all_kinds():
        assert kind.preview_title and kind.apply_title, kind.name


def test_a_preview_sends_validate_only_and_nothing_else(tools):
    tool, state, sim = tools
    preview = tool["preview_campaign_status"](
        campaign_id="120200000000001", new_status="PAUSED"
    )
    assert sim.validated, "the preview never asked Meta"
    assert sim.applied == [] and sim.created == []
    assert any("meta_validate_only" in c and c.startswith("OK") for c in preview.checks)


def test_meta_refusing_the_validation_blocks_the_preview(tools):
    tool, state, sim = tools
    original = sim.request

    def router(method, url, params=None, data=None, files=None, timeout=None):
        if method == "POST":
            return FakeResponse(
                {"error": {"message": "Invalid parameter", "code": 100}}, status=400
            )
        return original(method, url, params, data, files, timeout)

    sim.request = router
    preview = tool["preview_campaign_daily_budget"](
        campaign_id="120200000000001", new_daily_budget="260.00"
    )
    assert preview.blocked is True
    assert any(c.startswith("FAIL meta_validate_only") for c in preview.checks)


def test_a_chain_is_validated_one_step_and_says_so(tools):
    tool, state, sim = tools
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="new")
    line = next(c for c in preview.checks if "meta_validate_only" in c)
    assert "step 1 of 2" in line
    assert [v.get("edge") for v in sim.validated] == ["adcreatives"]


def test_a_creative_apply_is_refused_when_the_source_copy_moved(tools):
    """The hand-written creative path had no staleness check at all."""
    tool, state, sim = tools
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="new headline")
    sim.objects[CREATIVE_ID]["object_story_spec"]["link_data"]["name"] = "edited in Ads Manager"
    with pytest.raises(Exception) as caught:
        tool["apply_duplicate_ad"](preview_token=preview.preview_token)
    assert "has changed since the preview" in str(caught.value)
    assert sim.created == []


def test_an_audience_apply_without_an_id_is_not_recorded_as_applied(tools):
    """apply_create_audience used to ignore result.success."""
    tool, state, sim = tools
    preview = tool["preview_create_audience"](
        kind="saved", name="Example 25+", targeting={"age_min": 25}
    )
    original = sim.request

    def router(method, url, params=None, data=None, files=None, timeout=None):
        if method == "POST":
            return FakeResponse({"not_an_id": True})
        return original(method, url, params, data, files, timeout)

    sim.request = router
    with pytest.raises(Exception) as caught:
        tool["apply_create_audience"](preview_token=preview.preview_token)
    assert "did not confirm" in str(caught.value)
    assert tool["list_my_changes"]()[-1]["applied"] is False


def test_a_created_audience_is_recorded_under_its_new_id(tools):
    tool, state, sim = tools
    preview = tool["preview_create_audience"](
        kind="saved", name="Example 25+", targeting={"age_min": 25}
    )
    result = tool["apply_create_audience"](preview_token=preview.preview_token)
    row = tool["list_my_changes"]()[-1]
    assert row["entity_id"] == result.entity_id == "SAVED1"


def test_an_audience_cannot_be_reverted_and_says_why(tools):
    tool, state, sim = tools
    preview = tool["preview_create_audience"](
        kind="saved", name="Example 25+", targeting={"age_min": 25}
    )
    result = tool["apply_create_audience"](preview_token=preview.preview_token)
    with pytest.raises(Exception) as caught:
        tool["revert_change"](entry_id=result.entry_id)
    assert "never deleted" in str(caught.value)


def test_reverting_a_duplicate_pauses_the_created_ad(tools):
    tool, state, sim = tools
    preview = tool["preview_duplicate_ad"](ad_id=AD_ID, headline="new")
    result = tool["apply_duplicate_ad"](preview_token=preview.preview_token)
    new_ad = result.entity_id
    sim.objects[new_ad]["status"] = "ACTIVE"  # somebody activated it
    tool["revert_change"](entry_id=result.entry_id)
    assert sim.objects[new_ad]["status"] == "PAUSED"
    assert not any(c["method"] == "DELETE" for c in sim.calls)


def test_reverting_a_copy_change_points_the_ad_back_at_its_old_creative(tools):
    tool, state, sim = tools
    preview = tool["preview_update_ad_copy"](ad_id=AD_ID, headline="new")
    result = tool["apply_update_ad_copy"](preview_token=preview.preview_token)
    assert "CREATIVE1" in str(sim.objects[AD_ID]["creative"])

    tool["revert_change"](entry_id=result.entry_id)
    assert json.loads(sim.objects[AD_ID]["creative"]) == {"creative_id": CREATIVE_ID}


def test_a_copy_revert_is_refused_when_the_ad_was_repointed_since(tools):
    tool, state, sim = tools
    preview = tool["preview_update_ad_copy"](ad_id=AD_ID, headline="new")
    result = tool["apply_update_ad_copy"](preview_token=preview.preview_token)
    sim.objects[AD_ID]["creative"] = {"id": "SOMEONE_ELSES"}
    with pytest.raises(Exception) as caught:
        tool["revert_change"](entry_id=result.entry_id)
    assert "would overwrite that" in str(caught.value)


def test_the_inverse_is_machine_readable_in_the_ledger(tools):
    from metaads_mcp import ledger

    tool, state, sim = tools
    preview = tool["preview_campaign_daily_budget"](
        campaign_id="120200000000001", new_daily_budget="260.00"
    )
    result = tool["apply_campaign_daily_budget"](preview_token=preview.preview_token)
    entry = ledger.find_entry(result.entry_id)
    assert entry["inverse"]["kind"] == "campaign_daily_budget"
    assert entry["inverse"]["before_amount"] == "250.00"


@pytest.mark.parametrize(
    "tool_name, args, node",
    [
        ("preview_campaign_status", {"campaign_id": "120200000000001", "new_status": "PAUSED"},
         "120200000000001"),
        ("preview_ad_set_status", {"ad_set_id": "120200000000002", "new_status": "PAUSED"},
         "120200000000002"),
        ("preview_ad_status", {"ad_id": AD_ID, "new_status": "PAUSED"}, AD_ID),
        ("preview_duplicate_ad", {"ad_id": AD_ID, "headline": "x"}, AD_ID),
    ],
)
def test_an_entity_in_another_account_is_out_of_scope(monkeypatch, tool_name, args, node):
    """The check used to compare the default account with itself."""
    sim = make_sim()
    sim.objects[node]["account_id"] = "9876543210"
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    preview = registrar.tools[tool_name](**args)
    assert preview.blocked is True
    assert any(c.startswith("FAIL account_in_scope") and "9876543210" in c for c in preview.checks)
