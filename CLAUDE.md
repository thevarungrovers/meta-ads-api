# CLAUDE.md

Guidance for agents working on this repository: a Python library for the Meta
Marketing API (`metaads_reporting/`) and the `meta-ads` MCP server built on it
(`metaads_mcp/`), which lets an AI agent read a live ad account and change it
through guarded preview/apply pairs.

## Running things

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pytest                          # the whole suite; offline, no credentials used
.venv/bin/python metaads_mcp/server.py    # the MCP server (stdio)
```

- Graph API version is pinned in `metaads_reporting/config.py` (`v23.0`). The
  repo talks to the Graph API directly over `requests`, not through the
  `facebook-business` SDK: the HTTP method is the read/write boundary.
- The suite is green at every commit. Tests never touch the network, the real
  `.env` or the real log database (`tests/conftest.py`); `tests/graphsim.py` is
  an in-memory Graph API that really applies a POST.

## Rules that are not style

- **The account is live.** Never call an `apply_*` tool, `revert_change`, or a
  script that writes, to "check something". Read tools and previews only;
  previews send Meta's `validate_only`, which changes nothing.
- **Money is a decimal string in major units** wherever an agent or a human
  sees it: `"25.00"`, never minor units, never a float. Convert at the edge with
  `metaads_reporting.currency` (Meta's offsets, not ISO's: some currencies are
  offset 1).
- **Nothing is DELETED or ARCHIVED.** The mutating client has no delete method;
  `check_status` refuses both statuses. Pausing is the undo. Audiences are never
  deleted -- the undo for attaching one is detaching it.
- **Created things start PAUSED** (`write/client.py:check_create_paused`
  enforces it below every tool) and cost 3 applies.
- **Customer-list audiences and lead-form creation stay human-only.** Do not
  add a path to either.
- **Only `metaads_reporting/write/` may mutate.** `tests/test_readonly_guard.py`
  scans every other file for a write method; `write/async_insights.py` is the
  one read-side POST, to `<node>/insights` only.
- **`WRITABLE_FIELDS`** (`write/client.py`) is pinned as a whole set in
  `tests/test_repo_hygiene.py`. Widening it is a deliberate, reviewed edit.
- **Every tool declares all four ToolAnnotations hints**, and the tool surface
  is pinned in `tests/test_mcp_surface.py` and `install.py` (SAFE_TOOLS /
  WRITE_TOOLS); the README and the skill quote the tool count. Adding a tool
  means editing all of them.
- **Every failure reaches the agent** via `metaads_mcp/errors.py`, prefixed
  AUTH_EXPIRED / PERMISSION / RATE_LIMITED retry_after= / NOT_FOUND / POLICY /
  INVALID / TOO_MUCH_DATA / TRANSIENT / INTERNAL.
- **Day boundaries are LOCAL time.** The ledger stores UTC ISO (`logdb._now()`);
  the daily cap converts local midnight to that format before comparing.
- **The ledger is the `changes` table** (`logs/meta-ads.db`). No new tables:
  extra data goes in the nullable `inverse_json` / `verification_json` columns.
  Signed preview URLs are never stored.
- **No root-level scripts.** The only tracked root `.py` is `install.py`;
  scripts go in `scripts/`.
- **This repository is public.** No company, brand, campaign, ad set, ad,
  audience, Page or Instagram names from a real account, no real
  account/page/pixel/campaign ids, no credentials. Fixtures use placeholders
  (`Example Co`, `act_1234567890`, `example.com`, `Springfield`). Nothing under
  `.claude/plans/` is ever committed.

## Adding a change kind

A write is a `ChangeKind` (`metaads_mcp/changes/base.py`); the engine
(`changes/engine.py`) runs the one sequence for all of them: token, kill switch,
token health, staleness, session and daily caps, two-phase ledger, the mutation,
`result.success`, and Meta's `validate_only` in every preview.

1. Put any new mutation builder in `metaads_reporting/write/` (it returns a
   `PlannedChange`; a create needs an `edge`, an update a `node_id`, and a field
   not yet in `WRITABLE_FIELDS` needs adding there and to the pinned set).
2. Subclass `ChangeKind` -- or `common.NodeFieldKind` for "set these fields on
   one node", which handles build, revert and staleness from a `render()`:
   - `name` -> `preview_<name>` / `apply_<name>`; titles; `apply_annotations`;
     `weight` (3 for a creation); `verifies_ad = True` for anything that creates
     or changes an ad or creative (the engine then runs `metaads_mcp/verify.py`).
   - `propose(self, ctx, ...)` -- its parameters ARE the preview tool's schema;
     annotate each with `Annotated[..., Field(description=...)]`; the docstring
     is the tool description. Return a `Proposal` (before/after, checks,
     projected spend, JSON-able payload, `is_activation` for anything that
     resumes delivery, `items` + `batch=True` for a batch, `verify` for an ad).
   - `current(ctx, payload)` -- today's value, spelled exactly like `before`.
     `after` must be what a re-read will render, or revert and
     `reconcile_ledger` see false drift.
   - `build(ctx, payload)` -- the `PlannedChange` (a list for a batch).
   - `inverse` (+ `current_for_entry`, `revert_projection` if undoing it
     restores spend); put what it needs in `Proposal.with_inverse(...)`. A
     create's inverse pauses what was created.
3. Add it to `changes/__init__.all_kinds()`, pin both tool names in
   `tests/test_mcp_surface.py` and `install.py` (and `SPENDS` there if it moves
   money, delivery or targeting), update the counts in README and SKILL.md, and
   mention it in `server.INSTRUCTIONS` and the skill.
4. Tests with `tests/kindkit.py` on the simulator: preview renders before/after;
   `validate_only` is sent and nothing applied; a FAIL blocks; a stale value is
   refused; the inverse round-trips.
