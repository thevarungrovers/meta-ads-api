"""Copies and duplicates the agent makes carry ITS prefix on the source's base;
a creative-replacing edit names the new creative the same way and leaves the
ad's name alone. Placeholders only: AB / max, and CD / bot for another agent."""

from __future__ import annotations

import json

import pytest

from metaads_mcp import ledger
from metaads_reporting.naming import Identity
from tests.kindkit import as_identity, failed, preview_ok, rig

COMBOS = [
    pytest.param(Identity("AB", "max"), "AB-max-", id="both"),
    pytest.param(Identity("AB", ""), "AB-", id="owner-only"),
    pytest.param(Identity("", "max"), "max-", id="agent-only"),
    pytest.param(Identity(), "", id="neither"),
]


def _creative_sim():
    from tests.test_mcp_creative_path import make_sim

    return make_sim()


def _created(sim, edge):
    return [c["payload"] for c in sim.created if c["edge"] == edge]


# --- duplicate_ad ---------------------------------------------------------------------


@pytest.mark.parametrize("identity, prefix", COMBOS)
def test_duplicate_ad_without_a_name_restamps_the_source(monkeypatch, identity, prefix):
    from tests.test_mcp_creative_path import AD_ID

    s = _creative_sim()
    s.objects[AD_ID]["name"] = "CD-bot-launch"
    tools, state = rig(monkeypatch, s)
    as_identity(state, identity)
    preview = preview_ok(tools, "duplicate_ad", s, ad_id=AD_ID, headline="new headline")
    tools["apply_duplicate_ad"](preview_token=preview.preview_token)
    ad = _created(s, "ads")[0]["name"]
    creative = _created(s, "adcreatives")[0]["name"]
    # Another agent's prefix is stripped whatever the identity: with none
    # configured the copy is plain, never "CD-bot-launch (copy)".
    assert ad == f"{prefix}launch (copy)"
    assert creative == f"{ad} creative"


@pytest.mark.parametrize("identity, prefix", COMBOS)
def test_duplicate_ad_with_a_new_name_stamps_it(monkeypatch, identity, prefix):
    from tests.test_mcp_creative_path import AD_ID

    s = _creative_sim()
    tools, state = rig(monkeypatch, s)
    as_identity(state, identity)
    preview = preview_ok(tools, "duplicate_ad", s, ad_id=AD_ID, headline="h", new_name="spring v2")
    assert [b["final"] for b in preview.naming] == [f"{prefix}spring v2", f"{prefix}spring v2 creative"]
    tools["apply_duplicate_ad"](preview_token=preview.preview_token)
    assert _created(s, "ads")[0]["name"] == f"{prefix}spring v2"
    spec = _created(s, "adcreatives")[0]
    assert spec["name"] == f"{prefix}spring v2 creative"
    assert json.loads(spec["object_story_spec"])["link_data"]["name"] == "h", "the headline is the agent's text"


def test_a_derived_creative_name_over_the_limit_is_refused(monkeypatch):
    from tests.test_mcp_creative_path import AD_ID

    s = _creative_sim()
    tools, _ = rig(monkeypatch, s)
    preview = tools["preview_duplicate_ad"](ad_id=AD_ID, headline="h", new_name="x" * 390)
    assert failed(preview, "name_length"), preview.checks


# --- update_ad_copy and the creative repoints -------------------------------------------


@pytest.mark.parametrize("identity, prefix", COMBOS)
def test_update_ad_copy_names_the_new_creative_and_leaves_the_ad(monkeypatch, identity, prefix):
    from tests.test_mcp_creative_path import AD_ID

    s = _creative_sim()
    original = s.objects[AD_ID]["name"]
    tools, state = rig(monkeypatch, s)
    as_identity(state, identity)
    preview = preview_ok(tools, "update_ad_copy", s, ad_id=AD_ID, headline="new")
    tools["apply_update_ad_copy"](preview_token=preview.preview_token)
    assert _created(s, "adcreatives")[0]["name"] == f"{prefix}{original} creative (updated)"
    assert s.objects[AD_ID]["name"] == original, "a copy edit is not a rename"


def test_a_url_tags_repoint_names_its_creative_and_warns_about_name_macros(monkeypatch):
    from tests.test_mcp_creative_path import AD_ID

    s = _creative_sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "url_tags", s, ad_id=AD_ID,
                         url_tags="utm_source=meta&utm_content={{ad.name}}")
    assert any("{{ad.name}}" in w for w in preview.warnings), preview.warnings
    tools["apply_url_tags"](preview_token=preview.preview_token)
    assert _created(s, "adcreatives")[0]["name"] == f"AB-max-{s.objects[AD_ID]['name']} creative (url_tags)"


# --- /copies: one rename per created object -----------------------------------------------


def _copy_sim(child_name="Creative A"):
    from tests.test_mcp_write_path import AD_ID, make_sim

    s = make_sim()
    s.objects[AD_ID]["name"] = child_name
    s.copy_suffix = " - Copy"  # Meta's decoration, which the renames must beat
    return s


def test_a_deep_copied_ad_set_and_its_ads_are_renamed_with_our_prefix(monkeypatch):
    from tests.test_mcp_write_path import AD_ID, ADSET_ID

    s = _copy_sim(child_name="CD-bot-launch")
    tools, state = rig(monkeypatch, s)
    preview = preview_ok(tools, "duplicate_ad_set", s, ad_set_id=ADSET_ID)
    assert [b["final"] for b in preview.naming] == ["AB-max-Broad 25-54", "AB-max-launch"]
    result = tools["apply_duplicate_ad_set"](preview_token=preview.preview_token)
    assert s.objects[result.entity_id]["name"] == "AB-max-Broad 25-54"
    assert s.objects[f"{result.entity_id}_{AD_ID}"]["name"] == "AB-max-launch"
    assert all(f["ok"] for f in result.follow_ups) and len(result.follow_ups) == 2
    assert state.counters.applies == 3, "the renames are part of the create's cost"
    from metaads_mcp.guardrails import DailyUsage

    assert DailyUsage(state.limits).usage()[0] == 3


