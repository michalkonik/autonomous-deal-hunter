# Deal Hunter: design

*Date: 2026-10-29. Status: draft for the owner's approval. Target repo: new private repo `michalkonik/deal-hunter`.*
*Research behind this design, in the session scratchpad. It gets copied to `docs/research/` in the repo:*
- *`deal-tools-landscape.md`: products, open-source parts, platform access, economics, patterns, scams;*
- *`self-improving-agents.md`: the self-improvement literature and its statistics.*

Labels used below:
- **[owner]**: decided by the owner in conversation.
- **[design]**: my call. Correct it if wrong.
- **[builder]**: left to whoever builds it.

---

## 1. Intent

The owner lives in Kraków, runs a VAT-registered one-person company (JDG), has up to **$30k** per deal to invest, and has people who can do the buying, shipping and reselling. They want an agent that runs autonomously on their local LLM rig while it is switched on, and finds **electronics deals they can resell at a profit**. Electronics means GPUs first, then RAM, SSDs, whole PCs to part out and other electronics. The kinds of deal:
- regional price gaps between countries [owner];
- price gaps between shops [owner];
- underpriced used items [owner].

**Success is money** [owner]. The agent succeeds if the deals it reports earn more than the agent costs to run: rig electricity, plus any paid API spend. It fails if it burns compute on deals that are false, unrealistic, unreliable or too thin. The owner will not buy anything that has a real chance of not reselling higher. **Precision matters more than volume.** **Aim big:** the ideal deal is a larger investment with likely profit **over $500** [owner].

**It must beat what exists** [owner]. The research found that no public tool does what this agent will do: deals in PL/EU electronics, judged against real sold prices, with the full cross-border profit calculation. What exists:
- US Facebook-Marketplace alert apps: Swoopa, SuperFlip, Flipify;
- Amazon arbitrage tools: Tactical Arbitrage, SourceMogul;
- new-price comparison sites: Ceneo, Geizhals, idealo;
- single-platform open-source bots, all under 400★. The closest is ai-marketplace-monitor (FB-only, AGPL).

The edge this agent builds:
1. **A database of real clearing prices** for PL/CZ/SK/HU/DE classifieds, built from the moment listings disappear. No public source has this.
2. **The same product matched across countries and platforms.**
3. **The full landed-cost and tax calculation** for a Polish VAT payer.
4. **Scam and defect screening.**
5. **Long-tail platforms** nobody else watches: hardverapro.hu, Bazoš, sbazar, willhaben, Allegro Lokalnie, sprzedajemy, olx.ro/.bg/.pt and others.
6. **Self-improvement that does not regress.**

**It must self-improve for efficiency and accuracy, learning from the engineers who already did this well** [owner]. The design is my call [owner]. Section 9 is that design.

---

## 2. Goals, non-goals, anti-goals

**Goals**
1. Find resale deals that clear the owner's floors (§5) and log them with enough evidence for a human or another agent to verify each one.
2. Push **tier-1** deals to the owner's phone immediately. Log tiers 2–4.
3. Make the log readable from any device, including when the rig is off.
4. Use the rig at near full capacity whenever the agent is running. The rig is dedicated to this agent while it runs [owner].
5. Improve its own efficiency and accuracy over time, without regressions, and stop changing itself when it is working well.
6. Keep its own books: running cost against the expected and realised profit of its deals.
7. Be documented well enough for the owner to configure, tune and extend it, and to learn from it [owner].

**Non-goals (v1)**
- Buying, bidding, messaging sellers, or listing items for sale automatically. Humans act on deals. The agent finds, values and verifies them. [design]
- Logging in to platforms to scrape. Exception: the dedicated alert mailbox in §6.3, which uses ordinary saved-search features. [design]
- A public product or SaaS.
- Category-wide crawling or republishing marketplace data.

**Anti-goals: failure even if it "works"**
- A noisy feed: many deals that turn out false, sold, mis-matched, scams or too thin. One false tier-1 push costs more trust than five missed deals.
- Burning compute or API money without a matching return.
- A self-improvement loop that degrades a good agent, games its own score, or keeps churning.
- Getting the owner's accounts banned, or breaking platform rules in ways that risk the business.
- Losing on judgement. Paid Polish tools already win on raw speed on single platforms: MoneyAlert reports a 7-second median on OLX.pl and offers auto-buy, and BotOLX, SnipingTool and DealRadar exist for OLX and Kleinanzeigen. This agent does not try to win a seconds race there. Its target is minutes-level latency, and its edge is valuation, coverage across platforms and countries, and the economics. Speed matters most where nobody is fast: long-tail sites, cross-border gaps, and stale prices in a rising market. [design]

---

## 3. Shape of the solution: a dedicated agent, not only skills

The owner asked me to decide between "skills for OpenClaw/Pedro" and "a full agent". **Decision: build a dedicated agent with its own repo, plus one small skill so Pedro can query and control it.** [design]

