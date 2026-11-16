# Validation, 30 October 2026

This page records a live validation of Deal Hunter on the owner's rig: real sources, real local models, dry
rounds (no push, no publish). It lists what was measured, every deal found and how it was judged, the bugs found
and fixed, the profile of a round, and the known limits. It was run in a development clone so that the owner's
running agent was never touched.

## Summary

- **16 dry rounds** in the development clone, 01:08 to 03:33 CET on 30 October (2 h 25 min), one every 600 s
  (rounds 2 and 3 were 2.5 min apart while the loop script was switched). 12 rounds used the LLM, 4 ran on the
  rules alone because another workload held models that could not be used without evicting them, or free RAM
  was under 20 GB.
- **Sources:** 1147 of 1152 searches succeeded; the 5 failures were HTTP 429 from Allegro Lokalnie, each followed
  by the designed backoff. No sweep timed out and no round logged an error. 20 803 listing sightings, 5 314
  evaluations, 2 413 candidates, 1 554 LLM calls and 782 811 tokens.
- **Deals:** 1 (tier 4, a DDR5 64 GB kit on Kleinanzeigen: plausible but unproven, see below). No tier 1, 2 or 3,
  as expected from an empty ledger: every estimate rests on asking prices only.
- **Round cost:** 64-149 s per round (median 81 s; 30-80 s more when `dh` had to load the model first), 5-8 s of
  CPU, 183-226 MB peak RSS for the `dh` process; the lowest free RAM seen during any round was 17.4 GB.
- **Also judged:** the 13 deal cards of the owner's own live rounds of 29 October. 4 were false positives (a
  laptop, a water block, a swap ad, an exit price inflated by laptop asks); all four classes are now fixed.
- **Fixed in this wave:** 13 deferred minors from the build ledger; 7 issues found by this validation and its
  review (F1-F4, F6, F10, F11); two card readability fixes (F8, F9). Tests: 2466 passed, 4 skipped (2332 passed before this wave).

## Set-up

- **Where.** A clone of `feat/build` at `~/Desktop/github_repos/deal-hunter-dev`, with its own venv and its own
  `var/` (empty database: a cold start). Its `config/local/runtime.yaml` sets `unit_name: deal-hunter-dev`
  (new in this wave, see below), so `dh round` there is not refused because of the owner's `deal-hunter` unit.
  Its `config/local/publish.yaml` points the publisher away from the owner's log repo.
- **What ran.** `dh round --once --dry --json`, one round every 600 s start to start (the default
  `round_interval_s`), driven by a shell loop that recorded, per round: the round's JSON statistics, the code
  revision, `/usr/bin/time -v` (wall time, CPU time, peak RSS of the `dh` process) and the lowest
  `MemAvailable` seen while the round ran (sampled every 2 s).
- **Models.** The validation never evicted another workload's model. Before each round the loop read `llama-swap /running`: when another
  workload had the duo group loaded, the roles were mapped to it (worker `qwen3.6-35b-a3b`, senior and advisor
  `qwen3.8-flash-next`); when only `qwen3.8-flash-next` was loaded, every role used it; when nothing at all was
  loaded and at least 40 GB were free, `dh` loaded `qwen3.8-flash-next` itself and released it after the round;
  in every other case (another model group, or less than 20 GB free) the round ran with `--no-llm`.
- **Cold start.** The clone's ledger started empty: no sold comparables, no price history, no eBay keys, no
  alert mailbox. Every estimate is built from live asking prices only (weight 0.3 each, discounted x0.92), so
  every deal is low confidence by construction. Zero tier-1 deals is the expected result (tier 1 needs 12
  effective sold comparables from the last 30 days, which takes weeks of watched time).

## Rounds

Times are CET on 30 October 2026. "Round s" is the round as `dh` measures it; "Wall s" adds start-up, model
warm-up and shut-down. LLM column: items sent to the worker / calls (incl. senior confirms) / tokens. Code is the
commit the round ran (the fixes landed while the validation ran, so later rounds use later code).

