# Tuning

Which knob moves what, how to read what the agent reports, and what to do when something looks wrong.

Two rules before you turn anything:
- **Deals are rare, so a few quiet rounds mean nothing.** At 0.1 to 0.3 deals per round, three empty rounds happen
  41 to 74% of the time by chance (see [`research/self-improving-agents.md`](research/self-improving-agents.md) section 3.1).
  Judge a change over tens of rounds, not hours.
- **Frozen files** (`rules.yaml`, `economics.yaml`, `data/`) can be edited only while the agent is stopped.
  Everything else is read at start, so `dh stop && dh start` after any change.

## Floors and tiers (`config/rules.yaml`, frozen)

| Knob | Raise it and... | Lower it and... |
|---|---|---|
| `floors.easy` / `moderate` / `heavy` (50 / 120 / 250 USD) | fewer deals, each with more margin for error | more, thinner deals; tier 3 and 4 fill up |
| `tiers.t1_min_cons_net_usd` (300) | fewer pushes | more pushes. A push should be worth acting on at once, so go down carefully |
| `tiers.t4_floor_margin` (0.20) | more near-miss tier-4 rows (they also feed the bandit at 0.2 weight) | fewer |
| `star_usd` (500) | fewer starred deals | more stars. Stars sort first everywhere |
| `capital_cap_usd` (30000) | larger deals appear | deals needing more capital vanish |

Floors apply to the **conservative net** (the net at the P25 exit price), so they already include a margin.
The effort class is not a knob: it is decided by the route (country, import, bundle, days to sell).

## Confidence thresholds (`config/rules.yaml`, frozen)

Confidence decides tier 1 (high), tier 2 (medium) or tier 3 (low).

| Knob | Effect |
|---|---|
| `confidence.min_comps.base` / `capital_over_5000` / `capital_over_15000` (12 / 20 / 30) | How many *effective sold* comparables a deal needs for high confidence. This is the main cold-start brake. Lower it and tier 1 opens earlier on thinner evidence |
| `confidence.p_scam_max` (0.05) | The scam probability ceiling for high confidence |
| `confidence.match_certainty_min` (0.9) | How sure the product match must be |
| `confidence.verify_max_age_min` (10) | How fresh the live re-check must be. Rounds re-check just before saving, so this rarely binds |
| `confidence.medium.*` (0.75 / 0.15 / 0.5) | The tier 2 / tier 3 boundary. A weak match, a scam score of 0.15 or more, an unknown condition or half the comps missing makes a deal tier 3 |

Effective sold comparables count owner sales and eBay sold at weight 1.0 and "probably sold" at 0.5. **Active
asks never count.** So tier 1 is unreachable until the ledger has matured or eBay Marketplace Insights is granted.
Listings that come through the alert mailbox cannot be re-checked and so never reach tier 1.

## Exit valuation (`config/valuation.yaml`)

| Knob | Effect |
|---|---|
| `ask_to_sale_ratio` (0.92) | Lower it and every ask-based exit price falls, so nets and tiers fall. Calibrate it from your own Sold marks |
| `weights.*` | How much each comp kind pulls the quantiles. Raise `probably_sold` to trust the ledger more |
| `ceiling_factor` (0.95) | Caps an estimate at the cheapest new retail price x this. Lower it to be stricter |
| `recent_days` (30) | A shorter window follows the market faster but has fewer comps |
| `min_estimate_comps` in `scheduler.yaml` (3) | A channel needs this many comps to be priced at all |

## Bandit: gamma, floor and prior

The bandit decides which source x query arms get a search each round. Its parameters are **constants in code, not
config keys**: `Bandit(store, gamma=0.98, floor=0.10, prior=(1.0, 10.0), new_pulls=3)` in `dh/bandit.py`, created
with defaults in `dh/runtime.py: build_context`. To change one, change those defaults and restart.

| Parameter | Default | Effect |
|---|---|---|
| `gamma` | `0.98` | Per-round discount of all evidence. Lower forgets faster (adapts to market changes, noisier); at 0.98 an observation's weight halves after about 34 rounds |
| `floor` | `0.10` | Share of each round's slots given to uniformly random exploration (`ceil(floor x k)`). Raise it if good arms are being starved; lower it to exploit more |
| `prior` | `(1, 10)` | Gamma prior of value per fetch: 0.1 value unit (USD 10 of expected net) per fetch. A higher mean makes new arms more attractive |
| `new_pulls` | `3` | Guaranteed trial pulls of a new arm |

