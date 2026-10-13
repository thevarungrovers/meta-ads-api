#!/usr/bin/env python3
"""The `meta-ads` MCP server: stdio, read tools plus guarded write tools.

Run it the way ~/.claude.json does -- absolute interpreter, absolute script:

    /Users/you/dev/meta-ads-api/.venv/bin/python \\
        /Users/you/dev/meta-ads-api/metaads_mcp/server.py

Never a bare `python3`, and not `-m`: the launching process's PATH and cwd are
not yours.

The server name matters more than it looks. Claude Code renders a permission
prompt as `meta-ads - apply_campaign_daily_budget`, and its permission rules
key on the TOOL NAME -- which is the whole reason previews and applies are
separate tools rather than one tool with a `dry_run` flag. You can permanently
allowlist every `preview_*` and never allowlist a single `apply_*`.
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

# Launched by absolute path, sys.path[0] is this package's directory, so the
# package itself is not importable until the repo root is on the path.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from metaads_mcp import __version__, tools_read, tools_write  # noqa: E402
from metaads_mcp.guardrails import Limits, SessionCounters, load_limits  # noqa: E402
from metaads_mcp.previews import PreviewStore  # noqa: E402
from metaads_reporting import logdb  # noqa: E402

SERVER_NAME = "meta-ads"

INSTRUCTIONS = """\
Meta Ads (Facebook and Instagram), live production account. Read freely; write
only through the preview/apply pairs below.

THIS IS THE REAL ACCOUNT. Campaigns are serving and spending today. Meta has no
sandbox worth pointing this at, so there is no rehearsal: a mistake costs money
now, not at the next deploy.

HOW TO CHANGE SOMETHING
  1. Read first. get_account tells you the CURRENCY and TIME ZONE, which decide
     what every number here means. find_campaigns / find_ad_sets / find_ads give
     you the ids, and run_report tells you what the entity has been doing.
  2. Call get_guardrails. It tells you the bounds and how much of this session's
     change budget is left, so you propose something that can be applied rather
     than something that will be refused.
  3. Call the matching preview_* tool. It returns a readable before/after, where
     the entity sits, its last 7 days, and an upper bound on the daily spend
     change.
  4. Show the human the preview, then call the apply_* tool with the
     preview_token it returned. The human approves at that prompt -- that
     approval IS the authorization, so never call apply_* without having just
     shown them what it does.

A preview_token is single-use, expires in 10 minutes, is not interchangeable
between tools, and is refused if the entity's current value has moved since the
preview was taken. That last one matters here: Ads Manager is usually open in a
browser tab while you are working.

MONEY IS ALWAYS A DECIMAL STRING in major units: "25.00" means twenty-five.
Meta's own API takes an integer in the currency's MINOR unit -- 2500 for the
same amount, and 25 on a zero-decimal currency like JPY -- and these tools do
that conversion. Passing 2500 here would be a hundredfold error, and the
argument is rendered verbatim in the prompt a human is approving.

TWO THINGS THAT ARE META-SPECIFIC AND WILL CATCH YOU OUT:

  * WHERE THE BUDGET LIVES. A campaign holds the budget (Advantage campaign
    budget) or its ad sets do, never both. find_campaigns reports budget_level
    for exactly this reason. Changing a budget at the wrong level is refused.
  * status VERSUS effective_status. `status` is what somebody set on that
    object. `effective_status` folds in the parents and ad review -- an ACTIVE
    ad inside a PAUSED campaign reads CAMPAIGN_PAUSED and is NOT delivering.
    Quote effective_status when you tell a human whether something is running.

ALSO WORTH SAYING OUT LOUD: reported conversions use 7-day-click plus
1-day-view attribution. The view half counts people who saw an ad and never
touched it, which is why Meta's number is routinely several times any
click-based record of the same campaign. They are not measuring the same thing.

WHAT IS NOT HERE, deliberately: creating or deleting campaigns, ad sets and
ads; setting any status to DELETED or ARCHIVED (both are irreversible through
the API -- pause instead, which is reversible and achieves the same thing);
swapping an ad's creative (a Meta creative is immutable, so that is a create);
targeting, optimization goal, billing event and schedule; anything touching
billing or account access. There is no generic update/passthrough tool, so if
an operation is not listed it cannot be performed here. Creating a campaign is
done by a human running `scripts/manage.py campaign new`, because it carries a
special-ad-category declaration someone has to answer for.

READ-ONLY INSTALLS. If get_guardrails reports read_only_server: true, this
process was started with META_ADS_MCP_READ_ONLY set and published no apply_*
tool and no revert_change at all -- they are absent from the tool list, not
refused at call time. The preview_* tools still work, so the useful thing to do
is show the human exactly what you would change and let them make it elsewhere.

list_my_changes shows what you have already done in this account;
revert_change undoes one by its entry_id.
"""


@dataclass
class ServerState:
    """Everything the tools share, built once per server process."""

    limits: Limits
    counters: SessionCounters
    previews: PreviewStore
    read_client: Any = None
    write_client: Any = None
    default_account_id: str = ""


def build_state() -> ServerState:
    limits = load_limits()
    return ServerState(
        limits=limits,
        counters=SessionCounters(limits),
        previews=PreviewStore(),
    )


# Built once at import, and the lifespan hands out THIS object rather than
# building a second one: two SessionCounters would mean two sets of counters
# and a session budget silently twice what it claims to be.
STATE = build_state()


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[ServerState]:
    yield STATE


mcp = MCPServer(
    name=SERVER_NAME,
    version=__version__,
    instructions=INSTRUCTIONS,
    lifespan=lifespan,
)

# Registered through the instrumented wrapper so every tool call lands in
# logs/meta-ads.db. `mcp` itself is what gets run; the wrapper only intercepts
# the decorator, so the server object the SDK sees is unchanged.
_registrar = logdb.InstrumentedServer(mcp)
tools_read.register(_registrar, STATE)
tools_write.register(_registrar, STATE)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
