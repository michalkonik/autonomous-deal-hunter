# Configuration

Every setting lives in `config/*.yaml`, `data/*.yaml` or `playbook/items.yaml`. Defaults below are the values in
the repository. Keys that the code reads with a built-in default but that are absent from the shipped file are
marked *(built-in default)*.

## How configuration is loaded

- `dh/config.py` reads every `config/<name>.yaml` into a section called `<name>` (`config/models.yaml` becomes
  `cfg.models`).
- **Local overrides:** `config/local/<name>.yaml` is deep-merged over the shipped file (nested maps merge, lists
  and scalars are replaced). `config/local/` is gitignored, so put machine-specific and secret-free settings
  there. A local file for a section that has no shipped file creates the section.
- **No-circumvention policy.** `sources.fetch.proxies`, `solve_captcha` and `logged_in` must stay `false`. If any of them is truthy, in the shipped file or in `config/local/`, `dh/config.py` raises `PolicyViolation` and every command that loads the configuration (`dh start`, `dh round`, `dh status`...) refuses to run, with a message naming the key.
- The configuration is read when a process starts. After editing, run `dh stop` and `dh start`.
- **Frozen files.** `config/rules.yaml`, `config/economics.yaml`, their local overrides and everything in `data/`
  are part of the frozen layer. If one changes while the agent runs, the rounds PAUSE: a `frozen_changed`
  health event, `dh status` shows "PAUSED: frozen file changed (<path>)" and you get one ntfy push. Undo the
  change and the rounds resume; to keep it, `dh stop` and `dh start`. Edit them only while stopped. See [`ARCHITECTURE.md`](ARCHITECTURE.md#the-frozen-layer).

## Secrets

Secrets never go into the repository or the log. They are read at run time through the `bw-secret` helper, which
takes `item#field`:

| Reference | Used by | Content | If missing |
|---|---|---|---|
| `bw-secret ebay#client_id`, `ebay#client_secret` | eBay Browse and Marketplace Insights | eBay developer keys. Further applications are `ebay2` … `ebay8` with the same fields. A missing `client_id` ends the list. `dev_id` is stored and not sent | eBay adapters disable themselves and record one `no_keys` health event |
| `bw-secret allegro#client_id`, `allegro#client_secret` | Allegro.pl public offer search | Allegro REST client id and secret | The Allegro adapter records one `no_keys` event. A verified app that Allegro has not granted `/offers/listing` records `not_verified` |
| `bw-secret dh-mail#imap` | The alert mailbox | JSON `{"host","port","user","password","folder"}`; `folder` defaults to `INBOX` | The mailbox adapter disables itself, one `no_credentials` health event |
| `bw-secret dh-mail#smtp` | Email fallback for tier-1 pushes | JSON `{"host","port","user","password","to"}` (STARTTLS) | A failed ntfy push is queued and retried for 6 h |

**Adding or changing a secret needs a restart.** The eBay keys and the IMAP login are read once per process (a
missing one is remembered as missing): after you store `ebay#client_id` / `ebay#client_secret`, `allegro#client_id` / `allegro#client_secret`, or `dh-mail#imap`,
run `dh stop` and `dh start`. Only `dh-mail#smtp` is read at each fallback e-mail.

The ntfy topic is not a secret in Bitwarden: a random 24-character string is generated on first use and written to
`config/local/notify.yaml`. Anyone who knows the topic can read your pushes, so keep it private.

## models.yaml

Which LLM does which job. All calls go through one OpenAI-compatible endpoint (llama-swap).

| Key | Default | Meaning |
|---|---|---|
| `base_url` | `http://localhost:8080/v1` | Chat completions endpoint |
| `roles.worker` | `gemma-4-26b-a4b-pool` | Extraction of the product from an unclear title, the part-out parts step |
| `roles.senior` | `qwen3.8-flash-next-uncensored` | The second-pass confirmation of an LLM match, the round reflection |
| `roles.advisor` | `qwen3.8-flash-next-uncensored` | Drafting playbook proposals (self-improvement library) |
| `paid_api.enabled` | `false` | **Paid API off.** When on, only the roles listed in `paid_api.roles` use the paid endpoint; every other role stays local |
| `paid_api.monthly_cap_usd` | `0` | Hard monthly cap. With `0` (default) no paid call is ever made: the LLM client raises `PaidCapReached` before any request leaves the machine. Otherwise it refuses a paid call once this calendar month's paid spend (UTC, from the books) plus the client's own session spend reaches the cap |
| `paid_api.base_url`, `api_key_secret` | `null`, `null` | OpenAI-compatible endpoint, and a `bw-secret item#field` reference for its key (never stored in the repo or the DB) |
| `paid_api.roles` | `{}` | Role -> model name on the paid endpoint, e.g. `{advisor: some-model}` |
| `paid_api.usd_per_mtok_in`, `usd_per_mtok_out`, `pln_per_usd` | `0`, `0`, `4.0` | Price per million tokens (USD) used to cost each call, and the rate used to show the spend in PLN. The books record it as a `paid_api` row per round (USD in `units`) and `dh books` shows it |
| `providers.<name>` | `{}` | Another endpoint. Same keys as `paid_api` (`base_url`, `api_key_secret`, `monthly_cap_usd`, prices, `roles`) except there is no `enabled: true` switch: the block is on unless `enabled: false`. `auth: xai_oauth` with `wire: responses` is `dh xai-login`: a device code approved at accounts.x.ai, stored in `~/.config/deal-hunter/xai-oauth.json`, no API key, calls `https://cli-chat-proxy.grok.com/v1`. `auth: grok_session` is the existing `grok login` file instead. A role listed here is not loaded on llama-swap. One role can sit on only one provider (`paid_api` counts). Each provider's spend is its own books row, `paid:<name>`, and its own monthly cap. An API key is a `bw-secret` reference, never the value |

A role with no remote entry stays on `roles.<role>` at `base_url` (llama-swap). When every role is remote, `dh start` does not load or unload llama-swap and does not apply the free-RAM floor. Store a key with `bash .claude/skills/setup-deal-hunter/scripts/put_secret.sh grok#api_key` (it prompts on your terminal; do not paste the key into a chat), then put the provider block in `config/local/models.yaml` and restart.
| `min_free_ram_gb` | `12` | `dh start` and `dh round` refuse below this much free RAM |
| `timeouts.connect_s`, `read_s` | `5`, `60` | LLM HTTP timeouts |
| `breaker.failures`, `cooldown_s` | `3`, `120` | After 3 consecutive transport errors the client refuses calls for 120 s and the round continues on rules |
| `worker.parallel` | `4` | Concurrent worker LLM calls per round (the gemma pool serves 4 slots) |
| `llama_swap_url` | `http://localhost:8080` | Control API (`/running`, `/unload`) used by `dh start` and `dh stop` |

The default setup is **local only**: the llama-swap group `pedro` loads both models at once, so a start does not
swap models in and out. The spec's fourth role, a tier-1 second opinion, is not a separate role: the `senior` role
does that confirmation.

**Mapping roles to models that are already loaded.** If llama-swap already serves other models, `dh start`
refuses (loading the role models would evict them). Map the roles to what is loaded, in
`config/local/models.yaml`:

```yaml
# config/local/models.yaml  (the "duo" group example)
roles:
  worker: qwen3.6-35b-a3b
  senior: qwen3.8-flash-next
  advisor: qwen3.8-flash-next
```

Use the names that `curl -s localhost:8080/running` lists. Because those models were loaded before `dh start`,
`dh stop` leaves them loaded. Delete the file to return to the defaults. See [`RUNNING.md`](RUNNING.md).

## rules.yaml (frozen)

| Key | Default | Meaning |
|---|---|---|
| `floors.easy` / `moderate` / `heavy` | `50` / `120` / `250` | Minimum **conservative net** in USD for the effort class |
| `tiers.t1_min_cons_net_usd` | `300` | Tier 1 needs at least this conservative net (and high confidence) |
| `tiers.t4_floor_margin` | `0.20` | Tier 4 is a conservative net within this share under its floor |
| `confidence.match_certainty_min` | `0.9` | High confidence needs this product-match certainty |
| `confidence.p_scam_max` | `0.05` | ...and a scam probability below this |
| `confidence.comp_max_age_days` | `30` | Age limit of the comparables that count |
| `confidence.verify_max_age_min` | `10` | The live re-check must be this recent |
| `confidence.min_comps.base` / `capital_over_5000` / `capital_over_15000` | `12` / `20` / `30` | Effective sold comparables needed for high confidence, by capital |
| `confidence.capital_thresholds_usd.mid` / `high` | `5000` / `15000` | The capital breakpoints for the line above |
| `confidence.medium.match_certainty_min` / `p_scam_max` / `min_comps_frac` | `0.75` / `0.15` / `0.5` | What medium confidence (tier 2) needs; comps are a fraction of the high requirement |
| `star_usd` | `500` | Expected net at or above this marks a deal with a star |
| `capital_cap_usd` | `30000` | Deals needing more capital are not logged |
| `repair.enabled` | `false` | When false, for-parts listings are hard-excluded |

Effort classes are decided in code (`dh/tiering.py`): **heavy** for origin outside the EU, a "needs VAT-UE" or
non-EU "BDO" flag, or a bundle of three or more items; **moderate** for any non-Polish leg, an unknown origin on a
non-Polish source, or an expected sale slower than 14 days; **easy** otherwise.

## economics.yaml (frozen)

The net-profit engine's inputs. The formula and worked examples are in [`ECONOMICS.md`](ECONOMICS.md); the open
tax questions are in [`../ACCOUNTANT-QUESTIONS.md`](../ACCOUNTANT-QUESTIONS.md).

| Key | Default | Meaning |
|---|---|---|
| `currency.internal`, `rounding`, `dp`, `fx_source` | `PLN`, `ROUND_HALF_UP`, `2`, `NBP_mid` | Money handling |
| `fx_spread` | `0.01` | Spread charged on every currency conversion |
| `tax.tax_mode` | `liniowy` | `liniowy` (19% + 4.9% health), `skala` or `ryczalt` |
| `tax.pit_rate`, `health_rate` | `0.19`, `0.049` | Liniowy rates |
| `tax.skala_pit_rate`, `skala_health_rate` | `0.12`, `0.09` | Skala rates used when `tax_mode: skala` |
| `tax.ryczalt_rate`, `ryczalt_health_pln_per_sale` | `0.03`, `0` | Ryczalt rate on the gross sale price; per-sale health cost |
| `tax.vat_rate` | `0.23` | Polish VAT |
| `tax.vat_ue_registered` | `false` | When false, intra-EU purchases from businesses carry `needs_vat_ue` and are Heavy |
| `tax.vat_margin_scheme_private` | `true` | Use the margin scheme for used goods from private persons |
| `returns_reserve` | `0.05` | Share of the sale reserved for B2C returns |
| `exit_channels.enabled` | `[allegro, olx, ebay_pl, ebay_de]` | Only these channels compute nets. Add `kleinanzeigen` once someone can sell there |
| `import_costs.*` | see the file | EU consignment fee EUR 3 (parcels up to EUR 150, from 2026-07-01), handling fee EUR 2 (from 2026-11-01), DHL clearance fee 69 PLN or 2.5%, 7 transit days |
| `fiscal_cost_per_b2c_sale_pln` | `0` | Cash-register cost per B2C sale |
| `risk.*` | see the file | Scam score weights and thresholds; see [TUNING.md](TUNING.md#risk-weights) |

`data/fees.yaml` (platform fee per exit channel: Allegro 8% + 1 PLN, OLX 0, eBay PL 9%, eBay DE 11% + EUR 0.35,
Kleinanzeigen 0) and `data/shipping.yaml` (VAT-deductible lanes, default outbound and inbound shipping in PLN by
category and origin) are frozen data. `data/taxonomy/{gpu,ram,ssd}.yaml` list the known products.

## pickup.yaml

Pickup-only listings (no shipping) count only inside a radius, and cost a trip.

| Key | Default | Meaning |
|---|---|---|
| `max_km` | `60` | Maximum distance from the origin |
| `countries` | `[PL]` | Countries where pickup is allowed |
| `origin` | Krakow, 50.0647 / 19.9450 | `{name, lat, lon}`: the point pickup distances are measured from (haversine). Change it in `config/local/pickup.yaml` |
| `pln_per_km`, `pln_per_trip` | `1.2`, `40` | Trip cost = `pln_per_km` x 2 x km + `pln_per_trip` |

## scheduler.yaml

Owner-tunable pacing and limits (not frozen). See [`TUNING.md`](TUNING.md) for what each moves.

| Key | Default | Meaning |
|---|---|---|
| `round_interval_s` | `600` | Start-to-start pacing of rounds |
| `budget` | `60` | Searches per round chosen by the bandit (sentinel and feed arms come on top) |
| `budget_max` | `120` | Ceiling the budget can grow to |
| `budget_boost` | `1.5` | Growth factor after a round in which the LLM queue never waited |
| `max_searches_per_source` | `12` | Per source per round |
| `max_llm_items` | `200` | Listings sent to the worker LLM per round; the rest wait |
| `alive_confirm_per_source` | `5` | Newly disappeared listings re-checked per source per round |
| `reeval_hours` | `24` | An unchanged listing is re-valued at most this often |
| `round_timeout_s` | `1500` | A round is cut after this; unfinished work is dropped |
| `ebay_comp_keys` | `10` | Keys per round whose eBay active asks become comps (needs eBay keys) |
| `price_range` | `[0.15, 3.0]` | Cheap filter: a keyed price outside this multiple of the key's median is dropped |
| `min_prices_for_range` | `3` | ...only when at least this many prices of the key are known |
| `feed_sources` | `[pepper_pl, hotukdeals, retail]` | One query-less sweep per round; never ledger-swept |
| `min_estimate_comps` | `3` | An exit estimate needs at least this many comparables |
| `ebay_insights_keys` | `10` *(built-in default)* | Keys per round for eBay sold comps |
| `maintenance_hours` | `24` *(built-in default)* | Interval of the daily purge |
| `retention_days` | not set | Deprecated alias: used for both retention values only if `sources.retention` is removed. Use `sources.retention` |
| `bandit.gamma` | `0.98` | Per-round discount of old evidence ([TUNING.md](TUNING.md#bandit-gamma-floor-and-prior)) |
| `bandit.floor` | `0.10` | Share of each round's arms reserved for uniform exploration |
| `bandit.prior` | `[1.0, 10.0]` | Gamma prior (shape, rate) of value per fetch |
| `bandit.new_pulls` | `3` | Guaranteed trial pulls of a new arm |
| `trend.min_30d` | `0.15` | Tier-4 "trend" signal: the key's 30-day trend must be at least this (+15%) |
| `trend.min_cons_net_usd` | `0` | ...and the conservative net at least this (not losing money). The tier-4 row is only logged when the listing is below its floor and not within 20% of it |
| `requeue_hours`, `requeue_max` | `48`, `500` *(built-in defaults)* | Window and cap for re-queuing unevaluated listings |
| `timeout_grace_s` | `60` *(built-in default)* | Extra time for started evaluations after `round_timeout_s` |

## sources.yaml

| Key | Default | Meaning |
|---|---|---|
| `fetch.user_agent` | `DealHunter/0.1 (personal price research; +https://github.com/michal-koniu)` | Sent on every request |
| `fetch.min_interval_s`, `jitter_s` | `4`, `2` | Minimum gap between requests to one domain, plus 0 to 2 s of jitter; raised to a site's Crawl-delay |
| `fetch.backoff_start_s`, `backoff_cap_s` | `60`, `3600` | Block backoff doubles from 60 s to 1 h |
| `fetch.disable_after_blocks` | `3` | Consecutive blocks that disable a domain for the session |
| `fetch.robots_ttl_s`, `max_crawl_delay_s` | `86400`, `60` | robots.txt cache and the longest Crawl-delay honoured. Used only when `obey_robots` is true |
| `fetch.obey_robots` | `true` in code, `false` in this `sources.yaml` | When false, robots.txt is not fetched and not applied. A 403, a 429 and a challenge page still stop the request. Proxies, captcha solving and logged-in fetches stay refused |
| `fetch.timeout_s` | `30` | Request timeout |
| `fetch.proxies`, `solve_captcha`, `logged_in` | `false` | Policy keys. They exist so the rule is visible; the loader **refuses to start** if any is truthy (see above). There is no proxy, captcha or login code |
| `sweep_timeout_s` | `300` *(built-in default)* | One source's sweep is cut after this |
| `adapters.<name>.enabled` | per source | Turns an adapter on or off; an adapter not listed is enabled. See [`SOURCES.md`](SOURCES.md) |
| `adapters.<name>.reason` | | Free text; record why a source is off |
| `adapters.<name>.channel` | | Overrides the valuation channel name of a source |
| `adapters.bazos_*.pages` | `2` | Category pages read per sweep (20 ads each) |
| `adapters.mailbox.enabled` | `false` | The alert mailbox; enable after `dh-mail#imap` exists |
| `adapters.mailbox.image_hosts` | platform CDNs | Mail photos are kept only from these https hosts |
| `retail.drop_pct` | `0.10` | A shop price fall of this much emits a listing |
| `retail.watch` | 6 pages | List of `{shop, url}` pages with JSON-LD prices |
| `retention.photos_days` | `90` | Age at which photo hashes are deleted by the daily purge ([OPERATIONS.md](OPERATIONS.md#retention)) |
| `retention.snapshots_days` | `90` | Age at which raw page snapshots and saved alert mails (.eml) are deleted |

The `via:` keys (`mailbox`, `api`, `rss`) are documentation only. The `allegro` and `olx` entries do not name
adapters; those platforms arrive through the mailbox.

## valuation.yaml

| Key | Default | Meaning |
|---|---|---|
| `ask_to_sale_ratio` | `0.92` | An active ask is discounted to an expected sale price |
| `weights.owner_sale` / `ebay_sold` / `probably_sold` / `active_ask` / `ask_history` | `1.0` / `1.0` / `0.5` / `0.3` / `0.2` | Weight of one comparable in the weighted quantiles |
| `fallback.pl_pool`, `eu_pool_for_pl`, `eu_pool_for_eu` | `0.7`, `0.5`, `0.7` | Weight multipliers when a channel has no recent comps of its own |
| `ceiling_factor` | `0.95` | The estimate is capped at the lowest new-retail price x this |
| `recent_days` | `30` | Age of comps that count for confidence and the quantiles |
| `lookback_days` | `90` | How far back comps are read |
| `active_ask_max_age_days` | `3` | A tracked listing counts as a live ask if seen this recently |
| `fx_max_back_days` | `4` | A cached NBP rate may be this much older than the comp |
| `fx_stale_days` | `3` | The note `fx_stale` appears only when a recent comp (one that makes the quantiles) was converted at a rate more than this many days older than its date; weekend and holiday gaps are normal |
| `channels.<source>.channel`, `market` | per source | The channel name and country of each comparable source |
| `partout.parting_cost` | `0.12` | Share of a PC's parts value lost to listing, haggling and returns |
| `partout.heavy_parts` | `3` | Effort is heavy from this many detected parts |
| `partout.max_components` | `6` | Parts valued per PC |
| `partout.exit_channel` | `allegro` | Channel the parts are valued on |

## notify.yaml

| Key | Default | Meaning |
|---|---|---|
| `ntfy.server` | `https://ntfy.sh` | ntfy server |
| `ntfy.topic` | `null` | Filled on first use in `config/local/notify.yaml` |
| `ntfy.tier1_priority` | `urgent` | ntfy priority of tier-1 pushes |
| `email_fallback.enabled` | `true` | Use `dh-mail#smtp` when ntfy fails |
| `quiet_hours` | `null` | `null` = pushes at any time. Or `{start: "23:00", end: "07:00", tz: Europe/Warsaw}` (an overnight window wraps; `tz` defaults to `Europe/Warsaw`). A tier-1 push that falls inside the window is queued (`pending_pushes.not_before`) and sent by the first retry after it ends; its 6-hour retry life starts then. A malformed value is ignored (no quiet hours), never silences a push |

Push title format: `T1 <star> +$640 <product key> · <source> <city>`.

## publish.yaml

| Key | Default | Meaning |
|---|---|---|
| `interval_min` | `30` | Minutes between log-repo syncs |
| `remote` | `git@github.com:michal-koniu/deal-hunter-log.git` | Clone URL of `deal-hunter-log`; used only when `log_dir` is not yet a clone. Another owner overrides it in `config/local/publish.yaml` |
| `branch` | `log` | Must stay `log`; any other value is refused with a `bad_branch` health event |
| `log_dir` | `~/.local/share/deal-hunter/log` | Local clone of the log repo |
| `cards_dir` | `var/cards/deals` | Where cards are written (relative to the repo root) |
| `repo_web_url` | `https://github.com/michal-koniu/deal-hunter-log` | Used to link the card in pushes; `null` leaves the "Card" button out |

## books.yaml

| Key | Default | Meaning |
|---|---|---|
| `tariff_pln_kwh` | `1.10` | Electricity tariff, gross |
| `base_w` | `120` | Fixed estimate for CPU, RAM, disks and PSU losses |
| `gpu_sample_s` | `15` | `rocm-smi` sampling interval during a round |

Energy per round = (mean GPU power + `base_w`) x hours x tariff. Without `rocm-smi`, only `base_w` is counted.

## web.yaml

| Key | Default | Meaning |
|---|---|---|
| `host`, `port` | `0.0.0.0`, `8711` | Listen address |
| `allowed_hosts` | this machine's names and LAN IPs | Host names accepted on POST (the mark buttons). An explicit list replaces the automatic one |
| `extra_allowed_hosts` | empty | Names added on top of that list (Tailscale MagicDNS, mDNS). The client IP is not checked |

The page has no login: anyone on the LAN can read it and post marks. Do not expose port 8711 beyond a trusted
network.

## improve.yaml

| Key | Default | Meaning |
|---|---|---|
| `post_round_timeout_s` | `300` | The improvement step that runs after each round (`improve.after_round`) is cut after this many seconds, with an `improve_timeout` health event; `0` = no limit. An error in that step is an `improve_error` health event and never stops the rounds. The gates themselves are frozen (`dh/improve/gates.py`) and are not configuration |
| `apply_expiry_days` | `7` | A `dh playbook --apply ID` still queued after this many days (wall clock) is dropped, never applied; its result says so and the proposal still waits for review, so run the apply again if you still want it |

## runtime.yaml

| Key | Default | Meaning |
|---|---|---|
| `unit_name` | `deal-hunter` | The systemd user unit this checkout runs as. `DH_UNIT_NAME` in the environment wins over it. |

A second checkout on the same host (a development clone) must use its own name, in `config/local/runtime.yaml`
(`unit_name: deal-hunter-dev`) or `DH_UNIT_NAME=deal-hunter-dev`. Otherwise its `dh round` refuses because the
owner's unit is active, and its `dh stop` would stop the owner's agent. The lock, pid and state files are under
each checkout's own `var/run/`, and `dh status`/`dh stop` only count `dh.runtime serve` processes whose `DH_ROOT`
(or working directory) is this checkout. Give the second checkout its own web port too (`config/local/web.yaml`,
`port: 8712`) if you `dh start` it.

## The playbook

`playbook/items.yaml` is a list of items the improvement loop may change through gates. Each item has `id`,
`kind`, `target`, `value`, `provenance`, `helpful`, `harmful` and `sentinel`.

| Kind | Target | Value |
|---|---|---|
| `query` | `all` or comma-separated source names | Search text. Optional `min_pln` / `max_pln` |
| `alias` | `match` | `from => to` rewrite before matching |
| `scam_kw` | `risk` | A soft scam phrase, matched without diacritics. Each hit adds `risk.weights.scam_kw` |
| `prompt` | `llm.<role>` | An LLM system prompt (`prompt.extract`, `prompt.triage`, `prompt.confirm`, `prompt.reflect`, `prompt.propose`). `prompt.triage` is the senior model's finalist review of every tier 1-2 candidate; its reply keeps the `p_scam` / `verdict` (ok, suspicious, scam) / `reasons` format the gates check |
| `cadence` | a source | Minutes between searches of that source (10 to 1440). With rounds every R minutes: a cadence C > R runs the source's bandit arms only in rounds at least C minutes apart; C < R raises its per-round cap to `max_searches_per_source` x R / C (at most the budget). Sentinel and feed arms always run |

The seed has 3 sentinel queries (`sentinel.rtx_4090`, `sentinel.rtx_3090`, `sentinel.ddr5_32gb`, on
kleinanzeigen, hardverapro and allegro_lokalnie), 42 normal queries for GPUs, RAM, NVMe and gaming PCs, and
15 scam keyword items. `sentinel: true` items are the control group and are never changed by the agent.
You may edit this file by hand when the agent is stopped; a bad file raises an error that the web page's
playbook view shows.
