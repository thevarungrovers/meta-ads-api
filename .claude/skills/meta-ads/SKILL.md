---
name: meta-ads
description: Use the `meta-ads` MCP server to read a Meta (Facebook and Instagram) ad account and change it safely. Covers the preview/apply approval loop, pausing campaigns, ad sets and ads, daily budgets at both levels, ad set bid caps, insights reports, guardrails, the change ledger and undo. Use whenever the task involves Meta Ads, Facebook Ads, Instagram Ads, Ads Manager, campaigns, ad sets, ads, daily budgets, bid caps, creative fatigue, Advantage campaign budget, or the apply_*/preview_* tools on that server.
---

# Meta Ads

The `meta-ads` MCP server talks to **one real, live advertising account**.

Campaigns are serving and spending today. Meta has no sandbox worth pointing
this at, so there is no rehearsal: a mistake costs money now, not at the next
deploy.

## The one rule

**Read → preview → show the human → apply.** Never skip a step.

```
1. READ     get_account, then find_campaigns / find_ad_sets / find_ads,
            then run_report and get_guardrails
2. PREVIEW  preview_<thing>  ->  returns a preview_token
3. SHOW     put the preview in front of the human, in your own words
4. APPLY    apply_<thing>(preview_token="...")
```

Step 3 is the authorization. The human approving the `apply_*` permission prompt
**is** the sign-off — so never call an `apply_*` without having just shown them
what it does.

## Three things that are Meta-specific and will catch you out

### 1. Where the budget lives

A campaign holds the budget **or** its ad sets do. Never both.

- `budget_level: "campaign"` — Advantage campaign budget (CBO). The campaign has
  a `daily_budget`, its ad sets have none, and changing it moves spend for
  **every ad set underneath**.
- `budget_level: "adset"` — each ad set has its own, the campaign has none.

`find_campaigns` reports `budget_level` for exactly this reason. Changing a
budget at the wrong level is refused with an explanation — **read it rather than
retrying at the other level without telling the human**, because which level a
campaign uses is a real decision somebody made.

### 2. `status` is not `effective_status`

- `status` — what somebody set on **this** object.
- `effective_status` — what is **actually happening**, with the parents and ad
  review folded in.

An `ACTIVE` ad inside a `PAUSED` campaign has `effective_status:
CAMPAIGN_PAUSED` and **is not delivering**. `DISAPPROVED` and `PENDING_REVIEW`
mean the same thing for a different reason.

**Quote `effective_status` when you tell a human whether something is running.**
Quoting `status` makes a dead ad look live.

### 3. The conversion numbers are not click-based

Reported conversions use **7-day click plus 1-day view** attribution. The view
half counts people who *saw* an ad and never touched it.

That is why Meta's conversion count is routinely several times larger than any
click-based record of the same campaign — a UTM-tagged signups table, say. They
are not measuring the same thing and neither is wrong. Say so rather than
treating the gap as a bug.

## DO

- **DO call `get_account` first.** It gives you the **currency**, which decides
  what every budget number means, and the **time zone**, which decides what every
  report date means.
- **DO start with `find_campaigns`.** It gives you ids, `budget_level` and
  `effective_status` in one call.
- **DO call `get_guardrails` before proposing a change.** It tells you the bounds
  and how much of this session's budget is left, so you propose something that
  can be approved rather than something that will be refused.
- **DO call `run_report` first** when the change is about performance. Deciding a
  budget without looking at what the entity has been doing is guessing.
- **DO write money as a decimal string**: `"25.00"` is twenty-five.
- **DO read the whole preview**, especially `projected_daily_spend_delta`,
  `warnings`, `effective_status` and `checks`.
- **DO tell the human the `entry_id`.** It is what `revert_change` takes.
- **DO prefer pausing one ad** over pausing an ad set when the problem is one
  worn-out creative. It leaves the budget and the learning phase intact.

## DON'T

- **DON'T call an `apply_*` without showing the preview first.** The approval
  prompt is meaningless if nobody saw what it approves.
- **DON'T pass minor units.** Meta's own API wants an integer in the currency's
  minor unit — `2500` for `$25.00`, and `25` on a zero-decimal currency like JPY
  — and these tools do that conversion for you. Passing `2500` here is a
  hundredfold error, and the number is rendered verbatim in the prompt a human
  approves. The tool refuses anything over 100000 for this reason, but do not
  rely on that.
- **DON'T re-use a `preview_token`.** Single use, 10 minutes, and not
  interchangeable between tools.