| # | Start | LLM roles | Code | Searches ok | Listings | Evaluated | Candidates | LLM items / calls / tokens | Deals | Round s | Wall s | CPU s | Peak RSS MB | Min free RAM GB |
|---:|---|---|---|---|---:|---:|---:|---|---|---:|---:|---:|---:|---:|
| 1 | 01:08 | q3.8 (all roles, already loaded) | 1211c98 | 72/72 | 1180 | 1091 | 590 | 200 / 222 / 117 159 | 0 | 86 | 90 | 6.5 | 192 | 20.7 |
| 2 | 01:18 | off (unmapped models loaded) | c0ac746 | 72/72 | 1240 | 716 | 511 | - | 0 | 149 | 149 | 7.6 | 226 | 19.8 |
| 3 | 01:21 | duo (worker q3.6, senior q3.8) | b4d03f6 | 72/72 | 955 | 633 | 173 | 200 / 209 / 126 746 | 0 | 74 | 75 | 5.5 | 183 | 18.5 |
| 4 | 01:31 | off (< 20 GB free) | b2844fa | 72/72 | 1217 | 571 | 406 | - | 0 | 80 | 81 | 7.4 | 194 | 18.8 |
| 5 | 01:41 | q3.8 (loaded by dh) | fdfe34d | 72/72 | 1264 | 618 | 147 | 200 / 225 / 135 159 | 0 | 81 | 115 | 7.8 | 191 | 20.8 |
| 6 | 01:51 | q3.8 (loaded by dh) | 3a53886 | 71/72 | 1195 | 461 | 106 | 200 / 214 / 128 886 | 0 | 82 | 116 | 5.8 | 183 | 20.2 |
| 7 | 02:01 | q3.8 (loaded by dh), swapped out mid-round | 3a53886 | 71/72 | 1504 | 432 | 186 | 200 / 197 / 0 | 0 | 82 | 115 | 5.7 | 191 | 21.8 |
| 8 | 02:11 | q3.8 (all roles, already loaded) | b38754e | 72/72 | 1249 | 321 | 79 | 200 / 237 / 137 620 | T4: 1 | 83 | 92 | 7.1 | 210 | 22.7 |
| 9 | 02:21 | q3.8 (all roles, already loaded) | b38754e | 72/72 | 1326 | 89 | 32 | 72 / 85 / 46 859 | 0 | 68 | 73 | 5.3 | 194 | 22.4 |
| 10 | 02:31 | duo (worker q3.6, senior q3.8) | 643c8f1 | 72/72 | 1478 | 83 | 43 | 54 / 56 / 29 453 | 0 | 79 | 160 | 5.3 | 187 | 17.7 |
| 11 | 02:41 | off (< 20 GB free) | 643c8f1 | 72/72 | 1283 | 114 | 67 | - | 0 | 64 | 70 | 5.2 | 189 | 17.4 |
| 12 | 02:51 | q3.8 (loaded by dh) | 643c8f1 | 72/72 | 1312 | 50 | 8 | 45 / 48 / 28 416 | 0 | 80 | 114 | 5.2 | 183 | 23.0 |
| 13 | 03:01 | off (pedro group loaded) | 643c8f1 | 71/72 | 1386 | 56 | 28 | - | 0 | 86 | 91 | 5.4 | 188 | 22.6 |
| 14 | 03:11 | q3.8 (loaded by dh) | 643c8f1 | 71/72 | 1308 | 74 | 33 | 47 / 57 / 30 774 | 0 | 81 | 117 | 5.4 | 185 | 22.4 |
| 15 | 03:21 | q3.8 (loaded by dh) | 643c8f1 | 71/72 | 1359 | 4 | 4 | 3 / 3 / 1 135 | 0 | 79 | 112 | 5.2 | 183 | 22.8 |
| 16 | 03:31 | q3.8 (loaded by dh) | 643c8f1 | 72/72 | 1547 | 1 | 0 | 1 / 1 / 604 | 0 | 82 | 115 | 5.2 | 183 | 22.9 |
| **all** | | 12 with LLM | | **1147/1152** | **20 803** | **5 314** | **2 413** | **1 554 calls, 782 811 tokens** | **T4: 1** | | | | | |

q3.6 = `qwen3.6-35b-a3b`, q3.8 = `qwen3.8-flash-next`. "Loaded by dh": nothing was loaded, so the round loaded
q3.8 and released it afterwards (nothing else was running to evict).

