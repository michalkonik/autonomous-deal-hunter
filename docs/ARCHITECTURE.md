# Architecture

This page describes what the code does today. Where the design spec says something the code does not do yet, it
says so ([What is and is not wired yet](#what-is-and-is-not-wired-yet)).

## Components and data flow

```
            +------------- frozen layer (the agent cannot write it) -------------+
            | economics.py  customs.py  tiering.py  rules.yaml  economics.yaml    |
            | data/ (fees, shipping, taxonomy)  improve/gates.py  evals/          |
            +----------------------------------------------------------------------+

 sources ---> SWEEPS ------> ledger.observe -----> cheap filter ---> match/extract ---> VALUATION
 (adapters,   (per source,   (listings, prices,    (rules matcher,   (rules first,      (comps per exit
  RSS, retail, polite        observations,         price range)      worker LLM when    channel: owner sales,
  alert        fetcher,      disappearances)                         < 0.9 certain)     eBay sold, ledger,
  mailbox)     rate-limited)                                                            asks, retail ceiling)
                                                                                            |
                                                                                            v
   ntfy push <--- TIER-1 <--- verify (alive + same price) <--- tiering <--- risk <--- ECONOMICS (net per
   (email         only       detail fetch, photo check        (effort,       (scam     exit channel, best exit)
   fallback)                                                   confidence)    score)
        |
        v
   DEAL CARDS (var/cards/deals/*.md + .json) ---> web page :8711 (LAN) ---> log publisher ---> deal-hunter-log
        ^                                                                                         (GitHub, every 30 min)
        |
   BANDIT (every round) picks which source x query arms run next; BOOKS record energy, tokens and fetches
```

| Module | Job |
|---|---|
| `dh/scheduler.py` | One `run_round`: arms, sweeps, evaluation of every new or changed listing, deals, bandit update |
| `dh/bandit.py` | Discounted Thompson sampling over source x query arms, with an exploration floor |
| `dh/sources/` | One adapter per platform, plus the alert mailbox, RSS feeds and the retail watch |
| `dh/fetch.py` | The polite fetcher: honest User-Agent, robots.txt, per-domain pacing, backoff, snapshots |
| `dh/ledger.py` | The clears-at ledger: observation windows, disappearances, "probably sold" comps |
| `dh/match.py`, `dh/extract.py`, `dh/taxonomy.py` | Product key, condition, defects, bundle detection; rules first, worker LLM when unsure |
| `dh/valuation.py`, `dh/partout.py` | Exit price distribution per product key, condition and channel; part-out value of whole PCs |
| `dh/economics.py`, `dh/customs.py`, `dh/money.py` | Net profit engine, duty lookup, NBP FX (frozen except `money.py`) |
| `dh/risk.py`, `dh/photos.py` | Hard excludes, scam score, safe-buying checklist, photo reuse |
| `dh/tiering.py` | Effort class, confidence, tiers (frozen) |
| `dh/verify.py`, `dh/cards.py`, `dh/dedupe.py` | Live re-check, evidence screenshot, deal card, duplicate detection |
| `dh/notify.py`, `dh/publish.py` | ntfy and email push; log repo sync and mark import |
| `dh/books.py` | Energy, token and fetch cost per round |
| `dh/web/` | The local web page (Starlette) |
| `dh/runtime.py`, `dh/cli.py` | `dh start/stop/status`, the `serve` process, the CLI |
| `dh/improve/` | The gated self-improvement library (see below) |

## The round pipeline, step by step

`scheduler.run_round` runs once every `scheduler.round_interval_s` (600 s, start to start). In order:

1. **Frozen check.** If any frozen file changed since the agent started, the round raises `FrozenChanged` and the
   rounds PAUSE (health event `frozen_changed`, `var/run/paused.json`, which `dh status` prints as "PAUSED: frozen
   file changed (<path>)", and one ntfy push at default priority). The web page and the publisher keep running; the
   rounds resume when the files are back as at start (`frozen_restored`).
2. **Maintenance** (at most daily, `maintenance_hours`): delete photo hashes, raw snapshots and saved alert mails
   older than the retention.
3. **LLM guard.** If llama-swap runs a model outside the configured roles, this round runs on rules alone and the
   listings that need the LLM are marked `llm_pending` (health event `foreign_model`). The check is repeated before every
   LLM call of the round and of the improvement step after it (concurrent calls share one check in flight): a
   model another workload loads mid-round stops those calls (they would make llama-swap evict it).
4. **FX warm-up** for EUR, CZK, HUF, GBP, USD (NBP mid rate). The round's day is the LOCAL date, the same date
   `money.Fx` pins rates by. Each appraisal records every rate it used (`money.RecordingFx`); the deal's evidence
   keeps them as `fx_rates` (and the USD rate as `fx_usd`, shown on the card), so `dh verify` and the books replay
   the net with exactly those rates, even after NBP publishes the day's table (before it does, the day is served
   the previous table's rate, which is not pinned).
5. **Plan the arms.** An arm is one source x one playbook `query` item. A playbook `cadence` item spaces a
   source's bandit arms (see [`CONFIGURATION.md`](CONFIGURATION.md#the-playbook)). The round runs:
   - the **sentinel arms** (`sentinel: true` queries, the control group, never edited by the agent);
   - one **feed arm** per feed source (`scheduler.feed_sources`: Pepper.pl, hotukdeals, the retail watch), with an
     empty query, and never ledger-swept;
   - up to `budget` **bandit-chosen arms** (60 by default), at most `max_searches_per_source` per source. Arms of
     disabled adapters or of blocked or disabled domains are skipped.
6. **Re-queue.** Listings stored in the last `requeue_hours` (48) whose evaluation never happened or was deferred
   (an acked mail, a cut round, no LLM slot) are evaluated now.
7. **Sweeps.** Sources run in parallel, each source's arms one after another, each source under its own deadline
   (`sources.sweep_timeout_s`, 300 s). The alert mailbox is polled once per round. Every returned listing is
   written through `ledger.observe`, which records the sighting and any price change. A successful, non-empty
   sweep can mark other listings as disappeared.
8. **Evaluate each new, price-changed or stale listing** (stale = last evaluation older than `reeval_hours`, 24 h):
   1. Cheap filter with the rules matcher. Wanted ads, box-only and for-parts listings are dropped. A listing with
      no product key and no hardware word is dropped. A keyed price outside 0.15x to 3x the key's median known
      price is dropped (`price_range`).
   2. If the rules are less than 0.9 certain, ask the **worker LLM** to extract the product, through
      `models.worker.parallel` slots (4) and at most `max_llm_items` (200) per round. The rest wait.
      Whole PCs go to the part-out valuation instead.
   3. **Valuation** for every enabled exit channel. A channel needs at least `min_estimate_comps` (3) comparables.
      The clears-at ledger comparables (probably sold, live asks) are computed once per (key, condition) per round
      and shared by every candidate of that key, each estimate dropping the candidate's own listing; the median ask
      that decides "probably sold" comes from one in-memory index per (key, currency) per round.
   4. **Economics** for every channel, then the best exit by conservative net.
   5. **Risk** (hard excludes, scam score) and **tiering** (effort class, confidence, tier).
   6. For tiers 1-3: fetch the detail page (not for Allegro or OLX mailbox listings; a Kleinanzeigen or eBay one goes to its real adapter), run the photo-reuse check, re-run the
      flags on the full description, re-appraise. For tiers 1-2: **finalist triage** first (`dh/triage.py`): the
      senior model reads the listing (inside `<listing>`, no tools, schema-checked reply) with `prompt.triage`; ok
      keeps the tier, suspicious caps it at tier 3, scam is a hard exclude (`triage_exclude`); no answer (no LLM, an
      error, a malformed reply) caps confidence at medium, so a tier-1 push always had a triage. The result is
      cached per listing while title and price are unchanged and shown on the card. Then the **live re-check** (listing alive and at the same
      price). A gone listing is dropped and its existing deal gets the status `gone` (also when a price change
      takes it below the floor); a changed price is re-valued at the new price; a confirmed re-check marks
      the deal "verified now" and may lift it to tier 1. Allegro and OLX mailbox listings can never be re-checked, so
      they stay unverified and top out at tier 2.
   7. **Save**: one deal per listing (a unique index). The same physical item on another listing is not a second
      deal (`dh/dedupe.py`): same key, buy price within 5%, within 7 days, and either the same seller hash or, across
      platforms, the same city AND similar titles (rapidfuzz `token_set_ratio` >= 85). The card is written, and for tier 1 a discovery
       screenshot is taken and a push is reserved (at most one push per deal, ever). The screenshot browser (headless
       Chrome) checks robots.txt and the domain's block state BEFORE every navigation, redirects included, and aborts
       a refused one (`verify.navigation_guard`); a challenge page is discarded, never worked around.
9. **After the sweeps:** wait for the evaluations (round deadline `round_timeout_s`, 1500 s, plus
   `timeout_grace_s`); retry pending pushes; ingest eBay active asks as comps for up to `ebay_comp_keys` keys;
   turn owner "sold" marks into `owner_sale` comps (once each). Newly disappeared listings are confirmed with
   `alive()` (at most `alive_confirm_per_source` per source), which can undo a false disappearance.
10. **Bookkeeping.** Write the `rounds` row (the full stats as JSON in `notes`), the four cost rows
    (energy, llm, fetch, paid_api), update the bandit for every executed arm, discount all arms by gamma, and
    adapt the next round's budget: it grows by `budget_boost` (to `budget_max`) after a round where no LLM item
    ever waited for a slot, and shrinks back when the queue backed up.

## The frozen layer

The agent can never write these, and the rounds refuse to run if any of them changes while the agent is up
(`dh/config.py: FROZEN_PATHS`, a SHA-256 digest taken at start):

`config/rules.yaml`, `config/economics.yaml`, `dh/economics.py`, `dh/customs.py`, `dh/tiering.py`,
`dh/improve/gates.py`, the whole `data/` directory, `evals/`, and the local overrides
`config/local/rules.yaml` and `config/local/economics.yaml`.

Consequence for the owner: **edit frozen files only while the agent is stopped.** Editing one while it runs pauses
the rounds (`frozen_changed`, "PAUSED" in `dh status`, one push) until you undo it, or `dh stop` and `dh start` to
accept it. Not frozen: `valuation.yaml`,
`scheduler.yaml`, `sources.yaml`, `models.yaml`, `notify.yaml`, `books.yaml`, `publish.yaml`, `pickup.yaml` and
`web.yaml`. The scam weights are in `economics.yaml` (`risk`), so they are frozen, while the scam *keywords* are
playbook items.

Why: systems that could reach their own scorer gamed it. See
[`SELF-IMPROVEMENT.md`](SELF-IMPROVEMENT.md#what-is-frozen-and-why).

## The store

One SQLite database in WAL mode: `var/dh.db`. Decimals are stored as text, datetimes as UTC ISO-8601 text.

| Table | Holds |
|---|---|
| `listings`, `observations` | Every listing (seller only as a salted hash) and its price history |
| `windows`, `sweeps`, `listing_arms` | Observation windows (`dh start` to `dh stop`), per-arm sweeps, which arm returned which listing |
| `extractions` | Product key, condition, defects and flags per listing, and the method (`rules` or `llm`) |
| `comps` | Comparable prices by key, condition, channel and kind (`owner_sale`, `ebay_sold`, `probably_sold`, `active_ask`, `ask_history`, `retail_new`) |
| `deals`, `deal_pushes`, `pending_pushes` | Deals (net, tier, evidence), the one-push-per-deal guard, the retry queue |
| `marks`, `mark_comps`, `mark_imports` | Bought / Skipped / Sold marks, which sold marks became comps, imported log-repo edits |
| `evaluations` | Last evaluation of each listing (price, title hash, tier, note) |
| `rounds`, `costs` | One row per round with JSON stats; cost rows by kind |
| `arm_stats` | Bandit state per arm: pulls, discounted reward and fetches |
| `health` | Events per source and kind: `blocked`, `disabled`, `soft_backoff`, `robots_error`, `no_keys`, `sweep_timeout`... |
| `fx`, `duty_cache`, `retail_prices`, `retail_emitted`, `photo_hashes`, `maintenance` | Caches and housekeeping |
| `playbook_versions`, `proposals`, `shadow_results`, `playbook_counters`, `improve_*` | Self-improvement state |

Other files, all under `var/` (ignored by git): `var/cards/deals/YYYY-MM-DD/<id>-<slug>.md` and `.json`,
`var/snapshots/YYYY-MM-DD/<domain>/*.gz` (raw pages), `var/screenshots/`, `var/run/state.json` and
`var/run/round.pid`. The log repo clone defaults to `~/.local/share/deal-hunter/log`.

## Process model

The owner starts and stops the agent by hand, so nothing is installed as a service.

- `dh start` checks the guards, then runs
  `systemd-run --user --unit=deal-hunter --collect --property=KillMode=control-group python -m dh.runtime serve`.
  The unit is transient: it disappears when it stops. `KillMode=control-group` means stopping it kills every
  process the agent started, headless Chrome included.
- `serve` is one asyncio process containing three things: the **rounds loop**, the **web server** (uvicorn on
  `web.host:web.port`, default `0.0.0.0:8711`) and the **log publisher loop** (every `publish.interval_min`).
- Startup recovery closes observation windows that a crash left open. Shutdown (SIGTERM) cancels the current
  round, stops the web server and publisher, closes the windows and exits 0.
- `dh stop` stops the unit, closes windows (also a crash fallback), releases the role models with llama-swap
  `/unload` only if nothing else is loaded and none of them was loaded before `dh start`, then checks that no
  process is left.
- A foreground `dh round` and the service exclude each other (`var/run/round.pid`).
- The unit lives in the user's systemd session. If the owner's session ends, lingering must be enabled for it to
  survive (`loginctl enable-linger`); this is a systemd property, not something Deal Hunter configures.

## What is and is not wired yet

Checked against the code at the time of writing:

- **Self-improvement is wired.** `runtime.post_round` calls `improve.after_round` after every round of `serve` and
  of `dh round`, under `improve.post_round_timeout_s`; an error or a timeout there is a health event
  (`improve_error`, `improve_timeout`) and never stops the rounds. `serve` also writes the weekly report into the
  log repo every 168 watched hours. `dh playbook`, `dh eval` and `dh report --weekly` are real commands
  ([`RUNNING.md`](RUNNING.md)). Only `dh replay` is still a stub that prints "not implemented yet". Prompt changes
  always wait for the owner (`dh playbook --pending`, `--apply`).
- **Finalist triage and cadence are wired.** `prompt.triage` reviews every tier 1-2 candidate before its live
  re-check; `cadence` items change the round plan (`scheduler._Round._cadence`).
- **Trend-signal tier 4** is produced by `scheduler.trend_signal` (a rising 30-day trend on a non-losing listing
  below its floor), not by the frozen `tiering.assign`; thresholds are `scheduler.trend.*`.
- **Paid LLM APIs** exist behind `models.paid_api` and are off: the monthly cap defaults to 0, which makes the LLM
  client refuse every paid call; the books track paid spend per month.
- **`notify.quiet_hours`** delays tier-1 pushes inside the configured window until it ends (default: none).
- **Mailbox listings** from Kleinanzeigen (and eBay with keys) are verified through that platform's real adapter;
  Allegro and OLX ones stay unverifiable.
- **The Pedro `/deal-hunter` skill** from the spec lives outside this repository.