Value of an arm per round = (sum of expected net USD of tier 1-3 deals + 0.2 x tier 4) / 100.
`scheduler.budget` is how many arms the bandit may pick each round.

## Round interval, budget and timeouts (`config/scheduler.yaml`, `config/sources.yaml`)

| Knob | Effect |
|---|---|
| `round_interval_s` (600) | Time between round starts. Shorter finds deals sooner but fetches more, so the polite rate limits bind and rounds overlap their own pacing. If a round takes longer than the interval, the next starts immediately |
| `budget` (60) / `budget_max` (120) / `budget_boost` (1.5) | Searches per round. The budget grows after a round where the LLM queue never waited and shrinks back when it backed up |
| `max_searches_per_source` (12) | Protects one site from being hammered; the main limit on per-domain load per round |
| `sources.sweep_timeout_s` (300) | One source's sweep is cut after this. A stuck site no longer holds the round, but its listings that round are lost |
| `round_timeout_s` (1500) | The whole round's deadline. `timeout_grace_s` (60) is extra time for evaluations already started |
| `alive_confirm_per_source` (5) | How many disappeared listings are re-checked per source. More makes the ledger more accurate (fewer false "sold") at the cost of fetches |
| `reeval_hours` (24) | How long an unchanged listing is kept without re-valuing. Lower it in a fast market |

## The LLM budget

| Knob | Effect |
|---|---|
| `scheduler.max_llm_items` (200) | Listings sent to the worker LLM per round. Over it, listings wait (`llm_deferred`) and are re-queued next round |
| `models.worker.parallel` (4) | Concurrent worker calls. Match to the model's real slots. If `llm_max_wait` in a round is high, the queue is backing up |
| `models.timeouts.read_s` (60) / `breaker.*` (3 failures, 120 s) | How long a call may take, and how fast the LLM is declared down. While the breaker is open, listings are valued on rules alone and flagged `llm_pending` |
| `models.min_free_ram_gb` (12) | The start guard. Lower it at your own risk: RAM, not VRAM, is the rig's binding limit (a second model next to a large one caused a kernel panic on 2026-10-28) |

The design target is under 5% of new listings reaching the LLM. Check `LLM items` against `listings` in the round
summary line. Paid APIs are off and have no code path.

## Source rate limits (`config/sources.yaml`)

| Knob | Effect |
|---|---|
| `fetch.min_interval_s` (4) + `jitter_s` (2) | The gap between requests to one domain. Raising it is always safe; lowering it raises block risk. A site's robots.txt `Crawl-delay` overrides it upward (to at most `max_crawl_delay_s`, 60) |
| `fetch.backoff_start_s` (60) / `backoff_cap_s` (3600) | Backoff after a 403, 429 or challenge page |
| `fetch.disable_after_blocks` (3) | Consecutive blocks that disable a domain until restart |
| `adapters.bazos_*.pages` (2) | Category pages read per sweep. More pages find older ads |
| `adapters.<source>.enabled` | The switch. Turn off a source that only wastes fetches |

Never work around a block (see [`SOURCES.md`](SOURCES.md)). If a site blocks, slow down or drop it.

## Risk weights

Scam probability is `1 / (1 + exp(-(bias + sum of weight x feature)))` over 0/1 features. The weights are in
`config/economics.yaml` under `risk` (frozen), and the reasoning is in comments next to them.

| Weight | Default | Fires when |
|---|---|---|
| `bias` | -4.5 | always: a normal listing scores 1.1% |
| `too_cheap` | 1.4 | price is more than 2.5 robust standard deviations below the median (`z_threshold: -2.5`; `z_sigma_floor: 0.15` stops tight comps from making normal prices look extreme) |
| `extreme_price` | 2.5 | price below 35% of the median (`extreme_price_frac`) |
| `new_account` | 2.6 | seller account younger than 30 days |
| `no_ratings` | 2.4 | seller has no ratings |
| `stock_photo` | 2.0 | the same photo is on another seller's listing (perceptual hash) |
| `origin_cn_hk` | 2.0 | ships from CN or HK, high-end RTX card |
| `no_photos_flagship` | 2.0 | no photos on a 4090 or 5090 class card |
| `scam_kw` | 0.8 | per scam-keyword hit, capped at `scam_kw_max_hits` (2) |

