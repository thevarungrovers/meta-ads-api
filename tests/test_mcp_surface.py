"""The MCP tool surface is a contract, not an accident.

An agent can call anything registered. Widening that set is a security-relevant
change, so it is pinned here: adding a tool means editing this file, which
means someone reviews it.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from metaads_mcp import server

READ_TOOLS = frozenset(
    {
        "list_ad_accounts",
        "get_account",
        "find_campaigns",
        "find_ad_sets",
        "find_ads",
        "run_report",
        "get_guardrails",
        "list_my_changes",
    }
)

PREVIEW_TOOLS = frozenset(
    {
        "preview_campaign_status",
        "preview_campaign_daily_budget",
        "preview_ad_set_status",
        "preview_ad_set_daily_budget",
        "preview_ad_set_bid_amount",
        "preview_ad_status",
    }
)

APPLY_TOOLS = frozenset(
    {
        "apply_campaign_status",
        "apply_campaign_daily_budget",
        "apply_ad_set_status",
        "apply_ad_set_daily_budget",
        "apply_ad_set_bid_amount",
        "apply_ad_status",
        "revert_change",
    }
)

EXPECTED = READ_TOOLS | PREVIEW_TOOLS | APPLY_TOOLS


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def test_the_surface_is_exactly_what_is_expected(tools):
    assert set(tools) == EXPECTED, {
        "unexpected": sorted(set(tools) - EXPECTED),
        "missing": sorted(EXPECTED - set(tools)),
    }


def test_every_apply_has_a_matching_preview(tools):
    """An apply with no preview could be called cold, with nothing shown first."""
    for name in APPLY_TOOLS - {"revert_change"}:
        assert name.replace("apply_", "preview_", 1) in PREVIEW_TOOLS


def test_every_apply_takes_only_a_preview_token(tools):
    """Taking the VALUES instead would let an apply be called without a preview,
    which is the whole thing the pair exists to prevent."""
    for name in APPLY_TOOLS - {"revert_change"}:
        properties = (tools[name].input_schema or {}).get("properties", {})
        assert set(properties) == {"preview_token"}, (name, sorted(properties))


def test_no_tool_takes_a_dry_run_flag(tools):
    """Separate names are the whole design: permissions key on the tool NAME.

    With a dry_run flag, one "always allow" clicked during a harmless preview
    would silently authorise every future real write.
    """
    for name, tool in tools.items():
        properties = (tool.input_schema or {}).get("properties", {})
        for flag in ("dry_run", "dryRun", "apply", "confirm", "force", "validate_only"):
            assert flag not in properties, f"{name} exposes {flag}"


def test_there_is_no_passthrough_tool(tools):
    """An unregistered operation must be unreachable, not merely undocumented."""
    for name in tools:
        assert name not in {"request", "graph", "call", "update", "mutate", "post"}
    for name, tool in tools.items():
        properties = (tool.input_schema or {}).get("properties", {})
        for arbitrary in ("path", "endpoint", "url", "method", "payload", "body", "params"):
            assert arbitrary not in properties, f"{name} takes {arbitrary}"


def test_nothing_can_set_a_status_to_deleted_or_archived(tools):
    """Both are irreversible through the API. PAUSED does what anyone wants."""
    from metaads_reporting.write.client import SETTABLE_STATUSES, check_status

    assert SETTABLE_STATUSES == {"ACTIVE", "PAUSED"}
    for status in ("DELETED", "ARCHIVED"):
        with pytest.raises(Exception) as caught:
            check_status(status)
        assert "PAUSED" in str(caught.value), status


ALL_HINTS = ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint")

#: The only two tools that answer from this process without calling Meta, and
#: so the only two that carry open_world_hint=False.
LOCAL_ONLY_TOOLS = frozenset({"get_guardrails", "list_my_changes"})


def test_every_tool_declares_all_four_hints(tools):
    """An omitted hint is not "unknown" -- it is the spec's default.

    And the spec's default for destructiveHint is TRUE, so leaving one off
    makes a plain list_ad_accounts render with the same warning as a budget
    change.
    """
    for name, tool in tools.items():
        annotations = tool.annotations
        assert annotations is not None, f"{name} declares no annotations at all"
        for hint in ALL_HINTS:
            assert getattr(annotations, hint, None) is not None, f"{name} omits {hint}"


def test_reads_and_previews_are_declared_read_only(tools):
    for name in READ_TOOLS | PREVIEW_TOOLS:
        assert tools[name].annotations.read_only_hint is True, name
        assert tools[name].annotations.destructive_hint is False, name


def test_applies_are_not_declared_read_only(tools):
    for name in APPLY_TOOLS:
        assert tools[name].annotations.read_only_hint is False, name


def test_only_the_local_tools_claim_a_closed_world(tools):
    for name, tool in tools.items():
        expected = name not in LOCAL_ONLY_TOOLS
        assert tool.annotations.open_world_hint is expected, name


def test_the_money_moving_applies_are_marked_destructive(tools):
    """And only those. Marking a reversible single-ad pause destructive would
    train somebody to click through the loud prompts."""
    spends = {
        "apply_campaign_status",
        "apply_campaign_daily_budget",
        "apply_ad_set_status",
        "apply_ad_set_daily_budget",
        "apply_ad_set_bid_amount",
    }
    for name in APPLY_TOOLS:
        assert tools[name].annotations.destructive_hint is (name in spends), name


def test_every_tool_has_a_description(tools):
    """It is the only thing an agent reads before deciding to call it."""
    for name, tool in tools.items():
        assert (tool.description or "").strip(), f"{name} has no description"


def test_every_tool_was_instrumented(tools):
    """The fallback in logdb.instrument_tool is silent by design, and a silent
    gap in the audit log is exactly the thing worth failing a build over."""
    from metaads_reporting import logdb

    assert logdb.UNINSTRUMENTED == []


def test_the_installers_safe_tool_list_matches_the_server(tools):
    """install.py hardcodes the list because it must run before the
    dependencies exist. This is what keeps that copy honest."""
    install = importlib.import_module("install")
    assert set(install.SAFE_TOOLS) == READ_TOOLS | PREVIEW_TOOLS
    assert set(install.WRITE_TOOLS) == APPLY_TOOLS
    assert not set(install.SAFE_TOOLS) & set(install.WRITE_TOOLS)