What the numbers show:

- **Steady state comes fast.** The first rounds evaluate almost everything (cold start), with the worker model
  at its cap of 200 items and about 240 deferred to the next round. By round 9 the backlog is gone; rounds 15 and
  16 evaluated 4 and 1 listings: only new and price-changed listings are evaluated (unchanged ones at most daily).
- **Drops over all rounds:** 1735 no product key (most are laptops, PCs and accessories), 1377 below the floor,
  1000 no valuation (fewer than 3 comparables for the key yet), 116 weak matches waiting for the LLM (new in this
  wave, rounds without the LLM), 37 bait or for-parts, 30 outside the price range, 29 pickup out of range, 6 hard
  excludes.
- **Round 7** is the round in which another workload loaded `gemma-4-26b-a4b` mid-round: llama-swap swapped
  q3.8 out, four worker calls hung for 60 s and every other call was refused by the open circuit breaker (197
  calls, 0 tokens). The listings waited as `llm_pending` and were evaluated in round 8. This led to fix F10.

Per source over the 16 rounds:

| Source | Searches | OK | Failed | Listings seen |
|---|---:|---:|---:|---:|
| allegro_lokalnie | 172 | 167 | 5 (HTTP 429, backed off) | 4816 |
| bazos_cz | 149 | 149 | 0 | 148 |
| bazos_sk | 141 | 141 | 0 | 122 |
| hardverapro | 176 | 176 | 0 | 9740 |
| hotukdeals (feed) | 16 | 16 | 0 | 459 |
| kleinanzeigen | 176 | 176 | 0 | 3667 |
| marktplaats | 136 | 136 | 0 | 1354 |
| pepper_pl (feed) | 16 | 16 | 0 | 464 |
| retail (feed) | 16 | 16 | 0 | 0 (emits only price errors; none seen) |
| sprzedajemy | 154 | 154 | 0 | 33 |

Not enabled (owner set-up pending): Allegro.pl and OLX (alert mailbox), eBay DE/PL and eBay sold prices (keys).

## Deals found by the validation rounds

One deal in 16 rounds. Its card was read in full.

| Deal | Tier | Listing | Buy | Exit P25 / median | Judgement |
|---|---|---|---:|---:|---|
| 1 (round 8) | T4, low confidence | "DDR5 64GB (2 x 32GB) Kingston FURY Beast 5600", Kleinanzeigen | 450 EUR (1978 PLN) | 3082 / 3679 PLN | **Plausible, unproven: a correct watch item, not a buy signal.** The key is right (the 2x32 layout is in the title). The 11 comparables are Polish asks for 64 GB DDR5 kits; two are this exact model (2990 and 3499 PLN asked, shown x0.92). The conservative net (109 USD) is just under the 120 USD moderate-effort floor, hence tier 4. The card flags it `too_cheap` and tells the buyer to assume a scam until a live video call proves the item. The same listing was deal #10 of the owner's live rounds: the old and new code agree on it. |

No laptop, PC, accessory, lot, swap ad or for-parts listing reached a deal in the validation rounds, after the
fixes F2-F4 landed (round 6 onwards) or before them.

## The owner's live deals of 29 October (read-only review)

The owner's agent logged 13 deals in its first rounds on 29 October (old code, before the final fix wave). Their
cards in the owner's checkout were read, never changed. They are the only real deals available for judgement, so
each was judged here; the false-positive classes they show were checked against the current code and fixed when
they still reproduced.

