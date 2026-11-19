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
from dataclasses import dataclass, field
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
from metaads_reporting.naming import Identity  # noqa: E402

SERVER_NAME = "meta-ads"

INSTRUCTIONS = """\
Meta Ads (Facebook and Instagram), live production account. Read freely; write
only through the preview/apply pairs.

THIS IS THE REAL ACCOUNT. Campaigns are serving and spending today. Meta has no
sandbox worth pointing this at, so there is no rehearsal: a mistake costs money
now, not at the next deploy.

HOW TO CHANGE SOMETHING
  1. Read first. get_account tells you the CURRENCY and TIME ZONE. find_* give
     ids; get_campaign / get_ad_set / get_ad give one entity in full (the
     whole targeting spec, special ad categories, review state); run_report
     tells you what it has been doing.
  2. Call get_guardrails: the bounds, this session's budget and TODAY's (the
     daily cap is read from the ledger and survives restarts), token health.
  3. Call the matching preview_* tool. It returns before/after, where the entity
     sits, its last 7 days, an upper bound on the daily spend change, every
     check (OK / FAIL / WARN / SKIP), Meta's own validate_only verdict, and for
     an ad change, preview links per placement.
  4. Show the human the preview, then call the apply_* tool with the
     preview_token it returned. The human approves at that prompt -- that
     approval IS the authorization, so never call apply_* without having just
     shown them what it does.

A preview_token is single-use, expires in 10 minutes, is not interchangeable
between tools, and is refused if the entity has moved since the preview. Ads
Manager is usually open in a browser tab; list_recent_changes shows who moved it.

MONEY IS ALWAYS A DECIMAL STRING in major units: "25.00" means twenty-five.
Meta's own API takes an integer in the currency's MINOR unit -- 2500 for the
same amount -- and these tools do that conversion. Passing 2500 here would be a
hundredfold error, and the argument is rendered verbatim in the approval prompt.

THREE META-SPECIFIC TRAPS:
  * WHERE THE BUDGET LIVES. A campaign holds the budget (CBO) or its ad sets do,
    never both; find_campaigns reports budget_level. The wrong level is refused.
  * status VERSUS effective_status. effective_status folds in the parents and
    ad review. Quote it when you say whether something is running.
  * A CREATIVE IS IMMUTABLE. Changing what an ad says (copy, media, url_tags,
    Advantage+ options, a lead form) builds a NEW creative: duplicate_ad puts it
    on a new PAUSED ad; update_ad_copy and the other creative kinds repoint the
    existing ad, which discards its social proof and re-enters review.

Reported conversions use 7-day-click plus 1-day-view attribution, so Meta's
number is routinely several times any click-based record of the same campaign.

EVERYTHING IS CREATED PAUSED -- campaigns, ad sets, ads, copies -- and costs 3
applies against the caps. Activating is a separate status change the human
approves. An activation is counted against the DAILY total (and the session
total) and is exempt from the per-change limit. Nothing is ever DELETED or
ARCHIVED; reverting a create pauses it. Audiences are never deleted: an unused
one changes nothing, and detaching is the undo for attaching.
preview_create_campaign's special_ad_categories is a legal declaration with no
default: ask the human.

NAMING. Everything you create is named <OWNER>-<agent>-<base>, from the repo's
.env (each part only when set; neither set: the name as given). Pass the BASE
name only: the prefix is added, and another agent's or the CLI's prefix is
refused. The preview's `naming` block is the exact final name. Only a
creative's internal name is stamped, never its headline. With initials only,
a base like spring-promo is refused (it would read as agent "spring"): use _
or a space. Copies take your prefix on the source's base, any other prefix
stripped; if a copy's follow-up rename fails, the result's `warnings` and
`follow_ups[].live_name` give the real name: report that. You may rename ONLY
your own entities (your prefix, or the ledger shows you made it and the name
carries no other prefix): a manual entity or another agent's is refused, and
renames keep the prefix. Names over 400 characters are
refused, never truncated. find_* (and list_audiences, list_labels) filter by
origin / agent_name / owner / mine; get_guardrails shows the identity; a
malformed identity refuses creating until .env is fixed.

AD VERIFICATION. Every ad-changing apply returns `verification`: the live ad
re-read and diffed against what was approved, its review state, and fresh
preview links. passed=false reverts nothing; say what differs. Review usually
completes within a day: call verify_ad later. Preview links are signed and
short-lived; open them with a browser tool if you have one, otherwise give them
to the human, and never claim to have seen a rendering you did not open.

ERRORS are prefixed with their class: AUTH_EXPIRED, PERMISSION, RATE_LIMITED
retry_after=<s> (wait, then retry once), NOT_FOUND, POLICY, INVALID,
TOO_MUCH_DATA, TRANSIENT, INTERNAL.

WHAT THIS SERVER CANNOT DO:
  * delete or archive anything, audiences included;
  * create a customer-list audience -- it uploads customer personal data and
    stays in Ads Manager, where a person accepts the data terms;
  * create a lead form (a person owns its privacy policy and legal text), or
    download leads;
  * switch a campaign between campaign and ad-set budgets;
  * create automated rules that change money;
  * configure pixels or CAPI, edit catalogs, or touch billing or account access;
  * upload a new video (reuse an existing video_id).

If the human needs one of those, say so plainly and point them at Ads Manager.
scripts/manage.py has a smaller write surface than this server, so do not send
them to a script for it.

There is no generic update/passthrough tool, so if an operation is not listed it
cannot be performed here. low_risk_change edits names and ad labels only, on a
closed allowlist.

READ-ONLY INSTALLS. If get_guardrails reports read_only_server: true, this
process was started with META_ADS_MCP_READ_ONLY set and published no apply_*
tool and no revert_change at all -- they are absent from the tool list, not
refused at call time. The preview_* tools still work, so the useful thing to do
is show the human exactly what you would change and let them make it elsewhere.

list_my_changes shows what you have already done in this account;
reconcile_ledger checks it against the live account; revert_change undoes one
by its entry_id (or a whole batch by its batch_id).
"""


@dataclass
class ServerState:
    """Everything the tools share, built once per server process."""

    limits: Limits
    counters: SessionCounters
    previews: PreviewStore
    read_client: Any = None
    write_client: Any = None
    engine: Any = None
    headroom: Any = None
    audience_usage: Any = None
    default_account_id: str = ""
    #: Who names what this server creates: from the repo's .env ONLY (see
    #: metaads_reporting.naming). Read once, at startup.
    identity: Identity = field(default_factory=Identity.from_env)


def build_state() -> ServerState:
    limits = load_limits()
    return ServerState(
        limits=limits,
        counters=SessionCounters(limits),
        previews=PreviewStore(),
        identity=Identity.from_env(),
    )


def identity_warnings(state: ServerState) -> list[str]:
    """What is worth saying about the naming identity at startup."""
    identity = state.identity
    out = list(identity.warnings)
    if identity.problem:
        out.append(f"naming identity refused ({identity.problem}); creating and renaming "
                   "are refused at preview until it is fixed")
    return out


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


# Kept importable from here: the check now runs lazily, in get_guardrails.
from metaads_mcp.budget_check import budget_cap_warnings  # noqa: E402,F401


def main() -> None:
    for warning in identity_warnings(STATE):
        print(f"meta-ads: WARNING {warning}", file=sys.stderr)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