- **DON'T try to set anything to DELETED or ARCHIVED.** Both are irreversible
  through the API and neither is exposed. Pause instead — same effect on
  delivery, and it can be undone.
- **DON'T try to create a campaign, an ad set or an ad.** Not available here,
  and **not available from the CLI either** — there is no script for it. Say so
  and point the human at Ads Manager.
- **DON'T try to change an ad's creative, copy or media.** Primary text,
  headline, description, image, video: none of it is reachable. A Meta creative
  is immutable once attached, so changing what an ad says is a *new ad*.
- **DON'T try to create or edit an audience** — custom, lookalike or saved.
  Entirely out of scope.
- **DON'T sum `reach` or `frequency` across rows.** The same person reached on
  two days is one person. Use the `creative_fatigue` report, which collapses the
  window into one row per ad for exactly this reason.
- **DON'T work around the kill switch.** If a tool says writes are disabled, a
  person turned them off. Say so and stop.

## The tools

21 in total: 8 read, 6 `preview_*`/`apply_*` pairs, plus `revert_change`.

### Read — safe, call freely

| Tool | Gives you |
|---|---|
| `list_ad_accounts` | Which ad accounts this token can reach |
| `get_account` | **Currency, time zone**, status, spend cap, balance |
| `find_campaigns(name_contains_text, status, limit)` | Campaigns, ids, `budget_level`, budgets |
| `find_ad_sets(campaign_id, name_contains_text, limit)` | Ad sets, budgets, `bid_strategy`, `bid_amount` |
| `find_ads(ad_set_id, campaign_id, name_contains_text, limit)` | Ads and their `effective_status` |
| `run_report(report, days, since, until, campaign_name, node_id, limit)` | Performance. `report` is one of `account`, `campaigns`, `adsets`, `ads`, `creative_fatigue` |
| `get_guardrails` | The bounds, and this session's remaining budget |
| `list_my_changes(limit)` | What this server has changed, with `entry_id`s for undo |

### Change — preview first, always

| Pair | Changes |
|---|---|
| `preview_campaign_status` / `apply_campaign_status` | Pause or activate a **whole campaign** |
| `preview_campaign_daily_budget` / `apply_campaign_daily_budget` | A **campaign-budget** campaign's daily budget |
| `preview_ad_set_status` / `apply_ad_set_status` | Pause or activate an ad set |
| `preview_ad_set_daily_budget` / `apply_ad_set_daily_budget` | An **ad-set-budget** ad set's daily budget |
| `preview_ad_set_bid_amount` / `apply_ad_set_bid_amount` | An ad set's bid cap or cost target |
| `preview_ad_status` / `apply_ad_status` | Pause or activate **one ad** |
| `revert_change(entry_id)` | Undo one ledger entry |

Each `apply_*` takes **only** a `preview_token` — never the values. That is
deliberate: it means an apply cannot be called cold, with nothing shown first.

### Not here — and not anywhere else in this toolchain

Creating or deleting campaigns, ad sets and ads. Ad copy and creative of every
kind: primary text, headline, description, images, video. Audiences: custom,
lookalike, saved. Targeting, optimization goal, billing event, schedule, special
ad categories. Setting a status to DELETED or ARCHIVED. Billing and account
access. None of those fields is on the write client's allowlist at all.

There is **no generic update/passthrough tool**, so if an operation is not in the
tables above it cannot be done from here.

**And there is no CLI fallback.** `scripts/manage.py` has the same write surface
this server does — status, budget, bid, name — and nothing more. If the human
needs one of the above, tell them plainly that it is not built, and point them at
Ads Manager. Do not send them to a script.

## Reading the preview

| Field | Why it matters |
|---|---|
| `entity_name` / `path` | `Brand - Prospecting` reads differently from `120200000000001` |
| `currency` | A ceiling of 500 means nothing until you know 500 of what |
| `before` / `after` | In major units, already converted out of minor ones |
| `status` vs `effective_status` | The second is whether it is actually running |
| `metrics_7d` | What it has actually been doing, including `frequency` |
| `projected_daily_spend_delta` | Upper bound on extra spend per day |
| `checks` | One line per bound, `OK` or `FAIL` |
| `blocked` | `true` means a guardrail failed and the apply will refuse |

Three asymmetries worth repeating to the human:

- **Pausing** cannot increase spend, so its projected delta is `0`.
- **Activating** is bounded by the **budget**, not by past spend — a campaign
  that spent nothing last week can still spend its full budget tomorrow.
