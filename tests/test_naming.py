"""The naming convention: parse, stamp, copy, rename, and where the identity
comes from. Placeholder identities only: AB / max, and CD / bot for a second."""

from __future__ import annotations

import pytest

from metaads_reporting import naming
from metaads_reporting.naming import Identity, NameRefused, parse, rename, restamp_for_copy, stamp

BOTH = Identity("AB", "max")
OWNER_ONLY = Identity("AB", "")
AGENT_ONLY = Identity("", "max")
NEITHER = Identity()

COMBOS = [
    (BOTH, "AB-max-"),
    (OWNER_ONLY, "AB-"),
    (AGENT_ONLY, "max-"),
    (NEITHER, ""),
]


# --- the parser -------------------------------------------------------------------


@pytest.mark.parametrize("name, expected", [
    ("AB-max-spring_promo", ("AB", "max", "spring_promo")),
    ("ABCD-agent_2-a - b", ("ABCD", "agent_2", "a - b")),
    ("AB-cli-QC 25-54", ("AB", "cli", "QC 25-54")),
])
def test_parse_reads_a_full_prefix(name, expected):
    parsed = parse(name)
    assert (parsed.owner, parsed.agent, parsed.base) == expected


@pytest.mark.parametrize("name", ["Spring - Prospecting", "ab-max-x", "A-max-x", "AB-Max-x",
                                  "AB-m-x", "launch", "", None])
def test_parse_refuses_what_is_not_a_full_prefix(name):
    assert parse(name) is None


def test_origin_is_read_from_the_name():
    assert naming.origin_of("AB-max-x")["origin"] == "agent"
    assert naming.origin_of("AB-cli-x")["origin"] == "cli"
    assert naming.origin_of("Brand - Prospecting") == {"origin": "manual", "owner": None,
                                                      "agent": None, "base": None}


# --- the four identity combinations -------------------------------------------------


@pytest.mark.parametrize("identity, prefix", COMBOS)
def test_create_stamps_each_combination(identity, prefix):
    out = stamp(identity, "spring_promo", entity_type="campaign")
    assert out.final == f"{prefix}spring_promo"
    assert out.prefix == prefix
    if not prefix:
        assert out.rule == naming.NO_IDENTITY_RULE and out.final == "spring_promo"


@pytest.mark.parametrize("identity, prefix", COMBOS)
def test_copy_restamps_each_combination(identity, prefix):
    out = restamp_for_copy(identity, "CD-bot-launch", entity_type="ad", suffix=" (copy)")
    if prefix:
        assert out.final == f"{prefix}launch (copy)"
    else:
        assert out.final == "CD-bot-launch (copy)", "no identity: the old copy naming"


@pytest.mark.parametrize("identity, prefix", COMBOS[:3])
def test_rename_keeps_each_prefix(identity, prefix):
    out = rename(identity, f"{prefix}old", "new", entity_type="ad", ledger_owned=False)
    assert out.final == f"{prefix}new" and out.rule == "kept_prefix"


def test_rename_with_no_identity_needs_the_ledger():
    with pytest.raises(NameRefused, match="only its own"):
        rename(NEITHER, "old", "new", entity_type="ad", ledger_owned=False)
    assert rename(NEITHER, "old", "new", entity_type="ad", ledger_owned=True).final == "new"


# --- the rules ------------------------------------------------------------------------


def test_a_prefixed_name_is_never_prefixed_twice():
    assert stamp(BOTH, "AB-max-spring", entity_type="ad").final == "AB-max-spring"
    assert stamp(OWNER_ONLY, "AB-spring", entity_type="ad").final == "AB-spring"


@pytest.mark.parametrize("identity", [BOTH, OWNER_ONLY, AGENT_ONLY, NEITHER])
def test_another_agents_prefix_is_refused(identity):
    with pytest.raises(NameRefused, match="CD-bot-"):
        stamp(identity, "CD-bot-launch", entity_type="campaign")


def test_the_cli_marker_cannot_be_claimed_by_the_agent():
    with pytest.raises(NameRefused, match="CLI"):
        stamp(BOTH, "AB-cli-launch", entity_type="campaign")


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\tb", "a\x00b"])
def test_control_characters_are_refused(bad):
    with pytest.raises(NameRefused, match="control"):
        stamp(BOTH, bad, entity_type="ad")


def test_an_empty_name_is_refused():
    with pytest.raises(NameRefused):
        stamp(BOTH, "   ", entity_type="ad set")


@pytest.mark.parametrize("identity, prefix", COMBOS)
def test_length_counts_the_prefix_and_is_refused_not_truncated(identity, prefix):
    room = 400 - len(prefix)
    assert len(stamp(identity, "x" * room, entity_type="campaign").final) == 400
    with pytest.raises(NameRefused, match="never truncated"):
        stamp(identity, "x" * (room + 1), entity_type="campaign")


