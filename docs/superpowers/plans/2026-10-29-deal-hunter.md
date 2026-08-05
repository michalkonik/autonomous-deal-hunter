# Deal Hunter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A manually started, self-improving agent on the owner's rig. It finds electronics resale deals across PL and EU marketplaces, values them against real clearing prices, and computes the full Polish-JDG net profit. It logs evidence-backed deal cards, pushes tier-1 deals to ntfy, and publishes a private GitHub log.

**Architecture:** An asyncio Python service (`dh`).
- Code does the bulk work: collectors, ledger, valuation, economics, tiering.
- Local LLMs, reached through llama-swap's OpenAI-compatible API, handle only ambiguous extraction, triage of finalists, and reflection.
- The data store is a single SQLite file.
- There is a frozen layer that the agent cannot write: rules, economics, gates, eval sets.
- Self-improvement has two parts: a bandit over fetch arms, and a gated, one-change-at-a-time playbook loop.

**Tech Stack:**
- Python 3.12 (rig), stdlib `sqlite3`, `asyncio`.
- Libraries: `curl_cffi`, `selectolax`, `pyyaml`, `rapidfuzz`, `feedparser`, `jinja2`, `starlette` + `uvicorn`, `playwright` (system Chrome, screenshots only), `imagehash` + `Pillow`.
- Tests: `pytest`, `pytest-asyncio`.

**Spec:** `docs/superpowers/specs/2026-10-29-deal-hunter-design.md`. Executors read it; section numbers below (§) refer to it.

## Global Constraints

- **Python and dependencies**
  - Python ≥ 3.12. No dependency outside the Tech Stack list without noting it in the task.
  - All network code is async.
- **Config**
  - `config/*.yaml` hold the defaults and are committed. `config/local/*.yaml` (gitignored) deep-merge over them.
  - Secrets come only from env vars or the `bw-secret item#field` helper. They never go into the repo, the DB or the log repo.
- **Frozen layer**
  - The frozen files are `config/rules.yaml`, `config/economics.yaml`, `dh/economics.py`, `dh/tiering.py`, `dh/improve/gates.py` and `evals/`.
  - The agent's improvement code must never write to them. `dh/config.py` records their SHA-256 at start and refuses to run a round if they changed while running.
- **Money**
  - Compute internally in PLN with `Decimal`, 2 dp, ROUND_HALF_UP.
  - Convert to USD for thresholds at the NBP mid rate of the day. The rate and its date are stored with every deal.
- **Floors (on conservative net, USD):** easy 50, moderate 120, heavy 250.
- **Tiers**
  - Tier 1: conservative net ≥ 300 **and** confidence `high`.
  - Tier 2: clears the floor and confidence ≥ `medium`.
  - Tier 3: clears the floor with confidence `low`.
  - Tier 4: within 20% under the floor, or a trend signal.
  - ★ = expected net ≥ 500. Capital cap 30000 USD.
- **High confidence**
  - Match certainty ≥ 0.9.
  - At least `min_comps` comparables, each ≤ 30 days old. `min_comps` = 12, or 20 if capital > 5000 USD, or 30 if capital > 15000 USD.
  - p_scam < 0.05.
  - Verified live, at the same price, ≤ 10 min ago.
  - No unresolved defect flags.
- **Tax defaults**
  - `tax_mode: liniowy`: 19% PIT + 4.9% health on pre-tax profit.
  - VAT: the margin scheme (23/123 × max(0, sale − purchase)) for purchases from private persons. For invoiced or imported purchases, output VAT minus input VAT.
  - `vat_ue_registered: false`. `returns_reserve: 0.05` of the sale price for B2C channels. `fx_spread: 0.01`.
  - `exit_channels.enabled: [allegro, olx, ebay_pl, ebay_de]`.
  - Import costs: EU duty from ISZTAR, €3 per item for consignments ≤ €150 (from 2026-07-01), and a €2 handling fee per item for arrivals on or after 2026-11-01.
- **Fetch etiquette**
  - Logged out. Home IP, no proxies.
  - At least `min_interval_s` (default 4 s, plus 0–2 s jitter) between requests per domain.
  - On 403/429: exponential backoff from 60 s, capped at 1 h, plus a health event.
  - No CAPTCHA solving.
  - Allegro.pl only through the mailbox. Vinted off. eBay only through the API.
- **Personal data**
  - Store only the seller as an HMAC-SHA256 of `source:seller_id` with a local salt, plus account age, rating count and score.
  - No seller names or phone numbers anywhere.
  - Photos and raw snapshots are deleted after 90 days.
- **Untrusted text**
  - Listing text goes to LLMs only inside `<listing>…</listing>`.
  - The prompt states that it is data. Output is JSON-schema-validated. No tools are offered to the model in triage.
- **Pickup:** a pickup-only listing counts only within `pickup.max_km: 60` of Kraków (50.0647, 19.9450), in `pickup.countries: [PL]`. Pickup cost = `pickup.pln_per_km: 1.2` × 2 × distance + `pickup.pln_per_trip: 40`.
- **Repair:** `repair.enabled: false`. For-parts listings are hard-excluded.
- **Process control**
  - There is no autostart of any kind.
  - `dh start` refuses to start if free RAM < 12 GB, or if llama-swap is running a model outside the configured roles. It prints the reason.
  - `dh stop` leaves no process behind and unloads the models it loaded (`POST /unload` on llama-swap).
- **Default models (`config/models.yaml`)**
  - worker = `gemma-4-26b-a4b-pool`, senior = `qwen3.8-flash-next-uncensored`, both via `http://localhost:8080/v1`.
  - advisor = senior.
  - `paid_api.enabled: false`.
- **Git**
  - Author `Michal Konik <michal.konik.job@gmail.com>`. No AI attribution in commits or PRs.
  - Code repo `deal-hunter`: work on branch `feat/build`, open a PR, never push `main`.
  - Log repo `deal-hunter-log`: default branch `log`.

## Review Focus

