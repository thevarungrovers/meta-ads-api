"""install.py writes the naming identity into .env only -- never into an MCP
client config, never into a tracked file. Placeholders only."""

from __future__ import annotations

import importlib
import json

import pytest

install = importlib.import_module("install")


def test_the_identity_is_written_into_env_keeping_everything_else(tmp_path):
    env = tmp_path / ".env"
    env.write_text("META_ADS_ACCESS_TOKEN=x\nMETA_ADS_MCP_OWNER_INITIALS=\n")
    install.write_identity(env, "AB", "max", dry_run=False)
    text = env.read_text()
    assert "META_ADS_ACCESS_TOKEN=x" in text
    assert "META_ADS_MCP_OWNER_INITIALS=AB" in text and text.count("META_ADS_MCP_OWNER_INITIALS") == 1
    assert "META_ADS_MCP_AGENT_NAME=max" in text


def test_a_dry_run_writes_nothing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    assert install.write_identity(env, "AB", None, dry_run=True)
    assert env.read_text() == "A=1\n"


@pytest.mark.parametrize("owner, agent", [("a1", None), (None, "max-1"), (None, "cli")])
def test_a_malformed_value_is_refused(tmp_path, owner, agent):
    with pytest.raises(install.InstallError):
        install.write_identity(tmp_path / ".env", owner, agent, dry_run=False)


def test_only_env_is_ever_written(tmp_path):
    with pytest.raises(install.InstallError, match="only into .env"):
        install.write_identity(tmp_path / "settings.json", "AB", "max", dry_run=False)


def test_no_mcp_config_block_carries_the_identity():
    for read_only in (True, False):
        block = json.dumps(install.server_block(read_only)) + install.codex_block(read_only)
        assert "OWNER_INITIALS" not in block and "AGENT_NAME" not in block


def test_the_env_example_ships_both_identity_keys_empty():
    from pathlib import Path

    lines = (Path(install.REPO_ROOT) / ".env.example").read_text().splitlines()
    for key in install.IDENTITY_KEYS:
        assert f"{key}=" in lines, key
