"""Configuration, and the blank-is-not-absent trap."""

from __future__ import annotations

from pathlib import Path

import pytest

from metaads_reporting.config import (
    DEFAULT_API_VERSION,
    ConfigError,
    Settings,
    bool_env,
    env,
    int_env,
    normalize_api_version,
    require,
)


def test_a_blank_value_falls_back_to_the_default(monkeypatch):
    """The whole reason config.env() exists.

    .env.example ships every optional key spelled out with an empty value, and
    load_dotenv turns `META_ADS_API_VERSION=` into "". os.getenv would return
    that empty string rather than the default, so copying the template would
    silently override every default in the package with "".
    """
    monkeypatch.setenv("META_ADS_API_VERSION", "")
    assert env("META_ADS_API_VERSION", "v23.0") == "v23.0"


def test_whitespace_is_also_blank(monkeypatch):
    monkeypatch.setenv("META_ADS_API_VERSION", "   ")
    assert env("META_ADS_API_VERSION", "v23.0") == "v23.0"


def test_an_absent_key_falls_back_too(monkeypatch):
    monkeypatch.delenv("META_ADS_API_VERSION", raising=False)
    assert env("META_ADS_API_VERSION", "v23.0") == "v23.0"


def test_a_set_value_wins(monkeypatch):
    monkeypatch.setenv("META_ADS_API_VERSION", "v24.0")
    assert env("META_ADS_API_VERSION", "v23.0") == "v24.0"


def test_require_names_the_key_and_never_the_value(monkeypatch):
    monkeypatch.setenv("META_ADS_ACCESS_TOKEN", "")
    with pytest.raises(ConfigError) as caught:
        require("META_ADS_ACCESS_TOKEN", hint="Use a System User token.")
    message = str(caught.value)
    assert "META_ADS_ACCESS_TOKEN" in message
    assert "System User" in message


def test_int_and_bool_coercion(monkeypatch):
    monkeypatch.setenv("X", "7")
    assert int_env("X", 1) == 7
    monkeypatch.setenv("X", "yes")
    assert bool_env("X", False) is True
    monkeypatch.setenv("X", "off")
    assert bool_env("X", True) is False
    monkeypatch.setenv("X", "maybe")
    with pytest.raises(ConfigError):
        bool_env("X", True)


def test_api_version_gets_its_v(monkeypatch):
    """`/23.0/act_1/campaigns` 404s in a way that reads like a missing account
    rather than a missing prefix."""
    assert normalize_api_version("23.0") == "v23.0"
    assert normalize_api_version("v23.0") == "v23.0"
    assert normalize_api_version("  23.0  ") == "v23.0"
    assert normalize_api_version("") == DEFAULT_API_VERSION


def test_the_default_api_version_is_pinned_and_prefixed():
    """An UNVERSIONED Graph request is served by the OLDEST supported version,
    so leaving it off does not mean 'latest'."""
    assert DEFAULT_API_VERSION.startswith("v")
    assert DEFAULT_API_VERSION[1:].replace(".", "").isdigit()


# --- Settings ---------------------------------------------------------------


def test_settings_load_from_the_environment(settings):
    assert settings.access_token
    assert settings.account_id == "1234567890"
    assert settings.api_version == DEFAULT_API_VERSION


def test_a_missing_token_is_a_config_error(monkeypatch):
    monkeypatch.setenv("META_ADS_ACCESS_TOKEN", "")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_the_account_id_is_normalized_on_the_way_in(monkeypatch):
    monkeypatch.setenv("META_ADS_ACCOUNT_ID", "act_9876543210")
    assert Settings.from_env().account_id == "9876543210"


def test_describe_reports_the_token_as_presence_and_length_only(settings):
    """A Meta token is a bearer credential with no second factor. Not even a
    prefix goes into a diagnostic."""
    described = settings.describe()
    assert described["access_token"].startswith("present (")
    assert settings.access_token not in str(described)
    assert settings.access_token[:8] not in str(described)


def test_describe_shows_account_ids_in_full(settings):
    """They are not secret, and an elided one is useless in a diagnostic."""
    assert settings.describe()["account_id"] == "1234567890"


def test_describe_says_when_the_app_secret_is_absent(settings):
    assert "not set" in settings.describe()["app_secret"]


def test_resolve_account_id_prefers_the_override(settings):
    assert settings.resolve_account_id("act_5555555555") == "5555555555"
    assert settings.resolve_account_id(None) == "1234567890"


def test_resolve_account_id_raises_when_there_is_nothing_to_resolve(monkeypatch):
    monkeypatch.delenv("META_ADS_ACCOUNT_ID", raising=False)
    settings = Settings.from_env()
    with pytest.raises(ConfigError) as caught:
        settings.resolve_account_id(None)
    assert "--account-id" in str(caught.value)


def test_a_relative_output_dir_is_resolved_against_the_repo(monkeypatch):
    monkeypatch.setenv("META_ADS_OUTPUT_DIR", "somewhere")
    assert Settings.from_env().output_dir.is_absolute()


def test_an_absolute_output_dir_is_left_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("META_ADS_OUTPUT_DIR", str(tmp_path))
    assert Settings.from_env().output_dir == tmp_path


def test_the_graph_base_loses_its_trailing_slash(monkeypatch):
    """Otherwise every URL gets a double slash, which some proxies 404."""
    monkeypatch.setenv("META_ADS_GRAPH_BASE", "https://example.com/")
    assert Settings.from_env().graph_base == "https://example.com"