Why skills alone are not enough:
1. **Most of the work is deterministic code, not LLM reasoning.** That includes polling dozens of sources around the clock, a database, a price ledger, a profit engine, rate limiting and a bandit. Doing it through an LLM agent loop wastes the rig's capacity and adds variance.
2. **Safe self-improvement needs a frozen evaluator by construction.** The scorer, the gates, the economics and the evaluation data must be outside the agent's write scope. OpenClaw's Skill Workshop applies an agent's own skill edits immediately, with no approval gate. The research found that every system whose scorer was reachable ended up gaming it.
3. **Running 24/7 with rollback, versioning and health alarms** belongs in a service, not in chat sessions.

OpenClaw still has a role. A skill `/deal-hunter` in `local-rig-skills` lets the owner ask Pedro:
- "what did it find tonight",
- "explain deal 1432",
- "is it healthy",
- "start/stop".

The agent's LLM work uses the same llama-swap models as Pedro.

---

## 4. Architecture

```
               ┌──────────────── frozen layer (agent cannot write) ────────────────┐
               │ economics engine · tier rules · floors · scorer · gates · eval sets│
               └───────────────────────────────────────────────────────────────────┘
 sources ──► COLLECTORS ──► store(raw listings, snapshots) ──► NORMALISE+MATCH ──► VALUATION
 (adapters,    (code, no LLM;            SQLite + files        (rules first,      (clears-at ledger,
  mailbox,      rate-limited)                                    LLM for ambiguous) comps, retail ceiling)
  RSS, APIs)                                                                          │
                                                                                      ▼
   ntfy push ◄── TIERING ◄── RISK/SCAM ◄── ECONOMICS (net per exit channel, per effort class)
       │            │
       ▼            ▼
   owner's phone   DEAL CARDS (DB + .md + .json) ──► web page on the rig · GitHub log repo (every 30 min)
                    │
                    ▼
   SELF-IMPROVEMENT: bandit (every round) · gated change loop (proposals → offline → shadow → promote/rollback)
```

**Components** [design; library choices are [builder] unless named here]:

| # | Component | Runs on | Job |
|---|---|---|---|
| 1 | **Scheduler** | CPU | Runs rounds. Gives each round's fetch budget to source×query "arms" using the bandit (§9.2). Respects per-domain rate limits and backoff. |
| 2 | **Collectors** (one adapter per platform) | CPU, plus a headless browser only where needed | Fetch new and changed listings. Store raw snapshots. Order of preference: official API → JSON endpoint with TLS impersonation (curl_cffi) → stealth browser (Patchright or Camoufox) → alert mailbox (§6.3) → RSS. |
| 3 | **Store** | disk | SQLite (WAL) for structured data. Raw HTML/JSON and screenshots as files, with retention limits. |
| 4 | **Normaliser and matcher** | CPU, then the worker LLM | Map each listing to a canonical product key, e.g. GPU = chip + VRAM + variant + AIB line; RAM = type + capacity + kit + speed. Regex and taxonomy first; the LLM with JSON-schema output only for ambiguous titles. Also extracts condition, defects, bundle contents, "box only"/"for parts" flags, and the seller's shipping and pickup terms. |
| 5 | **Valuation** | CPU | Exit-price distribution per (product key, condition bucket, exit channel), from the sources in §6.2. Outputs P25, median, P75, the number of datapoints, freshness, days-to-sell, and the price trend. |
| 6 | **Economics engine** (frozen) | CPU | Net profit per candidate exit channel and effort class (§7). Picks the best exit. Computes a conservative net (P25 exit) and an expected net (median exit). |
| 7 | **Risk and scam** | CPU, then the worker LLM, then the senior LLM for finalists | Scam probability and deal confidence from the signals in §8. |
| 8 | **Tiering and verifier** | senior LLM plus code | Assigns tiers by the rules in §5. Before a tier-1 push, re-fetches the listing to confirm it is live with the same price, and writes the deal card. |
| 9 | **Notifier** | CPU | Tier 1 goes to ntfy (urgent priority) with email as fallback. A daily digest goes to the log. |
| 10 | **Web page** | CPU | Local, read-mostly page on the rig (LAN): the deals list, deal cards, Bought/Skipped/Sold buttons, rounds, playbook versions, costs. |
| 11 | **Log publisher** | CPU | Every 30 min, commits deal cards, an index and daily summaries to the private GitHub repo `deal-hunter-log`. |
| 12 | **Self-improvement** | senior LLM plus code | The bandit, the reflection reports, and the gated change loop (§9). |
| 13 | **Books and health** | CPU | Energy and API cost per round. Adapter health (zero results, 403s, layout changes). Alarms. RAM guard. |

