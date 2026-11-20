---
name: meta-ads
description: Use the `meta-ads` MCP server to read a Meta (Facebook and Instagram) ad account and change it safely. Covers the preview/apply approval loop, status, daily and lifetime budgets, bid strategies and ROAS floors, schedules, targeting and placements, custom/lookalike/saved audiences and their overlap, creating campaigns, ad sets, creatives and ads (always PAUSED), copy changes, UTM tags, Advantage+ creative options, lead forms, A/B tests, ad verification and previews, reports and breakdowns, guardrails and the session/daily caps, the change ledger, reconcile and undo. Use whenever the task involves Meta Ads, Facebook Ads, Instagram Ads, Ads Manager, campaigns, ad sets, ads, audiences, targeting, creatives, budgets, bids, creative fatigue, Advantage+, or the apply_*/preview_* tools on that server.
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
- **DON'T create anything live.** Everything is created PAUSED (campaigns, ad
  sets, ads, copies) and activating is a separate status preview the human
  approves. `special_ad_categories` on `preview_create_campaign` has no
  default: ask the human, never guess it.
- **DON'T try to delete anything.** Nothing is ever DELETED or ARCHIVED, and
  audiences are never deleted -- the undo for attaching one is detaching it.
- **DON'T reach for `update_ad_copy` by default.** It discards social proof.
  Call `get_ad_creative` first, read `social_proof`, and if it is non-zero say
  so and offer `duplicate_ad` instead.
- **DON'T try to create a customer-list audience or a lead form.** Website,
  engagement, video, lead-form, app, lookalike and saved audiences are here;
  anything that would upload customer emails or phone numbers is refused
  structurally, and correctly, and forms carry legal text a person owns --
  point the human at Ads Manager for both.
- **DON'T try to upload a video.** Reuse an existing `video_id` instead;
  duplicating a video ad with new copy needs no upload at all.
- **DON'T claim to have seen an ad preview you did not open.** Preview links are
  signed URLs; open them with a browser tool if you have one, otherwise hand
  them to the human.
- **DON'T sum `reach` or `frequency` across rows.** The same person reached on
  two days is one person. Use the `creative_fatigue` report, which collapses the
  window into one row per ad for exactly this reason.
- **DON'T work around the kill switch.** If a tool says writes are disabled, a
  person turned them off. Say so and stop.

## The tools

110 in total: 35 read, 37 `preview_*`/`apply_*` pairs, plus `revert_change`.

### Read — safe, call freely

| Tool | Gives you |
|---|---|
| `list_ad_accounts`, `get_account` | Accounts; **currency, time zone**, status, token health, rate limit |
| `find_campaigns` / `find_ad_sets` / `find_ads` | Ids, `budget_level`, budgets, bids, `effective_status`; `truncated` when cut off; `origin` / `ledger_created`, filter with `origin`, `agent_name`, `owner`, `mine` |
| `get_campaign` / `get_ad_set` / `get_ad` | One entity in full: special ad categories, the **whole targeting spec**, placements, review feedback, 7/30-day totals, preview links |
| `run_report` | Performance. Reports: `account`, `campaigns`, `adsets`, `ads`, `creative_fatigue`, `placements`, `demographics`, `geo`, `hour_of_day`, `conversions_by_action`; `breakdowns`, `max_rows`, `aggregate`, `action_types`, `run_async` |
| `get_report_run` | Polls an async report |
| `get_ad_creative` | What an ad **says**, its shape, and the social proof at stake |
| `verify_ad` / `get_ad_previews` | Review state, field diff against what was approved, preview links per placement |
| `list_audiences` / `get_audience` | Audiences with size, `usable`, and which ad sets use them (`unused_only`); the naming filters |
| `search_targeting` / `estimate_reach` | Interest, behaviour, place, locale ids; reach bounds for a spec |
| `list_ad_images` / `list_ad_videos` | Media to reuse, with dimensions and length |
| `list_pixels` / `get_pixel_health` / `list_custom_conversions` | Tracking diagnostics, read-only |
| `list_connected_identities` / `list_lead_forms` | Pages, Instagram accounts, apps; existing forms (never leads) |
| `list_labels` (naming filters), `list_ad_rules`, `list_ab_tests` / `get_ab_test`, `list_catalogs` / `list_product_sets` | Account assets; money-changing rules are flagged |
| `list_recent_changes` | Who changed what in Ads Manager -- the usual reason a preview is stale |
| `search_ad_library` | Meta's Ad Library (political/issue ads only outside the EU) |
| `get_guardrails` | The bounds, the session and **today's** remaining budget, token health |
| `list_my_changes` / `reconcile_ledger` | What this server changed, and whether the account still matches |

