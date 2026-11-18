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


# --- .env is replaced atomically, never left half-written or world-readable ------


def test_env_is_replaced_atomically_from_a_private_temp_file(tmp_path, monkeypatch):
    import os
    import stat

    env = tmp_path / ".env"
    env.write_text("META_ACCESS_TOKEN=x\n")
    env.chmod(0o644)
    seen = {}
    real_replace = os.replace

    def replace(src, dst):
        seen["src"], seen["dst"] = str(src), str(dst)
        seen["mode"] = stat.S_IMODE(os.stat(src).st_mode)
        return real_replace(src, dst)

    monkeypatch.setattr(install.os, "replace", replace)
    install.write_identity(env, "AB", "max", dry_run=False)
    assert seen["dst"] == str(env) and os.path.dirname(seen["src"]) == str(tmp_path)
    assert seen["mode"] == 0o600, oct(seen["mode"])
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"], "no temp file left behind"
    assert "META_ACCESS_TOKEN=x" in env.read_text()


def test_a_stricter_existing_mode_is_kept(tmp_path):
    import stat

    env = tmp_path / ".env"
    env.write_text("META_ACCESS_TOKEN=x\n")
    env.chmod(0o400)
    install.write_identity(env, "AB", "max", dry_run=False)
    assert stat.S_IMODE(env.stat().st_mode) == 0o400


def test_a_failed_write_leaves_env_untouched(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("META_ACCESS_TOKEN=x\n")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(install.os, "replace", boom)
    with pytest.raises(OSError):
        install.write_identity(env, "AB", "max", dry_run=False)
    assert env.read_text() == "META_ACCESS_TOKEN=x\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


@pytest.mark.parametrize("owner, agent, key", [
    ("zz9secret", None, "META_ADS_MCP_OWNER_INITIALS"),
    (None, "Bad-Secret9", "META_ADS_MCP_AGENT_NAME"),
])
def test_a_malformed_value_is_not_echoed(tmp_path, owner, agent, key):
    with pytest.raises(install.InstallError) as caught:
        install.write_identity(tmp_path / ".env", owner, agent, dry_run=False)
    message = str(caught.value)
    assert key in message
    assert (owner or agent) not in message, message
