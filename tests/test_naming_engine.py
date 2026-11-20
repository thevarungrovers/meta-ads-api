"""The engine's naming checks: the identity, and the backstop that no create
leaves unprefixed when a prefix is configured."""

from __future__ import annotations

from decimal import Decimal

from metaads_mcp.changes.base import NOTHING, ChangeKind, Proposal
from metaads_reporting.naming import Identity
from metaads_reporting.write.plan import FieldChange, PlannedChange
from tests.kindkit import failed, rig
from tests.test_mcp_write_path import make_sim


class Forgetful(ChangeKind):
    """A creating kind that names things however it is told to."""

    name = "forgetful_create"
    creates_named = ("campaign",)
    validate_in_preview = False
    weight = 3

    def __init__(self, names):
        self.names = names

    def propose(self, ctx, **_):
        proposal = Proposal(
            entity_type="campaign", entity_id="(new)", entity_name=self.names[0] if self.names else "",
            path="", field_name="created", before=NOTHING, after="PAUSED", checks=[],
            projected=Decimal("0"),
            payload={"name": self.names[0] if self.names else "x"},
        )
        if self.names:
            proposal.name(ctx.name_for_create("campaign", self.names[0].split("-")[-1]))
        return proposal

    def current(self, ctx, payload):
        return NOTHING

    def build(self, ctx, payload):
        return PlannedChange(action="create", entity="campaign", entity_type="campaign", node_id="",
                             label=payload["name"], edge="campaigns",
                             payload={"name": payload["name"], "status": "PAUSED", "objective": "OUTCOME_SALES",
                                      "special_ad_categories": []},
                             changes=[FieldChange("campaign", None, payload["name"])])


def _preview(monkeypatch, names, identity=None):
    tools, state = rig(monkeypatch, make_sim())
    if identity is not None:
        state.identity = identity
    return state.engine.preview(Forgetful(names), {}), state


def test_an_unprefixed_create_is_blocked_when_a_prefix_is_configured(monkeypatch):
    preview, _ = _preview(monkeypatch, ["spring"])
    assert failed(preview, "name_prefix"), preview.checks


def test_a_prefixed_create_passes_and_shows_the_naming_block(monkeypatch):
    preview, _ = _preview(monkeypatch, ["AB-max-spring"])
    assert not failed(preview, "name_prefix"), preview.checks
    assert preview.naming and preview.naming[0]["final"] == "AB-max-spring"


def test_with_no_identity_nothing_is_required(monkeypatch):
    preview, _ = _preview(monkeypatch, ["spring"], Identity())
    assert not any("name_prefix" in c or "agent_identity" in c for c in preview.checks
                   if c.startswith("FAIL")), preview.checks
    assert preview.naming[0]["prefix"] == "" and "no identity configured" in preview.naming[0]["rule"]


def test_a_malformed_identity_blocks_naming_and_names_the_env_key(monkeypatch):
    preview, _ = _preview(monkeypatch, ["spring"], Identity.from_values("a1", "max"))
    assert failed(preview, "agent_identity"), preview.checks
    assert any("META_ADS_MCP_OWNER_INITIALS" in c for c in preview.checks)


def test_a_creating_kind_that_reports_no_name_is_blocked(monkeypatch):
    preview, _ = _preview(monkeypatch, [])
    assert failed(preview, "name_prefix"), preview.checks


def test_the_ledger_row_carries_the_identity_and_the_requested_name(monkeypatch):
    from metaads_mcp import ledger

    preview, state = _preview(monkeypatch, ["AB-max-spring"])
    kind = Forgetful(["AB-max-spring"])
    state.engine.kinds[kind.name] = kind
    result = state.engine.apply(kind, preview.preview_token)
    inverse = ledger.find_entry(result.entry_id)["inverse"]
    assert inverse["identity"] == {"owner_initials": "AB", "agent_name": "max", "source": ".env"}
    assert inverse["requested_name"] == "spring"


class Liar(Forgetful):
    """Reports a prefixed name in its preview but SENDS an unprefixed one."""

    name = "liar_create"

    def build(self, ctx, payload):
        plan = super().build(ctx, payload)
        plan.payload["name"] = "spring"
        return plan


def test_the_backstop_checks_the_name_actually_sent(monkeypatch):
    tools, state = rig(monkeypatch, make_sim())
    preview = state.engine.preview(Liar(["AB-max-spring"]), {})
    assert preview.naming[0]["final"] == "AB-max-spring"
    assert failed(preview, "name_prefix"), preview.checks
    assert any("'spring'" in c for c in preview.checks if "name_prefix" in c)
