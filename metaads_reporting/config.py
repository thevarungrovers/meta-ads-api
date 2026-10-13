"""Configuration, loaded from a single ``.env`` file.

Every setting is read through :func:`env`, which treats a key that is *present
but blank* exactly like a key that is *absent*.

That distinction is the whole reason this module exists. ``.env.example`` ships
every optional key spelled out with an empty value so the template documents
itself -- but ``os.getenv(name, default)`` returns ``default`` only when the key
is **absent**. ``load_dotenv()`` turns ``META_ADS_API_VERSION=`` into the empty
string, so the key *is* present and the default is never reached. Copying the
template would silently override every default in this package with "".
So: never call ``os.getenv`` directly for a value that has a default. Call
:func:`env`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .account_id import normalize_account_id

#: Graph API version this code targets.
#:
#: Meta ships roughly three versions a year and supports each for about two
#: years. Pinning matters more here than it looks: an UNVERSIONED request is
#: served by the OLDEST version still supported, so leaving it off does not mean
#: "latest", it means "whatever is about to be switched off". Bump this
#: deliberately, then re-run scripts/test_connection.py -- rung 4 reports the
#: version the API actually answered on, which is how a bad pin surfaces.
DEFAULT_API_VERSION = "v23.0"

DEFAULT_GRAPH_BASE = "https://graph.facebook.com"
DEFAULT_OUTPUT_DIR = "output"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


_loaded = False


def load_env(path: Path | None = None, *, force: bool = False) -> None:
    """Load ``.env`` into ``os.environ`` once per process.

    ``load_dotenv`` does not override variables already in the environment, so
    a value exported in the shell (or pre-seeded by a test) always wins.
    """
    global _loaded
    if _loaded and not force:
        return
    load_dotenv(dotenv_path=path or ENV_PATH, override=False)
    _loaded = True


def env(name: str, default: str = "") -> str:
    """Return the setting ``name``, treating blank as unset.

    This is the only correct way to read a setting that has a default.
    """
    value = os.environ.get(name) or ""
    value = value.strip()
    return value if value else default


def require(name: str, *, hint: str = "") -> str:
    """Return a required setting, or raise :class:`ConfigError`.

    The error names the missing key only -- never a value, not even a partial
    one.
    """
    value = env(name)
    if not value:
        message = f"{name} is not set in {ENV_PATH.name} (or is blank)."
        if hint:
            message += f" {hint}"
        raise ConfigError(message)
    return value


def int_env(name: str, default: int) -> int:
    raw = env(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc


def bool_env(name: str, default: bool) -> bool:
    raw = env(name, "true" if default else "false").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}.")


def normalize_api_version(value: str) -> str:
    """Accept ``23.0`` or ``v23.0`` and return the ``v``-prefixed form.

    The path segment needs the ``v``; people copying a version out of the
    changelog routinely do not have it, and ``/23.0/act_1/campaigns`` 404s in a
    way that reads like a missing account rather than a missing prefix.

    >>> normalize_api_version("23.0")
    'v23.0'
    >>> normalize_api_version("v23.0")
    'v23.0'
    """
    text = (value or "").strip()
    if not text:
        return DEFAULT_API_VERSION
    if not text.startswith("v"):
        text = "v" + text
    return text


@dataclass(frozen=True)
class Settings:
    """Everything this package needs in order to talk to the Marketing API.

    ``access_token`` is a secret. Nothing in this package prints, logs or
    serialises it; see :meth:`describe`.
    """

    access_token: str
    account_id: str | None
    business_id: str | None
    app_secret: str | None
    api_version: str
    graph_base: str
    output_dir: Path

    @classmethod
    def from_env(cls, *, path: Path | None = None) -> "Settings":
        load_env(path)

        account_id = env("META_ADS_ACCOUNT_ID")
        output_dir = Path(env("META_ADS_OUTPUT_DIR", DEFAULT_OUTPUT_DIR))
        if not output_dir.is_absolute():
            output_dir = PROJECT_ROOT / output_dir

        return cls(
            access_token=require(
                "META_ADS_ACCESS_TOKEN",
                hint=(
                    "Use a SYSTEM USER token from Business Settings, not a user "
                    "token: a user token dies when that person changes their "
                    "password. See scripts/token_info.py."
                ),
            ),
            account_id=(
                normalize_account_id(account_id) if account_id else None
            ),
            business_id=env("META_ADS_BUSINESS_ID") or None,
            # Optional, and worth setting. With it, every request carries an
            # appsecret_proof, so a stolen token alone is not enough to use the
            # API -- see metaads_reporting.graph.
            app_secret=env("META_ADS_APP_SECRET") or None,
            api_version=normalize_api_version(env("META_ADS_API_VERSION")),
            graph_base=env("META_ADS_GRAPH_BASE", DEFAULT_GRAPH_BASE).rstrip("/"),
            output_dir=output_dir,
        )

    def describe(self) -> dict[str, str]:
        """A diagnostic summary that is safe to print.

        The token is reported as presence and length only -- no characters, not
        even a prefix. Account IDs are not secret and are shown in full.
        """
        def secret(value: str | None) -> str:
            return f"present ({len(value)} chars)" if value else "MISSING"

        return {
            "access_token": secret(self.access_token),
            "app_secret": (
                secret(self.app_secret)
                if self.app_secret
                else "(not set — appsecret_proof will not be sent)"
            ),
            "account_id": self.account_id or "(not set)",
            "business_id": self.business_id or "(not set)",
            "api_version": self.api_version,
            "graph_base": self.graph_base,
            "output_dir": str(self.output_dir),
        }

    def resolve_account_id(self, override: str | None = None) -> str:
        """Pick the account to work on: ``override`` first, else the default."""
        if override:
            return normalize_account_id(override)
        if self.account_id:
            return self.account_id
        raise ConfigError(
            "No ad account ID given. Pass --account-id, or set "
            "META_ADS_ACCOUNT_ID in .env."
        )