### Change — preview first, always

| Pair | Changes |
|---|---|
| `campaign_status` / `ad_set_status` / `ad_status` / `multi_ad_status` | Pause or activate (several ads in one ad set as a batch) |
| `campaign_daily_budget` / `ad_set_daily_budget` | A daily budget, at the level that holds it |
| `campaign_lifetime_budget` / `ad_set_lifetime_budget` | A lifetime budget, projected over the days left |
| `ad_set_bid_amount` / `ad_set_bid_strategy` / `campaign_bid_strategy` / `ad_set_roas_floor` | Bids, cost caps, min ROAS |
| `ad_set_schedule` / `campaign_schedule` / `ad_set_dayparting` / `ad_set_frequency_cap` | When it runs, and how often one person sees it |
| `ad_set_targeting` | Places, ages, genders, locales, interests, **audiences in/out**, saved-audience copy, Advantage+ audience |
| `ad_set_placements` | Advantage+ or manual placements, previewed for every ad |
| `ad_set_optimization_goal` / `ad_set_attribution_spec` | What it optimizes for; the attribution window |
| `create_audience` | Website, engagement (Page/IG), video viewers, lead form, app, lookalike (1-10%), saved |
| `duplicate_ad` / `update_ad_copy` | Copy or media: a new PAUSED ad, or the same ad repointed |
| `url_tags` / `advantage_creative_options` / `attach_lead_form` / `replace_ad_creative` | Creative-level changes, each a new creative + repoint |
| `upload_video` | A local MP4/MOV into the video library (resumable upload); waits up to wait_seconds for Meta's processing and says if it is ready. Use the video_id only once ready |
| `upload_image` | A local image into the library (shows nowhere); returns the image_hash to use. Optional: create_creative also takes local files (image_path, per language too) and uploads them at apply, but then Meta's validate_only and the previews wait until apply |
| `create_creative` / `create_ad` | A new creative (unattached; one language, or `locales` for a multi-language ad with copy, link and media per language), a new PAUSED ad. url_tags default to the campaign's own |
| `create_campaign` / `create_ad_set` / `duplicate_ad_set` / `duplicate_campaign` | New or copied, always PAUSED |
| `create_ab_test` | A split test between PAUSED cells |
| `low_risk_change` | Names and ad labels only, on a closed allowlist |
| `revert_change(entry_id or batch_id)` | Undo one ledger entry, or a whole batch |

Each `apply_*` takes **only** a `preview_token` — never the values. That is
deliberate: it means an apply cannot be called cold, with nothing shown first.

### Not here — and not anywhere else in this toolchain

Deleting or archiving anything, audiences included. A customer-list audience
(it uploads customer personal data). Creating a lead form (a person owns its
privacy policy and legal text). Downloading leads. Switching a campaign between
campaign and ad-set budgets. Automated rules that change money. Pixel or CAPI
configuration, catalog feeds, billing and account access. Uploading a new
video (reuse an existing `video_id`).

Ad copy, media and audiences **are** here, as are creation, targeting and
placements: see the tables above.

There is **no generic update/passthrough tool**, so if an operation is not in the
tables above it cannot be done from here.

**And there is no CLI fallback for the list above.** `scripts/manage.py` has a
smaller write surface than this server -- status, budget, bid, name, `ad
duplicate`, `ad set-copy`, `audience lookalike` and `audience saved` -- and
nothing more. If the human needs one of the things above, tell them plainly that
it is not built, and point them at Ads Manager. Do not send them to a script.

