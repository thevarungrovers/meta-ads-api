"""A prefix spelt with look-alike characters is still a prefix.

The head of a name (its first two dash-separated segments) is folded to a
skeleton -- NFKC, dash-like characters to "-", Cyrillic and Greek letters to
the Latin ones they read as -- and a name whose skeleton starts with a full
OWNER-agent- prefix (or our own), while the original differs there, is
refused: in stamp, in rename and owns, in the engine's backstop and in the
write client. Placeholder identities only: AB / max and CD / bot.
"""

from __future__ import annotations

import pytest

from metaads_reporting.naming import (
    Identity,
    NameRefused,
    look_alike_prefix,
    rename,
    restamp_for_copy,
    stamp,
)

BOTH = Identity("AB", "max")
OWNER_ONLY = Identity("AB", "")
NEITHER = Identity()

CYRILLIC_C = "СD-bot-launch"              # Cyrillic Es for the C
NB_HYPHEN = "CD‑bot-launch"               # U+2011 non-breaking hyphen
FULLWIDTH = "ＣＤ－bot－launch"  # fullwidth C, D and hyphen-minus


@pytest.mark.parametrize("identity", [BOTH, OWNER_ONLY, NEITHER])
@pytest.mark.parametrize("name", [CYRILLIC_C, NB_HYPHEN, FULLWIDTH])
def test_a_disguised_foreign_prefix_is_refused(identity, name):
    with pytest.raises(NameRefused, match="look-alike"):
        stamp(identity, name, entity_type="campaign")


@pytest.mark.parametrize("name", ["AB-spring‑promo", "AB-AB‑max-x"])
def test_owner_only_refuses_a_base_that_reads_as_an_agent_through_look_alikes(name):
    with pytest.raises(NameRefused, match="look-alike"):
        stamp(OWNER_ONLY, name, entity_type="ad")


def test_a_disguised_prefix_after_our_own_is_refused():
    with pytest.raises(NameRefused, match="look-alike"):
        stamp(BOTH, "AB-max-СD-bot-launch", entity_type="ad")


def test_a_disguise_of_our_own_prefix_is_refused():
    with pytest.raises(NameRefused, match="look-alike"):
        stamp(BOTH, "АB-max-spring", entity_type="ad")  # Cyrillic A


@pytest.mark.parametrize("identity, prefix", [(BOTH, "AB-max-"), (OWNER_ONLY, "AB-"),
                                              (NEITHER, "")])
@pytest.mark.parametrize("name", ["Café-Paris-promo", "Summer – Sale", "Paris-café",
                                  "Été – soldes"])
def test_accented_names_and_typographic_dashes_are_accepted(identity, prefix, name):
    assert stamp(identity, name, entity_type="campaign").final == f"{prefix}{name}"
    assert look_alike_prefix(name) is None


def test_a_copy_of_a_disguised_name_is_refused():
    with pytest.raises(NameRefused, match="look-alike"):
        restamp_for_copy(BOTH, CYRILLIC_C, entity_type="ad")


def test_owns_does_not_claim_a_name_that_only_reads_as_ours():
    assert not BOTH.owns("АB-max-x")
    assert not OWNER_ONLY.owns("AB-max‑x"), "it reads as agent max's prefix"
    assert BOTH.owns("AB-max-x") and OWNER_ONLY.owns("AB-spring")


def test_rename_refuses_an_entity_named_with_a_disguised_foreign_prefix():
    with pytest.raises(NameRefused, match="look-alike"):
        rename(BOTH, CYRILLIC_C, "launch", entity_type="ad", ledger_owned=True)


def test_the_engine_backstop_refuses_a_disguised_name_sent(monkeypatch):
    from tests.kindkit import failed, rig
    from tests.test_mcp_write_path import make_sim
    from tests.test_naming_engine import Forgetful

    _, state = rig(monkeypatch, make_sim())
    preview = state.engine.preview(Forgetful(["AB-max-СD-bot-launch"]), {})
    assert failed(preview, "name_prefix"), preview.checks
    assert any("look-alike" in c for c in preview.checks if "name_prefix" in c)


@pytest.mark.parametrize("prefix", ["AB-max-", ""])
def test_the_write_client_refuses_a_disguised_name(client, prefix):  # noqa: F811
    from metaads_reporting.write.client import MutationError

    client.required_name_prefix = prefix
    sent = len(client._sim.writes)
    with pytest.raises(MutationError, match="look-alike"):
        client.create(entity_type="campaign", edge="campaigns",
                      payload={"name": f"{prefix}{CYRILLIC_C}", "status": "PAUSED"}, apply=True)
    assert len(client._sim.writes) == sent


from tests.test_write_plan import client  # noqa: E402,F401 - the fixture
