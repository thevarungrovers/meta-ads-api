"""The scripts, driven through their own main() with a simulated account."""

from __future__ import annotations

import pytest

from tests.fakes import account, ad, ad_set, campaign, insights_row
from tests.graphsim import GraphSim

CAMPAIGN_ID = "120200000000001"
ADSET_ID = "120200000000002"
AD_ID = "120200000000003"


@pytest.fixture
def sim(monkeypatch):
    simulated = GraphSim(
        account=account(),
        objects={
            CAMPAIGN_ID: campaign(CAMPAIGN_ID, daily_budget="25000"),
            ADSET_ID: ad_set(ADSET_ID, daily_budget="5000"),
            AD_ID: ad(AD_ID),
        },
        edges={
            "act_1234567890": {
                "campaigns": [CAMPAIGN_ID],
                "adsets": [ADSET_ID],
                "ads": [AD_ID],
            }
        },
        insights=[insights_row(campaign_id=CAMPAIGN_ID, campaign_name="Brand")],
    )
    import metaads_reporting.graph as graph

    monkeypatch.setattr(graph.requests, "Session", lambda: simulated)
    return simulated


# --- fetch_report -----------------------------------------------------------


def test_fetch_report_runs_and_prints(sim, capsys):
    from scripts import fetch_report

    assert fetch_report.main(["campaigns", "--days", "7"]) == 0
    out = capsys.readouterr().out
    assert "campaigns:" in out
    assert "attribution:" in out
    assert "7d_click" in out


def test_fetch_report_names_the_currency(sim, capsys):
    from scripts import fetch_report

    fetch_report.main(["campaigns", "--days", "7"])
    assert "currency: CAD" in capsys.readouterr().out


def test_fetch_report_query_mode_calls_nothing(sim, capsys):
    from scripts import fetch_report

    assert fetch_report.main(["campaigns", "--query"]) == 0
    assert sim.calls == []
    assert "level = campaign" in capsys.readouterr().out


def test_fetch_report_rejects_an_unknown_report(sim, capsys):
    from scripts import fetch_report

    assert fetch_report.main(["wombats"]) == 1
    assert "Unknown report" in capsys.readouterr().err


def test_fetch_report_requires_both_ends_of_a_range(sim, capsys):
    from scripts import fetch_report

    assert fetch_report.main(["campaigns", "--since", "2026-01-01"]) == 1
    assert "together" in capsys.readouterr().err