## Changing what an ad says

A Meta creative is **immutable once attached**, so there is no "edit the
headline" call. Two paths, and the choice is real:

| | `duplicate_ad` | `update_ad_copy` |
|---|---|---|
| Result | New **PAUSED** ad beside the original | Same ad, repointed |
| The original | Untouched, keeps delivering | Replaced |
| Social proof | **Kept** | **Usually lost** |
| Review | New ad reviewed before running | Live ad re-enters review |

**Default to `duplicate_ad`.** It changes nothing live, and the human can pause
the old ad once happy. Reach for `update_ad_copy` only when they want to keep
the ad id and its reporting line, and tell them what the engagement cost is
first — `get_ad_creative` gives you the exact counts.

Call `get_ad_creative` before proposing either. The `shape` it reports decides
what you can do:

- `link_data` / `video_data` — one value per field.
- `asset_feed_spec` — **a list per field, one per locale.** A single value is
  applied to every locale; pass a list to set them individually, matching the
  existing length exactly.
- `page_only` — a catalog ad. Its copy comes from the product feed; there is
  nothing on the creative to change, and the tool will say so.

## Audiences

`list_audiences` reads everything, including customer-list audiences — you need
to see one to pick a lookalike source — and says whether each is `usable`.

`preview_create_audience` builds from data Meta already holds: website (pixel
events), Page and Instagram engagement, video viewers, lead-form openers,
app activity, lookalikes (1-10% of a country) and saved targeting specs. A
customer-list audience would upload hashed customer emails and phone numbers to
Meta, and is refused structurally. That is not a limitation to work around:
tell the human it belongs in Ads Manager, where a person accepts the data terms.

A rule-based audience or a lookalike takes hours to populate. Say that when you
create one, and attach it only once `get_audience` reports it usable.

## Caps, errors and the ledger

- **Session and daily caps.** Every apply counts against this session AND the
  local calendar day (read from the ledger, so a restart does not reset it).
  Creations cost 3 applies. `get_guardrails` shows both under `session` and
  `today`.
- **Activating is counted against the daily total**, not the per-change
  limit: the preview renders `SKIP projected_delta_per_day (activation: ...)`
  and a `max_projected_delta_per_day_total` line naming "uses X of today's
  remaining Y". Budget and bid increases must pass both.
- **Errors carry a prefix**: `AUTH_EXPIRED` (the token; a person must fix it),
  `PERMISSION` (a scope or an asset assignment), `RATE_LIMITED retry_after=<s>`
  (wait that long, then retry once), `NOT_FOUND`, `POLICY` (ad review),
  `INVALID` (the request), `TOO_MUCH_DATA` (narrow the report), `TRANSIENT`,
  `INTERNAL` (a bug: report it).
- **Verification.** Every ad-changing apply returns `verification`: the live ad
  re-read and diffed against what was approved, review state and fresh preview
  links. `passed: false` reverts nothing -- say what differs.
- **`reconcile_ledger`** lists drift (the live value no longer matches what a
  change left), intents with no outcome, and ads still in review.

## Recipes