def test_a_copy_over_the_limit_is_refused():
    with pytest.raises(NameRefused, match="too long"):
        restamp_for_copy(BOTH, "x" * 395, entity_type="ad", suffix=" (copy)")


def test_rename_refuses_manual_and_foreign_entities():
    with pytest.raises(NameRefused, match="only its own"):
        rename(BOTH, "Brand - Prospecting", "new", entity_type="campaign", ledger_owned=False)
    with pytest.raises(NameRefused, match="another agent"):
        rename(BOTH, "CD-bot-launch", "new", entity_type="campaign", ledger_owned=False)
    with pytest.raises(NameRefused, match="another agent"):
        rename(OWNER_ONLY, "AB-max-launch", "new", entity_type="campaign", ledger_owned=False)


def test_rename_restores_a_prefix_removed_elsewhere_on_a_ledger_owned_entity():
    out = rename(BOTH, "launch", "launch 2", entity_type="ad", ledger_owned=True)
    assert out.final == "AB-max-launch 2" and out.rule == "restoring_prefix"
    assert "restoring the prefix" in out.notes[0]


def test_rename_cannot_drop_or_swap_the_prefix():
    assert rename(BOTH, "AB-max-a", "AB-max-b", entity_type="ad", ledger_owned=False).final == "AB-max-b"
    with pytest.raises(NameRefused):
        rename(BOTH, "AB-max-a", "CD-bot-b", entity_type="ad", ledger_owned=False)


# --- validation of the identity itself ----------------------------------------------


@pytest.mark.parametrize("owner, agent, key", [
    ("a1", "max", naming.OWNER_KEY),
    ("ab", "", naming.OWNER_KEY),
    ("ABCDE", "", naming.OWNER_KEY),
    ("AB", "max-1", naming.AGENT_KEY),
    ("AB", "Max", naming.AGENT_KEY),
    ("", "m", naming.AGENT_KEY),
    ("AB", "cli", naming.AGENT_KEY),
    ("AB", "manual", naming.AGENT_KEY),
])
def test_a_malformed_value_is_refused_naming_the_env_key(owner, agent, key):
    identity = Identity.from_values(owner, agent)
    assert key in identity.problem
    with pytest.raises(NameRefused, match=key):
        stamp(identity, "x", entity_type="campaign")


def test_empty_values_are_not_a_problem():
    assert Identity.from_values("", "  ").problem == ""
    assert Identity.from_values(None, None).prefix == ""


def test_the_cli_identity():
    assert BOTH.cli().prefix == "AB-cli-"
    assert OWNER_ONLY.cli().prefix == "AB-cli-"
    assert AGENT_ONLY.cli().prefix == "", "no owner: nothing to mark"
    assert NEITHER.cli().prefix == ""


# --- where the identity comes from: .env (and config.env), never the process -------


def _write(path, owner=None, agent=None):
    lines = ["META_ADS_ACCESS_TOKEN=not-read-here"]
    if owner is not None:
        lines.append(f"{naming.OWNER_KEY}={owner}")
    if agent is not None:
        lines.append(f"{naming.AGENT_KEY}={agent}")
    path.write_text("\n".join(lines) + "\n")
    return path


def test_the_identity_comes_from_the_env_file(tmp_path):
    env = _write(tmp_path / ".env", "AB", "max")
    assert Identity.from_env([env]).prefix == "AB-max-"


def test_empty_keys_in_the_file_mean_no_prefix(tmp_path):
    env = _write(tmp_path / ".env", "", "")
    identity = Identity.from_env([env])
    assert identity.prefix == "" and identity.problem == ""


def test_env_wins_over_config_env(tmp_path):
    config = _write(tmp_path / "config.env", "CD", "bot")
    env = _write(tmp_path / ".env", "AB")
    identity = Identity.from_env([config, env])
    assert (identity.owner, identity.agent) == ("AB", "bot")


def test_the_process_env_is_ignored_with_a_warning(tmp_path, monkeypatch):
    env = _write(tmp_path / ".env", "AB", "max")
    monkeypatch.setenv(naming.OWNER_KEY, "CD")
    monkeypatch.setenv(naming.AGENT_KEY, "max")
    identity = Identity.from_env([env])
    assert identity.prefix == "AB-max-"
    assert len(identity.warnings) == 1 and naming.OWNER_KEY in identity.warnings[0]


def test_a_process_value_with_no_file_is_still_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv(naming.OWNER_KEY, "CD")
    identity = Identity.from_env([tmp_path / "missing.env"])
    assert identity.prefix == "" and identity.warnings


# --- the server reads it once, from .env -----------------------------------------


