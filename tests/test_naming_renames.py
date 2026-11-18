"""Renames: only the agent's OWN entities (its prefix, or the ledger shows this
server made it), the prefix kept or restored, revert verbatim. AB / max only."""

from __future__ import annotations

import pytest

from metaads_reporting.naming import Identity
from tests.kindkit import as_identity, failed, preview_ok, rig
from tests.test_mcp_write_path import ADSET_ID, CAMPAIGN_ID, make_sim

COMBOS = [
    pytest.param(Identity("AB", "max"), "AB-max-", id="both"),
    pytest.param(Identity("AB", ""), "AB-", id="owner-only"),
    pytest.param(Identity("", "max"), "max-", id="agent-only"),
]


def _rename(tools, entity_id, value, entity_type="ad_set"):
    return tools["preview_low_risk_change"](entity_type=entity_type, entity_id=entity_id,
                                            field="name", value=value)


@pytest.mark.parametrize("identity, prefix", COMBOS)
def test_an_own_entity_is_renamed_keeping_the_prefix(monkeypatch, identity, prefix):
    s = make_sim()
    s.objects[ADSET_ID]["name"] = f"{prefix}old"
    tools, state = rig(monkeypatch, s)
    as_identity(state, identity)
    preview = _rename(tools, ADSET_ID, "new")
    assert not preview.blocked, preview.checks
    assert preview.after == f"{prefix}new" and preview.naming[0]["rule"] == "kept_prefix"
    tools["apply_low_risk_change"](preview_token=preview.preview_token)
    assert s.objects[ADSET_ID]["name"] == f"{prefix}new"


def test_with_no_identity_only_a_ledger_owned_entity_is_renamed(monkeypatch):
    s = make_sim()
    tools, state = rig(monkeypatch, s)
    as_identity(state, Identity())
    assert failed(_rename(tools, ADSET_ID, "new"), "naming")
    created = tools["apply_create_campaign"](preview_token=tools["preview_create_campaign"](
        name="mine", objective="OUTCOME_SALES", special_ad_categories=[],
        budget_level="adset").preview_token)
    preview = _rename(tools, created.entity_id, "mine v2", entity_type="campaign")
    assert not preview.blocked, preview.checks
    tools["apply_low_risk_change"](preview_token=preview.preview_token)
    assert s.objects[created.entity_id]["name"] == "mine v2"


def test_a_manual_entity_is_refused(monkeypatch):
    s = make_sim()
    tools, _ = rig(monkeypatch, s)
    preview = _rename(tools, CAMPAIGN_ID, "new", entity_type="campaign")
    assert failed(preview, "naming") and preview.blocked
    assert any("only its own" in c for c in preview.checks)


def test_another_agents_entity_is_refused(monkeypatch):
    s = make_sim()
    s.objects[ADSET_ID]["name"] = "CD-bot-launch"
    tools, _ = rig(monkeypatch, s)
    preview = _rename(tools, ADSET_ID, "new")
    assert failed(preview, "naming") and any("another agent" in c for c in preview.checks)


def test_the_same_owners_other_agent_is_refused_too(monkeypatch):
    s = make_sim()
    s.objects[ADSET_ID]["name"] = "AB-bot-launch"
    tools, _ = rig(monkeypatch, s)
    assert failed(_rename(tools, ADSET_ID, "new"), "naming"), "no owner override"


def test_a_rename_to_another_prefix_is_refused(monkeypatch):
    s = make_sim()
    s.objects[ADSET_ID]["name"] = "AB-max-launch"
    tools, _ = rig(monkeypatch, s)
    assert failed(_rename(tools, ADSET_ID, "CD-bot-launch"), "naming")


def test_the_prefix_cannot_be_removed(monkeypatch):
    s = make_sim()
    s.objects[ADSET_ID]["name"] = "AB-max-launch"
    tools, _ = rig(monkeypatch, s)
    preview = _rename(tools, ADSET_ID, "launch 2")
    assert preview.after == "AB-max-launch 2"


def test_a_ledger_owned_entity_gets_its_prefix_back_and_reverts_verbatim(monkeypatch):
    s = make_sim()
    tools, _ = rig(monkeypatch, s)
    created = tools["apply_create_campaign"](preview_token=tools["preview_create_campaign"](
        name="mine", objective="OUTCOME_SALES", special_ad_categories=[],
        budget_level="adset").preview_token)
    s.objects[created.entity_id]["name"] = "mine renamed in Ads Manager"
    preview = _rename(tools, created.entity_id, "mine v2", entity_type="campaign")
    assert preview.after == "AB-max-mine v2" and any("restoring the prefix" in w for w in preview.warnings)
    result = tools["apply_low_risk_change"](preview_token=preview.preview_token)
    tools["revert_change"](entry_id=result.entry_id)
    assert s.objects[created.entity_id]["name"] == "mine renamed in Ads Manager", \
        "a revert restores the old name exactly, unprefixed or not"


def test_a_copy_object_is_ledger_owned(monkeypatch):
    s = make_sim()
    tools, state = rig(monkeypatch, s)
    result = tools["apply_duplicate_ad_set"](
        preview_token=preview_ok(tools, "duplicate_ad_set", s, ad_set_id=ADSET_ID).preview_token)
    s.objects[result.entity_id]["name"] = "renamed by hand"
    preview = _rename(tools, result.entity_id, "copy v2")
    assert not preview.blocked and preview.after == "AB-max-copy v2", preview.checks


def test_the_limit_counts_the_prefix(monkeypatch):
    s = make_sim()
    s.objects[ADSET_ID]["name"] = "AB-max-launch"
    tools, _ = rig(monkeypatch, s)
    assert not _rename(tools, ADSET_ID, "x" * 393).blocked
    assert failed(_rename(tools, ADSET_ID, "x" * 394), "name_length")
