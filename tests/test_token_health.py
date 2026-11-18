"""Token health: surfaced in the reads, and a dead token refuses a write."""

from __future__ import annotations

import time

import pytest

from tests.harness import build_tools
from tests.test_mcp_write_path import CAMPAIGN_ID, make_sim


def tools_with(monkeypatch, **token):
    sim = make_sim()
    sim.token_debug.update(token)
    registrar, _ = build_tools(sim, monkeypatch=monkeypatch)
    return registrar.tools, sim


def test_a_system_user_token_reports_never(monkeypatch):
    tool, _ = tools_with(monkeypatch)
    health = tool["get_guardrails"]()["token"]
    assert health["status"] == "ok" and health["expires"] == "never"
    assert "TEST-TOKEN" not in str(health)


def test_a_token_expiring_soon_warns(monkeypatch):
    tool, _ = tools_with(monkeypatch, expires_at=int(time.time()) + 5 * 86400)
    health = tool["get_account"]()["token"]
    assert health["status"] == "expiring" and "expires in" in health["warning"]


def test_an_expired_token_refuses_the_apply_before_anything_is_sent(monkeypatch):
    tool, sim = tools_with(monkeypatch)
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    from metaads_reporting import token_health

    token_health._cache.clear()
    sim.token_debug.update(is_valid=False)
    with pytest.raises(Exception) as caught:
        tool["apply_campaign_status"](preview_token=preview.preview_token)
    assert str(caught.value).startswith("AUTH_EXPIRED")
    assert sim.applied == []


def test_an_unreadable_token_does_not_block(monkeypatch):
    tool, sim = tools_with(monkeypatch)
    sim.token_debug = None  # the sim then answers garbage
    preview = tool["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    tool["apply_campaign_status"](preview_token=preview.preview_token)
    assert sim.applied