def test_fetch_report_writes_a_csv(sim, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("META_ADS_OUTPUT_DIR", str(tmp_path))
    from scripts import fetch_report

    assert fetch_report.main(["campaigns", "--days", "7", "--csv"]) == 0
    written = list(tmp_path.glob("campaigns_*.csv"))
    assert len(written) == 1
    assert "campaign_id" in written[0].read_text()


def test_fetch_report_accounts_lists_without_insights(sim, capsys):
    from scripts import fetch_report

    assert fetch_report.main(["accounts"]) == 0
    assert "1234567890" in capsys.readouterr().out


def test_fetch_report_totals_say_why_reach_is_missing(sim, capsys):
    from scripts import fetch_report

    fetch_report.main(["campaigns", "--days", "7"])
    out = capsys.readouterr().out
    assert "cannot be summed across rows" in out


# --- manage -----------------------------------------------------------------


def test_manage_find_lists_candidates(sim, capsys):
    from scripts import manage

    assert manage.main(["find", "campaign", "Brand"]) == 0
    out = capsys.readouterr().out
    assert CAMPAIGN_ID in out
    assert "budget 250.00 CAD/day" in out


def test_manage_find_reports_nothing_found(sim, capsys):
    from scripts import manage

    assert manage.main(["find", "campaign", "wombat"]) == 1


def test_manage_dry_run_validates_and_changes_nothing(sim, capsys):
    """Not a local guess: Meta really does check it and really does not apply it."""
    from scripts import manage

    assert manage.main(["campaign", "pause", "--id", CAMPAIGN_ID, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Meta validated it" in out
    assert "--dry-run: stopping here." in out
    assert sim.validated and not sim.applied
    assert sim.objects[CAMPAIGN_ID]["status"] == "ACTIVE"


def test_manage_yes_applies(sim, capsys):
    from scripts import manage

    assert manage.main(["campaign", "pause", "--id", CAMPAIGN_ID, "--yes"]) == 0
    assert "APPLIED" in capsys.readouterr().out
    assert sim.objects[CAMPAIGN_ID]["status"] == "PAUSED"


def test_manage_asks_before_applying(sim, capsys, monkeypatch):
    from scripts import manage

    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert manage.main(["campaign", "pause", "--id", CAMPAIGN_ID]) == 0
    assert "Not applied." in capsys.readouterr().out
    assert sim.applied == []


def test_manage_requires_the_word_yes(sim, capsys, monkeypatch):
    from scripts import manage

    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert manage.main(["campaign", "pause", "--id", CAMPAIGN_ID]) == 0
    assert sim.applied == []


def test_manage_applies_on_a_typed_yes(sim, capsys, monkeypatch):
    from scripts import manage

    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert manage.main(["campaign", "pause", "--id", CAMPAIGN_ID]) == 0
    assert sim.objects[CAMPAIGN_ID]["status"] == "PAUSED"


def test_manage_set_budget_converts_to_minor_units(sim, capsys):
    from scripts import manage

    assert (
        manage.main(["adset", "set-budget", "--id", ADSET_ID, "--amount", "60.00", "--yes"])
        == 0
    )
    assert sim.objects[ADSET_ID]["daily_budget"] == "6000"


def test_manage_refuses_minor_units_typed_as_an_amount(sim, capsys):
    from scripts import manage

    assert (
        manage.main(
            ["adset", "set-budget", "--id", ADSET_ID, "--amount", "600000", "--yes"]
        )
        == 1
    )
    assert "NOT minor units" in capsys.readouterr().err


def test_manage_refuses_a_negative_amount(sim):
    from scripts import manage

    with pytest.raises(SystemExit):
        manage.main(["adset", "set-budget", "--id", ADSET_ID, "--amount", "-5", "--yes"])


def test_manage_refuses_a_bid_under_a_strategy_that_ignores_it(sim, capsys):
    from scripts import manage

    assert (
        manage.main(["adset", "set-bid", "--id", ADSET_ID, "--amount", "3.50", "--yes"])
        == 1
    )
    assert "does not use bid_amount" in capsys.readouterr().err


def test_manage_needs_an_id_or_a_name_and_not_both(sim):
    from scripts import manage

    with pytest.raises(SystemExit):
        manage.main(["campaign", "pause"])
    with pytest.raises(SystemExit):
        manage.main(["campaign", "pause", "--id", "1", "--name", "x"])


def test_manage_reports_a_refusal_rather_than_a_traceback(sim, capsys):
    from scripts import manage

    # Already ACTIVE, so activating is a no-op the plan refuses to build.
    assert manage.main(["campaign", "activate", "--id", CAMPAIGN_ID, "--yes"]) == 1
    assert "already ACTIVE" in capsys.readouterr().err


def test_manage_logs_what_it_did(sim):
    from metaads_reporting import logdb
    from scripts import manage

    manage.main(["campaign", "pause", "--id", CAMPAIGN_ID, "--yes"])
    tools = [r["tool_name"] for r in logdb.connect().execute("SELECT * FROM tool_calls")]
    assert "manage.campaign" in tools


# --- test_connection --------------------------------------------------------


def test_connection_ladder_runs_green(sim, capsys, monkeypatch, tmp_path):
    from metaads_reporting import config
    from scripts import test_connection

    env = tmp_path / ".env"
    env.write_text("META_ADS_ACCESS_TOKEN=x\n")
    env.chmod(0o600)
    monkeypatch.setattr(test_connection, "ENV_PATH", env)
    monkeypatch.setattr(config, "ENV_PATH", env)

    assert test_connection.main([]) == 0
    out = capsys.readouterr().out
    assert "Connection is good." in out
    assert "read-only guard" in out
    assert "currency is supported" in out


def test_connection_ladder_fails_on_a_missing_env(capsys, monkeypatch, tmp_path):
    from scripts import test_connection

    monkeypatch.setattr(test_connection, "ENV_PATH", tmp_path / "nope")
    assert test_connection.main([]) == 1
    assert "cp .env.example .env" in capsys.readouterr().out


def test_connection_ladder_warns_about_a_world_readable_env(sim, capsys, monkeypatch, tmp_path):
    from metaads_reporting import config
    from scripts import test_connection

    env = tmp_path / ".env"
    env.write_text("META_ADS_ACCESS_TOKEN=x\n")
    env.chmod(0o644)
    monkeypatch.setattr(test_connection, "ENV_PATH", env)
    monkeypatch.setattr(config, "ENV_PATH", env)

    test_connection.main([])
    assert "chmod 600" in capsys.readouterr().out


# --- token_info -------------------------------------------------------------


def test_token_info_warns_when_the_token_is_not_a_system_user(sim, capsys):
    from scripts import token_info
    from tests.fakes import FakeResponse

    original = sim.request

    def router(method, url, params=None, data=None, files=None, timeout=None):
        if "debug_token" in url:
            return FakeResponse(
                {
                    "data": {
                        "type": "USER",
                        "is_valid": True,
                        "app_id": "123",
                        "scopes": ["ads_read", "ads_management", "business_management"],
                    }
                }
            )
        return original(method, url, params, data, files, timeout)

    sim.request = router
    assert token_info.main([]) == 0
    out = capsys.readouterr().out
    assert "NOT a System User token" in out
    assert "190" in out and "460" in out


def test_token_info_is_happy_with_a_system_user(sim, capsys):
    from scripts import token_info
    from tests.fakes import FakeResponse

    original = sim.request

    def router(method, url, params=None, data=None, files=None, timeout=None):
        if "debug_token" in url:
            return FakeResponse(
                {
                    "data": {
                        "type": "SYSTEM_USER",
                        "is_valid": True,
                        "app_id": "123",
                        "scopes": ["ads_read", "ads_management", "business_management"],
                    }
                }
            )
        return original(method, url, params, data, files, timeout)

    sim.request = router
    assert token_info.main([]) == 0
    out = capsys.readouterr().out
    assert "This is a System User token" in out
    assert "Every scope this toolchain uses is present" in out


def test_token_info_lists_the_scopes_that_are_missing(sim, capsys):
    from scripts import token_info
    from tests.fakes import FakeResponse

    original = sim.request

    def router(method, url, params=None, data=None, files=None, timeout=None):
        if "debug_token" in url:
            return FakeResponse(
                {"data": {"type": "SYSTEM_USER", "is_valid": True, "scopes": ["ads_read"]}}
            )
        return original(method, url, params, data, files, timeout)

    sim.request = router
    token_info.main([])
    out = capsys.readouterr().out
    assert "ads_management" in out
    assert "business_management" in out


def test_token_info_check_fails_when_expiry_is_near(sim, capsys):
    import datetime as dt

    from scripts import token_info
    from tests.fakes import FakeResponse

    soon = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)).timestamp())
    original = sim.request

    def router(method, url, params=None, data=None, files=None, timeout=None):
        if "debug_token" in url:
            return FakeResponse(
                {
                    "data": {
                        "type": "SYSTEM_USER",
                        "is_valid": True,
                        "expires_at": soon,
                        "scopes": ["ads_read", "ads_management", "business_management"],
                    }
                }
            )
        return original(method, url, params, data, files, timeout)

    sim.request = router
    assert token_info.main(["--check", "--days", "14"]) == 1