**Language and runtime** [design]:
- Python 3.12, with a CLI `dh` (`start`, `stop`, `status`, `deals`, `show <id>`, `verify <id>`, `replay`, `eval`, `playbook`, `books`).
- A `systemd --user` service that the owner starts and stops: "when I start this agent" [owner].
- LLM calls go through the llama-swap OpenAI-compatible endpoint at `localhost:8080`.
- Nothing runs in Docker unless a reused tool requires it.

---

## 5. Deal rules (frozen layer)

All values live in `config/rules.yaml`. The agent can read this file and cannot change it.

**Floors by effort class.** These apply to the **conservative net** [owner, refined in conversation]:

| Effort class | Rule of thumb | Minimum conservative net |
|---|---|---|
| Easy | Bought and sold in Poland, shippable by courier, easy to test | **$50** [owner] |
| Moderate | Any intra-EU international leg, or slow expected sale (more than 14 days to sell) | **$120** [owner] |
| Heavy | Import from outside the EU (VAT, clearance, long transit), lots or bundles to split, items needing repair or testing beyond plug-in | **$250** [design] |

The builder defines the exact classification rules. [builder]

**Tiers:**

| Tier | Rule | Action |
|---|---|---|
| **1** | Conservative net ≥ **$300** **and** high confidence [owner] | Instant ntfy push, plus log |
| 2 | Clears its floor, and confidence is medium or better | Log |
| 3 | Clears its floor, but confidence is low (thin data, unclear condition, weak seller) | Log |
| 4 | Within 20% under its floor, or a trend signal (a price gap opening, a model falling) | Log |

**"High confidence"** means all of the following [design; thresholds in config]:
- The product match is certain (rules matched, or LLM certainty ≥ 0.9 confirmed by a second pass).
- The exit valuation rests on at least **12 comparables** less than 30 days old. The threshold rises with capital: 20 comparables above $5k, 30 above $15k.
- Scam probability is below 0.05.
- The listing was re-checked live, at the same price, within the last 10 minutes.
- There are no unresolved defect flags.

Other rules:
- **★ Big deal:** expected net ≥ **$500** [owner]. ★ deals sort first everywhere and are marked in pushes.
- **Capital cap:** skip anything needing more than **$30k** [owner]. Capital means buy price plus inbound costs plus VAT advanced.
- **No volume limit** [owner]: every deal that clears its floor is logged.
- **Money:** amounts are shown in USD and PLN at the NBP rate of the day, and the rate's date is recorded on the card.

---

## 6. Sources

### 6.1 Discovery (where deals are found)

Phase 1 is the first build; phase 2 follows once phase 1 is stable. Access and anti-bot ratings come from the research's live probe on 2026-10-29.

| Phase | Source | Access | Notes |
|---|---|---|---|
| 1 | OLX.pl | JSON endpoint via curl_cffi; browser fallback (CloudFront blocks plain HTTP) | The same adapter later covers olx.ro/.bg/.pt |
| 1 | Allegro Lokalnie | plain HTTP | |
| 1 | sprzedajemy.pl | plain HTTP | |
| 1 | Kleinanzeigen.de | plain HTTP at low rate; reuse DanielWTE/ebay-kleinanzeigen-api if useful | |
| 1 | Bazoš (cz/sk/at), sbazar.cz | plain HTTP | Within driving distance |
| 1 | hardverapro.hu | plain HTTP | Hungary's hardware market |
| 1 | Marktplaats/2dehands | JSON via marktplaats-py | |
| 1 | eBay (DE/PL/FR/IT/ES/US) | official Browse API, 5k calls/day | Also active comparables |
| 1 | Pepper.pl / hotukdeals RSS | RSS | Retail price errors |
| 1 | x-kom / Morele / Komputronik / Media Expert / RTV Euro AGD outlet pages | changedetection-style watches | New-retail price errors and outlet |
| 2 | Allegro.pl | Camoufox, logged out, home IP, about 1 request per 10–20 s | DataDome; no public search API since Oct 2026 |
| 2 | Vinted (as a source only) | browser-warmed session, then JSON | Its terms forbid bots and reselling for profit on standard accounts, so it is a sourcing channel only, at low rate. **Owner to confirm they accept the ToS risk.** |
| 2 | Alert mailbox (§6.3) | IMAP | Allegro/OLX/Kleinanzeigen/eBay saved searches |
| 2 | willhaben, tori/finn/blocket, Subito, Wallapop, Mercari JP (sold-out view), Buyee/ZenMarket | mixed | Mostly price references or sell-side |
| 2 | Bailiff/KAS/zoll-auktion, returns pallets (manifests only) | daily crawl | Reuses `scan-bailiff-auctions` logic |

Facebook Marketplace and groups are out of scope in v1: they need a login, and the ban risk falls on the owner's account [design].

### 6.2 Price truth (exit valuation)