def test_the_suite_runs_as_the_placeholder_identity(monkeypatch):
    from tests.harness import build_tools
    from tests.test_mcp_write_path import make_sim

    registrar, state = build_tools(make_sim(), monkeypatch=monkeypatch)
    assert state.identity.prefix == "AB-max-"
    shown = registrar.tools["get_guardrails"]()["identity"]
    assert shown["prefix"] == "AB-max-" and shown["parts_set"] == ["owner_initials", "agent_name"]


def test_startup_warns_about_a_process_value_and_a_bad_one(tmp_path, monkeypatch):
    from metaads_mcp.server import ServerState, identity_warnings
    from metaads_mcp.guardrails import DEFAULTS, Limits, SessionCounters
    from metaads_mcp.previews import PreviewStore

    env = _write(tmp_path / ".env", "a1", "max")
    monkeypatch.setenv(naming.AGENT_KEY, "bot")
    limits = Limits(**DEFAULTS)
    state = ServerState(limits=limits, counters=SessionCounters(limits), previews=PreviewStore(),
                        identity=Identity.from_env([env]))
    warnings = identity_warnings(state)
    assert any(naming.AGENT_KEY in w and "IGNORED" in w for w in warnings)
    assert any(naming.OWNER_KEY in w for w in warnings)


# --- the review's naming rules -------------------------------------------------------


@pytest.mark.parametrize("name", ["AB-bot-x", "AB-cli-x"])
def test_initials_only_never_own_another_agents_or_the_clis_names(name):
    assert not OWNER_ONLY.owns(name)
    assert OWNER_ONLY.owns("AB-x")
    with pytest.raises(NameRefused):
        rename(OWNER_ONLY, name, "y", entity_type="ad", ledger_owned=False)


def test_impersonation_is_refused_even_with_no_identity():
    with pytest.raises(NameRefused, match="CD-bot-"):
        stamp(NEITHER, "CD-bot-x", entity_type="ad")


@pytest.mark.parametrize("identity, name", [
    (BOTH, "AB-max-AB-max-x"),
    (BOTH, "AB-max-CD-bot-x"),
    (OWNER_ONLY, "AB-AB-x"),
    (OWNER_ONLY, "AB-CD-bot-x"),
    (AGENT_ONLY, "max-max-x"),
])
def test_a_double_prefix_is_refused(identity, name):
    with pytest.raises(NameRefused, match="twice"):
        stamp(identity, name, entity_type="campaign")


def test_a_copy_of_a_double_prefixed_source_strips_them_all():
    assert restamp_for_copy(BOTH, "CD-bot-AB-max-x", entity_type="ad").final == "AB-max-x"


@pytest.mark.parametrize("char", ["​", " ", " ", "‮", "﻿", "\x7f"])
def test_invisible_and_format_unicode_is_refused(char):
    with pytest.raises(NameRefused, match="invisible"):
        stamp(BOTH, f"spring{char}promo", entity_type="ad")


def test_an_empty_env_value_does_not_override_config_env(tmp_path):
    config = _write(tmp_path / "config.env", "CD", "bot")
    env = _write(tmp_path / ".env", "", "")
    identity = Identity.from_env([config, env])
    assert (identity.owner, identity.agent) == ("CD", "bot")


# --- an owner-only prefix must not make the base read as an agent ---------------


@pytest.mark.parametrize("base", ["spring-promo", "qc_fr-launch", "ab-x"])
def test_owner_only_refuses_a_base_that_would_parse_as_an_agent(base):
    """``AB-`` + ``spring-promo`` is ``AB-spring-promo``: agent "spring". It
    used to be stamped and then fail later with a misleading message."""
    with pytest.raises(NameRefused) as caught:
        stamp(OWNER_ONLY, base, entity_type="campaign")
    message = str(caught.value)
    assert "agent" in message and ("_" in message and "space" in message), message


@pytest.mark.parametrize("base", ["spring_promo", "spring promo", "Spring-promo", "a-b", "2x-y"])
def test_owner_only_accepts_a_base_that_cannot_parse_as_an_agent(base):
    final = stamp(OWNER_ONLY, base, entity_type="campaign").final
    assert final == f"AB-{base}"
    assert parse(final) is None or parse(final).agent != base.split("-")[0]


def test_owner_only_copy_of_an_ambiguous_source_is_refused_too():
    with pytest.raises(NameRefused):
        restamp_for_copy(OWNER_ONLY, "spring-promo", entity_type="ad", suffix=" (copy)")


def test_other_identities_still_accept_a_hyphenated_base():
    for identity in (BOTH, AGENT_ONLY, NEITHER):
        assert stamp(identity, "spring-promo", entity_type="campaign").base == "spring-promo"