| # | Tier | Listing | Buy | Exit P25 / median (asks) | Judgement |
|---|---|---|---:|---:|---|
| 1 | T3★ | MSI "Core Ultra 9 275HX ... RTX 5090" on Marktplaats (category windows-laptops) | 3197 EUR | 20781 / 25788 PLN | **False positive: a laptop** valued as a desktop RTX 5090. Still reproduced on the current matcher (certain desktop key). **Fixed** (CPU, drive and screen-size signals make it a system). |
| 2 | T3 | "Karta graficzna Rtx3090 24GB", Allegro Lokalnie | 3500 PLN | 5151 / 6348 PLN | Plausible but unproven. Nine PL asks of 4462-8170 PLN (after x0.92); asks are not sales and two are outliers. Low confidence, labelled "thin price data". Correct tier for what is known. |
| 3 | T3 | "Pamięć ram Kingston Fury 64GB DDR5 6000", Allegro Lokalnie | 2950 PLN | 3956 / 4370 PLN | Weak match (0.70): read as a single 64 GB DIMM, probably a 2x32 kit. Marginal. Low confidence is right. |
| 4 | T4 | "Gigabyte rx 6800xt 16gb OC", Allegro Lokalnie | 380 PLN | 1722 / 2022 PLN | Almost certainly scam bait or a typo (a 6800 XT at 380 PLN). The scam score caught it (p_scam 0.354, "scam score too high"); it stays a T4 watch item, never a push. Acceptable. |
| 5 | T4 | Zotac RTX 5080 16GB, Kleinanzeigen | 1500 EUR | 8083 / 8487 PLN | Marginal. Its comparables included an MSI Vector laptop and a pre-built PC. Fixed by the same system rule as #1. |
| 6 | T4 | Asus RTX 4060 8GB, Marktplaats | 300 EUR | 2300 / 3450 PLN | **False positive: the exit price was inflated by laptop asks** (Predator 16 at 6256 PLN, HP Omen Transcend at 4139 PLN among four comparables). 300 EUR is the normal price of a 4060. **Fixed**: "Predator 16" is a laptop series; CPU/drive/screen signals keep laptops out of the comparables. |
| 7 | T3★ | "rtx 5090 asus tuf tausche gegen 5080", Kleinanzeigen | 4000 EUR | 21145 / 28304 PLN | **False positive: a swap ad.** Already fixed before this wave (swap ads are hard-excluded). |
| 8 | T4 | ASUS TUF RTX 3090 OC, Kleinanzeigen | 900 EUR | 5151 / 6440 PLN | Unproven cross-border spread (DE buy, PL asks). Two comparables were absurd asks (14 904 and 14 720 PLN for a 3090). Low confidence; fair as a watch item. |
| 9 | T3 | Transcend DDR5 64GB (2x32) 5600, Allegro Lokalnie | 2600 PLN | 3219 / 3679 PLN | Plausible, marginal (conservative net 54 USD against a 50 USD floor). |
| 10 | T4 | Kingston Fury Beast DDR5 64GB (2x32) 5600, Kleinanzeigen | 450 EUR | 3082 / 3679 PLN | Plausible cross-border spread, marginal. |
| 11 | T3 | "Rtx 4070ti zotac gaming", Allegro Lokalnie | 2400 PLN | 3633 / 3680 PLN | Plausible true positive: five consistent asks at 3496-3864 PLN. Worth a look by a human. |
| 12 | T3 | "Karta graficzna RTX 4070Ti Suprim X", Allegro Lokalnie | 2600 PLN | 3496 / 3680 PLN | Plausible, same market as #11. |
| 13 | T3 | "Blok wodny Alphacool Rtx 4070TI Asus rog Strix", Allegro Lokalnie | 380 PLN | 2944 / 3680 PLN | **False positive: a water block** ("blok wodny") valued as the card. Still reproduced on the current matcher (0.95). **Fixed** (water-block words in PL, CZ, HU, DE, NL). |