In order of trust:
1. **The owner's realised sales**, from the Sold button.
2. **The clears-at ledger.** For every tracked listing, record first price, every price cut, and when it disappears. A listing that disappears within 72 h at or below the median ask counts as **probably sold**, weighted lower than a real sale. This is the moat, and it needs **4–8 weeks** to mature.
3. **eBay sold data**:
   - Marketplace Insights, if eBay grants access; apply for it.
   - Otherwise, Product Research in Seller Hub, through a logged-in browser at very low rate. This is optional and the owner must OK the account risk.
   - Allegro's "X osób kupiło" counter as a sales-velocity signal.
4. **Live comparables:** active asking prices, discounted by a learned ask-to-sale ratio.
5. **The new-retail ceiling:** Ceneo, Geizhals, Okazje.info price history. A used item rarely sells above the new price.

**Cold start** [design]: until the ledger has enough data for a product key, confidence is capped at medium. Tier 1 is then only possible with eBay sold data, or with ≥ 12 real comparable sales from the ledger.

**Expect this:** in the first weeks, tier-1 pushes will be rare or absent unless an eBay sold-price source is available. Tiers 2–3 will carry the early value, and the owner's people can act on them with the evidence on the card. To shorten the cold start:
- the M1 build seeds the ledger from Bazoš's public two-year dataset (kukas/bazos_dataset) and from eBay Browse active prices;
- if the owner accepts the account risk, logged-in eBay Product Research moves into M1.

### 6.3 Alert mailbox [design; owner sets it up]

- A dedicated email account, stored in Bitwarden folder `agents`.
- The owner signs up on Allegro, OLX, Kleinanzeigen and eBay with that email, and saves narrow searches. Allegro allows up to 300 followed searches. The agent can suggest which searches to save.
- The agent reads the alert emails over IMAP and treats each one as a new-listing event.
- This is normal platform use, low ban risk and cheap. It does not replace polling on sites where polling is safe.

### 6.4 Fetching etiquette (frozen) [design]

- Logged out, except the mailbox.
- From the rig's home IP. No datacenter proxies; residential proxies are configurable but off.
- At most one request every few seconds per site, with jitter. Exponential backoff on 403/429.
- No CAPTCHA solving.
- Detail pages fetched only for candidates.
- Never automate the accounts used for selling.
- Seller names and phone numbers are pseudonymised in the store and **never** written to the GitHub log.
- GDPR (the Bisnode ruling: the duty to inform people whose data you collect is read strictly). The store keeps only what scoring needs:
  - a salted hash of the seller ID;
  - account age;
  - rating count and score.

  It keeps no names, phone numbers or free-text profile data. Photos and descriptions are kept only for live deals, for evidence, and deleted after a retention period (config, default 90 days). They are never republished.

---

## 7. Economics engine (frozen layer)

For each candidate, compute net profit for every plausible exit channel (Allegro, OLX, eBay.de, eBay.com, Kleinanzeigen, local pickup, …) and keep the best. Every input on a deal card shows its source and timestamp. Defaults live in `config/economics.yaml`.

```
gross_exit          = exit_price (P25 for conservative, median for expected) in the exit currency
exit_fees           = platform fee(channel, category) + payment fee + promotion (0 by default)
inbound             = buy_price + inbound shipping + insurance + forwarder fees
import (non-EU)     = duty(CN code via ISZTAR API, cached) + import VAT 23% + clearance fee
VAT (JDG, VAT payer):
   bought from a private person in the EU, no invoice  -> margin scheme: VAT = 23/123 × max(0, sale − purchase)
   bought with a VAT invoice or imported               -> input VAT deducted; output VAT 23% on sale -> net effect on margin
   (intra-EU B2B purchase = intra-Community acquisition, reverse charge; needs VAT-UE; flag on the card)
outbound            = shipping + insurance (if seller-paid)
returns reserve     = p_return(channel, B2C) × (2 × shipping + restock loss)   # 14-day withdrawal, 2-year warranty
risk haircut        = p_scam_or_DOA × buy_price
fx                  = NBP mid + spread (1% default)
pre-tax profit      = exit − fees − inbound − import − VAT due − outbound − reserves − fx
income tax          = 19% (podatek liniowy) + health contribution 4.9%  -> 23.9% of pre-tax profit
net                 = pre-tax profit − income tax
```