**Daily health check.** `get_guardrails` (token health, today's budget) ->
`reconcile_ledger` (drift, unknown outcomes, pending review) -> `find_ads
(status="DISAPPROVED")` and `status="WITH_ISSUES"` -> `list_ad_rules` (money
rules acting outside these caps) -> report, change nothing.

**Creative fatigue rotation.** `run_report("creative_fatigue", days=14)` ->
`get_ad_creative` on the worst -> `preview_duplicate_ad` with fresh copy (new
PAUSED ad) -> human approves -> `preview_ad_status` ACTIVE on the new one and
PAUSED on the old (or `preview_multi_ad_status`).

**Budget reallocation across ad sets.** `run_report("adsets", days=14)` ->
`get_guardrails` -> one `preview_ad_set_daily_budget` cut and one raise, shown
together; the cut projects negative and refunds nothing from the caps.

**Launching a campaign.** `preview_create_campaign` with a BASE name (ask the
human for `special_ad_categories`; the prefix is added) -> `preview_create_ad_set` (targeting from
`search_targeting`, `estimate_reach` first) -> `preview_create_creative` ->
`preview_create_ad` -> everything is PAUSED; the human activates with the
status tools, campaign last.

**Breakdown analysis.** `run_report("placements" | "demographics" | "geo" |
"hour_of_day")`, or any report with `breakdowns=[...]`; follow with
`preview_ad_set_placements` or `preview_ad_set_targeting` only if the human
wants to act on it.

**Verify an ad.** Read the `verification` block every ad-changing apply
returns and report any failed field. Call `verify_ad` again after review
(usually within 24 hours; Meta says up to 48) and report approval or the
`ad_review_feedback` reasons. Open the preview links with a browser tool if
you have one; otherwise hand them to the human, and never claim to have seen a
rendering you did not open.

**Build an audience and use it.** `list_connected_identities` / `list_pixels`
-> `preview_create_audience` -> wait for `get_audience` to report `usable` ->
`estimate_reach` -> `preview_ad_set_targeting(audiences_include=[...])`, and
read the overlap and exclusion warnings.

**A/B test.** `preview_duplicate_ad_set` (PAUSED copy, renamed with your
prefix) -> change ONE variable
on the copy (`ad_set_targeting`, `ad_set_placements` or a creative change) ->
`preview_create_ab_test(variable=...)` -> the human activates the cells.

**Audiences cannot be deleted here.** An unused one just sits there and
changes nothing; `list_audiences(unused_only=True)` shows them.

## Naming

Everything you create is named `<OWNER>-<agent>-<base>` (e.g. `AB-max-spring`,
placeholders), from the repo's `.env`; each part only when it is set, and with
neither set the name is used as given. `get_guardrails` shows the identity.

- **Pass the base name only.** The prefix is added; passing it already is fine,
  passing another agent's (`CD-bot-...`) or the CLI's (`AB-cli-...`) is refused,
  and so is one spelt with look-alike characters (a Cyrillic letter, a
  non-ASCII dash, a fullwidth form): use plain ASCII at the start of a name.
- The preview's `naming` block shows the EXACT final name; read it to the human.
- A creative's top-level name is stamped; the headline (`link_data.name`) is
  your text, never touched.
- With initials only (no agent name), a base like `spring-promo` is refused
  (it would read as agent `spring`): use `spring_promo` or `spring promo`.
- Copies and duplicates take your prefix on the source's base, with any other
  prefix stripped (identity or not); `/copies` are renamed right after, one
  ledger entry each. If one of those renames fails or is skipped, the apply
  result has a `warnings` line and each object's `live_name` in `follow_ups`:
  tell the human the real name.
- Renames: only your own entities (your prefix, or the ledger shows you made
  it). A manual entity or another agent's is refused -- say so and leave it.
  A name carrying a different full prefix is refused even when the ledger
  shows you made it.
- Over 400 characters (prefix included) is refused, never truncated: shorten.
- `find_*(mine=True)` finds yours; `reconcile_ledger` reports lost prefixes.

## Reading the preview

| Field | Why it matters |
|---|---|
| `entity_name` / `path` | `AB-max-Brand - Prospecting` reads differently from `120200000000001` |
| `naming` | For a create, copy or rename: the requested and EXACT final name, its prefix, base, length and rule |
| `currency` | A ceiling of 500 means nothing until you know 500 of what |
| `before` / `after` | In major units, already converted out of minor ones |
| `status` vs `effective_status` | The second is whether it is actually running |
| `metrics_7d` | What it has actually been doing, including `frequency` |
| `projected_daily_spend_delta` | Upper bound on extra spend per day |
| `checks` | One line per bound: `OK`, `FAIL`, `WARN` (does not block) or `SKIP` |
| `previews` | For an ad change: preview links per placement (signed, short-lived) |
| `apply_weight` | 3 for a creation, 1 otherwise |
| `blocked` | `true` means a guardrail failed and the apply will refuse |

Three asymmetries worth repeating to the human:

- **Pausing** cannot increase spend, so its projected delta is `0`.
- **Activating** is bounded by the **budget**, not by past spend — a campaign
  that spent nothing last week can still spend its full budget tomorrow. An
  ABO campaign is bounded by its ACTIVE ad sets' budgets summed.
- **Activating one ad** projects `0` while siblings already spend the ad set's
  budget, and the ad set's budget when it would be the only ACTIVE ad.

If `blocked: true`, do not try the apply. Read the `FAIL` line and propose
something inside the bounds.

## A bid is not a price

`bid_amount` is only honoured under `LOWEST_COST_WITH_BID_CAP` (a cap) and
`COST_CAP` (an average-cost target). `LOWEST_COST_WITH_MIN_ROAS` uses a ROAS
floor instead (`preview_ad_set_roas_floor`), and `TARGET_COST` is deprecated. Under
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

The transport handles it: a rate-limit refusal is retried automatically when
the wait fits inside a tool call (up to a minute), because Meta refusing a write
means nothing was applied. So an `apply_*` may simply **take half a minute to
return**. That is working, not hanging — do not cancel it and do not re-issue
the call. A longer wait comes back as `RATE_LIMITED retry_after=<seconds>`:
wait that long, then retry once.

What this means in practice:

- **Batches take time.** `multi_ad_status` is one write per ad; changing five
  ad sets is five writes. Propose the change that matters most first.
- **A revert right after an apply is the most likely call to be delayed**, since
  it is the second write in the window. Wait for it.
- If a `RATE_LIMITED` error reaches you on an apply, nothing was applied. Check
  `list_my_changes`, wait `retry_after`, and take a fresh preview.

## Undo

```
revert_change(entry_id="...")
```

Reverting something this server CREATED pauses it; an audience or an
unattached creative cannot be reverted (nothing is deleted). A `batch_id`
reverts the whole batch. It refuses if the entry was never applied, if it has
already been reverted, or if **the value has moved since** — in that last case somebody edited it in Ads
Manager after your change, and reverting would discard their edit rather than
undo yours.

A revert that restores spend counts against the caps like the change it
undoes, in the cap currency (converted once, from the entity's currency).
Undoing a change that tightened delivery -- and every later revert in that
chain that lifts it again -- projects the live unused budget headroom
(undoing a shortened daily-budget flight projects the whole daily budget, as
does adding days to one, or re-applying an extension by reverting its revert). A pause or a cut in a currency with no configured
rate is let through (counted as 0), and so is a change whose direction is
provably down (a stricter frequency cap, a lower bid or cost cap, a higher ROAS
floor, targeting only narrowed, a flight only shortened); a projection of 0
alone is not enough, and a looser cap or a swap between bid strategies is refused;
every other change that can move money needs the currency in
`allowed_currencies`.

**Pausing is always allowed.** Setting a campaign, ad set or ad (or a batch of
ads) to PAUSED is never blocked by either currency check, whatever the
account's currency: stopping spend must always work. The account scope still
applies, and activating it again is checked as usual.

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
| `daily apply limit reached` / `daily spend total reached` | Today's cap (local day, all processes) | Stop; it resets at local midnight |
| `RATE_LIMITED retry_after=N` | Meta asked to wait | Wait N seconds, retry once |
| `looks like minor units` | You passed `2500` for `$25.00` | Pass the decimal string |
| `does not hold its own budget` | Wrong budget level | Use the other level's tool — and say why |
| `does not use bid_amount` | The bid strategy ignores it | Do not propose a bid change on this ad set |
| `DELETED ... irreversible` | Working as intended | Use `PAUSED` |
| `AUTH_EXPIRED` | The access token died | A person must regenerate it; nothing you can do |
| `PERMISSION` | A permission is missing | Needs both the scope **and** the asset assignment |

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
