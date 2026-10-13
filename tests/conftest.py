"""Shared test setup.

Two things here, and both are important.

**NO TEST MAY WRITE TO THE REAL LOG DATABASE.** ``logs/meta-ads.db`` is the
live record of what this toolchain did to a production ad account. Several
tests call a CLI ``main()`` directly, and those entry points are instrumented,
so without this fixture a plain ``pytest`` run appends dozens of rows to the
operator's actual log -- quietly, and indistinguishably from real activity.

**NO TEST MAY READ THE REAL .env.** Importing anything that builds a client
reads the operator's credentials and, worse, could reach the live account if a
stub were ever imperfect. Every test gets a fake token and a fake account id
instead, set before ``Settings.from_env`` can see the real ones.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Obviously fake, and shaped like the real thing so the normalizers accept it.
FAKE_TOKEN = "TEST-TOKEN-NOT-A-REAL-CREDENTIAL"
FAKE_ACCOUNT_ID = "1234567890"

# --- AT IMPORT TIME, before any test module is collected --------------------
#
# The autouse fixture below is not early enough on its own. `metaads_mcp.server`
# builds both clients AT MODULE IMPORT, so a test file that imports it does so
# during COLLECTION -- before any fixture has run. Without these three lines
# that import reads the operator's real .env, and the suite either fails on a
# machine with no credentials or quietly points at the live account on one that
# has them. conftest is imported before every test module, which is what makes
# this the right place.
os.environ.setdefault("META_ADS_ACCESS_TOKEN", FAKE_TOKEN)
os.environ.setdefault("META_ADS_ACCOUNT_ID", FAKE_ACCOUNT_ID)

from metaads_reporting import config, logdb  # noqa: E402

#: Stops load_dotenv from ever reading the real .env over the top.
config._loaded = True


@pytest.fixture(autouse=True)
def _isolate_log_database(tmp_path):
    """Point logdb at a per-test database and put the real one back after."""
    original_dir, original_path = logdb.LOG_DIR, logdb.DB_PATH
    logdb.reset_for_tests(tmp_path / "logs" / "meta-ads.db")
    try:
        yield logdb.DB_PATH
    finally:
        logdb.LOG_DIR, logdb.DB_PATH = original_dir, original_path
        logdb.reset_for_tests()


@pytest.fixture(autouse=True)
def _fake_credentials(monkeypatch):
    """Fake credentials, and a .env that cannot be found.

    `load_env` is also neutralised: it would otherwise load the operator's real
    .env over the top on the first call in the process.
    """
    monkeypatch.setattr(config, "_loaded", True)
    monkeypatch.setenv("META_ADS_ACCESS_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("META_ADS_ACCOUNT_ID", FAKE_ACCOUNT_ID)
    for key in (
        "META_ADS_APP_SECRET",
        "META_ADS_BUSINESS_ID",
        "META_ADS_API_VERSION",
        "META_ADS_GRAPH_BASE",
        "META_ADS_OUTPUT_DIR",
        "META_ADS_MCP_READ_ONLY",
        "META_ADS_MCP_ALLOWED_ACCOUNT_IDS",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings():
    from metaads_reporting.config import Settings

    return Settings.from_env()


@pytest.fixture(autouse=True)
def _no_real_backoff_sleeps(monkeypatch):
    """Retry backoff sleeps for real seconds, which the suite does not need.

    The retry COUNT is what the tests assert on (see tests/test_graph.py); the
    wall-clock delay between attempts is the transport being polite to Meta,
    and there is no Meta here.
    """
    monkeypatch.setattr(
        "metaads_reporting.graph._sleep_backoff", lambda *args, **kwargs: None
    )