**Tax defaults** [design, conservative pending the owner's accountant]:
- **Income tax:** the owner stated their JDG is on **podatek liniowy**, with IP Box for IT income [owner]. Resale profit is not IP Box income, so the default is 19% flat plus 4.9% health contribution, applied to profit. The owner first said 12%, but that is a progressive-scale (or ryczałt-for-IT) rate and does not fit liniowy. The engine supports `tax_mode: liniowy | skala | ryczalt` so the structure is right whatever the accountant says; under ryczałt, tax is a share of revenue. Default: `liniowy`.
- **Intra-EU purchases from businesses:** these need VAT-UE registration, which is not yet confirmed. Config: `vat_ue_registered: false` (default). While it is false, such deals carry a "needs VAT-UE" flag and are treated as Heavy.
- **Exit channels:** the owner named Allegro, OLX, eBay and Vinted, plus others if needed [owner]. Vinted is excluded as a sell channel (its terms forbid reselling for profit on standard accounts). Only channels listed in `economics.yaml: exit_channels.enabled` are used to compute nets. Default: `allegro, olx, ebay_pl, ebay_de`. Adding Kleinanzeigen or eBay.com is one line once someone can actually sell there. This prevents "best exit" figures for channels nobody can sell on.
- **VAT:** the margin scheme for used goods bought from private persons.
- **B2C returns reserve:** 5% (config).

Everything is configurable. An `ACCOUNTANT-QUESTIONS.md` file in the repo lists what to confirm:
- the activity code (PKD) for trade;
- whether a cash register is needed for electronics sold online;
- margin-scheme bookkeeping;
- VAT-UE registration and OSS;
- BDO/WEEE when importing.

Legal facts the engine and the cards encode [verified by research, 2026-10-29]:
- **B2C warranty:** liability for non-conformity in B2C sales is **2 years, and also for used goods**. Since the 2023 reform it cannot be shortened (UOKiK). The returns and warranty reserve must reflect that.
- **GPSR:** a business seller's listings must show the manufacturer's name and address, the EU responsible person, product identification and Polish safety warnings. This also applies to used goods. Deal cards include a GPSR checklist for the resale listing.
- **WEEE/BDO:** a Polish business that brings equipment into Poland from another EU state or from outside the EU is likely a "wprowadzający". That means BDO registration and fees. Whether a bare GPU is in scope is unconfirmed. Until the accountant or BDO helpdesk confirms, imported deals carry a "BDO" flag and stay Heavy. Domestic resale makes the owner a distributor, which brings take-back duties but no registration.
- **eBay terms:** since Feb 2026 they ban AI shopping agents and scrapers. eBay is used only through its official API.
- **Cash register:** the research reports that computers, computer parts, consoles, phones and monitors are excluded from every cash-register exemption, including the mail-order one. B2C sales of these goods therefore need a cash register (kasa fiskalna) from the first sale. The research rates this as verified in the statute texts; the accountant should confirm. The economics engine adds a per-sale fiscal cost (config, default 0). Deal cards note that B2C receipts are needed.
- **EU import fees:** a €3 duty per tariff line on parcels up to €150 has applied since 1 July 2026. A **€2 EU handling fee per item** starts on 1 Nov 2026 and also applies above €150. Both are in the import calculation. DHL's clearance fee for recipients without a DHL account is at least 69 PLN or 2.5% of duties and taxes (verified).

The engine is pure functions with unit tests on hand-computed examples. Changing it is a human code change, never an agent change.

---

## 8. Risk, scams and untrusted input

**Hard excludes:**
- "box only", "pudełko", "nur OVP";
- for-parts listings, unless the deal is a repair deal in the heavy class;
- listings asking for payment off-platform: WhatsApp, Telegram, links, PayPal Friends & Family.

**Scam score inputs:**
- account age and ratings;
- price z-score against clears-at. Below −2.5 is suspicious, not "great";
- photo reuse (perceptual hash, CLIP);
- template text clustering across cities;
- a mismatch between the claimed model and the photos;
- origin CN/HK for high-end cards;
- missing photos of the connector or the GPU die side for 4090/5090 cards.

Known 2026 patterns to encode:
- die- or VRAM-stripped RTX 4090s on eBay;
- fake "4090s" with plastic dies;
- the Kleinanzeigen and Vinted payment scams: "Sicher bezahlen" phishing, QR codes, triangle fraud, false buyer-protection disputes.

**What the owner's people need at hand-off:** each deal card carries a **"how to buy safely"** checklist for that deal. It covers payment method, what to test at pickup (GPU-Z device ID, VRAM test, connector), and which photos to ask for.

**Untrusted content:** listing text and web pages are data, never instructions. LLM prompts quote listing text inside clear delimiters. The LLM's output is schema-validated, and the model has no tools in the triage step. The research hit pages with hidden instructions aimed at AI readers.

---

## 9. Self-improvement (my design, based on the research)

The research's main findings drive this design:
- **Deals are rare, so a few empty rounds mean nothing.** At 0.1–0.3 deals per round, three empty rounds happen 41–74% of the time by chance. Confirming that a change doubles the deal rate takes about 40–60 deals.
- **A reachable scorer gets gamed:** DGM, STOP, AlphaEvolve, METR's o3 runs.
- **Full rewrites destroy playbooks.** In ACE, a rewrite shrank one from 18,282 tokens to 122 and scored below having no playbook.
- **Self-judged success poisons learning.**
- **Unchecked accumulation hurts.** Skills the agent wrote itself scored below no skills at all in SkillsBench.

### 9.1 What may change, and how

| Surface | Who changes it | How |
|---|---|---|
| Fetch budget per source×query arm | **Bandit**, automatically, every round | Discounted Thompson sampling, with an exploration floor of 10% |
| Playbook items: search queries, keyword aliases, product-matching aliases, scam keyword lists, LLM triage prompts, per-source poll cadence | **The agent proposes; code decides** | One typed delta at a time, through the gates in 9.3 |
| New sources | The agent proposes a URL or platform with evidence | Config-only adapters (RSS, simple JSON/HTML with selectors) can go through the gates. Anything needing new code becomes a ticket in `improvements/` for a human or Claude developer. |
| Adapter code, economics, tiers, floors, scorer, gates, eval sets, logs | **Never the agent** | Human code change through git and PR, tested by `dh eval` |
| Models (which LLM per role) | The owner, in config | Treated as a challenger: a model change goes through the same offline gates |

### 9.2 Fast loop: every round, automatic, cannot break anything

**The bandit:**
- Each arm, a source plus query, keeps a discounted posterior of **value per fetch**. Value is **expected net of tier 1–3 deals, plus 0.2 × value of tier-4 near-misses**. Near-misses are frequent, so the signal is denser.
- Arms with blocks or errors are throttled by health checks, not by the bandit.
- New arms start with an optimistic prior and a guaranteed trial budget.

**The sentinel:**
- A small fixed set of arms that the agent never modifies runs every round as the control group.
- A Poisson CUSUM on the champion's deal rate, compared against the sentinel, separates "the market changed" from "my change broke it".

### 9.3 Slow loop: gated changes

1. **Reflect.** After every round, the senior model writes a short structured round report. It sorts failures by cause, e.g. false match, stale listing, scam caught late, block, empty query, valuation miss, slow fetch. It also lists missed deals, found later by another source or reported by the owner.
   - It does **not** change anything.
   - A proposal is drafted only when one of these triggers fires:
     - ≥ 3 failures with the same cause;
     - a health alarm;
     - a CUSUM alarm;
     - the weekly scheduled review.
2. **Propose.** One typed delta:
   - target item ID;
   - change;
   - hypothesis;
   - evidence (log IDs);
   - predicted effect;
   - the inverse delta.

   Deltas merge by code into an itemised playbook. Each item has an ID, provenance, helpful and harmful counters, and a size cap. No full rewrites.
3. **Gate, cheapest first:**
   1. Schema check and size cap.
   2. **Must-pass set**: frozen labelled cases (known scams, box-only, true deals), 100% required.
   3. **Golden set**: paired comparison with the champion. Exact McNemar test; reject if regressions ≥ fixes.
   4. **Replay** on archived raw snapshots from the last 2–4 weeks. Compare deals found, false positives, and compute used.
   5. **Shadow**: the challenger scores the live stream alongside the champion, without acting, for N rounds. Disagreements are labelled by later outcomes, by the clears-at ledger, or by the owner's marks.
   6. **Promote** with a version number. **Probation** follows, with automatic rollback if CUSUM or health alarms fire while the sentinel stays stable.
4. **One change in flight.** No new proposal starts while one is in shadow or probation.

### 9.4 Knowing when to stop

All waits below count **watched hours**, i.e. time the agent was running, not calendar days.
- **After a promotion:** probation with no new proposals for 48 watched hours (config).
- **After a rejection:** the wait before the next proposal doubles: 8, 16, 32, 64 watched hours.
- **Maintenance mode:** after 3 rejections in a row, or when the largest plausible gain is smaller than 2 weeks of data could detect. The playbook freezes and only the bandit adapts. The agent states this in its weekly report ("working well; no changes proposed"). Only alarms or the weekly review reopen it.

### 9.5 Learning only from verified outcomes

Training and eval labels come only from:
- the owner's Bought/Skipped(reason)/Sold(price) marks;
- disappearance-ledger outcomes;
- eBay sold data;
- deterministic checks.

The agent's own opinion of a deal is never a label.

### 9.6 The reflection model

- Default: the senior local model. It classifies failures and drafts small deltas, which a 30–80B model does reliably.
- **Weekly review:** a configurable "advisor" role. Default: the same local model. Optionally a paid model with a monthly cap (§11).
- Critique runs in a fresh context, not in the context that produced the decision.

### 9.7 The scoreboard

Per round and per week:
- expected net of deals by tier;
- precision on owner-marked deals;
- false tier-1 count;
- missed-deal count;
- fetches, LLM tokens and kWh per deal;
- cost against expected and realised profit;
- adapter health.

The weekly report is published to the log repo.

---

## 10. Capacity plan (rig: 4× R9700, 60 GB RAM, 16 cores)

**RAM is the binding limit, not VRAM.** A second model next to a large one caused a kernel panic on 2026-10-28.

**Default model setup** [design]: the existing llama-swap group **`pedro`**, which loads both of these at once:

| Role | Model | Cards | Measured throughput | Used for |
|---|---|---|---|---|
| Workers | `gemma-4-26b-a4b-pool` | 2+3 | 4 parallel slots, ~74 tok/s each, ~184 aggregate | Extraction, matching, first-pass scam check, round reports |
| Senior | `qwen3.8-flash-next-uncensored` | 0+1 | (not stated) | Tiering finalists, verification, proposals, weekly review |

Other rules:
- **Plain code does the bulk work.** Collectors, ledger and economics are code, so LLM tokens go only to listings that survive cheap filters. The design target is under 5% of new listings reaching the LLM.
- **One headless browser at a time.** A RAM guard refuses to start a second browser or any extra model when free memory is below 8 GB.
- **Full capacity whenever running.** The worker queue is kept full: the bandit gets extra fetch budget when the LLM queue is empty, and fetching throttles when the queue backs up. The builder measures and tunes slot counts and queue depth. [builder]
- **Cost tracking:** energy is measured from GPU power (rocm-smi) plus an estimate for CPU and the rest of the system, times the owner's tariff (config, PLN/kWh).

---

## 11. Configuration surface

| File | Contents | Who changes it |
|---|---|---|
| `config/models.yaml` | role → endpoint and model. Roles: worker, senior, advisor, tier-1 second opinion. Default: local only, paid APIs **off** [owner]. Optional `paid_api.monthly_cap_usd`. | owner |
| `config/rules.yaml` | floors, effort classes, tiers, confidence thresholds, capital cap, ★ threshold | owner (frozen to the agent) |
| `config/economics.yaml` | fees per channel, shipping tables, tax and VAT mode, return reserve, FX spread, energy tariff | owner (frozen to the agent) |
| `config/sources.yaml` | enabled adapters, rate limits, proxy (off), mailbox | owner |
| `playbook/` | itemised playbook (queries, aliases, scam keywords, prompts) | agent, via gates only; versioned |
| `config/notify.yaml` | ntfy topic and server, email fallback, quiet hours (default: none for tier 1) | owner |

Secrets come from Bitwarden folder `agents` through the existing `bw-secret` helper. Secrets never go into the repo or the log.

---

## 12. Outputs [design, approved by the owner in conversation]

1. **SQLite database**, the source of truth. `dh` CLI prints JSON for other agents.
2. **Deal card** for each deal: `deals/YYYY-MM-DD/<id>-<slug>.md` plus `.json`. Contents:
   - headline numbers: buy, exit (P25 and median), conservative and expected net, confidence, capital, days to sell, tier, ★, effort class;
   - the line-by-line net calculation, with the FX rate and its date;
   - evidence: listing link, a screenshot at discovery and at verification, comparables with links and dates, the retail ceiling, the trend;
   - scam checks passed and failed;
   - why this tier;
   - "how to buy safely".

   `dh verify <id>` recomputes the net from the stored evidence and re-checks the listing is live.
3. **Web page** on the rig (LAN): the deals list (filter by tier, category, country, platform, date; sorted ★ → tier → net), deal cards with the Bought/Skipped(reason)/Sold(price) buttons, rounds, playbook versions with diffs and scoreboard, books.
4. **Private GitHub repo `deal-hunter-log`.** Updated every 30 min with deal cards for tiers 1–4, `INDEX.md` (★ and tier 1 first), daily summaries and weekly self-review reports. Readable on any device, including when the rig is off [owner: reading is enough]. Pseudonymised: no seller personal data.
   - **Marks from anywhere** [design]. The people who buy and sell may not be on the home network, so outcomes can also be recorded in the log repo. Each deal card has a `outcome:` front-matter block (status, price, date, note) that anyone with repo access can edit in the GitHub app. The publisher pulls before each push, and edits there are imported as marks. The LAN page buttons write the same block. Outcomes are the main verified labels, so they need a channel that works away from home.
5. **ntfy push for tier 1.** Title: `T1 ★ +$640 RTX 4090 FE · OLX Kraków`. Body: buy price, exit, net, confidence, link to the listing and to the deal card in the log repo. Email is the fallback when ntfy fails.

---

## 13. Documentation (the owner learns from it) [owner]

In the repo, English:
- `README.md`: what it is, quick start.
- `docs/ARCHITECTURE.md`
- `docs/CONFIGURATION.md`: every key, with examples.
- `docs/TUNING.md`: which knob moves what, and how to read the scoreboard.
- `docs/ADDING-A-SOURCE.md`: writing an adapter, with a worked example.
- `docs/SELF-IMPROVEMENT.md`: how the loops work, how to read a proposal, how to force a rollback or maintenance mode.
- `docs/ECONOMICS.md`: the formula, with worked examples.
- `docs/OPERATIONS.md`: start, stop, health, backups, what to do on a block.
- `docs/research/`: the two research reports.
- `ACCOUNTANT-QUESTIONS.md`

Also:
- A chapter in `llm-private-education` (EN + PL) on running and developing Deal Hunter on the rig, plus config-map and history rows.
- The generic guide "Self-improving agents without regressions" (EN + PL) in the public `llm-education`. It is already being written.

---

## 14. Phasing and acceptance

**M1: a working, honest hunter (phase-1 sources)**
- Store, collectors, normaliser and matcher for GPUs, RAM and SSDs.
- Clears-at ledger, valuation, economics engine with unit tests.
- Scam rules, tiering and verifier.
- Deal cards, web page, ntfy, log publisher.
- Bandit and sentinel, books and health.
- `/deal-hunter` skill for Pedro.
- **Acceptance:**
  - The economics engine matches 10 hand-worked examples to the cent.
  - A 48-hour run produces a valid deal card for every logged deal.
  - `dh verify` passes on all of them.
  - Zero tier-1 pushes for a listing that was already sold or that is box-only or for-parts.
  - RAM never drops below the guard.
  - The GitHub log renders on mobile.

**M2: reach and price truth**
- Allegro (Camoufox), alert mailbox, Vinted as a source, eBay sold data.
- Whole-PC part-out valuation, photo checks.
- Long-tail additions.
- **Acceptance:** a per-source health dashboard, and the ledger reports clears-at prices for at least 30 product keys.

**M3: the gated change loop**
- Golden and must-pass sets (bootstrapped from M1–M2 data and owner marks), replay, shadow, promotion and rollback, maintenance mode, weekly review.
- **Acceptance:**
  - A deliberately bad delta is rejected by the gates.
  - A deliberately good delta is promoted.
  - A simulated market shift triggers no rollback, because the sentinel moves too.
  - A simulated breakage does trigger rollback.

---

## 15. Owner set-up tasks

1. Create the dedicated alert email and the platform accounts (M2), and store them in Bitwarden `agents`.
2. Get an eBay developer account and Browse API keys. Apply for Marketplace Insights.
3. Install the ntfy app and subscribe to the topic the agent prints.
4. Create the private repos `deal-hunter` and `deal-hunter-log`, or let the builder create them with `gh` on the rig.
5. Take `ACCOUNTANT-QUESTIONS.md` to the accountant, then update `config/economics.yaml`.
6. Decide on Vinted (ToS risk) and on logged-in eBay Product Research (account risk).

## 15a. Open owner decisions and the defaults used until answered [design]

1. **Rig uptime: answered** [owner]. The owner always starts the agent by hand and wants control over when it runs. The rig is also used for other things. The owner only promised that no other heavy processes run while this agent is working. Consequences:
   - **No autostart.** No boot service, timer or cron. `dh start` starts everything; `dh stop` stops everything: collectors, browsers and scheduler. It also releases the models it loaded (llama-swap unload) and leaves no process behind. `dh status` shows whether anything is still running.
   - While running, it may use the rig at full capacity. It still keeps the RAM guard, and refuses to start if free RAM or the GPUs are already taken by something else. In that case it says so rather than evicting another workload.
   - The clears-at ledger records **observation windows**. A listing that disappears while the agent was stopped has an unknown disappearance time and is **not** counted as "probably sold". Clears-at prices mature only from watched time, so the cold start lasts longer the less the agent runs. The scoreboard reports watched hours.
   - Self-improvement counts rounds, not calendar days. Probation, cooldowns and backoff are measured in rounds or watched hours.
   - The always-on collector idea is dropped. It contradicts the owner's wish to control when the agent works.
2. **Platform-risk appetite** (unknown). Defaults:
   - OLX: **TLS impersonation (curl_cffi) at slow rates, allowed**. Falls back to the browser only if that fails.
   - Allegro.pl: **alert mailbox only**, no stealth-browser scraping, until the owner says yes.
   - eBay: **official API only**, no logged-in Product Research.
   - Vinted: **off**.

   Every one of these is a switch in `sources.yaml`.
3. **What the owner's people can do** (unknown). Defaults:
   - Deals must be **shippable**. Pickup-only listings count only within **60 km of Kraków** (config `pickup.max_km`, `pickup.countries: [PL]`). The pickup cost is a per-km rate plus time (config).
   - **Repair deals are off** (`repair.enabled: false`): for-parts listings are hard-excluded.
   - "Testing" means plug-in tests only (boot, GPU-Z, VRAM test). The deal card lists them.

## 16. Left to the builder

- Library choices beyond those named.
- The schema details.
- The web framework: a server-rendered page is enough.
- Exact effort-class rules.
- The scam-score weights. Start with hand-set weights and calibrate them on owner marks.
- Retention periods.
- Slot and queue tuning on the rig.
- The adapter order within phase 1.