Verdict on the 13: 4 clear false positives (#1, #6, #7, #13), all now fixed (#7 before this wave, the other three
in this wave); 1 probable scam correctly scored (#4); 8 plausible low-confidence deals that only sold-price data
can confirm or refute. None was tier 1 or tier 2, and none was pushed.

## Findings and fixes

| # | Finding | Where seen | Fix (commit) |
|---|---|---|---|
| F1 | `dh round` in a second checkout refused because it asked systemd about the owner's unit `deal-hunter`; `dh stop`/`dh status` there counted the owner's serve process as their own. | Setting up this validation | `DH_UNIT_NAME` / `runtime.unit_name`; serve processes are matched by their `DH_ROOT` (1211c98) |
| F2 | Water blocks keyed as the card ("Blok wodny Alphacool RTX 4070 Ti", 380 PLN, became a T3 deal). | Live deal #13 | Water-block words in PL, CZ, HU, DE, NL are accessories (3a53886) |
| F3 | Laptops and pre-built PCs keyed as the bare card (certainty 0.95): a T3★ deal of their own, and their asks inflated the comparables of real cards. | Live deals #1, #5, #6 | CPU, TB-drive and screen-size words in a GPU title make it a system (certainty 0.5, never a comparable, the LLM decides); "Predator 16" is a laptop series (3a53886) |
| F4 | With the LLM off or unreachable, a weak rules key (a system, a lot, certainty < 0.7) was still valued as the bare product. | Code review while tracing F3 | It waits for the LLM (`llm_pending`, dropped as `weak_match_waits_for_llm`) (3a53886) |
| F5 | The owner's unit had done no round since 21:48 on 29 October while `dh status` said RUNNING: a frozen file (`evals/must_pass.jsonl`) changed under it, and the code it runs predates the PAUSED status line. | `dh status` and the journal of the live unit (read-only) | Already fixed in the code (the pause is shown and pushed). The owner must restart the unit: OWNER-TODO.md item 1 |
| F6 | Every round blocked its event loop for about 9.6 s on two sequential `bw-secret` calls (eBay keys missing), and for 5.1 s on 3009 SQLite commits. | cProfile of round 4 | See Performance (fdfe34d) |
| F7 | Five HTTP 429 from Allegro Lokalnie, one each in rounds 6, 7, 13, 14 and 15 (the rounds table's 71/72). Each time the fetcher backed off (60 s, doubling on consecutive blocks) as designed and the next round started clean. | Health table | None needed |
| F8 | A card listed "active_ask 3219.99 PLN" while the linked page asked 3499.99 PLN: the x0.92 ask-to-sale discount was not explained. | Reading live deal cards | One-line legend under the comparables (b38754e) |
| F9 | A card showed "p_scam: 0.043107254941086116". | Validation deal 1 | Three decimals on the card; the JSON keeps the value (643c8f1) |
| F10 | The foreign-model guard ran only at the start of a round. In round 7 another workload loaded its model mid-round; any further call would have made llama-swap load the role model back and evict theirs. | Round 7 (0 tokens), llama-swap journal | Every LLM call of a round, and of the improvement step after it, first checks `/running` (no cache; concurrent calls share the check in flight) and is refused while a model outside the roles is loaded (8ae8626, d61a849, 04d0f08). An owner `--apply` whose live calls were refused stays queued, and an advisor outage keeps the improvement trigger pending (3413130) |
| F11 | The matcher fix does not touch verdicts already stored: old laptop/PC/water-block extractions at 0.95 stay comparables until re-evaluated, a gone listing for up to 90 days. | Review of this wave | `dh recheck [--apply]`, run once with the agent stopped (833d0f1). Dry run on the validation database: 11 of 1044 rules comparables (rounds before 3a53886) are no longer accepted, e.g. "OKAZJA Predator 16" Intel i7 GeForce RTX 4060 1TB" and "Msi - Inch Gb 1 Tb Geforce Rtx 5080" |

Deferred minors from the build ledger, closed in this wave, each with tests:

| Minor | Commit |
|---|---|
| Matcher defect vocabulary: cracked, pęknięta, gebrochen, prasklá, törött PCB; "without core" | ee9c302 |
| SSD/RAM accessory window anchored on the first drive token only; `heatsink` | ee9c302 |
| RSS `_OLD` word boundaries; coupon "do 100 zł" read as the price | 823f2a7 |
| Publisher `reset --hard` dropped clone-only cards (and unpushed weekly reports) | 4411c85 |
| `_link` double escape on the fallback; unescaped `)` in a URL | 09ed453 |
| A successful paced page load did not reset `consecutive_blocks` | bffd0fb |
| `fx_stale` / `fx_missing` scope (recent comps only; stale only beyond 3 days) | c0ac746 |
| Rollback reply warns about queued applies; queued applies expire (`improve.apply_expiry_days`, 7); serve waits for an owner command holding the lock instead of 30 s | 28c3ba3 |
| Scheduler-level `paid_booked` test; behavioural (not source-inspection) bandit-clamp test | b4d03f6 |

## Performance

A round is I/O-bound: polite per-domain pacing and the LLM dominate its wall time, and the `dh` process uses
6-8 s of CPU per round with a peak RSS under 230 MB. The RAM that matters is the model's (about 30 GB for
`qwen3.8-flash-next`); the lowest free RAM seen during any round is in the table above.

cProfile of one dry round before and after the fix (`python -m cProfile -o rN.prof dh round --once --dry`):

| Measure | Round 4 (before, b2844fa) | Round 5 (after, fdfe34d) |
|---|---:|---|
| Wall time under the profiler | 80.5 s (LLM off) | 115.0 s (LLM on, model loaded by `dh` first) |
| Event loop blocked by `bw-secret` inside the round | 9.6 s (2 sequential calls) | 0 s (prefetched before the round, both refs in parallel: 4.8 s once per run) |
| SQLite commits | 3009 calls, 5.1 s | 1284 calls, 1.5 s |
| ... of which one per upserted listing | 1217 calls, 3.6 s | 0 (one commit per sweep) |
| Time waiting in `epoll` (network, LLM) | 61 s | 103 s |

The two fixes take about 13 s of blocking work out of every round (`dh round --once`) and out of the first
round of every `serve` run; nothing else in the profile was worth changing (the next CPU items are text folding
for redaction at 0.5 s and the matcher at 0.2 s per round).

## Known limits

- **Cold ledger.** All validation estimates came from asking prices of the last three days. Asks are not sales:
  every deal is low confidence until the clears-at ledger has weeks of watched time, or eBay sold prices exist.
  No tier-1 or tier-2 deal is possible yet, by design.
- **Sources that need the owner.** Allegro.pl and OLX (alert mailbox), eBay DE/PL (keys) and eBay sold prices
  (Marketplace Insights) were off. See OWNER-TODO.md.
- **Shared models.** The rig is shared. Two rounds ran without the LLM because another workload held models the
  validation could not use without evicting them; their LLM listings were re-queued (`llm_pending`) for the
  next LLM round, as designed.
- **The LLM backlog.** A cold start sends many unknown listings to the worker model: every LLM round hit the cap
  of 200 items and deferred about 240 to the next round. It drains as the ledger fills (unchanged listings are
  re-valued at most daily); raise `scheduler.max_llm_items` only if the worker has idle slots.
- **Matcher precision vs recall.** The system rule (CPU, TB drive or screen size in a GPU title) makes a few real
  card ads uncertain ("RTX 4090 ... 1TB ready" is now asked to the LLM instead of keyed by rules). That costs LLM
  calls, not deals. A bare "<SSD> 2TB NVMe heatsink" is read as a heatsink; "with heatsink" keeps the SSD key.
- **Old verdicts in a live database.** The matcher fix does not rewrite what is already stored: in the owner's
  database, laptops, PCs and water blocks stored at certainty 0.95 by the old matcher stay comparables until they
  are re-evaluated, and a gone listing is never re-evaluated (it stays a comparable for up to 90 days). Run
  `dh recheck` then `dh recheck --apply` once with the agent stopped (OWNER-TODO.md item 1, OPERATIONS.md).
- **Live deals were judged from their cards only.** Nobody bought anything; "plausible" means the arithmetic and
  the comparables hold up, not that the item would sell at P25.

## How to reproduce

```bash
git clone ~/Desktop/github_repos/deal-hunter ~/Desktop/github_repos/deal-hunter-dev
cd ~/Desktop/github_repos/deal-hunter-dev && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
mkdir -p config/local
printf 'unit_name: deal-hunter-dev\n' > config/local/runtime.yaml
printf 'remote: null\nrepo_web_url: null\nlog_dir: ~/.local/share/deal-hunter/log-dev\n' > config/local/publish.yaml
printf 'port: 8712\n' > config/local/web.yaml   # only matters for `dh start`; the owner's agent uses 8711
curl -s localhost:8080/running            # map config/local/models.yaml to what is loaded (docs/RUNNING.md)
.venv/bin/dh round --once --dry --json
```

The loop scripts and per-round raw data of this run are in the clone's `var/validation/` (not in the repository).