- **Pausing or activating one ad** has a projected delta of `0` in both
  directions. The ad set's budget is the cap and it does not move; what changes
  is which creative spends it.

If `blocked: true`, do not try the apply. Read the `FAIL` line and propose
something inside the bounds.

## A bid is not a price

`bid_amount` is only honoured under `LOWEST_COST_WITH_BID_CAP` and `COST_CAP`
(as a cap), and `TARGET_COST` / `LOWEST_COST_WITH_MIN_ROAS` (as a target). Under
`LOWEST_COST_WITHOUT_CAP` — Meta's default, and what most ad sets are on — Meta
**accepts a bid write and ignores it**.

The tool refuses rather than letting that happen, because otherwise it would
report a change that never occurred. Check `bid_strategy` in `find_ad_sets`
before proposing a bid change.

Even where it is honoured, a cap is not a price: raising one changes nothing
unless the auction was already pressing against it.

## Writes are limited to one call per 30 seconds

Meta caps writes on a campaign node at **1 call per 30 seconds** (error code
613). This is not a per-day quota you will never reach — it is a window you hit
by doing two writes in a row, which is the normal shape of a correction.

The transport handles it: a rate-limit refusal is retried automatically, after
a wait longer than the window, because Meta refusing a write means nothing was
applied. So an `apply_*` may simply **take half a minute to return**. That is
working, not hanging — do not cancel it and do not re-issue the call.

What this means in practice:

- **Do not batch.** Changing five ad sets is five writes and takes several
  minutes. Propose the one change that matters most and say the rest can follow.
- **A revert right after an apply is the most likely call to be delayed**, since
  it is the second write in the window. Wait for it.
- If you ever see a `code 613` reach you as an error, the automatic retries were
  exhausted. Stop, tell the human, and check `list_my_changes` before anything
  else — do not retry by hand.

## Undo

```
revert_change(entry_id="...")
```

It refuses if the entry was never applied, if it has already been reverted, or if
**the value has moved since** — in that last case somebody edited it in Ads
Manager after your change, and reverting would discard their edit rather than
undo yours.

An entry showing `applied: false` **with no error** is the one to look at
carefully: the write left the process and nothing recorded how it ended, so it
may have landed at Meta's end. Tell the human to check Ads Manager rather than
retrying.

## When something goes wrong

| Message | What it means | What to do |
|---|---|---|
| `unknown, already used, or older than 10 minutes` | Tokens are single-use | Take a fresh preview |
| `has changed since the preview` | Somebody edited it in Ads Manager | Re-preview, look at the new numbers, decide again |
| `failed a guardrail check` | The preview was blocked | Read `checks`, propose something smaller |
| `writes are disabled: ...` | The kill switch | A person set it. Say so and stop; do not work around it |
| `session apply limit reached` | Per-session cap | Show `list_my_changes` and let the human decide |
| `looks like minor units` | You passed `2500` for `$25.00` | Pass the decimal string |
| `does not hold its own budget` | Wrong budget level | Use the other level's tool — and say why |
| `does not use bid_amount` | The bid strategy ignores it | Do not propose a bid change on this ad set |
| `DELETED ... irreversible` | Working as intended | Use `PAUSED` |
| `code 190` | The access token died | A person must regenerate it; nothing you can do |
| `code 200` / `code 10` | A permission is missing | Needs both the scope **and** the account assignment |

## A worked example

> "Our Meta spend is up but sales aren't. Fix it."

```
1. get_account()                  -> currency and time zone, so the numbers mean something
2. run_report(report="campaigns", days=30)
                                  -> which campaigns, and what the trend is
3. run_report(report="creative_fatigue", days=14)
                                  -> one row per ad, with FREQUENCY, which is the
                                     usual culprit behind "spend up, results flat"
4. find_ads(ad_set_id=...)        -> ids and effective_status for the worst ones
5. get_guardrails()               -> what can actually be approved
6. preview_ad_status(ad_id=..., new_status="PAUSED")
7. Summarise: the ad, its 7-day frequency and CTR, what else is still running in
   that ad set, and the projected delta (0 — the budget does not move).
8. apply_ad_status(preview_token="...")
9. Report `applied` and the `entry_id` for undo.
```

Pausing the fatigued ad rather than cutting the ad set's budget is the right
first move: it leaves the budget and the learning phase alone and lets the
remaining creative take the delivery. Say that, rather than just doing it.

If step 6 comes back `blocked: true`, do not retry it differently without
saying so — tell the human the bound, and what change would fit inside it.
