"""Guardrails: the bounds, the session budget, the kill switch and read-only mode."""

from __future__ import annotations

from decimal import Decimal

import pytest

from metaads_mcp.guardrails import (
    DEFAULTS,
    Check,
    GuardrailConfigError,
    Limits,
    SessionCounters,
    WritesDisabled,
    any_failed,
    check_account_in_scope,
    check_campaign_in_scope,
    check_ceiling,
    check_count,
    check_currency,
    check_pct_increase,
    check_writes_enabled,
    load_limits,
    read_only_mode,
    render_checks,
    require_writes_enabled,
)


def limits(**overrides) -> Limits:
    base = dict(DEFAULTS)
    base.update(overrides)
    return Limits(**base)


# --- loading ----------------------------------------------------------------


def test_the_committed_file_loads():
    """guardrails.toml is committed on purpose; it must parse."""
    loaded = load_limits()
    assert loaded.max_daily_budget > 0
    assert loaded.allowed_currencies


def test_a_missing_file_falls_back_to_the_code_defaults(tmp_path):
    loaded = load_limits(tmp_path / "nope.toml")
    assert loaded.max_daily_budget == DEFAULTS["max_daily_budget"]


def test_a_misspelled_key_raises_at_startup_rather_than_being_ignored(tmp_path):
    """A silently-ignored ceiling is worse than no ceiling: it reads as
    protection that is not there."""
    path = tmp_path / "guardrails.toml"
    path.write_text('[limits]\nmax_dailly_budget = "10.00"\n')
    with pytest.raises(GuardrailConfigError) as caught:
        load_limits(path)
    assert "unknown key" in str(caught.value)
    assert "max_daily_budget" in str(caught.value)


def test_a_malformed_value_raises(tmp_path):
    path = tmp_path / "guardrails.toml"
    path.write_text('[limits]\nmax_applies_per_session = "lots"\n')
    with pytest.raises(GuardrailConfigError):
        load_limits(path)


def test_invalid_toml_raises(tmp_path):
    path = tmp_path / "guardrails.toml"
    path.write_text("[limits\n")
    with pytest.raises(GuardrailConfigError):
        load_limits(path)


def test_the_act_prefix_is_stripped_from_scoped_account_ids(tmp_path):
    path = tmp_path / "guardrails.toml"
    path.write_text('[scope]\nallowed_account_ids = ["act_123", "456"]\n')
    assert load_limits(path).allowed_account_ids == ("123", "456")


def test_an_env_override_can_narrow_the_scope(tmp_path, monkeypatch):
    path = tmp_path / "guardrails.toml"
    path.write_text('[scope]\nallowed_account_ids = ["111", "222"]\n')
    monkeypatch.setenv("META_ADS_MCP_ALLOWED_ACCOUNT_IDS", "111")
    assert load_limits(path).allowed_account_ids == ("111",)


def test_an_env_override_can_never_widen_the_scope(tmp_path, monkeypatch):
    """A machine-local override that could ADD accounts would defeat the
    committed file entirely."""
    path = tmp_path / "guardrails.toml"
    path.write_text('[scope]\nallowed_account_ids = ["111"]\n')
    monkeypatch.setenv("META_ADS_MCP_ALLOWED_ACCOUNT_IDS", "111,999")
    assert load_limits(path).allowed_account_ids == ("111",)


# --- individual checks ------------------------------------------------------


def test_account_scope_defaults_to_the_configured_account_only():
    """One System User token routinely reaches every account a business owns."""
    check = check_account_in_scope(limits(), "1234567890", "1234567890")
    assert check.ok
    assert not check_account_in_scope(limits(), "9999999999", "1234567890").ok


def test_an_empty_campaign_list_means_all_campaigns():
    assert check_campaign_in_scope(limits(), "123").ok


def test_a_campaign_list_excludes_everything_else():
    scoped = Limits(**DEFAULTS, allowed_campaign_ids=("123",))
    assert check_campaign_in_scope(scoped, "123").ok
    assert not check_campaign_in_scope(scoped, "456").ok
    assert not check_campaign_in_scope(scoped, None).ok


def test_currency_check():
    assert check_currency(limits(), "CAD").ok
    assert not check_currency(limits(), "USD").ok
    assert not check_currency(limits(), None).ok


def test_a_ceiling_is_inclusive():
    assert check_ceiling("x", Decimal("10"), Decimal("10")).ok
    assert not check_ceiling("x", Decimal("10.01"), Decimal("10")).ok


