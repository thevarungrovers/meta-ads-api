"""Credentials out of anything that is logged, printed or returned.

One list of secret names, used three ways: a params dict before it reaches the
log database (``graph._redact``), the text of an exception before it reaches the
log database or a terminal (``logdb``, ``graph``), and every message the agent
reads (``metaads_mcp.errors.redact``).

``input_token`` is on the list because ``debug_token`` takes the token under
inspection there -- the same long-lived token, under another name.
"""

from __future__ import annotations

import re
from typing import Any

SECRET_KEYS = frozenset(
    {"access_token", "appsecret_proof", "app_secret", "client_secret", "input_token"}
)

_SECRET = re.compile(
    r"(" + "|".join(sorted(SECRET_KEYS)) + r")=[^&\s'\"]+",
    re.IGNORECASE,
)


def redact_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """``params`` with every credential's value replaced."""
    return {
        key: ("<redacted>" if key in SECRET_KEYS else value)
        for key, value in (params or {}).items()
    }


def redact_text(text: str) -> str:
    """``text`` with every ``<secret>=<value>`` replaced.

    >>> redact_text("GET /v23.0/me?access_token=EAAB123&fields=id")
    'GET /v23.0/me?access_token=<redacted>&fields=id'
    """
    return _SECRET.sub(lambda m: f"{m.group(1)}=<redacted>", text or "")