def test_token_info_never_prints_the_token(sim, capsys):
    from scripts import token_info
    from tests.conftest import FAKE_TOKEN
    from tests.fakes import FakeResponse

    original = sim.request

    def router(method, url, params=None, data=None, files=None, timeout=None):
        if "debug_token" in url:
            return FakeResponse({"data": {"type": "SYSTEM_USER", "is_valid": True}})
        return original(method, url, params, data, files, timeout)

    sim.request = router
    token_info.main([])
    captured = capsys.readouterr()
    assert FAKE_TOKEN not in captured.out + captured.err


# --- naming: OWNER-cli-<base> when the initials are set, plain when not ----------


CLI_COMBOS = [
    pytest.param("AB", "max", "AB-cli-", id="both"),
    pytest.param("AB", "", "AB-cli-", id="owner-only"),
    pytest.param("", "max", "", id="agent-only"),
    pytest.param("", "", "", id="neither"),
]


def _env(monkeypatch, tmp_path, owner, agent):
    from metaads_reporting import config

    path = tmp_path / ".env"
    path.write_text(f"META_ADS_MCP_OWNER_INITIALS={owner}\nMETA_ADS_MCP_AGENT_NAME={agent}\n")
    monkeypatch.setattr(config, "ENV_PATH", path)


def _sent(sim, edge):
    return next(c for c in sim.created if c["edge"] == edge)["payload"]