By design, a deep discount alone from an established seller stays under 5% (it can still be tier 1), but a deep
discount plus a weak-seller signal rises above 30%. Lower a weight to accept more risk; raise it to be stricter.
Calibrate against your Skipped marks and reasons.

## Reading the scoreboard, books and health

**`dh books`** (also the web page `/books`):
- `today` and `all_time`: rounds and cost by kind. `energy` is PLN and kWh; `llm` is tokens; `fetch` is requests;
  `paid_api` is always 0 PLN.
- `watched hours`: the time the agent was actually running. This is the clock for everything the ledger learns.
- `deals by tier` and `forecast conservative net`: the sum of the conservative net of all deals found (an
  expectation, not income).
- The web page also computes **realised** profit from your Sold marks, re-running the net with the price you
  actually got. Compare realised with forecast to calibrate `ask_to_sale_ratio`. The agent pays for itself when
  realised profit exceeds the cost lines.

**`dh status`:** rounds total and since start, the last round (listings, candidates, LLM calls, deals by tier),
deals today, and health over the last 24 hours (blocked and disabled sources).

**Web `/rounds`:** one row per round with its cost. **Web `/health`:** every health event, a summary by source and
kind, and the observation windows still open.

The round summary line (in `journalctl --user -u deal-hunter`) shows per source `ok/searches (listings)`, listings,
evaluated, candidates, LLM items (calls, tokens, deferred), deals by tier, duplicates and disappeared
(alive-confirmed).

Health kinds you will meet: `blocked`, `disabled`, `soft_backoff`, `robots_error`, `robots_disallow`,
`fetch_error`, `adapter_error`, `no_keys`, `no_credentials`, `not_granted`, `sweep_timeout`, `foreign_model`,
`frozen_changed`, `round_failed`, `ntfy_failed`, `email_disabled`, `push_expired`, `no_clone`, `clone_failed`.

## Symptoms and fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| No tier-1 pushes for days | Cold start: fewer than 12 effective sold comps per key | Keep running (watched hours); get eBay Marketplace Insights; mark your own sales; do not lower `min_comps` just to see pushes |
| Many tier-2 and tier-3 deals, few tier 1 | Mailbox listings cannot be re-checked; or comps thin | Read the "confidence" reasons on a card; they list every unmet condition |
| A deal is sold or wrong when you click | Listing changed after the re-check, or a mismatch | Mark it Skipped with a reason that names the problem ("scam", "wrong model", "sold"); check `dh verify <id>` |
| Round summary shows 0 listings for a source | Layout change (a parser broke), or a block | `dh probe --source <name> --query "rtx 4090"`; look in `/health`. A zero-result sweep is never used as evidence that listings vanished |
| `blocked` or `disabled` in status | The site refused plain polite requests | Wait for the backoff; restart to clear a disable. If it repeats, raise `fetch.min_interval_s` or set the source to `enabled: false` with a `reason`. Do not circumvent |
| Rounds run longer than the interval | Too many arms or slow LLM | Lower `budget`; check `llm_max_wait`; raise `models.worker.parallel` only if the model has slots |
| `llm_deferred` is high | More uncertain titles than `max_llm_items` | Raise `max_llm_items`, or improve the rules matcher so fewer titles need the LLM |
| Rounds say "rules-only" or `foreign_model` | Another model is loaded in llama-swap | Unload it, or map roles to loaded models (see [CONFIGURATION.md](CONFIGURATION.md#modelsyaml)) |
| `dh start` refuses on RAM | Another workload holds the memory | Stop it; do not lower the guard casually |
| Rounds stopped, `frozen_changed` | A frozen file was edited while running | `dh stop`, `dh start` |
| Same deal pushed twice | Should not happen: one push per deal is enforced | Check `deal_pushes`; report it as a bug |
| Costs exceed value | Rounds too frequent, or nothing clears the floors | Increase `round_interval_s`, shrink `budget`, or fix the cause upstream (coverage, valuation) |
| Too many false tier-1 | Scam score too lax, or comps wrong | Raise the scam weights or lower `confidence.p_scam_max`; mark Skipped with reasons; check the comps on the card |
| Valuation looks too high | Asks dominating comps; stale retail ceiling | Lower `ask_to_sale_ratio` or `ceiling_factor`; check `/books` realised vs forecast |