def test_a_copy_rename_is_revertible_on_its_own(monkeypatch):
    from tests.test_mcp_write_path import ADSET_ID

    s = _copy_sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "duplicate_ad_set", s, ad_set_id=ADSET_ID)
    result = tools["apply_duplicate_ad_set"](preview_token=preview.preview_token)
    rename = result.follow_ups[0]
    tools["revert_change"](entry_id=rename["entry_id"])
    assert s.objects[result.entity_id]["name"] == "Broad 25-54 - Copy", "restored verbatim"


def test_a_copy_with_more_objects_than_max_entities_per_call_is_refused(monkeypatch):
    from tests.test_mcp_write_path import CAMPAIGN_ID

    s = _copy_sim()
    tools, _ = rig(monkeypatch, s, max_entities_per_call=2)
    preview = tools["preview_duplicate_campaign"](campaign_id=CAMPAIGN_ID)
    assert failed(preview, "copy_renames"), preview.checks


def test_with_no_identity_metas_copy_naming_is_kept(monkeypatch):
    from tests.test_mcp_write_path import ADSET_ID

    s = _copy_sim()
    tools, state = rig(monkeypatch, s, max_entities_per_call=1)
    as_identity(state, Identity())
    preview = preview_ok(tools, "duplicate_ad_set", s, ad_set_id=ADSET_ID)
    assert "Meta's copy naming is kept" in preview.naming[0]["rule"]
    result = tools["apply_duplicate_ad_set"](preview_token=preview.preview_token)
    assert result.follow_ups is None
    assert s.objects[result.entity_id]["name"] == "Broad 25-54 - Copy"


@pytest.mark.parametrize("identity, prefix", COMBOS[1:3])
def test_a_partial_identity_names_the_copies_with_its_part(monkeypatch, identity, prefix):
    from tests.test_mcp_write_path import ADSET_ID

    s = _copy_sim()
    tools, state = rig(monkeypatch, s)
    as_identity(state, identity)
    preview = preview_ok(tools, "duplicate_ad_set", s, ad_set_id=ADSET_ID, deep_copy=False)
    result = tools["apply_duplicate_ad_set"](preview_token=preview.preview_token)
    assert s.objects[result.entity_id]["name"] == f"{prefix}Broad 25-54"


def test_a_rename_that_fails_is_reported_and_the_copy_stands(monkeypatch):
    from tests.test_mcp_write_path import ADSET_ID

    s = _copy_sim()
    tools, _ = rig(monkeypatch, s)
    preview = preview_ok(tools, "duplicate_ad_set", s, ad_set_id=ADSET_ID)
    original = s._post

    def post(segments, body):
        if body.get("name", "").startswith("AB-max-") and "execution_options" not in body \
                and len(segments) == 1 and "_" in segments[0]:
            from tests.fakes import FakeResponse

            return FakeResponse({"error": {"message": "rename refused", "code": 100}}, status=400)
        return original(segments, body)

    s._post = post
    result = tools["apply_duplicate_ad_set"](preview_token=preview.preview_token)
    failures = [f for f in result.follow_ups if not f["ok"]]
    assert len(failures) == 1 and failures[0]["error"]
    entry = ledger.find_entry(failures[0]["entry_id"])
    assert not entry["applied"] and entry["error"]
    assert s.objects[result.entity_id]["status"] == "PAUSED"


# --- an object Meta copied that the preview did not list -----------------------------


def _add_ad_after_preview(s, name):
    """A second ad lands in the ad set between the preview and the apply."""
    from tests.test_mcp_write_path import AD_ID, ADSET_ID

    s.objects["AD2"] = {**s.objects[AD_ID], "id": "AD2", "name": name}
    s.edges[ADSET_ID]["ads"] = [*s.edges[ADSET_ID]["ads"], "AD2"]


def test_an_unlisted_copy_whose_name_is_refused_is_skipped_and_reported(monkeypatch):
    from tests.test_mcp_write_path import ADSET_ID

    s = _copy_sim()
    tools, state = rig(monkeypatch, s)
    as_identity(state, Identity("AB", ""))
    preview = preview_ok(tools, "duplicate_ad_set", s, ad_set_id=ADSET_ID)
    # Under an owner-only prefix this base would read as agent "spring".
    _add_ad_after_preview(s, "spring-promo")
    result = tools["apply_duplicate_ad_set"](preview_token=preview.preview_token)
    copied = f"{result.entity_id}_AD2"
    assert s.objects[copied]["name"] == "spring-promo - Copy", "nothing refused is sent"
    report = next(f for f in result.follow_ups if f["entity_id"] == copied)
    assert report["ok"] is False and "would read as agent" in report["error"], report
    assert report["entry_id"] is None, "a refused name is reported, never sent or ledgered"


def test_an_unlisted_copy_counts_against_max_entities_per_call(monkeypatch):
    from tests.test_mcp_write_path import ADSET_ID

    s = _copy_sim()
    tools, _ = rig(monkeypatch, s, max_entities_per_call=2)
    preview = preview_ok(tools, "duplicate_ad_set", s, ad_set_id=ADSET_ID)  # 2 objects
    _add_ad_after_preview(s, "Creative B")  # now 3
    with pytest.raises(Exception) as caught:
        tools["apply_duplicate_ad_set"](preview_token=preview.preview_token)
    assert "max_entities_per_call" in str(caught.value)
    assert not [c for c in s.created if c["edge"] == "copies"], "nothing was copied"