@pytest.mark.parametrize("owner, agent, prefix", CLI_COMBOS)
def test_manage_ad_duplicate_stamps_the_cli_prefix(sim, monkeypatch, tmp_path, capsys, owner, agent, prefix):
    from scripts import manage
    from tests.test_mcp_creative_path import CREATIVE_ID, link_creative_obj

    _env(monkeypatch, tmp_path, owner, agent)
    sim.objects[AD_ID]["creative"] = {"id": CREATIVE_ID}
    sim.objects[CREATIVE_ID] = link_creative_obj()
    assert manage.main(["ad", "duplicate", "--id", AD_ID, "--headline", "new",
                        "--new-name", "spring v2", "--yes"]) == 0
    assert _sent(sim, "ads")["name"] == f"{prefix}spring v2"
    assert _sent(sim, "adcreatives")["name"] == f"{prefix}spring v2 creative"
    out = capsys.readouterr().out
    assert (prefix in out) if prefix else ("names are used as given" in out)


@pytest.mark.parametrize("owner, agent, prefix", CLI_COMBOS)
def test_manage_audiences_stamp_the_cli_prefix(sim, monkeypatch, tmp_path, owner, agent, prefix):
    from scripts import manage

    _env(monkeypatch, tmp_path, owner, agent)
    assert manage.main(["audience", "saved", "--name", "QC 25-54", "--yes",
                        "--targeting", '{"geo_locations": {"countries": ["CA"]}}']) == 0
    assert _sent(sim, "saved_audiences")["name"] == f"{prefix}QC 25-54"


def test_manage_audience_lookalike_stamps_the_cli_prefix(sim, monkeypatch, tmp_path):
    from scripts import manage

    sim.objects["SRC"] = {"id": "SRC", "name": "Buyers", "approximate_count_lower_bound": 5000,
                          "approximate_count_upper_bound": 6000, "delivery_status": {"code": 200},
                          "operation_status": {"code": 200}, "subtype": "WEBSITE"}
    assert manage.main(["audience", "lookalike", "--name", "LAL 1%", "--from-id", "SRC",
                        "--country", "CA", "--yes"]) == 0
    created = [c for c in sim.created if c["edge"] == "customaudiences"]
    assert created[0]["payload"]["name"] == "AB-cli-LAL 1%"


def test_manage_refuses_a_name_carrying_the_agents_prefix(sim, capsys):
    from scripts import manage

    assert manage.main(["audience", "saved", "--name", "AB-max-x", "--yes",
                        "--targeting", '{"geo_locations": {"countries": ["CA"]}}']) == 1
    assert "prefix" in capsys.readouterr().err
    assert not sim.created


def test_manage_refuses_a_malformed_owner_naming_the_key(sim, monkeypatch, tmp_path, capsys):
    from scripts import manage

    _env(monkeypatch, tmp_path, "a1", "")
    assert manage.main(["audience", "saved", "--name", "x", "--yes",
                        "--targeting", '{"geo_locations": {"countries": ["CA"]}}']) == 1
    assert "META_ADS_MCP_OWNER_INITIALS" in capsys.readouterr().err


@pytest.mark.parametrize("edge", ["campaigns?x=1", "campaigns#frag", "x/campaigns", "/campaigns"])
def test_an_edge_with_a_query_fragment_or_slash_is_refused(sim, edge):
    from metaads_reporting.write.client import MutatingMetaAdsClient, MutationError

    client = MutatingMetaAdsClient.from_env()
    client.required_name_prefix = "AB-cli-"
    with pytest.raises(MutationError, match="bare path segment"):
        client.create(entity_type="campaign", edge=edge, payload={"name": "plain", "status": "PAUSED"})
    with pytest.raises(MutationError, match="bare path segment"):
        client.update(f"{CAMPAIGN_ID}/campaigns", entity_type="campaign", payload={"name": "AB-cli-x"})
    assert not sim.writes
