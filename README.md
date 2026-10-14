# Meta Ads API

Read a Meta (Facebook and Instagram) ad account, and change it carefully — from
a terminal, or from an AI agent that has to show you what it is about to do
before it does it.

Talks to the **Marketing API** over plain HTTP. No `facebook-business` SDK, on
purpose: see [Why this is careful](#why-this-is-careful).

> **This points at a real account.** Meta has no sandbox worth using for
> campaign management, so every write is the real thing. Everything below is
> built around that one fact.

## The big idea, in one picture

```
  You, or your agent
         |
         v
  +---------------------+        +----------------------------+
  |  preview_<thing>    | -----> |  a readable before/after,  |
  |  (reads, writes     |        |  the last 7 days, the      |
  |   nothing)          |        |  projected spend change,   |
  +---------------------+        |  and a preview_token       |
                                 +----------------------------+
                                             |
                                 you read it, and approve
                                             |
                                             v
                                 +----------------------------+
                                 |  apply_<thing>(token)      |
                                 |  - kill switch re-checked  |
                                 |  - value re-checked for    |
                                 |    changes made elsewhere  |
                                 |  - session budget checked  |
                                 |  - written to the ledger    |
                                 +----------------------------+
```

An `apply_*` takes **only** the token. It cannot be called cold.

## Why this is careful

| Risk | What stops it |
|---|---|
| An agent changes something without asking | `preview_*` and `apply_*` are **separate tools**, so your client's permission list can allow every preview and no apply |
| An approval gets reused | Tokens are single-use, expire in 10 minutes, and are not interchangeable |
| Somebody edits in Ads Manager mid-flow | The recorded `before` is re-checked at apply time |
| A loop makes 400 individually-legal changes | Per-**session** caps, not just per-call |
| A budget typed in the wrong unit | Amounts are decimal strings in your currency; minor units are detected and refused |
| Something irreversible | No delete anywhere, and `DELETED`/`ARCHIVED` cannot be set |
| You need to stop everything, now | `touch audit/WRITES_DISABLED` |
| The reporting code writing by accident | It physically cannot send anything but `GET` |

## Install it

```bash
# 1. Get the code and its dependencies
git clone <this repo> meta-ads-api && cd meta-ads-api
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 2. Fill in your credentials
cp .env.example .env && chmod 600 .env
$EDITOR .env

# 3. Check the token is the right kind
./.venv/bin/python scripts/token_info.py

# 4. Check all of it works
./.venv/bin/python scripts/test_connection.py

# 5. Add it to your AI agent
python3 install.py
```

### What you need to collect

**Use a System User token. Not a personal one.** This is the single most
important decision in the setup.

A personal user token is invalidated the moment its owner changes their Facebook
password — error code 190, subcode 460 — and the symptom is every automation
failing at once, at 3am, with a message about a password that nobody will
connect to this repo. A System User has no password to change.

```
Business Settings > Users > System Users > Add
  > Assign Assets: your ad account
      - "Manage campaigns" if you want writes
      - "View performance" if you only want reports
  > Generate New Token > pick your app > select scopes
```

| Scope | Needed for |
|---|---|
| `ads_read` | Reports: `run_report`, `scripts/fetch_report.py` |
| `ads_management` | Every `preview_*`/`apply_*` pair, and `scripts/manage.py` |
| `business_management` | Listing which ad accounts the token can reach |

**Two halves, both required.** The token needs the scope **and** the System User
must be assigned to the ad account. Either one alone produces exactly the same
permission error, so check both before debugging either.

Optionally set `META_ADS_APP_SECRET` too. With it, every request carries an
`appsecret_proof`, so a stolen token on its own is not enough to use the API.

### Adding it to an agent

`install.py` is stdlib-only and runs on `python3`, so it works before you have
made a venv. It writes:

| Agent | File |
|---|---|
| Claude Code | `~/.claude.json`, and `~/.claude/settings.json` for permissions |
| Claude Desktop | `claude_desktop_config.json` |
| Cursor | `~/.cursor/mcp.json` |
| Codex | `~/.codex/config.toml` |

Every file is backed up first, every edit is a merge, and running it twice is a
no-op.

It asks whether you want **basic permissions only**, and that answer means
something slightly different per client:

- **Claude Code** has a per-tool allowlist, so the 14 safe tools are
  pre-approved and the 7 that change the account still stop and ask you. That is
  strictly better, so Claude Code always gets the full tool set with an
  allowlist.
- **Everyone else** has no per-tool permissions, so the limit is applied in the
  server: it is started with `META_ADS_MCP_READ_ONLY=1` and publishes **no**
  `apply_*` tools at all. They are absent from the tool list, not refused at call
  time.

```bash
python3 install.py --agent cursor --read-only
python3 install.py --agent claude-code --no-read-only --yes
python3 install.py --agent codex --dry-run
```

## Three things about Meta that will bite you

### 1. Money is an integer in the currency's minor unit

`daily_budget: 5000` on a USD account is **fifty dollars**. So are
`lifetime_budget`, `bid_amount`, `budget_remaining`, `spend_cap` and
`amount_spent`.

Worse than Google's micros for two reasons: there is no `_micros` suffix to warn
you, and **the divisor is not constant**. It is 100 for most currencies and **1**
for zero-decimal ones — on a JPY account `5000` is five thousand yen, and
dividing by 100 renders it as "50.00" in the text you are about to approve.

And insights go the *other* way: `spend`, `cpc`, `cpm` come back as decimal
strings **already in your currency**.

This package never guesses. `metaads_reporting/currency.py` holds the ISO 4217
exponent for every currency Meta supports, and an unknown one is **refused**
rather than assumed — a wrong exponent is silent, and silence here is a
hundredfold error in an approval prompt.

**Everything you type is in major units.** `"25.00"` means twenty-five.

### 2. The budget lives at one level or the other

A campaign holds the budget (Advantage campaign budget, "CBO") **or** its ad sets
do. Never both.

`find_campaigns` reports `budget_level` for this reason, and changing a budget at
the wrong level is refused with an explanation. Setting one on a campaign that
does not hold it would convert it to CBO and redistribute spend across every ad
set underneath.

### 3. `status` is not `effective_status`

`status` is what somebody set on that object. `effective_status` folds in the
parents and ad review. An `ACTIVE` ad inside a `PAUSED` campaign reads
`CAMPAIGN_PAUSED` and **is not delivering**.

Every preview reports both.

## Reading

```bash
# What is available
./.venv/bin/python scripts/fetch_report.py --help

# Which accounts this token can reach
./.venv/bin/python scripts/fetch_report.py accounts

# Last 30 days of campaign performance
./.venv/bin/python scripts/fetch_report.py campaigns --days 30

# One row per ad over 14 days, with frequency — the fatigue view
./.venv/bin/python scripts/fetch_report.py creative_fatigue --days 14

# A fixed window, narrowed to one campaign, written to output/
./.venv/bin/python scripts/fetch_report.py adsets \
    --since 2026-09-01 --until 2026-09-30 --campaign "retargeting" --csv

# See the request parameters without calling the API
./.venv/bin/python scripts/fetch_report.py campaigns --query
```

Five reports: `account`, `campaigns`, `adsets`, `ads`, `creative_fatigue`.

`creative_fatigue` is deliberately **not** daily. Frequency is impressions over
*reach*, and reach cannot be summed across days — the same person reached on
Monday and Tuesday is one person. A daily frequency column looks flat and
reassuring on exactly the ad that is burning out, so that report collapses the
whole window into one row per ad.

### About the conversion numbers

Reports ask for **7-day click plus 1-day view** attribution, explicitly, rather
than inheriting whatever the account is set to. The 1-day-view half counts people
who *saw* an ad and never touched it.

That is why Meta's conversion count is routinely several times larger than any
click-based record of the same campaign. They are not measuring the same thing.
If you want to reconcile against a UTM-tagged table, ask for `7d_click` alone.

Dates are evaluated in the **ad account's** time zone, not yours.

## Changing things yourself

```bash
# Find it first
./.venv/bin/python scripts/manage.py find campaign "retargeting"

# Validate against Meta and stop. Changes nothing
./.venv/bin/python scripts/manage.py campaign pause --name "Brand - EN" --dry-run

# The same, then a typed confirmation before it applies
./.venv/bin/python scripts/manage.py adset set-budget --id 123456 --amount 50.00

# A bid cap (only on ad sets whose bid_strategy actually uses one)
./.venv/bin/python scripts/manage.py adset set-bid --id 123456 --amount 3.50

# Pause one ad
./.venv/bin/python scripts/manage.py ad pause --id 789012
```

`--dry-run` is not a local guess. It sends the request to Meta with
`execution_options=["validate_only"]`, so the full server-side check runs and
nothing changes.

Entities are addressable **by name**: exact match wins, then case-insensitive
substring, and ambiguity is refused with the candidates listed rather than
resolved for you.

## What the agent can do

21 tools: 8 read, 6 `preview_*`/`apply_*` pairs, and `revert_change`.

### Can read (safe)

| Tool | Gives you |
|---|---|
| `list_ad_accounts` | Accounts this token can reach |
| `get_account` | Currency, time zone, status, spend cap, balance |
| `find_campaigns` | Campaigns, ids, `budget_level`, budgets |
| `find_ad_sets` | Ad sets, budgets, `bid_strategy`, `bid_amount` |
| `find_ads` | Ads and their `effective_status` |
| `run_report` | Performance, five reports |
| `get_guardrails` | The bounds and the session budget left |
| `list_my_changes` | What it has changed, with `entry_id`s |

### Can change (needs your yes)

| Pair | Changes |
|---|---|
| `preview/apply_campaign_status` | Pause or activate a whole campaign |
| `preview/apply_campaign_daily_budget` | A campaign-budget campaign's daily budget |
| `preview/apply_ad_set_status` | Pause or activate an ad set |
| `preview/apply_ad_set_daily_budget` | An ad set's daily budget |
| `preview/apply_ad_set_bid_amount` | An ad set's bid cap or cost target |
| `preview/apply_ad_status` | Pause or activate one ad |
| `revert_change` | Undo one ledger entry |

### Cannot do at all — including from the CLI

**This toolchain changes the status, budget, bid and name of things that already
exist. That is its entire write surface.** Nothing in it — not the MCP server,
not `scripts/manage.py` — can:

| Not available | Why it is not a small addition |
|---|---|
| Create a campaign, ad set or ad | A campaign carries a `special_ad_categories` declaration, which is a legal statement about housing/employment/credit/social-issue advertising |
| Write or edit ad copy (primary text, headline, description) | A Meta creative is **immutable once attached**, so this is a create, not an edit — and repointing a live ad at a new creative resets its social proof |
| Upload or swap an image or video | Media goes to the `adimages`/`advideos` edges first, then into a creative |
| Create or edit an audience (custom, lookalike, saved) | A custom audience means uploading hashed customer PII under Meta's data terms |
| Change targeting, optimization goal, billing event, schedule | Not on the write client's field allowlist |
| Set a status to `DELETED` or `ARCHIVED` | Irreversible through the API — pause instead |
| Anything touching billing or account access | Out of scope by design |

None of that is "refused at call time" — there is **no generic
update/passthrough tool**, and the write client's field allowlist contains only
`status`, `name`, `daily_budget`, `lifetime_budget` and `bid_amount`. An
unregistered operation is unreachable, not merely undocumented.
`tests/test_mcp_surface.py` pins the tool names so adding one is a reviewed diff.

For any of the above, use Ads Manager.

## What a preview tells you

```
entity_name                  Brand - Prospecting
path                         campaign 120200000000001 > Broad 25-54
field_name                   daily_budget
before                       50.00 CAD
after                        75.00 CAD
currency                     CAD
status                       ACTIVE
effective_status             ACTIVE
metrics_7d                   spend 312.40, clicks 840, frequency 1.9, ctr 2.1
projected_daily_spend_delta  25.00
checks                       OK   writes_enabled: kill switch is not set
                             OK   account_in_scope: 1234567890 is in scope
                             OK   currency_allowed: CAD against CAD
                             OK   max_daily_budget: 75.00 CAD against 500.00 CAD
                             FAIL max_budget_change_pct: +50.0% against +40%
blocked                      true
warnings                     more than doubling a budget re-enters the learning
                             phase on most optimization goals
```

`blocked: true` means the apply will refuse. The `FAIL` line says which bound.

Three asymmetries the preview spells out:

- **Pausing** cannot increase spend. Projected delta `0`.
- **Activating** is bounded by the budget, not past spend — something that spent
  nothing last week can spend its full budget tomorrow.
- **Pausing one ad** is `0` in both directions. The ad set's budget is the cap
  and it does not move; what changes is which creative spends it.

## Safety net

### 1. Limits per change

`guardrails.toml`, committed on purpose — raising a limit is a visible diff with
an author and a date, not an env var somebody exported once.

```toml
[limits]
max_daily_budget            = "500.00"
max_budget_change_pct       = 50.0
max_bid_amount              = "50.00"
max_bid_increase_pct        = 50.0
max_entities_per_call       = 25
max_projected_delta_per_day = "200.00"
```

A key you delete falls back to the code default. A key you **misspell is an
error at startup**, not a silently ignored line — a ceiling that is quietly not
there reads as protection.

### 2. Limits per conversation

```toml
max_applies_per_session     = 20
max_projected_session_delta = "500.00"
```

These matter more. A per-call limit does nothing against a loop making four
hundred individually-legal changes. Counters live in memory and reset only when
the server restarts — which is a deliberate act.

### 3. Scope

```toml
[scope]
allowed_account_ids  = []        # empty = only META_ADS_ACCOUNT_ID
allowed_campaign_ids = []        # empty = every campaign in that account
allowed_currencies   = ["CAD"]
```

One System User token routinely reaches every account a business owns, so the
default is "the one account in `.env`, and nothing else". Pin
`allowed_campaign_ids` to one campaign for the first week, then widen.

`META_ADS_MCP_ALLOWED_ACCOUNT_IDS` can **narrow** this further but never widen
it — a machine-local override that could add accounts would defeat the committed
file.

### 4. The stop button

```bash
touch audit/WRITES_DISABLED              # halts every apply within one tool call
echo "incident 4821" > audit/WRITES_DISABLED   # the reason is shown in the refusal
rm audit/WRITES_DISABLED                 # back on
```

A **file**, not an env var, because a long-lived stdio server never sees a new
env var. It is re-read at apply time and never cached, so a preview taken ten
minutes ago still stops.

## Writes are rate limited to one per 30 seconds

Meta caps writes on a campaign node at **1 call per 30 seconds** (error code
613, subcode 4841018). Found the hard way: an apply succeeded, the revert issued
a second later was refused, and the account sat in the changed state.

The transport now distinguishes two kinds of write failure, because they are not
the same risk:

| Failure | Retried? | Why |
|---|---|---|
| Rate limit (613, 4, 17, 32, 429, 80xxx) | **Yes**, up to 3 times | Meta refused it outright. Nothing was applied, so repeating it is safe |
| Timeout / connection error | **Never** | The request may have reached Meta with the response lost. A retry makes a second change |
| HTTP 5xx | **Never, on a write** | Same ambiguity |
| Anything permanent (100, 200, …) | No | Repeating it just fails again |

The rate-limit wait starts **above** the 30-second window — an exponential
backoff starting at one second would spend every retry inside the same window
and fail three more times for nothing. Reads are unaffected and still retry
everything transient.

**So an `apply_*` can take ~30 seconds to return.** That is the retry working.
It also means batching writes is slow by construction: five changes is five
windows.

## Everything is written down

`logs/meta-ads.db` — SQLite, gitignored, four tables.

| Table | What it holds |
|---|---|
| `tool_calls` | One row per MCP tool invocation or CLI command |
| `api_calls` | One row per request to Meta, linked to the tool call that caused it |
| `changes` | What the agent changed, with the before-value needed to reverse it |
| `mutations` | Every write the library sent, including the `validate_only` passes |

Two phases per change: the intent goes in **before** the request leaves the
process, the outcome after it returns.

**A row with an intent and no outcome is the one to look at.** The write left the
process and nothing recorded how it ended, so it may have landed at Meta's end.
That is not a rare crash artefact here — the write path deliberately does **not**
retry, because a POST that timed out may already have applied and nothing on this
side can tell the difference. Reads retry; writes do not.

```bash
sqlite3 logs/meta-ads.db \
  "SELECT at, tool, entity_name, field, before, after, ok FROM changes ORDER BY at DESC LIMIT 10;"
```

### Undoing

```
revert_change(entry_id="a1b2c3")
```

Refused if the entry was never applied, if it has already been reverted, or if
the value has moved since — in that last case somebody changed it in Ads Manager
after you did, and reverting would discard *their* change while the ledger
recorded it as a tidy undo of yours.

A revert is itself a change and is itself recorded, so the ledger stays a
history rather than a list of things that are still true.

## How reading and writing are kept apart

On the Graph API the **HTTP method is the boundary**: `GET` reads, `POST` creates
and updates, `DELETE` deletes. So "can this file change the account?" reduces to
"can this file name a write method?" — a question the AST can answer.

That is the whole reason this repo does not use the `facebook-business` SDK. The
SDK hangs `.api_update()` and `.remote_delete()` off every object it returns, so
a write would be one attribute access away from every read, with nothing to grep
for.

1. `ReadOnlyMetaAdsClient` routes every request through `assert_read_method`,
   which admits `GET` and `HEAD` and nothing else. It exposes no method that
   takes a body.
2. `MutatingMetaAdsClient` lives in a separate subpackage, behind a separate
   import. It has a **field allowlist** per entity type — Meta's update endpoint
   takes the whole node, so which fields this code may touch is answered
   explicitly. It has no delete method at all.
3. `tests/test_readonly_guard.py` AST-parses every file outside that subpackage
   and fails on a write method named in code or a `.post()`/`.delete()` call —
   and separately asserts the write package **does** contain a write, so the
   guard cannot pass by guarding nothing.

## Tests

```bash
./.venv/bin/python -m pytest -q
```

Everything is offline. `tests/graphsim.py` is a small in-memory Graph API that
keeps an object store and really applies a POST, so `preview → apply → read it
back` is a real round trip — which is what makes the staleness assertions mean
anything.

Two things every test gets automatically, from `tests/conftest.py`:

- a **scratch log database**, so a test run never appends to the real record of
  what this toolchain did to a production account;
- **fake credentials**, set at conftest *import* time rather than in a fixture —
  `metaads_mcp.server` builds its clients at module import, which happens during
  collection, before any fixture has run.

## Security

- The token never reaches the log database, a traceback, or a tool result. It is
  redacted at the transport boundary, and errors name the URL **path** only —
  never the query string, which is where it is.
- `.env` is gitignored three ways, and `scripts/test_connection.py` warns if it
  is group- or world-readable.
- `logs/` and `audit/` are gitignored: they carry real account ids and campaign
  names.
- Writes go in the form body, not the query string, so a budget is not logged by
  every proxy in between.
- `tests/test_repo_hygiene.py` checks tracked files for token-shaped literals,
  non-example hostnames and real-looking object ids.

## When something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `code 190, subcode 460` | The token belonged to a person who changed their password | Generate a **System User** token |
| `code 190, subcode 463` | The token expired | Generate a new one; `token_info.py --check` warns in advance |
| `code 200` or `code 10` | Missing scope **or** the System User is not assigned to the account | Check both — they give the same error |
| `code 100, subcode 33` | The object does not exist, *or* the token cannot see it | Meta does not distinguish these. Check the id and the assignment |
| `code 4`, `17`, `32`, `613`, `80xxx` | Rate limit | Reads retry automatically. `get_account` reports headroom |
| Server "connected" but tools missing | You registered it mid-session | Restart the client; the tool list is read once, at startup |
| A budget looks 100x wrong | Minor units | You are reading a raw node field. `currency.from_minor_units` is the converter |
| Every number is one day out | Account time zone | Report dates are evaluated in the account's zone. `get_account` shows it |

Start with `./.venv/bin/python scripts/test_connection.py`. The rungs are
ordered so the **first** failure names the real problem.

## Layout

```
metaads_reporting/        the library
  graph.py                the one HTTP path; where the read/write boundary lives
  client.py               ReadOnlyMetaAdsClient — GET and HEAD only
  currency.py             minor units, and the refusal to guess an exponent
  fields.py               which fields are money, and in which direction
  insights.py             building an /insights request
  account_id.py           the act_ prefix, in exactly one place
  config.py               .env, with blank treated as absent
  export.py               DataFrames and CSV
  logdb.py                the SQLite log: tool calls, API calls, changes, mutations
  reports/                five declarative report definitions
  write/                  THE ONLY PLACE THAT CAN MUTATE
    client.py             field allowlist, validate_only, no delete
    campaigns.py adsets.py ads.py   plan builders
    lookup.py plan.py audit.py

metaads_mcp/              the MCP server
  server.py guardrails.py previews.py ledger.py tools_read.py tools_write.py

scripts/
  test_connection.py      the diagnostic ladder
  token_info.py           what the token is, and when it dies
  fetch_report.py         reports
  manage.py               changes, with a human driving

guardrails.toml           committed limits
install.py                stdlib-only installer for four agents
.claude/skills/meta-ads/  how an agent should drive it
```

## Licence

MIT. See `LICENSE`.