def test_a_percentage_increase_with_no_previous_value_passes():
    """A budget being set for the first time has nothing to be a percentage of."""
    assert check_pct_increase("x", None, Decimal("10"), 50.0).ok
    assert check_pct_increase("x", Decimal("0"), Decimal("10"), 50.0).ok


def test_a_percentage_decrease_always_passes():
    """The ceiling is on INCREASES. Cutting a budget is the safe direction."""
    assert check_pct_increase("x", Decimal("100"), Decimal("10"), 50.0).ok


def test_a_percentage_increase_over_the_ceiling_fails():
    assert not check_pct_increase("x", Decimal("100"), Decimal("200"), 50.0).ok
    assert check_pct_increase("x", Decimal("100"), Decimal("150"), 50.0).ok


def test_count_check():
    assert check_count(limits(max_entities_per_call=2), 2).ok
    assert not check_count(limits(max_entities_per_call=2), 3).ok


def test_rendering_names_the_check_and_whether_it_passed():
    rendered = render_checks([Check("x", True, "fine"), Check("y", False, "nope")])
    assert rendered[0].startswith("OK")
    assert rendered[1].startswith("FAIL")


def test_any_failed():
    assert not any_failed([Check("x", True, "")])
    assert any_failed([Check("x", True, ""), Check("y", False, "")])


# --- the session budget -----------------------------------------------------


def test_the_apply_count_is_capped():
    """A per-call limit does nothing to stop a loop making 400 legal changes."""
    counters = SessionCounters(limits(max_applies_per_session=2))
    counters.record(Decimal("0"))
    counters.record(Decimal("0"))
    assert "session apply limit reached" in counters.would_exceed(Decimal("0"))


def test_the_total_projected_spend_is_capped():
    counters = SessionCounters(limits(max_projected_session_delta=Decimal("100")))
    counters.record(Decimal("60"))
    assert counters.would_exceed(Decimal("50")) is not None
    assert counters.would_exceed(Decimal("40")) is None


def test_a_negative_delta_does_not_refund_the_budget():
    """Cutting a budget must not buy headroom to raise another one."""
    counters = SessionCounters(limits())
    counters.record(Decimal("-500"))
    assert counters.projected_delta == Decimal("0")


def test_the_snapshot_reports_what_is_left():
    counters = SessionCounters(limits(max_applies_per_session=5))
    counters.record(Decimal("10"))
    snapshot = counters.snapshot()
    assert snapshot["applies_used"] == 1
    assert snapshot["applies_remaining"] == 4
    assert snapshot["projected_session_delta"] == "10"


# --- the kill switch --------------------------------------------------------


def test_the_kill_switch_is_off_by_default():
    assert check_writes_enabled().ok
    require_writes_enabled()


def test_the_kill_switch_stops_writes_and_names_the_file(monkeypatch, tmp_path):
    switch = tmp_path / "WRITES_DISABLED"
    switch.write_text("paused by ops")
    monkeypatch.setattr("metaads_mcp.guardrails.KILL_SWITCH", switch)

    assert not check_writes_enabled().ok
    with pytest.raises(WritesDisabled) as caught:
        require_writes_enabled()
    assert "paused by ops" in str(caught.value)
    assert str(switch) in str(caught.value)
    assert "do not work around it" in str(caught.value)


def test_an_empty_kill_switch_file_still_disables_writes(monkeypatch, tmp_path):
    switch = tmp_path / "WRITES_DISABLED"
    switch.write_text("")
    monkeypatch.setattr("metaads_mcp.guardrails.KILL_SWITCH", switch)
    with pytest.raises(WritesDisabled):
        require_writes_enabled()


# --- read-only mode ---------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_read_only_mode_is_on_for_the_truthy_spellings(monkeypatch, value):
    monkeypatch.setenv("META_ADS_MCP_READ_ONLY", value)
    assert read_only_mode()


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_read_only_mode_is_off_otherwise(monkeypatch, value):
    monkeypatch.setenv("META_ADS_MCP_READ_ONLY", value)
    assert not read_only_mode()


def test_read_only_mode_is_read_from_the_process_env_not_dotenv():
    """Every client's MCP config has its own env block, which is the point: one
    checkout, read-only for one client and writable for another."""
    import inspect

    from metaads_mcp import guardrails

    source = inspect.getsource(guardrails.read_only_mode)
    assert "os.environ" in source
    assert "load_env" not in source and "dotenv" not in source