1. **A listing seen again after the agent was stopped.** The disappearance must not be inferred across a stop. Observation windows must close at `dh stop` or on a crash. Test in Task 11.
2. **Prices in the wrong unit or currency.** Examples: "3 500 zł", "3.500,00 €", "1,2 tys.", Czech "Kč", Hungarian "Ft", and "do negocjacji" with price 0. They must parse to the right Decimal and currency, or be skipped (price ≤ 0 → skip). Test in Task 8.
3. **Bait titles.** "Pudełko po RTX 4090", "RTX 4090 box only", "kupię RTX 4090" (a buyer's ad), and "RTX 4090 *like* performance" must never become GPU deals. Test in Task 5.
4. **The same physical item listed on two platforms, or relisted.** It must produce one deal, not two pushes. Dedupe by seller hash + product key + price within 5% + 7 days. Test in Task 15.
5. **ntfy or GitHub unreachable.** The round must continue. A tier-1 push falls back to email and is retried. The publisher retries next cycle without losing marks. Tests in Tasks 16 and 17.

---

## File structure

```
deal-hunter/
  pyproject.toml  README.md  ACCOUNTANT-QUESTIONS.md  .gitignore
  config/{rules,economics,models,sources,notify,pickup}.yaml   config/local/ (gitignored)
  playbook/items.yaml            # itemised, versioned, agent-editable via gates only
  data/taxonomy/{gpu,ram,ssd}.yaml  data/shipping.yaml  data/fees.yaml
  dh/__init__.py  cli.py  config.py  models.py  store.py  money.py  customs.py
  dh/llm.py  extract.py  taxonomy.py  match.py
  dh/fetch.py  sources/{__init__,base,olx,allegro_lokalnie,sprzedajemy,kleinanzeigen,bazos,sbazar,hardverapro,marktplaats,ebay,rss,retail,mailbox,willhaben}.py
  dh/ledger.py  valuation.py  economics.py  risk.py  tiering.py  verify.py  cards.py
  dh/notify.py  publish.py  web/{app.py,templates/*.html}  bandit.py  scheduler.py  books.py  runtime.py  partout.py  photos.py
  dh/improve/{playbook.py,cusum.py,replay.py,gates.py,shadow.py,reflect.py,controller.py,report.py}
  evals/{must_pass.jsonl,golden.jsonl}
  tests/... (mirrors dh/)   tests/fixtures/<source>/*.html|json
  docs/{ARCHITECTURE,CONFIGURATION,TUNING,ADDING-A-SOURCE,SELF-IMPROVEMENT,ECONOMICS,OPERATIONS}.md
```

---

## M1: a working, honest hunter

### Task 1: Scaffold, config, CLI skeleton

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `dh/__init__.py`, `dh/config.py`, `dh/cli.py`, `config/*.yaml` (values from Global Constraints), `tests/test_config.py`.

**Interfaces:**
- Produces:
  - `config.load(root: Path) -> Config`: attribute access, deep-merged with `config/local`.
  - `Config.frozen_digest() -> dict[str, str]`.
  - `config.assert_frozen_unchanged(cfg, digest) -> None`: raises `FrozenChanged`.
  - The CLI entry point `dh` (argparse) with subcommands `start stop status deals show verify replay eval playbook books round` (stubs print "not implemented" until their task).

- [ ] Test `test_local_override_deep_merges`: `local/rules.yaml` `{floors: {easy: 60}}` changes only `floors.easy`.
- [ ] Test `test_frozen_digest_detects_change`: editing `rules.yaml` after the digest makes `assert_frozen_unchanged` raise.
- [ ] Implement. Run `pytest -q`; expect PASS. `dh --help` lists the subcommands.
- [ ] Commit `feat: scaffold, config loader, CLI skeleton`.

### Task 2: Models and store

**Files:**
- Create: `dh/models.py`, `dh/store.py`, `tests/test_store.py`.

**Interfaces:**
- Produces dataclasses in `dh/models.py`:
  - `Listing(source, source_id, url, title, description, price: Decimal, currency, country, city, lat, lon, seller_hash, seller_age_days, seller_ratings, seller_score, ships: bool|None, pickup_only: bool, is_business: bool|None, photos: list[str], posted_at, seen_at, raw_path)`
  - `Extraction(key: str|None, category, certainty: float, condition: Literal['new','like_new','used','for_parts','unknown'], defects: list[str], box_only: bool, for_parts: bool, wanted_ad: bool, bundle_items: list[str], method: Literal['rules','llm'])`
  - `Deal(id, listing_ref, key, tier, star, effort, confidence, cons_net_pln, exp_net_pln, cons_net_usd, exp_net_usd, capital_usd, breakdown_json, evidence_json, reasons, created_at, verified_at, status)`
- Produces `Store(path)` with methods:
  - `upsert_listing(l) -> int`
  - `record_observation(listing_id, price, seen_at)`
  - `open_window(source, ts) -> int`, `close_window(window_id, ts)`, `open_windows() -> list`
  - `listings_missing_since(source, ts) -> list[int]`
  - `save_extraction(listing_id, e)`
  - `save_deal(d) -> int`, `deals(filters) -> list[Deal]`, `get_deal(id)`
  - `add_mark(deal_id, status, price_pln, note, source)`
  - `add_comp(key, condition, channel, price_pln, sold_at, kind, ref)`, `comps(key, condition, channel, since) -> list`
  - `log_round(...)`, `add_cost(round_id, kind, amount_pln, units)`, `add_health(source, kind, detail)`
- Schema in one `SCHEMA` string with WAL mode. Tables: listings, observations, windows, extractions, deals, marks, comps, rounds, arm_stats, costs, health, fx, playbook_versions, proposals, shadow_results.

- [ ] Test `test_upsert_is_idempotent_and_updates_price`.
- [ ] Test `test_observations_and_windows_roundtrip`.
- [ ] Test `test_deal_save_and_filter_by_tier`.
- [ ] Implement; tests PASS; commit `feat: data models and SQLite store`.

### Task 3: Money and customs

**Files:**
- Create: `dh/money.py`, `dh/customs.py`, `tests/test_money.py`, `tests/test_customs.py`, and fixtures for NBP and ISZTAR JSON.

**Interfaces:**
- Produces:
  - `money.parse_price(text: str, default_currency: str) -> tuple[Decimal, str] | None`
  - `async money.Fx.rate(ccy: str, day: date) -> Decimal`: PLN per unit. Source is NBP table A (`https://api.nbp.pl/api/exchangerates/rates/a/{ccy}/{date}/?format=json`), falling back to the previous business day. Cached in the `fx` table.
  - `Fx.to_pln(amount, ccy, day) -> Decimal`, `Fx.pln_to_usd(amount_pln, day) -> Decimal`
  - `async customs.duty_rate(cn_code: str, origin: str, day: date) -> Decimal`: from the ISZTAR measures API (`https://ext-isztar4.mf.gov.pl/tariff/rest/goods-nomenclature/measures?nomenclatureCode=…&date=…&language=EN`), cached for 30 days. Default map: gpu 8473302000, ram 8473302000, ssd 8471700000, pc 8471500000.
  - `customs.import_fees_eur(value_eur, items: int, day: date) -> Decimal`: €3 per item if value ≤ €150 and day ≥ 2026-07-01, plus €2 per item if day ≥ 2026-11-01.

- [ ] Parametrised test `test_parse_price`. Each case maps text and default currency to (amount, currency), or to None:

  | Text | Default currency | Expected |
  |---|---|---|
  | `"3 500 zł"` | PLN | (3500, PLN) |
  | `"3.500,00 €"` | EUR | (3500.00, EUR) |
  | `"1,2 tys. zł"` | PLN | (1200, PLN) |
  | `"12 990 Kč"` | CZK | (12990, CZK) |
  | `"189 000 Ft"` | HUF | (189000, HUF) |
  | `"Za darmo"` | PLN | None |
  | `"0 zł"` | PLN | None |

- [ ] Test `test_fx_falls_back_to_previous_business_day`, using a fixture 404 then 200.
- [ ] Test `test_import_fees`:
  - (100 EUR, 1 item, 2026-10-30) → 3.
  - (100, 1, 2026-11-02) → 5.
  - (900, 1, 2026-11-02) → 2.
- [ ] Implement; PASS; commit `feat: money parsing, NBP FX, ISZTAR duty, EU import fees`.

### Task 4: Economics engine (frozen)

**Files:**
- Create: `dh/economics.py`, `data/fees.yaml`, `data/shipping.yaml`, `tests/test_economics.py`, `docs/ECONOMICS.md` (formula plus the 10 worked examples, in a table).

**Interfaces:**
- Consumes: `Fx`, `customs`, `Config`.
- Produces:
  - `NetInputs(buy_price, buy_ccy, origin_country, seller_is_business: bool, has_vat_invoice: bool, exit_channel, exit_price_cons_pln, exit_price_exp_pln, inbound_ship_pln, outbound_ship_pln, pickup_km: float|None, items: int, day: date, category, p_scam: float)`
  - `Line(label: str, pln: Decimal, note: str)`
  - `NetResult(lines_cons: list[Line], lines_exp: list[Line], cons_net_pln, exp_net_pln, capital_pln, flags: list[str])`
  - `async economics.net(inp: NetInputs, cfg, fx) -> NetResult`
  - `economics.best_exit(results: dict[str, NetResult]) -> tuple[str, NetResult]`: highest conservative net among enabled channels.
- Rules (spec §7):
  - Fees per channel come from `data/fees.yaml`. Defaults: allegro 8% + 1 PLN; olx 0%; ebay_pl 9%; ebay_de 11% + 0.35 EUR; kleinanzeigen 0%.
  - Import applies when the origin is outside the EU. CIF value = buy + inbound shipping; import VAT 23% on (CIF + duty); clearance fee 69 PLN.
  - With a VAT invoice or an import, the purchase VAT (or import VAT) is input VAT. Output VAT = sale × 23/123. VAT due = output − input.
  - From a private person in the EU: margin scheme.
  - Intra-EU purchase from a business while `vat_ue_registered` is false: flag `needs_vat_ue`.
  - Origin outside PL: flag `bdo_check`.
  - Returns reserve = `returns_reserve` × exit price, for B2C channels.
  - FX spread is 1% of foreign-currency amounts.
  - Risk haircut = p_scam × buy price (PLN).
  - Income tax = 23.9% × max(0, pre-tax profit) under liniowy. Skala is 12%, plus 9% health. Ryczałt is 3% of revenue, plus a fixed health amount from config.
  - Capital = buy + inbound + import VAT advanced + duty.

- [ ] Write the 10 worked examples as `tests/test_economics.py::test_worked_example[n]`. Values are hand-computed and documented in `docs/ECONOMICS.md`. They must cover:
  1. PL private → Allegro (margin scheme).
  2. PL business with invoice → Allegro.
  3. CZ private → OLX (FX in CZK).
  4. DE private → eBay.de.
  5. US business → Allegro (import, duty 0, VAT 23%, clearance).
  6. JP private via forwarder → Allegro (import, low-value fee).
  7. A loss case (negative net).
  8. Pickup 40 km.
  9. A ryczałt mode case.
  10. A case where capital exceeds the cap.
- [ ] Each test asserts `cons_net_pln` and `exp_net_pln` to the grosz, and the set of flags.
- [ ] Test `test_best_exit_ignores_disabled_channels`.
- [ ] Implement; PASS; commit `feat: frozen economics engine with worked examples`.

### Task 5: Taxonomy and rules matcher

**Files:**
- Create: `data/taxonomy/gpu.yaml` (NVIDIA RTX 20/30/40/50 incl. Ti/Super/D variants and VRAM; AMD RX 6000/7000/9000; Intel Arc A/B; datacenter A100/H100/L40S/A6000/RTX 6000 Ada/RTX PRO 6000), `data/taxonomy/{ram,ssd}.yaml`, `dh/taxonomy.py`, `dh/match.py`, `tests/test_match.py`.

**Interfaces:**
- Produces:
  - `match.rules_extract(title: str, description: str, lang: str) -> Extraction`
  - `taxonomy.canonical(category, fields) -> str`. Example keys: `gpu:rtx-4090:24`, `gpu:rx-7900-xtx:24`, `ram:ddr5:32:2x16`, `ssd:nvme:2tb`.
- Behaviour:
  - Set `wanted_ad` for buyer phrases: "kupię", "szukam", "suche", "koupím", "veszek", "wanted", "WTB".
  - Set `box_only` for: "pudełko", "karton", "box only", "nur OVP", "leere Verpackung", "krabice", "doboz".
  - Set `for_parts` for: "uszkodzon", "na części", "defekt", "for parts", "bastler", "nefunkční", "hibás", "brak obrazu", "artefakt".
  - Comparisons such as "like", "jak", "wie", "vs", "lepsza niż" must not produce a key match.
  - Certainty: 0.95 for one unambiguous chip and VRAM; 0.7 if the VRAM was inferred from the default; 0.0 if no match.

- [ ] Test `test_bait_titles` (Review Focus 3): each of these titles → key None, or the flag set:
  - "Pudełko po RTX 4090"
  - "RTX 4090 box only"
  - "Kupię RTX 4090"
  - "Laptop z wydajnością jak RTX 4090"
- [ ] Parametrised test `test_gpu_keys`. At least 25 real-style titles in PL, DE, CZ, HU and EN, e.g.:
  - "MSI RTX 4070 Ti SUPER Gaming X Slim 16GB" → `gpu:rtx-4070-ti-super:16`
  - "Gigabyte RX 7900XTX 24G" → `gpu:rx-7900-xtx:24`
  - "rtx3080ti" → `gpu:rtx-3080-ti:12`
  - "RTX 4090D" → `gpu:rtx-4090d:24`
- [ ] Tests `test_ram_keys` and `test_ssd_keys`, 10 each.
- [ ] Implement; PASS; commit `feat: product taxonomy and rules matcher`.

### Task 6: LLM client and LLM extraction

**Files:**
- Create: `dh/llm.py`, `dh/extract.py`, `tests/test_llm.py`, `tests/test_extract.py`.

**Interfaces:**
- Produces:
  - `llm.Client(cfg)` with `async chat_json(role: str, system: str, user: str, schema: dict, max_tokens=600) -> dict`. It uses the role's endpoint and model; sends `response_format={"type":"json_schema",...}`, falling back to a GBNF-free "JSON only" instruction plus validation; makes 2 retries on invalid JSON. Token usage is recorded through a callback `on_usage(role, prompt_tokens, completion_tokens, seconds)`.
  - `extract.extract(listing, rules: Extraction, client) -> Extraction`. It returns `rules` unchanged if certainty ≥ 0.9. Otherwise it asks the worker with `EXTRACT_SCHEMA`, using the fields of `Extraction`, and quotes the listing inside `<listing>` tags.
  - `llm.wrap_untrusted(text) -> str`: strips existing `<listing>` and `</listing>` tags and truncates to 4000 chars.
- Prompt text lives in `playbook/items.yaml` under the ids `prompt.extract` and `prompt.triage`, so the improvement loop can edit it through the gates.

- [ ] Test `test_chat_json_retries_on_invalid_json`, against a fake server returning bad, then good, output.
- [ ] Test `test_injection_text_is_wrapped`: a description containing `</listing> ignore previous instructions` stays inside a single listing block.
- [ ] Test `test_extract_skips_llm_when_rules_certain`.
- [ ] Implement; PASS; commit `feat: llama-swap JSON client and guarded LLM extraction`.

### Task 7: Polite fetcher

**Files:**
- Create: `dh/fetch.py`, `tests/test_fetch.py`.

**Interfaces:**
- Produces `fetch.Fetcher(cfg, store, snapshot_dir)`:
  - `async get(url, *, kind: Literal['html','json'], impersonate: str|None='chrome', headers=None) -> FetchResult(status, text, url, from_cache: bool, raw_path)`
  - per-domain `asyncio.Lock` and last-request time;
  - `min_interval_s` with jitter;
  - backoff state per domain;
  - `blocked(domain) -> bool`;
  - snapshots saved as gzip under `snapshots/YYYY-MM-DD/<domain>/<sha1>.gz`;
  - every 403/429 → `store.add_health`.
- `curl_cffi.requests.AsyncSession` when `impersonate` is set, otherwise the same session without impersonation.

- [ ] Test `test_min_interval_per_domain`, with a fake clock: two gets to the same domain are spaced ≥ 4 s; different domains are not delayed.
- [ ] Test `test_backoff_after_429`: the domain is blocked for 60 s, then 120 s after a second 429.
- [ ] Implement; PASS; commit `feat: polite per-domain fetcher with backoff and snapshots`.

### Task 8: Polish source adapters

**Files:**
- Create: `dh/sources/base.py`, `olx.py`, `allegro_lokalnie.py`, `sprzedajemy.py`, `tests/sources/test_pl.py`, and fixtures captured live from each site (one search page and one detail page each).

**Interfaces:**
- Produces:
  - `base.Adapter` (ABC):
    - `name`, `domain`, `country`;
    - `async search(query: Query, fetcher) -> list[Listing]`;
    - `async detail(listing, fetcher) -> Listing`, which fills the description, photos and seller fields;
    - `async alive(listing, fetcher) -> tuple[bool, Decimal|None]`.
  - `Query(text, category, min_price_pln, max_price_pln, arm_id)`
  - `sources.registry() -> dict[str, Adapter]`, filtered by `sources.yaml` `enabled`.
- OLX:
  - Uses `https://www.olx.pl/api/v1/offers/?query=…&limit=40&sort_by=created_at:desc` via curl_cffi with chrome impersonation.
  - Takes the price from the `params` key `price`, the seller from `user.id`/`user.created`, and the city from `location.city.name`.
  - The `delivery.rock.active` flag tells whether the item ships.
  - The adapter is reused for olx.ro/.bg/.pt by a `country` parameter (M2).
- Allegro Lokalnie and sprzedajemy: HTML search parsed with selectolax. The selectors live in the adapter as constants.

- [ ] Capture live fixtures on the rig (Task 8 includes a live capture script `tests/fixtures/capture.py <source> <query>`).
- [ ] Test `test_<source>_search_parses_fixture` for each source: at least 10 listings, each with an id, a URL, a price > 0, a currency and a title.
- [ ] Test `test_price_parsing_edge_cases` (Review Focus 2), on fixture items with "do negocjacji" and 0 zł: they are skipped.
- [ ] Implement; PASS; live smoke check on the rig with `dh round --source olx_pl --query "rtx 4090" --dry`, which prints ≥ 1 parsed listing. Commit `feat: OLX, Allegro Lokalnie, sprzedajemy adapters`.

### Task 9: EU classifieds adapters

**Files:**
- Create: `dh/sources/kleinanzeigen.py`, `bazos.py` (cz, sk, at subdomains), `sbazar.py`, `hardverapro.py`, `marktplaats.py`, `tests/sources/test_eu.py`, fixtures.

**Interfaces:**
- Consumes: `base.Adapter`, `Fetcher`.
- Produces: one adapter per source. Each sets `country` and the native currency: EUR, CZK, HUF.
- Marktplaats: `https://www.marktplaats.nl/lrp/api/search?query=…&limit=30&offset=0&sortBy=SORT_INDEX&sortOrder=DECREASING`.
- Others: HTML.

- [ ] The same three test kinds as Task 8 for each source, with live-captured fixtures.
- [ ] Implement; PASS; live smoke for each source; commit `feat: Kleinanzeigen, Bazoš, sbazar, hardverapro, Marktplaats adapters`.

### Task 10: eBay API, RSS and retail adapters

**Files:**
- Create: `dh/sources/ebay.py`, `rss.py`, `retail.py`, tests and fixtures.

**Interfaces:**
- `ebay.EbayBrowse`:
  - OAuth client-credentials, with the keys from `bw-secret ebay#client_id` and `ebay#client_secret`.
  - If the keys are missing, `enabled` = False and one health event is recorded.
  - `search(query)` calls `GET https://api.ebay.com/buy/browse/v1/item_summary/search?q=…&filter=itemLocationCountry:{cc}` with header `X-EBAY-C-MARKETPLACE-ID: EBAY_DE|EBAY_PL|EBAY_US`.
  - `active_comps(key) -> list[CompRow]`.
- `rss.RssAdapter(name, url, country)` for `https://www.pepper.pl/rss/hot` and `https://www.hotukdeals.com/rss/hot`. The price is parsed from the title.
- `retail.RetailWatch`: a list of (shop, URL) pairs in `sources.yaml`. It extracts the price from JSON-LD `offers.price`, and emits a listing only when the price drops ≥ 10% below the last seen price, or below the clears-at P25 of its key.

- [ ] Test `test_ebay_disabled_without_keys`.
- [ ] Test `test_ebay_parses_fixture` (a recorded Browse API JSON sample).
- [ ] Test `test_rss_parses_pepper_fixture`.
- [ ] Test `test_retail_emits_only_on_drop`.
- [ ] Implement; PASS; live smoke for RSS and retail. eBay is live only if keys exist; otherwise report it as "skipped: no keys". Commit.

### Task 11: Clears-at ledger

**Files:**
- Create: `dh/ledger.py`, `tests/test_ledger.py`.

**Interfaces:**
- Consumes: `Store`.
- Produces:
  - `ledger.observe(store, source, listings: list[Listing], ts)`: upserts the listings and records observations.
  - `ledger.sweep_disappeared(store, source, ts)`: a listing not seen in the last 2 successful sweeps of its arm, while a window was open the whole time, gets `disappeared_at`.
  - `ledger.clears_at(store, key, condition, since) -> list[CompRow]`: rows have kind `probably_sold` and weight 0.5. The rule: disappeared within 72 h of first being seen, at or below the median ask for its key.
  - `ledger.close_all_windows(store, ts)`: called by `dh stop` and on startup recovery, which closes stale open windows at their last observation time.

- [ ] Test `test_no_disappearance_across_stop` (Review Focus 1): window closed, listing absent at the next start → no `disappeared_at`.
- [ ] Test `test_quick_disappearance_counts_as_probably_sold`.
- [ ] Test `test_slow_disappearance_not_counted`.
- [ ] Test `test_crash_recovery_closes_windows`.
- [ ] Implement; PASS; commit `feat: clears-at ledger with observation windows`.

### Task 12: Valuation

**Files:**
- Create: `dh/valuation.py`, `dh/seed.py` (import from the kukas/bazos_dataset release, when its URL is reachable, of GPU, RAM and SSD rows as `kind=ask_history`, weight 0.2), `tests/test_valuation.py`.

**Interfaces:**
- Consumes: `Store.comps`, `ledger.clears_at`, `EbayBrowse.active_comps`.
- Produces:
  - `ExitEstimate(channel, key, condition, p25_pln, median_pln, p75_pln, n_effective: float, n_sold: int, newest_days, days_to_sell: float|None, trend_30d: float|None, ceiling_pln: Decimal|None, comps: list[CompRef])`
  - `valuation.estimate(store, key, condition, channel, day) -> ExitEstimate|None`. Weighted quantiles: owner sale 1.0, eBay sold 1.0, probably_sold 0.5, active ask × `ask_to_sale_ratio` (default 0.92) at 0.3, ask_history 0.2.
  - Comps older than 30 days are excluded from the confidence count, but used for the trend.
  - The estimate is capped at the new-retail ceiling (lowest new price × 0.95) when one is known.

- [ ] Test `test_weighted_quantiles`.
- [ ] Test `test_ceiling_caps_estimate`.
- [ ] Test `test_returns_none_when_no_comps`.
- [ ] Test `test_trend_sign`.
- [ ] Implement; PASS; commit.

### Task 13: Risk and scam scoring

**Files:**
- Create: `dh/risk.py`, `playbook/items.yaml` (the `scam_kw.*` items), `tests/test_risk.py`.

**Interfaces:**
- Produces `risk.assess(listing, extraction, estimate) -> Risk(p_scam: float, hard_exclude: bool, flags: list[str], checklist: list[str])`.
- Hard exclusions:
  - `box_only`;
  - `wanted_ad`;
  - `for_parts` (when repair is disabled);
  - off-platform contact cues: phone-number regex, whatsapp, telegram, a URL in the description;
  - "przelew na konto" combined with a new account.
- The score is a logistic over:
  - z-score below −2.5 against the median;
  - seller age < 30 days;
  - 0 ratings;
  - stock-photo hint (filled by M2 photo checks);
  - origin CN/HK for gpu keys ≥ 4080;
  - missing photos for 4090/5090 keys.
- The weights live in `config/economics.yaml` under `risk.weights` (frozen).
- The checklist covers: platform payment only; at pickup run GPU-Z and check the device ID, run a VRAM test, inspect the 12V-2x6 connector; ask for photos of the die side and the serial number.

- [ ] Test `test_hard_excludes`.
- [ ] Test `test_too_cheap_new_account_is_risky`: p_scam > 0.3.
- [ ] Test `test_normal_listing_low_risk`: p_scam < 0.05.
- [ ] Implement; PASS; commit.

### Task 14: Effort class, confidence and tiering (frozen)

**Files:**
- Create: `dh/tiering.py`, `tests/test_tiering.py`.

**Interfaces:**
- Produces:
  - `effort_class(listing, exit_channel, flags, estimate) -> Literal['easy','moderate','heavy']`:
    - heavy: non-EU origin, or `needs_vat_ue`, or `bdo_check` with non-EU, or bundle items ≥ 3;
    - moderate: any non-PL leg, or days_to_sell > 14;
    - else easy.
  - `confidence(extraction, estimate, risk, verified_age_s, capital_usd, rules) -> tuple[str, list[str]]`
  - `assign(cons_usd, exp_usd, effort, conf, capital_usd, rules) -> TierResult(tier: int|None, star: bool, floor_usd, reasons: list[str])`. Rules exactly as in Global Constraints; tier None means "not logged".

- [ ] Parametrised test `test_assign`. Columns: cons_usd, exp_usd, effort, confidence, capital_usd, then the expected tier and star:

  | cons_usd | exp_usd | effort | confidence | capital_usd | tier | star |
  |---|---|---|---|---|---|---|
  | 320 | 520 | easy | high | 2000 | 1 | true |
  | 320 | 400 | easy | medium | 2000 | 2 | false |
  | 60 | 80 | easy | low | 500 | 3 | false |
  | 45 | 60 | easy | high | 500 | 4 | false |
  | 100 | 150 | moderate | high | 900 | 4 | false |
  | 20 | 30 | easy | high | 300 | None | — |
  | 900 | 1200 | heavy | high | 31000 | None | — (capital cap) |

- [ ] Test `test_high_confidence_needs_more_comps_for_big_capital`.
- [ ] Implement; PASS; commit.

### Task 15: Verifier, dedupe and deal cards

**Files:**
- Create: `dh/verify.py`, `dh/cards.py`, `tests/test_cards.py`, `tests/test_verify.py`.

**Interfaces:**
- Produces:
  - `async verify.recheck(deal, adapter, fetcher) -> tuple[bool, Decimal|None]`
  - `async verify.screenshot(url, path) -> bool`: Playwright, system Chrome `/usr/bin/google-chrome`, headless, 1280×2000, 30 s timeout. Failure returns False and does not block.
  - `cards.render(deal, listing, estimate, net, risk) -> tuple[str, dict]`: Markdown and JSON per spec §12. The front matter includes an `outcome: {status: open, price_pln: null, date: null, note: ""}` block.
  - `cards.write(deal, root) -> Path`: `deals/YYYY-MM-DD/<id>-<slug>.md|.json`.
  - `dedupe.is_duplicate(store, deal) -> bool`: same seller hash + key + price within 5% + created within 7 days, or the same listing.
- CLI: `dh verify <id>` recomputes the net from the stored evidence JSON (same `economics.net`) and re-checks the listing is alive. It prints `OK` or a diff.

- [ ] Test `test_card_contains_required_sections`: the headline numbers, every breakdown line, ≥ 1 comp link, the checklist, the outcome block.
- [ ] Test `test_verify_recomputes_same_net`.
- [ ] Test `test_cross_platform_duplicate_suppressed` (Review Focus 4).
- [ ] Implement; PASS; commit.

### Task 16: Notifier

**Files:**
- Create: `dh/notify.py`, `tests/test_notify.py`.

**Interfaces:**
- Produces `async notify.push_tier1(deal, card_url) -> bool`:
  - ntfy: `POST {server}/{topic}` with headers `Title`, `Priority: urgent`, `Tags: moneybag`, `Click: <listing url>`, `Actions: view, Card, <card_url>`.
  - On failure, SMTP email (credentials from `bw-secret dh-mail#smtp`). If both fail, the push is queued in a `pending_pushes` table and retried every round for up to 6 h.
- `notify.topic()`: generates a random 24-char topic on first run and stores it in `config/local/notify.yaml`. `dh status` prints it.
- Title format: `T1 ★ +$640 RTX 4090 · OLX Kraków`.

- [ ] Test `test_push_falls_back_to_email_then_queue` (Review Focus 5).
- [ ] Test `test_title_format`.
- [ ] Implement; PASS; live: one test push to the real topic on ntfy.sh; commit.

### Task 17: Log publisher and marks import

**Files:**
- Create: `dh/publish.py`, `tests/test_publish.py`.

**Interfaces:**
- Produces `async publish.sync(store, cfg)`, every `publish.interval_min: 30`. In the local clone `~/.local/share/deal-hunter/log` of `deal-hunter-log` (branch `log`), it does:
  1. `git pull --rebase`;
  2. imports edited `outcome:` blocks as marks (source `github`);
  3. writes new and updated cards, `INDEX.md` (★ and tier 1 first, then by tier and conservative net), `daily/YYYY-MM-DD.md` and `weekly/` reports;
  4. commits as "deal-hunter";
  5. `git push origin log`.

  Push failure: log a health event and retry next cycle; marks are never lost (Review Focus 5).
- No seller data is written. The card links point to the listing URL only.

- [ ] Test `test_outcome_edit_becomes_mark`, using a temp bare repo.
- [ ] Test `test_index_order`.
- [ ] Test `test_push_failure_keeps_state`.
- [ ] Implement; PASS; live: create the private `deal-hunter-log` repo on the rig with `gh`, set its default branch to `log`, run one sync. Commit.

### Task 18: Web page

**Files:**
- Create: `dh/web/app.py`, `dh/web/templates/{base,deals,deal,rounds,playbook,books}.html`, `tests/test_web.py`.

**Interfaces:**
- Produces a Starlette app with these routes:
  - `/` deals list, with filters `tier`, `category`, `country`, `source`, `since`;
  - `/deal/{id}` card with Bought, Skipped(reason) and Sold(price) forms, `POST /deal/{id}/mark`;
  - `/rounds`, `/playbook`, `/books`, `/health`.
- Served by uvicorn on `0.0.0.0:8711` inside the `dh start` process.
- Server-rendered pages. Light and dark CSS inline. No JavaScript beyond the forms.

- [ ] Test `test_mark_post_creates_mark`.
- [ ] Test `test_deals_sorted_star_tier_net`.
- [ ] Implement; PASS; commit.

### Task 19: Bandit, sentinel, scheduler, runtime and books

**Files:**
- Create: `dh/bandit.py`, `dh/scheduler.py`, `dh/runtime.py`, `dh/books.py`, `tests/test_bandit.py`, `tests/test_scheduler.py`, `tests/test_runtime.py`.

**Interfaces:**
- Produces:
  - `bandit.Bandit(gamma=0.98, floor=0.10, prior=(1.0, 10.0))`:
    - `choose(arms: list[str], budget: int, rng) -> list[str]`, where the arms come from playbook query items × enabled sources. Sample a Gamma rate per arm and allocate the budget proportionally. At least `floor`/len share per arm. New arms get 3 guaranteed pulls.
    - `update(arm, value_units: float, fetches: int)`
    - `discount()`
    - The state lives in `arm_stats`.
    - Value units = (Σ expected net USD of tier 1–3 deals + 0.2 × Σ tier 4) / 100.
  - `scheduler.run_round(ctx) -> RoundStats`. Steps:
    1. Pick the arms: bandit arms plus the sentinel arms (`playbook` items tagged `sentinel: true`, never edited).
    2. Search.
    3. Ledger observe and sweep.
    4. Cheap filter: has a key, price within the key's sane range, not hard-excluded.
    5. Extract, using the LLM only for uncertain items.
    6. Value, economics, risk, tier.
    7. Tier ≤ 3: detail fetch, then verify.
    8. Tier 1: screenshot, push.
    9. Cards, costs.

    It keeps an asyncio worker pool sized to `models.yaml` `worker.parallel: 4`, and adds fetch budget when the LLM queue is empty.
  - `runtime.start(cfg)`:
    - checks the RAM guard (free ≥ 12 GB) and llama-swap `/running`;
    - `systemd-run --user --unit=deal-hunter --property=KillMode=control-group <venv>/bin/python -m dh.runtime serve`;
    - `serve` runs the rounds loop and the web app until SIGTERM.
  - `runtime.stop()`: `systemctl --user stop deal-hunter`, `ledger.close_all_windows`, then `POST localhost:8080/unload` for the role models only if no other llama-swap client is using them (check `/running` before and after). `runtime.status()`.
  - `books.energy_pln(seconds, gpu_watts_samples, tariff_pln_kwh)`: GPU power from `rocm-smi --showpower --json`, plus a 120 W base. The tariff in config defaults to 1.10 PLN/kWh.

- [ ] Test `test_bandit_shifts_budget_to_productive_arm`: after 200 simulated rounds, the 0.3 vs 0.05 arms get ≥ 3× the budget.
- [ ] Test `test_floor_keeps_exploring`.
- [ ] Test `test_round_pipeline_with_fakes`: fake adapter, fake LLM; one listing becomes a tier-2 deal card.
- [ ] Test `test_start_refuses_when_ram_low`.
- [ ] Implement; PASS; live: `dh start`, two rounds, `dh status`, `dh stop`; the process is gone and the windows are closed. Commit.

### Task 20: Pedro skill

**Files:**
- Create in the `local-rig-skills` repo (separate branch and PR): `skills/deal-hunter/SKILL.md`. It follows `reference/skill-format.md`: description ≤ 300 chars starting "Use when…", the file ≤ 6 KB, and the script does the work.

**Interfaces:**
- Consumes: the `dh` CLI. JSON output comes from `dh deals --json`, `dh show <id> --json`, `dh status --json`, `dh books --json`.
- Adds the `--json` flags to `dh/cli.py`.

- [ ] Run `bin/sync_skills.py --check`; expect PASS.
- [ ] Live: ask Pedro "/deal-hunter status" through `openclaw agent`; the reply quotes `dh status`.
- [ ] Commit in both repos.

### Task 21: M1 docs and live acceptance

**Files:**
- Create: `README.md`, `docs/{ARCHITECTURE,CONFIGURATION,TUNING,ADDING-A-SOURCE,OPERATIONS}.md`, `ACCOUNTANT-QUESTIONS.md`.

- [ ] Live acceptance on the rig. The spec says a 48 h run; run the longest feasible window, ≥ 3 h, and record the actual duration. Check that:
  - every logged deal has a card;
  - `dh verify` passes on all of them;
  - there is no tier-1 push for a sold, box-only or for-parts listing;
  - free RAM stays above the guard throughout;
  - `INDEX.md` renders on github.com.
- [ ] Commit `docs: M1 documentation and acceptance notes`.

## M2: reach and price truth

### Task 22: Alert mailbox

**Files:** `dh/sources/mailbox.py`, `tests/sources/test_mailbox.py`, fixtures with the `.eml` alert formats for Allegro, OLX, Kleinanzeigen and eBay (built from the platforms' documented formats; replace them with real ones once the mailbox exists).

**Interfaces:**
- IMAP over SSL using the credentials `bw-secret dh-mail#imap`.
- `async poll() -> list[Listing]`: parses the links and prices, then fetches the detail page only where the platform is allowed (Allegro: no fetch, only the email data plus the link). Marks the mail as seen.
- Disabled when the credentials are missing.

- [ ] Fixture parse tests per platform; the disabled-without-credentials test; commit.

### Task 23: eBay sold data

**Files:** `dh/sources/ebay.py` (extend), tests.

**Interfaces:**
- `EbayInsights.sold_comps(key) -> list[CompRow]` via `GET https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search`.
- Enabled only if the token has the scope. Otherwise one health event, "Marketplace Insights not granted".

- [ ] Fixture test; the scope-missing test; commit.

### Task 24: Part-out valuation

**Files:** `dh/partout.py`, tests.

**Interfaces:**
- `partout.value(listing, extraction, store, day) -> ExitEstimate|None`: for category `pc`, extract the GPU, CPU, RAM and SSD keys (rules first, then the LLM); sum the per-part P25 and median; subtract a 12% parting cost and the extra shipping legs.
- The effort is heavy if there are ≥ 3 parts.

- [ ] Test on a PC listing fixture; commit.

### Task 25: Photo checks

**Files:** `dh/photos.py`, tests.

**Interfaces:**
- `async photos.check(listing, fetcher, store) -> list[str]`: downloads up to 4 photos for candidates only; computes pHash; returns a `photo_reuse` flag if the Hamming distance is ≤ 6 to a photo from a different seller hash in the last 90 days.
- The hashes are stored; the images are deleted after 90 days.

- [ ] Reuse-detection test; commit.

### Task 26: Long-tail adapters

**Files:** `dh/sources/olx.py` (`country` ro, bg, pt), `willhaben.py`, tests and fixtures.

- [ ] Same test pattern as Task 9; live smoke; commit.

## M3: the gated change loop

### Task 27: Itemised playbook and deltas

**Files:** `dh/improve/playbook.py`, `playbook/items.yaml`, tests.

**Interfaces:**
- `Item(id, kind: Literal['query','alias','scam_kw','prompt','cadence'], target, value, provenance, helpful: int, harmful: int, sentinel: bool)`
- `Delta(op: Literal['add','update','remove'], item: Item, hypothesis, evidence: list[str], predicted_effect, inverse: 'Delta')`
- `Playbook.load(version|None)`, `apply(delta) -> Playbook` (returns a new instance), `save_version(store) -> int`.
- Caps: ≤ 400 items; a prompt item ≤ 1500 chars; sentinel items are immutable.

- [ ] Test `test_apply_and_inverse_roundtrip`.
- [ ] Test `test_sentinel_immutable`.
- [ ] Test `test_caps_enforced`.
- [ ] Commit.

### Task 28: Replay archive and offline gates (frozen)

**Files:** `dh/improve/replay.py`, `dh/improve/gates.py`, `evals/must_pass.jsonl` (≥ 30 cases from fixtures: scams, box-only, wanted ads, true deals), `evals/golden.jsonl` (bootstrapped from M1 data, extended by owner marks), tests.

**Interfaces:**
- `replay.run(playbook, since, until, store) -> ReplayStats(deals_by_tier, false_pos_known, llm_tokens, fetches)`: re-runs the pipeline from snapshots, with no network.
- `gates.evaluate(champion, challenger, store) -> GateResult(passed: bool, stage: str, detail: dict)`. Order:
  1. Schema and caps.
  2. Must-pass at 100%.
  3. Golden set, McNemar exact (scipy-free binomial): reject if regressions ≥ fixes.
  4. Replay: reject if the expected net of tier 1–3 falls more than 5%, or known false positives rise.

- [ ] Test `test_bad_delta_rejected`: a delta that removes the "pudełko" scam keyword fails must-pass.
- [ ] Test `test_good_delta_passes`: adding an alias that fixes a golden miss passes.
- [ ] Commit.

### Task 29: Shadow mode, probation, CUSUM and rollback

**Files:** `dh/improve/shadow.py`, `dh/improve/cusum.py`, tests.

**Interfaces:**
- `shadow.score_round(champion, challenger, round_candidates) -> list[Disagreement]`: stored in `shadow_results` and labelled later by outcomes or the ledger.
- `cusum.PoissonCusum(lambda0, lambda1, h)`: `update(count) -> bool` (alarm).
- Promotion after N=40 shadow rounds if the labelled disagreements favour the challenger (exact binomial, p < 0.1) and no must-pass regression appears. Probation is 48 watched hours.
- Rollback on a champion CUSUM alarm while the sentinel CUSUM is quiet.

- [ ] Test `test_market_shift_no_rollback`: the sentinel and the champion both drop → no rollback.
- [ ] Test `test_breakage_rollback`: only the champion drops → rollback to the previous version.
- [ ] Commit.

### Task 30: Reflection, proposals, stop rules and weekly report

**Files:** `dh/improve/reflect.py`, `dh/improve/controller.py`, `dh/improve/report.py`, tests, `docs/SELF-IMPROVEMENT.md`.

**Interfaces:**
- `reflect.round_report(round_stats, failures) -> dict`: the senior model classifies the failure causes in a fresh context (schema).
- `reflect.propose(trigger, evidence, playbook) -> Delta|None`: one typed delta.
- `controller.Controller` state machine: `idle → shadow → probation → idle | maintenance`.
  - Triggers: ≥ 3 failures with the same cause, a health alarm, a CUSUM alarm, or a weekly review every 168 watched hours.
  - Cooldown doubling: 8, 16, 32, 64 watched hours.
  - Maintenance mode after 3 consecutive rejections, or when the minimum detectable effect from 2 weeks of watched data exceeds the plausible gain.
- `report.weekly(store) -> str`: scoreboard per spec §9.7, with the line "working well; no changes proposed" when in maintenance.

- [ ] Test `test_cooldown_doubles`.
- [ ] Test `test_maintenance_after_three_rejections`.
- [ ] Test `test_only_one_change_in_flight`.
- [ ] Commit.

### Task 31: Education docs, final live acceptance, PRs

- [ ] `llm-private-education`: chapter 13 "Deal Hunter" in EN (`en/13-DEAL-HUNTER.md`) and PL (`pl/13-DEAL-HUNTER.md`), plus config-map and history rows.
- [ ] M3 acceptance live on the rig:
  - inject a bad delta → rejected;
  - inject a good delta → shadow, then promote;
  - simulate a market shift → no rollback;
  - simulate a breakage → rollback.
- [ ] Final live run of the full system; `dh stop` leaves nothing running.
- [ ] Push `feat/build` to `deal-hunter` and open a PR. Open PRs for `local-rig-skills` and `llm-private-education`.
