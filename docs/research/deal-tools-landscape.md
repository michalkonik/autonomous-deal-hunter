# Electronics/GPU deal-finding: landscape, building blocks, economics (as of 2026-10-29)

**Scope.** A 24/7 autonomous agent for a Kraków-based reseller covering GPUs and electronics. It looks for cross-border and cross-shop price gaps and for cheap used items to flip. It runs on a local rig: llama.cpp, the OpenClaw harness, and a Playwright browser agent.

**Evidence markers.**
- **[VERIFIED]** means read on a primary source: the official docs or repo, the platform's own pages or staff replies, or our own live test on 2026-10-29.
- **[SECONDARY]** means a news, blog or vendor page.
- **[INFERENCE]** means my reasoning or a design proposal.
- **[UNVERIFIED-KNOWLEDGE]** means it comes from background knowledge (cutoff around mid-2026) and was **not re-checked in this pass**.

**Coverage caveat.** Three parallel research threads were dispatched: commercial products, PL economics/legal, and arbitrage patterns plus scams. They had not reported back when this report was due. Sections 2, 5, 6 and 7 therefore rest on targeted checks plus background knowledge. Their numbers are marked accordingly and **must be confirmed before they go into the profit calculator.** Sections 3 and 4 (building blocks, platform access, price truth) were researched in full against primary sources.

---

## 1. Executive summary

**What exists today.** It falls into five groups, none of which does what the user needs.

1. **US-centric "flip alert" SaaS** such as Swoopa and Flipify. These watch Facebook Marketplace, Craigslist, OfferUp, eBay and Mercari US and push keyword or price alerts. They are fast and easy to use. They do not cover PL, CZ, HU or DE classifieds, have no landed-cost model, and have no sold-price valuation for EU platforms [UNVERIFIED-KNOWLEDGE].
2. **Amazon online-arbitrage tools**: Tactical Arbitrage, SourceMogul, and Keepa as the data layer. They are built for "retail shop → Amazon FBA". Their sold-velocity data is Amazon BSR. They do not fit a used-GPU flipper [UNVERIFIED-KNOWLEDGE].
3. **Price comparison and history sites**: Ceneo, Geizhals, idealo, PriceSpy/PriceRunner, camelcamelcamel. These hold **new-retail asking prices** with price history and drop alerts. They are useful as the "new price ceiling" and for spotting retail price errors. They have no used-market data [UNVERIFIED-KNOWLEDGE; Geizhals and Ceneo reachability VERIFIED].
4. **Deal communities**: Pepper.pl, mydealz and hotukdeals. Human-curated new-retail deals with keyword alerts. Good for price errors, but every flipper sees the same post within minutes [UNVERIFIED-KNOWLEDGE].
5. **Open-source alert bots**, one per platform: Vinted, OLX, Kleinanzeigen, Marktplaats, Wallapop, Bazoš, Mercari. Almost all are below 260 GitHub stars and single-country, and none values a deal [VERIFIED: `gh` survey, §3]. The closest thing to the user's idea is **BoPeng/ai-marketplace-monitor** (397★, AGPL-3.0, pushed 2026-10-26). It does LLM scoring and seller-reputation checks for each new Facebook Marketplace listing, but it is FB-only, US-oriented, and has no resale or landed-cost model [VERIFIED].

**Structural changes in 2025–2026 that shape the build** [VERIFIED unless noted]:
- **Allegro removed buyer-side search from its public API.** Staff said on 9, 12 and 29 Oct 2026 that `GET /offers/listing` "is no longer part of the public API, and access is no longer granted". There is also no market-data programme (lowest price or offer count by EAN), and no partners are recommended for it. **Archiwum Allegro** stopped archiving in Aug 2025, with full shutdown planned for March 2026. ⇒ Allegro can only be monitored by browser scraping against **DataDome**, which our probe confirmed.
- **eBay sold data is gated.**
  - The Finding API, including `findCompletedItems`, was decommissioned on 5 Feb 2025.
  - The Marketplace Insights API (sold, 90 days) is a limited release.
  - Sold-search pages reportedly require login since July/Aug 2026 [SECONDARY].
  - **Product Research (ex-Terapeak)** still gives **3 years of sold data across all eBay sites, free in Seller Hub** [VERIFIED: eBay help]. It is the best global "exit price" source.
- **Vinted** forbids bots and scrapers. From Sept–Oct 2026 it enforces "no reselling for profit on standard accounts", and Vinted Pro was not available in PL as of May 2026 [VERIFIED T&Cs; SECONDARY enforcement]. ⇒ Treat Vinted as a sourcing channel only, never a sell channel, for this user.
- **Customs.** The EU is abolishing the €150 customs-duty exemption for low-value parcels, with an interim flat duty per item reported from 1 Jul 2026. The US ended de-minimis for all countries on 29 Aug 2025 [UNVERIFIED-KNOWLEDGE: confirm before modelling].
- **Polish tax.** The "działalność nierejestrowana" limit is quarterly *revenue* of 225% of the minimum wage. With the 2026 minimum wage of 4,806 PLN that is ≈ **10,813.50 PLN per quarter** [UNVERIFIED-KNOWLEDGE]. A $30k-per-deal operation **cannot** run as unregistered activity. It needs a JDG (sole proprietorship), ideally with the **VAT margin scheme for used goods (art. 120 VAT Act)** [INFERENCE].

**The gap we can beat.** Nobody publicly combines all six of these for EU/PL electronics:
1. **Sold-price-based valuation for PL/CZ/HU/DE classifieds.** No public source exists. We build it from a "disappearance ledger": first-seen price, price cuts and time-to-vanish per listing.
2. **Cross-platform same-SKU matching across 10–20 countries and languages.** Use a chip/VRAM/AIB canonical key, with a local LLM only for ambiguous titles.
3. **A full landed-cost net-profit model.** It covers both shipping legs, platform fees, payment fees, customs and VAT, the margin-scheme VAT, PIT/ryczałt, FX, and a risk haircut, evaluated against **the best exit channel per item**.
4. **Condition, defect and scam scoring** before alerting: box-only, for-parts, die-less cards, phishing patterns.
5. **Long-tail coverage.** hardverapro.hu, Bazoš CZ/SK/AT, sbazar, willhaben, Allegro Lokalnie, sprzedajemy, olx.ro/.pt/.bg, tori, finn and others are covered by **no commercial tool**. Most answer plain HTTP (§4c).
6. **Minutes-level latency with confidence-tiered pushes**, where tier 1 is ≥ $300 net at high confidence.

**Top reusable components.** These are the shortlist; §3 has the details.
- Fetching:
  - **Patchright** (Apache-2.0) or **Camoufox** (MPL-2.0) on the existing Playwright agent, for DataDome or Cloudflare sites.
  - **curl_cffi** (MIT) for JSON endpoints once a session is warm.
  - **crawl4ai** (Apache-2.0) for long-tail HTML extracted with the local LLM.
- Platform clients:
  - **marktplaats-py** (MIT)
  - **lbc** for Leboncoin (MIT)
  - **DanielWTE/ebay-kleinanzeigen-api** (MIT)
  - **Giglium/vinted_scraper** (MIT)
  - **mercapi** for Mercari JP (MIT)
  - **wallapop-cli** (MIT)
  - **openclaw-ebay-research**, an OpenClaw-native eBay Browse API plugin (MIT)
- Workflow and data:
  - **ai-marketplace-monitor** (AGPL) as the design reference for LLM listing triage and seller reputation.
  - **changedetection.io** (Apache-2.0) for retail-shop price and restock watches.
  - **RapidFuzz**, **sentence-transformers** and **splink** for matching.
  - **apprise** and **ntfy** for tiered push.
- Data sources:
  - **eBay Browse API**: active listings, 5,000 calls/day default.
  - **eBay Product Research**: sold prices.
  - **AmCompute GPU Resale Index** for data-centre GPU timing. Aug 2026 = 75, down 12% m/m, the lowest of 13 months.

**Bottom line.** The commodity layer (fetching, alerting, single-platform monitors) is free and mature. The defensible value is the **valuation and economics layer**: the PL/CEE sold-price ledger, multi-country SKU matching, and the landed-cost and tax engine. The **long-tail CEE coverage** is the other part. Build those; reuse everything else.

---

## 2. Product comparison

Pricing and coverage in this table are **[UNVERIFIED-KNOWLEDGE]**. The products research thread did not return in time, so re-check each vendor's pricing page before quoting. Items marked [VERIFIED] were checked in this pass.

| Tool | Category | Coverage | Does well | Misses for a PL/EU GPU flipper | Pricing (indicative) |
|---|---|---|---|---|---|
| **Swoopa** | Flip-alert SaaS | FB Marketplace, Craigslist, OfferUp, eBay, Mercari US and similar; US/CA (some UK) | Fast new-listing alerts, price filters, some comps | No Allegro/OLX/Kleinanzeigen/Bazoš; no EU landed cost; no sold-price model for EU | ≈ $30–100/mo by tier [UNVERIFIED] |
| **Flipify** | Flip-alert SaaS/app | North-American classifieds | Simple saved-search pushes | Same gaps; consumer-grade | Low monthly subscription [UNVERIFIED] |
| **kleinanzeigen-agent.de** | Hosted Kleinanzeigen agents | Kleinanzeigen.de only | Email/Telegram/Discord alerts **every 10 min**; **MCP server, A2A endpoint, REST API, webhooks** [VERIFIED] | One platform; no valuation; credits-based pricing not published [VERIFIED] | Credits (not listed) |
| **Tactical Arbitrage** | Online arbitrage (retail → Amazon) | Scans hundreds of retailer sites against Amazon US/UK/CA/DE and others | Bulk retail-vs-Amazon matching, ROI calculation | Amazon-only exit; not used goods; Amazon.pl weak or absent | ≈ $59–129/mo [UNVERIFIED] |
| **SourceMogul** | Online arbitrage | US retailers → Amazon US | Easy UI | US-only; Amazon exit only | ≈ $50–100/mo [UNVERIFIED] |
| **Keepa** | Amazon price-history and data API | Amazon US, UK, DE, FR, IT, ES, JP, CA and others | Deep history, BSR (sales-rank proxy), drop alerts, API | Amazon only; Amazon.pl coverage questionable | Subscription ≈ €19/mo; API from ≈ €49/mo [UNVERIFIED] |
| **camelcamelcamel** | Amazon price history | Amazon US/UK/DE/FR/IT/ES/CA/JP/AU | Free history and alerts | Amazon only | Free |
| **PriceSpy / Prisjakt / PriceRunner** | Price comparison | Nordics, UK, NZ; not PL | New-retail history and alerts | No PL/CEE; asking prices only | Free |
| **idealo** | Price comparison | DE, AT, FR, IT, ES, UK | Large DE retail catalogue, price history, alerts | New retail only; no used | Free |
| **Geizhals / skinflint** | Price comparison | AT, DE, UK, PL, EU locales [SECONDARY: geizhals Python lib] | Best EU hardware catalogue (every AIB SKU per GPU chip), price history, wishlist alerts | Asking prices only; Cloudflare 403 to plain HTTP [VERIFIED probe] | Free; PriceAPI resells AT/DE data [SECONDARY] |
| **Ceneo.pl** | Price comparison | PL | Main PL new-retail benchmark; price-history chart; drop alerts | Asking prices only; no used market | Free |
| **Pepper.pl / mydealz / hotukdeals** | Community deal aggregators | PL / DE / UK | Price errors and coupons surface within minutes; keyword alerts; RSS | Everyone sees the same deal; mostly new retail; quantity limits | Free |
| **eBay Product Research (Terapeak)** | Sold-price research | All eBay sites, 3 years [VERIFIED] | The only large sold-price DB; filters by buyer and seller location, condition, EAN | UI only, no API; eBay only | Free with Seller Hub [VERIFIED] |
| **BestValueGPU** | GPU price tracker | US (Newegg new; **eBay sold** for used) [SECONDARY] | Daily used-vs-MSRP trend | US only | Free |
| **videocardprices.com, gpupoet.com** | GPU price indexes | Mostly US | Monthly market reports. gpupoet Jan 2026: "RTX 50 near MSRP while used GPUs hit 70% off" [SECONDARY] | US-centric | Free |
| **Tom's Hardware GPU price index / eBay series** | Editorial | US | Monthly eBay sold averages per GPU (last confirmed 2023 edition) [SECONDARY] | Monthly; US | Free |
| **3DCenter / PCGH** | DE editorial | DE retail | Monthly DE street-price summaries | New retail only | Free |
| **AmCompute GPU Resale Index** | Data-centre GPU index | Used DC GPUs, T4 to H100 | Monthly index plus CSV and residual-value bands [VERIFIED] | DC only | Free |
| **NowInStock / HotStock / Distill.io** | Restock and change alerts | US/UK/DE retailers; Distill is generic | Drop alerts at launch | Commodity; bot competition | Free / freemium |
| **changedetection.io** (self-host) | Change and price detection | Any web page | Restock and price processor, thresholds, Playwright steps, Apprise, API [VERIFIED] | Watches known pages; does not discover new classifieds | Free OSS; hosted $8.99/mo [VERIFIED] |
| **ai-marketplace-monitor** (OSS) | LLM deal monitor | Facebook Marketplace | LLM verdict per listing, seller-reputation analysis, web UI, Docker/NAS [VERIFIED] | FB only; no resale model; AGPL | Free |
| **Apify actors** (OLX, Allegro, Vinted, Wallapop, eBay sold and others) | Scraping marketplace | Most platforms | Ready scrapers | Per-result cost; latency of minutes; ToS risk transfers to you | ≈ $1.5–3 per 1k results [SECONDARY] |

**Takeaway** [INFERENCE]. Commercial tools are either **US classifieds alerts** or **new-retail price comparison** and **Amazon arbitrage**. None values **used** electronics in **CEE classifieds**, matches SKUs **across countries**, or computes **PL-tax-aware landed net profit**. That is the gap.

---
## 3. Building blocks (open source)

GitHub stats were pulled with `gh api` on **2026-10-29** [VERIFIED]. "Last push" is the date of the most recent push to any branch.

### 3a. Crawling, anti-bot and browser-agent stack

| Repo | Stars | Last push | License | Role in our stack | Notes |
|---|---|---|---|---|---|
| [microsoft/playwright](https://github.com/microsoft/playwright) (+ [playwright-python](https://github.com/microsoft/playwright-python), 15.1k) | 98.8k | 2026-10-29 | Apache-2.0 | Already in the rig (local browser agent) | Vanilla Playwright is fingerprinted by DataDome and Cloudflare. Use it with a persistent real profile, or swap in Patchright/Camoufox. |
| [Kaliiiiiiiiii-Vinyzu/patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) | 5.0k | 2026-10-25 | Apache-2.0 | Drop-in "undetected" Playwright (Chromium) | Lowest-friction upgrade for the existing Playwright agent. |
| [daijro/camoufox](https://github.com/daijro/camoufox) | 12.9k | 2026-10-28 | MPL-2.0 | Anti-detect Firefox with a Playwright API | Best open option for DataDome sites: Allegro, Leboncoin, Vinted. |
| [ultrafunkamsterdam/nodriver](https://github.com/ultrafunkamsterdam/nodriver) | 4.9k | 2026-10-26 | **AGPL-3.0** | CDP-only Chrome automation | AGPL is fine for private use, but keep it out of anything you distribute. |
| [seleniumbase/SeleniumBase](https://github.com/seleniumbase/SeleniumBase) | 13.2k | 2026-10-29 | MIT | "CDP Mode" stealth browser | Alternative to nodriver under a permissive license. |
| [lexiforest/curl_cffi](https://github.com/lexiforest/curl_cffi) | 6.8k | 2026-10-27 | MIT | HTTP client that impersonates browser TLS/JA3 | Use for JSON endpoints (OLX, Marktplaats, Wallapop, Vinted catalog) once a browser has warmed the session cookies. About 10–50× cheaper than running a browser. |
| [D4Vinci/Scrapling](https://github.com/D4Vinci/Scrapling) | 90.3k | 2026-10-29 | BSD-3 | Adaptive parsing plus stealth fetchers | Its "adaptive selectors" survive DOM changes, which helps with the HTML of Allegro, OLX and Bazoš. |
| [omkarcloud/botasaurus](https://github.com/omkarcloud/botasaurus) | 5.8k | 2026-10-29 | MIT | Scraper framework with anti-detect defaults | The same author ships "free" Allegro, Vinted and Kleinanzeigen scrapers (1–6★, Oct 2026). Treat these as templates, not dependencies. |
| [scrapy/scrapy](https://github.com/scrapy/scrapy) | 65.1k | 2026-10-28 | BSD-3 | Scheduling, retries and pipelines for HTML/JSON crawls | Overkill for roughly 30 saved searches. Useful if we later crawl whole categories for price history. |
| [apify/crawlee-python](https://github.com/apify/crawlee-python) / [crawlee (JS)](https://github.com/apify/crawlee) | 9.6k / 26.3k | 2026-10-29 | Apache-2.0 | Crawling with a request queue, session pool, proxy rotation and Playwright integration | Its session-pool and fingerprint handling ([apify/fingerprint-suite](https://github.com/apify/fingerprint-suite), 2.6k) fit "many sites, low rate". |
| [unclecode/crawl4ai](https://github.com/unclecode/crawl4ai) | 86.5k | 2026-10-29 | Apache-2.0 | Page → LLM-ready markdown, with local-LLM extraction | Good for long-tail shops and pallet or auction sites that have no JSON. Can run extraction with llama.cpp through LiteLLM. |
| [firecrawl/firecrawl](https://github.com/firecrawl/firecrawl) | 196.7k | 2026-10-29 | AGPL-3.0 | Self-hostable crawl/extract API | Heavier than crawl4ai. Only worth it if you want the API surface. |
| [ScrapeGraphAI/Scrapegraph-ai](https://github.com/ScrapeGraphAI/Scrapegraph-ai) | 32.3k | 2026-10-06 | MIT | LLM-driven scraping graphs | Same niche as crawl4ai. |
| [getmaxun/maxun](https://github.com/getmaxun/maxun) | 18.1k | 2026-10-28 | AGPL-3.0 | No-code "website → API" robots | Optional. |
| [browser-use/browser-use](https://github.com/browser-use/browser-use) | 119.5k | 2026-10-27 | MIT | LLM-driven browser agent | Keep it for **actions**: messaging sellers, checking a listing's details, checkout dry-runs. Too slow and costly for polling. |
| [browserbase/stagehand](https://github.com/browserbase/stagehand) | 25.9k | 2026-10-29 | MIT | `act/extract/observe` primitives over Playwright | Pairs well with an existing Playwright agent. |
| [Skyvern-AI/skyvern](https://github.com/Skyvern-AI/skyvern) | 23.3k | 2026-10-29 | AGPL-3.0 | Vision-LLM workflows | Optional, for form-heavy auction or bailiff sites. |
| [FlareSolverr/FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) | 16.1k | 2026-10-24 | MIT | Cloudflare challenge solver proxy | Use only for Cloudflare-fronted long-tail shops. Ineffective against DataDome. |
| [mitmproxy/mitmproxy](https://github.com/mitmproxy/mitmproxy) | 46.5k | 2026-10-27 | MIT | Reverse-engineer mobile-app JSON APIs (Wallapop, Kleinanzeigen, Mercari) | Development-time tool. |

### 3b. Change detection, orchestration, notification

| Repo | Stars | Last push | License | Role |
|---|---|---|---|---|
| [dgtlmoon/changedetection.io](https://github.com/dgtlmoon/changedetection.io) | 35.4k | 2026-10-29 | Apache-2.0 | **Reuse for retail-shop watches.** It has a "Re-stock & Price detection" processor that reads price from page metadata or JSON-LD, upper/lower price limits and % change alerts, a Playwright fetcher with "browser steps", per-watch proxies, Apprise notifications, a REST API and price-history graphs. Hosted plan is $8.99/mo [VERIFIED README]. It does **not** cover classifieds discovery, where new listings appear rather than existing pages changing. |
| [huginn/huginn](https://github.com/huginn/huginn) | 50.2k | 2026-10-26 | MIT | Event agents (RSS → filter → notify). Useful for Pepper/mydealz RSS feeds. OpenClaw cron may already cover this. |
| [n8n-io/n8n](https://github.com/n8n-io/n8n) | 209k | 2026-10-29 | Sustainable Use (fair-code) | Visual workflows. Optional glue. |
| [caronc/apprise](https://github.com/caronc/apprise) | 17.7k | 2026-10-29 | BSD-2 | One library for Telegram, Discord, ntfy and email. Use it for tier-1 pushes. |
| [binwiederhier/ntfy](https://github.com/binwiederhier/ntfy) | 35.2k | 2026-10-29 | Apache-2.0 | Self-hosted push to phone, with priorities. Map tier 1 to `urgent`. |

### 3c. Marketplace clients and scrapers, by platform

| Platform | Best OSS starting point | Stars / last push / license | How it works | Reuse value |
|---|---|---|---|---|
| **Facebook Marketplace** | [BoPeng/ai-marketplace-monitor](https://github.com/BoPeng/ai-marketplace-monitor) | 397 / 2026-10-26 / AGPL-3.0 | Logged-in Playwright. Includes LLM rating of each listing (OpenAI, DeepSeek or Ollama), seller-reputation scoring, a web UI, a NAS/Docker template, and PushBullet/Telegram/email alerts [VERIFIED README]. | **High.** The closest existing thing to the user's design. It is US-centric and has no landed-cost or resale model. AGPL. |
| Facebook Marketplace (alt) | [passivebot/facebook-marketplace-scraper](https://github.com/passivebot/facebook-marketplace-scraper) | 404 / 2024-04 / none / **archived** | Playwright + BeautifulSoup | Reference only. |
| **Vinted** | [Giglium/vinted_scraper](https://github.com/Giglium/vinted_scraper) | 103 / 2026-10-26 / MIT | Session-managed catalog search. The README says the item JSON endpoint now returns **403 from anti-bot**, so item data is read from the public page instead [VERIFIED README]. | Medium-high. Actively maintained against Vinted's defences. |
| Vinted (alt) | [Pawikoski/vinted-api-wrapper](https://github.com/Pawikoski/vinted-api-wrapper), [Androz2091/vinted-api](https://github.com/Androz2091/vinted-api), [jasp-nerd/vinted-sniper](https://github.com/jasp-nerd/vinted-sniper) | 82 / 2025-07 / MIT · 111 / 2025-04 / none · 8 / 2026-10 / MIT | Wrappers and a self-hosted saved-search monitor | Low-medium. Wrappers go stale. |
| **OLX (PL/UA/KZ)** | [lerdem/olx-parser](https://github.com/lerdem/olx-parser) | 50 / 2026-08-06 / GPL-3.0 | Parses OLX into RSS or Telegram | Medium. Easy to reimplement against OLX's internal JSON. |
| OLX | [Pawikoski/olx-api-wrapper](https://github.com/Pawikoski/olx-api-wrapper) | 17 / 2025-06 / MIT | Wraps the **official Partner API**, which is seller-side | Only useful for *listing* your own items on OLX. |
| **Allegro** | none worth reusing; [omkarcloud/allegro-scraper](https://github.com/omkarcloud/allegro-scraper) | 1 / 2026-10-26 / MIT | Template | Low. Build it ourselves with Camoufox. |
| Allegro (official) | [allegro/allegro-api](https://github.com/allegro/allegro-api) issue tracker | 248 | Official forum | Use the REST API only for **selling**: offers, orders, `/sale/products` GTIN lookup. |
| **Kleinanzeigen.de** | [DanielWTE/ebay-kleinanzeigen-api](https://github.com/DanielWTE/ebay-kleinanzeigen-api) | 251 / 2026-10-08 / MIT | FastAPI + Playwright search/detail API. The author also sells the hosted [kleinanzeigen-agent.de](https://kleinanzeigen-agent.de/), which offers MCP/A2A/REST and 10-minute alerts [VERIFIED]. | High. Self-host it as a microservice. |
| Kleinanzeigen (alt) | [monkrel/kleinanzeigen-api](https://github.com/monkrel/kleinanzeigen-api); [Second-Hand-Friends/kleinanzeigen-bot](https://github.com/Second-Hand-Friends/kleinanzeigen-bot) | 14 / 2026-09 / MIT · 438 / 2026-10-28 / AGPL-3.0 | Python client. The bot **publishes and republishes** listings (sell side). | The bot is useful if you sell on Kleinanzeigen. |
| **Marktplaats** | [jensjeflensje/marktplaats-py](https://github.com/jensjeflensje/marktplaats-py) | 74 / 2026-10-28 / MIT | Plain HTTP JSON search. Filters for price, condition, category, distance and `offered_since` [VERIFIED README]. | High. Also see [PonClick/marktplaats-mcp](https://github.com/PonClick/marktplaats-mcp) (20, MIT) and [eigenein/mrktpltsbot](https://github.com/eigenein/mrktpltsbot) (7, MIT, Telegram). |
| **Leboncoin** | [etienne-hd/lbc](https://github.com/etienne-hd/lbc) | 222 / 2026-09-26 / MIT | Unofficial API client with proxy support | Medium. DataDome is the limiting factor (see §4). |
| **Wallapop** (ES/IT/PT) | [Microck/wallapop-cli](https://github.com/Microck/wallapop-cli) | 29 / 2026-10-21 / MIT | Go CLI with search, watch-for-changes and seller chat | Medium-high. Alternatives: [z0r3f/wallbot](https://github.com/z0r3f/wallbot) (46, GPL-3.0) and [danielhuici/Wallamonitor](https://github.com/danielhuici/Wallamonitor) (34). |
| **Subito.it** | [mxyp/subot](https://github.com/mxyp/subot) | 4 / 2026-08-24 / MIT | Subito + Vinted.it watcher that pushes to ntfy | Low-medium. |
| **Bazoš** (CZ/SK/AT) | [Mahrkeenerh/apbi](https://github.com/Mahrkeenerh/apbi); [peter115342/bazos_alert_bot](https://github.com/peter115342/bazos_alert_bot) | 7 / 2026-03 / MIT · 6 / 2026-07 / MIT | APBI: "there is no API to wrap", so it is an async HTML interface. The other is a Discord alert bot. | Medium. Low anti-bot, and Czech prices are often below PL. [kukas/bazos_dataset](https://github.com/kukas/bazos_dataset) holds two years of Bazoš ads (GPL-3.0), usable for training a price model. |
| **Mercari JP** | [take-kun/mercapi](https://github.com/take-kun/mercapi) | 82 / 2026-09-12 / MIT | Implements the web app's request-signing (DPoP) [VERIFIED README] | High for the JP sourcing leg. Also [moikdev/mercari-jp-mcp](https://github.com/moikdev/mercari-jp-mcp) (15, MIT). |
| **Yahoo! Auctions JP** | [vlourme/yahoo-auction-alert-discord-bot](https://github.com/vlourme/yahoo-auction-alert-discord-bot) | 26 / 2024-08 / GPL-3.0 | Yahoo Auctions + Mercari → Discord | Low-medium. Stale. |
| **eBay** | [Jason-Vaughan/openclaw-ebay-research](https://github.com/Jason-Vaughan/openclaw-ebay-research) | 0 / 2026-06-13 / MIT | **OpenClaw plugin.** Browse API search, item details and categories. Sold history needs Marketplace Insights approval (`enableInsights`) [VERIFIED hub.openclaw.ai] | **High for this rig, since it is already OpenClaw-native.** Also [eBay/ebay-oauth-python-client](https://github.com/eBay/ebay-oauth-python-client) (103). Avoid [timotheus/ebaysdk-python](https://github.com/timotheus/ebaysdk-python): it wraps the dead Finding API. |
| **Geizhals** | [Samxel/geizhals-mcp](https://github.com/Samxel/geizhals-mcp); [d-Rickyy-b/GoGeizhalsBot](https://github.com/d-Rickyy-b/GoGeizhalsBot) | 6 / 2026-10-29 / MIT · 7 / 2026-07 / GPL-3.0 | MCP: search, prices, price history. The bot sends wishlist price alerts. | Medium. Geizhals' new-retail price is a good EU benchmark. |
| Facebook Marketplace + OpenClaw | [Scratchycarl/OpenClaw_Facebook_Marketplace_Scraper](https://github.com/Scratchycarl/OpenClaw_Facebook_Marketplace_Scraper) | 7 / 2026-03 | OpenClaw skill | Reference for skill structure. |
| Photo matching | [mintcloud/stolen-bike-watch](https://github.com/mintcloud/stolen-bike-watch) | 17 / 2026-10-16 / MIT | Ranks marketplace photos with a CLIP pre-gate | Shows how to do image dedup and stolen-photo detection (see §7). |

Survey context [VERIFIED via `gh search`, 2026-10-29]: every platform-specific repo except Facebook Marketplace has **under 260 stars**. Most are single-platform, single-country alert bots with **no valuation, no cross-platform matching and no landed-cost logic**. The open-source ecosystem therefore supplies plumbing but no intelligence.

### 3d. Product matching and entity resolution

| Component | Stars / license | Use |
|---|---|---|
| [rapidfuzz/RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) | 4.2k / MIT | Fast fuzzy title matching. This is the first-pass blocker. |
| [huggingface/sentence-transformers](https://github.com/huggingface/sentence-transformers) | 19.2k / Apache-2.0 | Multilingual embeddings (PL/DE/FR/JP) for nearest-SKU retrieval, e.g. `multilingual-e5` / `bge-m3`. |
| [moj-analytical-services/splink](https://github.com/moj-analytical-services/splink) | 2.5k / MIT | Probabilistic record linkage on DuckDB. Good for building a canonical SKU table from many shop feeds. |
| [dedupeio/dedupe](https://github.com/dedupeio/dedupe) | 4.5k / MIT (last push 2025-07) | Active-learning dedup. Fine, but slowing down. |
| [zinggAI/zingg](https://github.com/zinggAI/zingg) | 1.3k / AGPL-3.0 | Spark-scale ER. Overkill here. |
| [megagonlabs/ditto](https://github.com/megagonlabs/ditto) | 321 / Apache-2.0 (2024) | Research baseline for transformer-based entity matching. A local LLM used as a judge replaces it. |
| GTIN/EAN lookups | UPCitemdb (free 100 req/day; DEV $99/mo for 20k/day) [SECONDARY: shopsavvy comparison, 2026-10]; Go-UPC ($74.95/mo for 5k) [SECONDARY]; EAN-Search.org (has an official MCP server; pricing not verified); Open Icecat (free spec sheets for sponsoring brands; site is behind Anubis proof-of-work, so not verified) | Used listings rarely carry an EAN. These lookups matter for **retail and pallet manifests** (Amazon return pallets list ASIN/EAN) and for listing on Allegro, whose product catalogue requires a GTIN. |

**Recommended matching design** [INFERENCE: design proposal]

A GPU's resale value is determined by **chip + VRAM + generation**. The board partner and model line (ASUS TUF vs Strix, MSI Ventus vs Suprim) adds roughly ±5–15%.

1. Normalise with deterministic regex and taxonomy rules first: `RTX 4070 Ti Super 16GB`, `RX 7900 XTX`, `4090D`, `5070 Ti`, plus the AIB line. Seed the taxonomy from the TechPowerUp GPU DB chip list and Geizhals' graphics-card catalogue, which lists every AIB SKU per chip.
2. Send only unresolved or ambiguous titles to the local LLM. Use JSON-schema-constrained output (llama.cpp grammar) with fields `{chip, vram_gb, variant, aib, model_line, condition, defects[], bundle_items[], is_box_only, is_for_parts}`.
3. Run a cheap text classifier for "for parts / uszkodzona / defekt / bez obrazu / artefakty / box only / pudełko / zamiennik", because a missed "pudełko" title creates a false tier-1 alert.
4. Price per canonical key = (chip, VRAM, condition bucket). Keep an AIB premium table on top.

## 4. Per-platform access sheet

Legend. Anti-bot: L = low, M = medium, H = high, VH = very high. ToS risk applies to *read-only monitoring at human-like rates*; buying and selling on the platform is a separate issue.

| Platform | Official buyer-side search API? | Practical access for a 24/7 monitor | Sold-price data | Anti-bot | ToS / ban risk |
|---|---|---|---|---|---|
| **Allegro.pl** | **No.** `GET /offers/listing` "is no longer part of the public API, and access is no longer granted" (Allegro staff, 9 and 12 Oct 2026). On 29 Oct 2026 Allegro confirmed there is no market-data programme (lowest price or offer count by EAN) in the public API, and that it recommends no partners for it [VERIFIED: GitHub issues #14138, #14140, #14240]. Still available: `GET /sale/products?phrase={EAN}&mode=GTIN` for catalogue product lookup, with **no other sellers' prices** [VERIFIED #11455]. | Browser (Camoufox/Patchright), logged out, from the home PL residential IP, low rate, about 1 request per 10–20 s per query. DataDome confirmed by our probe (403 + `datadome` cookie) [VERIFIED §4b]. | Offer-level counter "X osób kupiło" = purchases in the **last 30 days** [SECONDARY: Allegro community]. **Archiwum Allegro was closed**: no new archived offers from Aug 2025, redirects from Nov 2025, full shutdown planned for Mar 2026 [VERIFIED: tabletowo.pl, 6 Aug 2025; that the shutdown actually happened is INFERENCE]. Allegro Analytics (business accounts with a seller subscription) covers your own sales over 6/18/36 months, plus market and competitor trends on higher tiers. It does not expose other sellers' individual transactions [SECONDARY: shoper.pl, Aug 2026]. Tier prices last seen as 39 / 199 / 3000 PLN per 30 days [SECONDARY, undated forum]. | H | Medium. REST API terms art. 3.11 (from 23 Sep 2025) forbid sharing keys and presenting or importing Allegro offers on competing sites [SECONDARY: moderator quote, spolecznosc.allegro.pl]. Scraping logged-out is a contract/ToS issue, not criminal. **Never scrape with the account you sell from.** |
| **Allegro Lokalnie** | No API, and no plans for one [SECONDARY: moderator post]. | HTML scraping. It is lighter than allegro.pl and is where private sellers list with "Allegro Lokalnie" delivery. | None | M | Low-medium. |
| **OLX.pl** | **No.** The Partner API is seller-side only: adverts, orders, delivery, and a status feed of *your* adverts [VERIFIED: developer.olx.pl]. | The internal JSON endpoint `/api/v1/offers?query=…` is what most scrapers and Apify actors call. Category filtering via `category_id` reportedly returns 400 [SECONDARY]. Our plain-curl test from a PL (WAW) non-residential egress got a **CloudFront 403** on both the page and the JSON [VERIFIED §4b]. Browser or TLS impersonation from a residential IP is expected to work, as dozens of maintained Apify actors do [INFERENCE]. | **None.** Ads vanish when sold or deleted. Build your own proxy (see §4b). | M | Medium. Risk to the account used for messaging. Keep scraping logged-out. |
| **eBay** (all sites) | **Partly.** Browse API (`item_summary/search`) covers **active** listings, 5,000 calls/day by default, raisable via "Application Growth Check" [SECONDARY: eBay call-limits page and dev-support posts]. Finding API, including `findCompletedItems`, was **decommissioned 5 Feb 2025** [SECONDARY: eBay dev forum and deprecation table]. Marketplace Insights (`item_sales/search`, sold items, last 90 days) is a **limited release** gated by eBay business units and effectively closed to new applicants [SECONDARY: eBay docs via search; vendor blogs]. The Buy API page says production use is "intended for eBay partners only" [VERIFIED: developer.ebay.com buy-requirements]. | Browse API for active comparables and cross-border sourcing (filters: price, condition, `itemLocationCountry`, `deliveryCountry`, buying options). Web scraping is hard: our plain HTTP requests to ebay.com and ebay.de **got 403** [VERIFIED: own test]. | **Product Research (ex-Terapeak)** in Seller Hub: **3 years** of sold data across **all eBay marketplaces**, filterable by condition, buyer and seller location, and EAN/MPN/UPC. Free with Seller Hub access [VERIFIED: ebay.com/help id=4853]. It is UI-only, so automate it in a logged-in browser at very low rate or use it manually. Sold/completed searches (`LH_Sold=1`/`LH_Complete=1`) **redirect logged-out visitors to sign-in since about 22 Jul 2026**. Ordinary searches are unaffected, and logged-in users keep about 90 days of sold results [SECONDARY: at least 4 independent vendor blogs agree (openwebninja, scavio, quicksync, flowlister), dating it July or late Aug 2026; no eBay announcement found]. Third-party sold-data APIs exist (SoldComps, Apify actors) [SECONDARY]. | H (web) / L (API) | API is low risk. Scraping is high risk because the User Agreement bans robots. Seller-account suspension is very costly. |
| **Vinted** | No. | Internal `/api/v2/catalog/items` needs the anonymous `access_token_web` cookie from a page load. It sits behind Cloudflare + DataDome, returns 96 items per page and caps at 10 pages [SECONDARY: dev.to / Apify]. The item JSON endpoint returns **403** to bots [VERIFIED: vinted_scraper README]. | No sold search. Sold items show "Sold" on the item page and seller profile. | H | **High.** The T&Cs forbid "bots, scraping programs, crawling programs, spiders" and screen-scraping [VERIFIED: vinted.com T&Cs, version from 18-12-2025; new T&Cs effective 8 Oct 2026]. **Reselling for profit is not allowed on standard accounts.** Enforcement tightened in Sept 2026 with 24 h to 30-day bans [SECONDARY: crosslist.com]. Vinted Pro was **not launched in PL** as of May 2026 [SECONDARY]. ⇒ For a PL flipper, Vinted is a *sourcing* channel (electronics category since 2024, with an electronics verification service [SECONDARY]), **not a sell channel**. |
| **Kleinanzeigen.de** | No. | Playwright-based OSS API (DanielWTE) or the hosted kleinanzeigen-agent.de. PL residential IP works (inference). | None | M | Medium. German BGH I ZR 224/12 (2014): screen-scraping public data despite ToS is not *per se* unfair competition. A login wall or technical block changes that analysis [SECONDARY: ra-plutte.de]. |
| **Marktplaats.nl / 2dehands.be** | No. | Public JSON search endpoint (marktplaats-py), no browser needed [VERIFIED: README]. | None | L–M | Low-medium. |
| **Leboncoin.fr** | No. | `lbc` client. **DataDome** protects about 30 sensitive endpoints and blocks about 9.5 M malicious requests per day, 90% of them scraping [VERIFIED: DataDome case study]. Expect to need Camoufox plus FR residential IPs. Most private sellers only do hand delivery or domestic shipping. | None | **VH** | Medium. Low return for a PL buyer. |
| **Wallapop** (ES/IT/PT) | No. | App JSON `api.wallapop.com/api/v3/...search` with an app User-Agent. Non-EU IPs reportedly get 0 results [SECONDARY: Apify actor]. wallapop-cli is available. | Items flagged reserved/sold | M | Medium. Wallapop Envíos supports cross-border shipping only within ES/IT/PT [INFERENCE]. |
| **Subito.it** | No. | HTML/JSON scraping (subot). | None | M | Medium. |
| **Bazoš** (cz/sk/at) | No ("there is no API to wrap"). | Plain HTML, light anti-bot. | None, but kukas/bazos_dataset has 2 years of historical asks | **L** | Low. CZ/SK is drivable from Kraków (Ostrava 2 h). |
| **Mercari JP** | No (no public API). Mercari Global App opened to TW/HK (Sep 2025) and the **US (Jun 2026)**, **not the EU** [SECONDARY: valueaddedresource, digitalcommerce360]. | mercapi (implements request signing). Buy through Buyee/ZenMarket: ZenMarket ¥800 per Mercari or auction item [SECONDARY, Oct 2026]; Buyee about ¥300 per item plus extras [SECONDARY, unverified]. | **Yes.** Search results include sold-out (売り切れ) items with final prices. The app also has a "相場検索" market-price tool, launched 14 Jul 2025 [SECONDARY: allabout / commercepick]. | M | Medium. |
| **Yahoo! Auctions JP** | Legacy Auctions Web API status unclear. The Yahoo Shopping search API was retired in 2020 [SECONDARY]. | Scrape directly or through Buyee/ZenMarket mirrors. | Closed-auction prices: Yahoo's own "落札相場" and **Aucfan** (about 10 years of closing prices) [SECONDARY: aucfan.co.jp] | M | Medium. |
| **Facebook Marketplace** | No. | Logged-in Playwright (ai-marketplace-monitor). Logged-out access is very limited. | Briefly shows "Sold" | H | **High for the account.** Meta's terms prohibit automated collection. *Meta v. Bright Data* (N.D. Cal., Jan 2024): logged-out scraping of public data did not breach Meta's terms [SECONDARY: TechCrunch]. Use a separate account and expect checkpoints. |
| **Ceneo.pl / Skąpiec** | Ceneo's API is for shops, not price data [INFERENCE] | Light HTML. Useful as a **new-retail PL ceiling** and price-history reference. | No (asking prices only) | L–M | Low. |
| **Geizhals / idealo** | No public API (PriceAPI resells Geizhals AT/DE data) [SECONDARY] | geizhals-mcp, or HTML | No (asking prices plus price history) | L–M | Low. |

### 4a. Price truth: where *sold* prices exist

| Source | What you get | Access | Verdict |
|---|---|---|---|
| eBay Product Research (Terapeak) | 3 years of sold items, all marketplaces, filters by buyer/seller country, condition, EAN [VERIFIED] | Seller Hub UI, free | **Best global sold-price source.** Pull weekly per canonical GPU key. Use eBay.de and eBay.com sold medians as the "global exit price". |
| eBay Marketplace Insights API | 90-day sold items | Restricted; new applicants are generally not approved [SECONDARY] | Apply anyway, since it costs nothing. Don't plan on it. |
| eBay sold-search scraping / third-party sold APIs | Sold listings | Login-walled since Jul/Aug 2026 [SECONDARY]: a logged-in browser session is needed. Vendors (SoldComps, Apify) resell it. | Fallback. |
| Allegro "X osób kupiło" | 30-day purchase count per offer [SECONDARY] | Visible on the page | A **velocity** signal, not a price, though for fixed-price "Kup teraz" offers count × price ≈ realised sales. |
| Allegro Analytics | Your own sales; market and competitor trends on higher tiers | Business account plus subscription | Worth it only once registered as a business (see §5). |
| Mercari JP sold-out search; Aucfan for Yahoo Auctions | Realised JP prices | Web | Good for the JP sourcing side. |
| BestValueGPU | Daily US prices; used prices **from eBay sold listings**, new from Newegg. Sept 2026 median used price was 116% of MSRP [SECONDARY: search snippet; page rate-limited us] | Web | US-only reference. |
| Tom's Hardware eBay GPU pricing series | Monthly eBay sold averages and volumes per GPU | Web | Last confirmed edition was 2023 [SECONDARY]. Its current "GPU price index" tracks new retail. |
| AmCompute GPU Resale Index | Monthly index for used **data-centre** GPUs (T4…H100, H1-2025 = 100). Aug 2026 = **75** (−12% m/m, 14% below its depreciation baseline). Hopper HGX servers held value. Residual-value bands, e.g. H100 at 11–19% of new in 2027 [VERIFIED: amcompute.com] | Web plus CSV | Use it to time data-centre GPU purchases. The trend says *don't hold inventory*. |
| Kaggle "historical GPU prices" (48 GPUs; Amazon retail + eBay used) | Historic | Free | Model priors only. |
| **Self-built "disappearance ledger"** [INFERENCE: proposal] | For OLX, Allegro Lokalnie, Kleinanzeigen, Bazoš, Marktplaats and Vinted, log every listing's first-seen price, price cuts and **time-to-disappear**. A listing that vanishes within 72 h at ≤ the median ask was most likely sold. | Our own crawler | **The real moat.** Nobody publishes PL/CZ/DE classifieds clearing prices. After 4–8 weeks this gives a per-model "clears-at" price and days-to-sell distribution that no public tool has. |

### 4b. Live anti-bot probe (2026-10-29)
[VERIFIED: own test]

Method: one plain `curl` request per URL with a Chrome User-Agent and no TLS impersonation. The egress exited via Cloudflare's **WAW** (Warsaw) PoP, so it is PL-located but probably not residential. This measures the *floor* of difficulty. A real browser on a home IP does better.

| URL | Result | Interpretation |
|---|---|---|
| allegro.pl/listing | **403 + `datadome` cookie, `x-datadome-cid`** | DataDome confirmed. A real browser and residential IP are required. |
| allegrolokalnie.pl search | 200 | Plain HTTP works. |
| olx.pl search page and `/api/v1/offers` | **403 (CloudFront WAF)** | Fingerprint-based block. Try curl_cffi impersonation or a browser. |
| vinted.pl catalog page | 200 (Cloudflare; DataDome cookie cleared) | The page loads. |
| vinted.pl `/api/v2/catalog/items` | **403** without session cookies | Warm the session in a browser, then call the JSON API. |
| kleinanzeigen.de search | 200 (Akamai edge) | Plain HTTP works at low rate. |
| marktplaats.nl `/lrp/api/search` | 200 | JSON works without a browser. |
| bazos.cz search | 200 (nginx) | Trivial. |
| subito.it search | 200 | Plain HTTP works. |
| jp.mercari.com search `status=sold_out` | 200 (Cloudflare) | The sold-out view is reachable. Item data is loaded by a signed API (mercapi). |
| leboncoin.fr search | **403 + DataDome** | Hardest target. |
| api.wallapop.com v3 search | **403 (CloudFront)** | Needs app headers and signature. |
| geizhals.de search | **403 (Cloudflare)** | Needs a browser or impersonation. |
| ceneo.pl search | 200 | Plain HTTP works. |
| facebook.com/marketplace | 400 | Login and browser required. |
| ebay.com / ebay.de search (incl. sold) | **403** | Use the Browse API instead. |

### 4c. Long-tail platforms (the intended edge)

Plain-HTTP probe on 2026-10-29 [VERIFIED status codes]. The one-line descriptions are [INFERENCE: general knowledge, not individually re-verified]. 3xx means a redirect, normally to a canonical URL, so reachable. 403 means a WAF fingerprint block, so a browser or impersonation is needed.

| Platform | Country | Probe | Why it matters |
|---|---|---|---|
| **hardverapro.hu** | HU | 302 | A *dedicated* hardware classifieds site and Hungary's main used-PC market. A dense GPU supply with low competition from PL buyers. Budapest is about 5 h by car. |
| jofogas.hu | HU | 200 | General HU classifieds. |
| **bazos.cz / .sk / .at** | CZ/SK/AT | 200 / 200 / 200 | Simple HTML, no anti-bot. CZ is within 2–3 h of Kraków. |
| **sbazar.cz** (Seznam), aukro.cz | CZ | 200 / 301 | Second CZ classifieds plus the CZ auction site. |
| **willhaben.at** | AT | 200 | Austria's dominant classifieds site. Prices are often EUR-high, which makes it a **sell-side** reference. |
| Kleinanzeigen.de, quoka.de | DE | 200 / 302 | DE supply. Pickup is in range from Görlitz/Dresden. |
| hardwareluxx.de Marktplatz, ComputerBase Marktplatz | DE | 403 (Luxx) | Enthusiast forums. High-quality listings and honest sellers, but membership and post-count rules apply. |
| tweakers.net Vraag & Aanbod | NL | 403 | NL hardware enthusiast marketplace with Pricewatch history. |
| marktplaats.nl / 2dehands.be | NL/BE | 200 / 200 | Same Adevinta stack, JSON API. |
| tori.fi, finn.no, blocket.se, dba.dk | Nordics | 200 / 200 / 301 / 301 | High-price markets, so mostly **sell-side** or price references. Shipping into the EU is easy, though NO is non-EU. |
| ss.lv, skelbiu.lt, okidoki.ee | Baltics | 200 / 403 / 403 | Small and low-competition. |
| olx.ro / .bg / .pt / .ua | RO/BG/PT/UA | 403 (all) | Same OLX WAF as olx.pl, so one Camoufox/curl_cffi adapter covers five countries. **UA is outside the EU**, so imports pay customs and VAT. |
| njuskalo.hr, bolha.com (SI), kupujemprodajem (RS) | Balkans | 302 / 301 / 302 | Low competition. RS is non-EU. |
| ricardo.ch / tutti.ch | CH | 403 / 403 | Non-EU (customs and VAT). Swiss prices are high, so CH is a sell-side reference, not a source. |
| sprzedajemy.pl, Allegro Lokalnie, elektroda.pl Giełda, PurePC/PCLab-successor forum boards, FB groups ("Karty graficzne – kupię/sprzedam") | PL | 200 (sprzedajemy) | Long-tail PL supply. FB groups need a logged-in browser. |

### 4d. Proxy needs
[VERIFIED for the endpoints we tested; the rest is INFERENCE.]

- PL sites (Allegro, Allegro Lokalnie, OLX, Ceneo): use the rig's **own residential PL IP** at human-like rates. It is the best "proxy" available and avoids a cost.
- DE/NL/CZ classifieds (Kleinanzeigen, Marktplaats, Bazoš) usually tolerate EU residential IPs.
- Leboncoin, Wallapop and Vinted (cross-country) may need country-matched residential IPs. 2026 pay-as-you-go prices vary by source: IPRoyal ≈ $7.35/GB at 1 GB down to about $1.75/GB in bulk; Decodo from about $2/GB; Bright Data $4–10.50/GB PAYG; Oxylabs about $8/GB PAYG [SECONDARY: aimultiple, use-apify, costbench].
- Budget: a JSON-first design that only renders the browser for detail pages uses roughly **1–3 GB/month** for about 50 saved searches every 2–5 min [INFERENCE]. That is about $5–25/month.
- Avoid datacentre IPs entirely. Our non-residential PL egress was blocked by OLX's CloudFront, eBay, Allegro's DataDome and Geizhals' Cloudflare.

---

## 5. Economics and legal sheet (Poland-based reseller)

**Status.** The dedicated econ/legal research thread did not return in time. Every number below is **[UNVERIFIED-KNOWLEDGE]** unless marked. Each is a *calculator default to confirm*, not a fact to rely on. The "Confirm at" column names the primary source to check.

### 5a. Import costs

| Item | Default value | Confirm at |
|---|---|---|
| Customs duty, graphics card (CN 8473 30 80, parts of ADP machines) | **0%**, ITA-covered | ISZTAR4 (ext-isztar4.mf.gov.pl) / EU TARIC |
| Duty: laptops (8471 30), smartphones (8517 13), consoles (9504 50) | 0% | TARIC |
| Duty: monitors (8528 52) | 0% for most "ADP" monitors; some TV-like models up to 14% | TARIC (classification disputes are common) |
| Import VAT, PL | **23%** on (customs value incl. transport and insurance to the EU border, plus duty) | podatki.gov.pl |
| Courier customs-clearance fee (DHL, UPS, FedEx, InPost/Poczta for postal) | ≈ 20–100 PLN per shipment, plus sometimes a % disbursement fee on VAT advanced | Each carrier's PL tariff |
| EU €150 duty exemption | Being abolished. An interim **flat duty per item for low-value parcels** was reported as agreed by the Council in late 2025, from **1 Jul 2026** | Council of the EU press releases; DG TAXUD |
| IOSS | Still the VAT one-stop shop for consignments ≤ €150. GPUs are usually above €150, so they go through normal import with full VAT plus clearance | taxation-customs.ec.europa.eu |
| UK → PL | A full import since Brexit: VAT plus clearance; the UK seller can zero-rate the export | gov.uk / podatki.gov.pl |
| PL/EU → US buyer | US de minimis ended for all countries on **29 Aug 2025**. EU-origin goods face the US–EU tariff deal rate (≈ 15% reported). eBay international shipping passes these costs to the buyer | CBP / USTR / eBay |

**Rule of thumb** [INFERENCE]. For non-EU sourcing of a $1,000 GPU:

- Landed cost ≈ (price + shipping + insurance) × 1.23, plus clearance (≈ $15–25), plus forwarder fees.
- That ~23% VAT is **not recoverable for a private person**.
- A VAT-registered JDG can deduct it as input VAT.
- It is also *irrelevant under the margin scheme only for goods bought from private persons in the EU*. Imports do not qualify for margin-scheme input.

**Conclusion.** **US→PL and JP→PL GPU arbitrage needs a ≥ 30% raw price gap just to break even.** Intra-EU sourcing pays no customs and no import VAT.

### 5b. Shipping (calculator defaults)

| Lane | Default | Notes |
|---|---|---|
| PL domestic, InPost Paczkomat size C (GPU box) | ≈ 20–25 PLN retail; cheaper via Allegro/OLX delivery programmes | Check the insured-value cap: InPost and courier standard cover is often ≤ 5,000 PLN. A $2k GPU needs declared-value insurance |
| PL domestic courier (DPD, DHL, Orlen) | ≈ 18–30 PLN via aggregators (Furgonetka, Apaczka, Sendit) | Aggregators quote live; scrape their price calculator |
| PL ↔ DE/CZ/HU/AT courier, 5 kg | ≈ 45–90 PLN | Self-pickup in CZ/SK/HU is often cheaper and lets you **test the card** |
| US → PL via forwarder, 3–5 kg | ≈ $50–120 | Add the forwarder handling fee |
| JP → PL via Buyee/ZenMarket (EMS or FedEx), 3–5 kg | ≈ ¥8–20k | ZenMarket service ¥800 per Mercari/auction item [SECONDARY, Oct 2026] |

### 5c. Platform selling fees

These are calculator defaults. Allegro's category table is published in its seller price list ("cennik").

| Channel | Default | Confirm |
|---|---|---|
| Allegro (business account), PC components / GPUs | Category commission ≈ 5–9% of price incl. shipping, plus listing fees on some formats. Seller subscriptions 39 / 199 / 3000 PLN per 30 days (older figures) [SECONDARY] | allegro.pl/cennik |
| Allegro Lokalnie (private) | Low or zero seller fee for classifieds; buyer-protection fee with delivery | allegrolokalnie.pl help |
| OLX.pl | Limited free ads per category, then paid; promotion extra; "Przesyłka OLX" buyer-protection fee is paid by the buyer | pomoc.olx.pl |
| eBay.de, private seller | **0% final value fee for private sellers in DE** (since 2023). Business sellers pay a category FVF (≈ 6–11% for computer components) plus a fixed fee | ebay.de/help, Gebühren |
| eBay.com | FVF ≈ 13.25–13.6% of the total, plus $0.30–0.40 per order, plus an international fee of ≈ 1.35–1.65% | ebay.com fees page |
| Vinted | Seller 0%; the buyer pays protection fee (≈ €0.70 + 5%). **But commercial reselling is not allowed on standard accounts** [VERIFIED T&Cs / SECONDARY enforcement] | vinted.pl help |
| Kleinanzeigen | Free for private sellers; "Sicher bezahlen" fee charged to the buyer | kleinanzeigen.de |
| Facebook Marketplace (local) | 0% | — |

### 5d. Polish tax treatment

| Topic | Default / rule | Confirm |
|---|---|---|
| Private sale of **own** movable item | PIT-free if sold after 6 months, counted from the end of the month of acquisition (art. 10 ust. 1 pkt 8 lit. d, PIT Act) | podatki.gov.pl |
| Buying *in order to resell* | Is **business activity** (zarobkowa, zorganizowana, ciągła). The 6-month exemption does not protect it | biznes.gov.pl |
| **Działalność nierejestrowana** (unregistered activity) | Revenue limit **225% of the minimum wage per quarter**. With the 2026 minimum wage of 4,806 PLN that is **10,813.50 PLN per quarter**. The limit is on *revenue*, not profit, so one RTX 4090 flip consumes most of a quarter | biznes.gov.pl, "Działalność nierejestrowana" |
| JDG tax options | Ryczałt **3%** of revenue for trade; or the tax scale 12/32%; or flat 19%; plus ZUS | biznes.gov.pl |
| VAT | Subjective exemption limit 200k PLN (reported to rise to **240k PLN** from 2026). **Art. 113 ust. 13** excludes certain goods and activities from that exemption; check whether electronics or distance sales apply. **VAT margin scheme for used goods (art. 120)** is the natural regime: VAT is paid on the margin only (23/123 of the margin) when goods are bought from non-VAT private sellers | podatki.gov.pl; tax adviser (doradca podatkowy) |
| DAC7 | Platforms report sellers with ≥ 30 sales or ≥ €2,000 per year to KAS (the PL tax authority). Allegro, OLX, Vinted and eBay all report | podatki.gov.pl, DAC7 |
| Cash register (kasa fiskalna) | Online sales to consumers paid by bank transfer are generally exempt up to a turnover threshold. **The cash-register regulation excludes some product groups**; confirm whether computer and electronic equipment is excluded | Rozporządzenie MF on kasy exemptions |

### 5e. Consumer obligations when selling as a business (B2C)

- **14-day right of withdrawal** for distance sales. Budget a 3–8% return rate and the cost of return shipping when you offer it [INFERENCE].
- **Non-conformity liability** ("rękojmia" / niezgodność z umową) runs 2 years. For **used goods it can be shortened to 1 year in B2C**, and this must be agreed explicitly.
- **GPSR** (from 13 Dec 2024): marketplaces require the seller to show the responsible-person and manufacturer data. Allegro added a `GET /sale/responsible-persons/{id}` API on 27 Oct 2026 [VERIFIED: developer.allegro.pl news].
- **BDO/WEEE.** Whoever *first places* electrical equipment on the PL market must register in BDO and finance collection. This includes importing it, even intra-EU. Reselling equipment already on the PL market does not trigger it.
- **Omnibus** rules: show the lowest price of the last 30 days when advertising a price cut.
- **Private sellers** can exclude rękojmia between private persons, and there is no withdrawal right. This is why "private" listings are cheaper to run, but that only holds at hobby scale.

### 5f. Scraping legality and ToS risk

| Layer | Position | Risk |
|---|---|---|
| EU Database Directive (sui generis right) | Extracting or reusing a **substantial part** of a protected database is infringing. Repeated, systematic extraction of insubstantial parts can also be. *CV-Online Latvia v Melons* (C-762/19, 2021) requires the maker to show its investment is harmed. *Ryanair v PR Aviation* (C-30/14, 2015) lets **contract (ToS) terms** bind users of unprotected databases [UNVERIFIED-KNOWLEDGE] | Low for personal monitoring of a few hundred listings per hour. Higher for whole-category crawling or republishing |
| DE: BGH I ZR 224/12 (2014) | Screen-scraping publicly accessible data despite a ToS prohibition is not *per se* unfair competition. Circumventing technical protection changes this [SECONDARY: ra-plutte.de] | Low–medium |
| PL | The site's terms are a contractual template accepted by use. Database Act and copyright apply to substantial takings. Insubstantial parts of public databases are generally permitted [SECONDARY: rpms.pl] | Low–medium |
| GDPR | Seller names, phone numbers and profile data are personal data. Store only what scoring needs; keep seller IDs pseudonymised and short-lived | Medium if stored at scale |
| US: *Meta v. Bright Data* (Jan 2024) | Logged-out scraping of public data did not breach Meta's terms [SECONDARY: TechCrunch] | Context only |
| Platform ToS | Allegro: REST API terms art. 3.11 (key sharing, re-presenting offers on competitor sites). Vinted: explicit ban on bots and scrapers [VERIFIED]. eBay: User Agreement bans robots. Meta: bans automated collection | **The real risk is account bans.** Never scrape with the accounts you sell from. Monitor logged-out where possible |

**Practical rule** [INFERENCE]. Low-risk means:
- logged-out;
- human-like rates (≤ 1 request per few seconds per site);
- no CAPTCHA-solving services;
- no republication;
- only the fields needed;
- per-listing detail fetched only for candidates.

High-risk means:
- logged-in automated crawling;
- account-based automation on the selling account;
- bulk category crawling;
- redistributing data.

### 5g. Net-profit calculator inputs (defaults to confirm)

```
net_profit = exit_price_net_of_fees
           - buy_price
           - inbound_shipping - inbound_insurance
           - import_duty(cn_code, origin) - import_vat_if_nonEU(23%) - clearance_fee
           - forwarder_fees (Buyee/ZenMarket/US forwarder)
           - outbound_shipping (if seller-paid) - outbound_insurance
           - payment_fee - promo_fee
           - vat_margin_scheme(23/123 * (exit - buy))  # if JDG on art. 120
           - income_tax(ryczałt 3% of revenue | 19% | scale)
           - fx_spread (≈0.5–2%)
           - expected_return_cost (p_return * (2*shipping + restock_loss))
           - risk_haircut (p_scam_or_DOA * buy_price)
thresholds: PL-only ≥ $50; international ≥ $120; Tier-1 push ≥ $300 at P(conf) ≥ 0.8; cap $30k
```

---

## 6. Arbitrage patterns, ranked by likely (net profit × frequency) / effort

**Status.** The dedicated patterns thread did not return. Ranking is **[INFERENCE]** from platform mechanics, the price-truth findings above, and background knowledge. Specific market datapoints are marked.

**Market context, late 2026** [SECONDARY, not fully verified]:
- BestValueGPU reports the **Sept 2026 median used GPU price at 116% of MSRP**.
- A Sept 2026 retailer guide cites the Tom's Hardware index listing the **RTX 5090 at about $5,199 against a $1,999 MSRP**.
- gpupoet reported in **Jan 2026 that RTX 50 cards were near MSRP while used cards were up to 70% off**.

Read together, these suggest a **sharp 2026 tightening**, consistent with the reported DRAM/HBM-driven memory squeeze. Data-centre GPUs moved the opposite way: the AmCompute index fell to **75 in Aug 2026**, the lowest of 13 months, with pre-Hopper cards leading the decline [VERIFIED]. **Implication:** consumer-GPU *scarcity* gaps are wide right now, data-centre cards are *depreciating fast*, and price volatility rewards speed and penalises holding inventory.

| Rank | Pattern | Mechanism | Typical net | Frequency | Effort / risk | How the agent detects it |
|---|---|---|---|---|---|---|
| 1 | **Underpriced local classifieds**: OLX, Allegro Lokalnie, FB groups, sprzedajemy; CZ/SK Bazoš, sbazar; HU hardverapro | Non-technical sellers, urgent sales, bad titles or typos ("rtx 3080ti", "karta grafiki"), wrong category, missing VRAM | $50–300 per card | Daily in PL; several per week ≥ $150 | Low capital; **speed is everything** (minutes); in-person test possible | Poll every 1–3 min, normalise, compute the z-score against the *clears-at* ledger price, alert. Also alert on "seller's 2nd+ price drop" |
| 2 | **Intra-EU cross-border** (CZ/SK/HU/DE/AT/NL → PL, or PL → DE/AT/Nordics) | Price levels differ by country and currency; no customs or import VAT in the EU | $80–250 per item | Weekly | Low–medium; courier or a drive (Ostrava 2 h, Budapest 5 h) | Same canonical key across countries; compare against the best exit (Allegro / eBay.de sold) |
| 3 | **"For parts" / faulty GPUs that are cheap to fix** (dead fans, thermal pads, BIOS, cracked shroud, missing backplate) | Sellers price at 20–40% of working value | $100–400 | Weekly | Needs bench skill and parts; risk of real die or VRAM faults | Defect-keyword classifier plus LLM triage of the description and photos; exclude "no die", "short to ground", "artefakty po rebballingu" |
| 4 | **Bundle splitting**: prebuilt PCs and gaming laptops → part out the GPU, CPU, RAM and SSD | In a memory and GPU squeeze, prebuilts lag component prices | $150–500 per PC | Weekly | Medium; selling 4–6 parts takes time | Parse PC specs from listings; sum the per-part clears-at values; subtract a 10–15% parting cost |
| 5 | **Retail price errors, outlet and returns**: x-kom/morele/Komputronik hot shots, Media Expert / RTV Euro AGD outlet, Amazon.de Warehouse, Pepper/mydealz | Mispricing, open-box discounts, coupons | $50–400; occasionally more | Several per month | Low; competition from deal-site readers; order cancellations | changedetection.io plus Pepper/mydealz RSS keyword filters plus Ceneo/Geizhals delta alerts |
| 6 | **New-launch scarcity** (restocks at MSRP vs secondary price) | MSRP drops sell out; the secondary market pays a premium | $100–600 at launch, falling fast | Episodic | Bot competition; per-customer limits; a reputational issue | Retail restock watchers (changedetection/Distill); push immediately |
| 7 | **Data-centre and AI liquidations** (A100/V100/A6000/L40S, ex-AI-lab RTX 3090/4090s) | Decommissioning floods eBay US/China; EU buyers pay more locally | $200–2,000 per card | Monthly | **High**: capital, fakes, falling index (AmCompute −12% m/m), import VAT 23% | eBay Browse API (`itemLocationCountry=US/CN/HK`) plus the AmCompute trend gate (don't buy into a falling index) |
| 8 | **US/JP → PL import** | Different price levels and FX | Gap must exceed ≈ 30% to cover VAT, duty and clearance | Rare for GPUs; better for JP-only items (retro consoles, niche audio) | Medium–high | Mercari sold-out and Aucfan comps against the eBay.de sold exit |
| 9 | **Bailiff, customs and bankruptcy auctions**: elicytacje.komornik.pl, KAS auctions, syndyk sales; DE zoll-auktion.de, justiz-auktion.de, VEBEG | Thin buyer pools, administrative pricing | Variable | Sporadic | Medium: auction rules, inspection, pickup | Low-frequency crawl (daily); LLM extraction from PDF/HTML lot descriptions |
| 10 | **Return and overstock pallets** (Merkandi, Jobalots, B-Stock, PL "palety zwrotów") | Buy a lot by manifest value | Often **negative EV** for electronics after defects | Weekly listings | High effort; DOA rate; manifests inflated | Only with an EAN manifest: price each line by Ceneo/eBay sold, apply a DOA haircut of 30–50% |

**Why this order** [INFERENCE]. The user's floors are $50 domestic and $120 international, with a $300 tier 1. Patterns 1–4 hit these most often with the least capital and almost no customs friction. Patterns 7–10 can produce tier-1 hits but rarely, and with fat tails of risk.

---

## 7. Scam and risk signals (agent checklist)

**Status.** The scams thread did not return. These are well-documented platform patterns [UNVERIFIED-KNOWLEDGE / INFERENCE]. Each row is phrased as a check the agent can automate.

### 7a. When buying

| Pattern | Typical platforms | Signals to check automatically | Mitigation |
|---|---|---|---|
| **Fake-courier / payment-link phishing** ("OLX Dostawa", "Vinted Go" or "Allegro Lokalnie" fake checkout; buyer or seller moves to WhatsApp or Telegram) | OLX, Vinted, Allegro Lokalnie, Kleinanzeigen, FB | Text contains a phone number, "WhatsApp", "Telegram", "napisz SMS" or a link; asks to pay outside the platform; seller account under 30 days old; zero ratings | Never pay outside the platform; use built-in checkout or meet in person |
| **Too-good-to-be-true price** | All | Price below 55–60% of the model's clears-at median (z-score < −2.5) **and** a new account, **or** stock photos | Auto-downgrade confidence; require an in-person test |
| **Stolen or stock photos** | All | Reverse-image and perceptual-hash match against other listings or shop images (CLIP plus pHash; see stolen-bike-watch); missing EXIF; identical photos in other countries | Ask for a photo with a handwritten nickname, date and serial number |
| **"Box only" / "photo of the GPU" / "pudełko"** | eBay, Allegro, OLX | Title or description classifier (pudełko, box, karton, empty, "nur OVP", "zdjęcie"); price under 15% of median | Hard-exclude |
| **Die- or VRAM-stripped GPUs** (no-die 4090/5090 cards resold as faulty or working) | eBay, Amazon returns, OLX "uszkodzona" | "No display / brak obrazu", "not detected", "after repair", backplate screws missing, close-up photos missing; price 20–40% of median | Treat as for-parts; only buy with die-side photos, or in person with a boot test |
| **Fake or relabelled cards** (old GPU flashed with a new-model BIOS) | AliExpress, eBay CN/HK sellers, OLX | Shroud or PCB mismatch with the model (TechPowerUp board photos), "unbranded", suspiciously cheap high-end card, origin CN/HK | GPU-Z / nvidia-smi device-ID check at pickup; a benchmark |
| **Ex-mining / ex-AI-lab wear** | All (3060–3090, 4090) | Batches of identical cards, "from rig", missing original box, yellowed pads, fan noise | Lower the condition bucket; test VRAM (memtest_vulkan / OCCT) |
| **Melted 12VHPWR / 12V-2x6 connector** | 4090/5090 | "Connector", "przypalony", "melted" in text; close-up connector photo missing | Require a connector close-up |
| **Stolen goods** | Classifieds | Many new-in-box high-end items from a new account; no invoice; "bez paragonu"; price far below retail | Require an invoice (faktura/paragon) for new items; this also matters for warranty |
| **Bait listings / data harvesting** | OLX, Vinted, FB | Same text across several cities; seller has dozens of active top-tier GPU ads | Cluster detector on text and image hashes |

### 7b. When selling

| Pattern | Platforms | Signals | Mitigation |
|---|---|---|---|
| Empty-box / "item not as described" swap returns | eBay, Allegro, Vinted | New buyer, high-value item, international address, asks for a fast ship | Record serial numbers and seal; video the packing; insure; use signature-required shipping |
| PayPal Friends & Family / chargeback | FB, forums | Request to "send as F&F", or a buyer paying via F&F then disputing | Accept only platform-protected payments |
| Overpayment / fake transfer confirmation | OLX, FB | "I paid more, refund the difference"; screenshot as proof | Ship only after funds settle |
| Fake courier pickup / phishing to steal seller payout | OLX, Vinted, Kleinanzeigen | "Click to receive payment" link | Never click; payouts happen only in the app |

### 7c. Scoring

[INFERENCE] `p_scam = σ(w·features)` from: account age, rating count, z-score, off-platform contact cues, photo-reuse hits, text-template cluster size, origin/shipping mismatch, and payment method.

- **Tier 1 (instant push) requires `p_scam < 0.05` and a valuation from ≥ 20 comparable sold/clears-at datapoints.**
- Otherwise the alert is demoted to a tier 2 digest.

---

## 8. Sources

Primary or verified (read in this pass):
- Allegro API: "no longer part of the public API" — https://github.com/allegro/allegro-api/issues/14138 · https://github.com/allegro/allegro-api/issues/14140 · no market-data programme (29 Oct 2026) — https://github.com/allegro/allegro-api/issues/14240 · `/sale/products` GTIN alternative — https://github.com/allegro/allegro-api/issues/11455 · 2021 restrictions — https://github.com/allegro/allegro-api/issues/4566 · access refused (Jun 2025) — https://github.com/allegro/allegro-api/issues/11707
- Allegro developer news (GPSR responsible-persons, 27 Oct 2026) — https://developer.allegro.pl/news
- Archiwum Allegro closure — https://www.tabletowo.pl/?p=995820
- Allegro REST API terms art. 3.11 (from 23 Sep 2025) — https://spolecznosc.allegro.pl/t5/zaawansowani-sprzedawcy/10-09-2025-nowy-regulamin-rest-api/m-p/1078806
- Allegro Analytics — https://www.shoper.pl/learn/?p=66844
- OLX Partner API docs — https://developer.olx.pl/api/doc
- eBay Buy API requirements — https://developer.ebay.com/api-docs/buy/static/buy-requirements.html
- eBay Product Research (Terapeak) — https://www.ebay.com/help/selling/selling-tools/terapeak-research-and-SEO?id=4853
- eBay Marketplace Insights method — https://www.developer.ebay.com/api-docs/buy/marketplace-insights/resources/item_sales/methods/search
- eBay Finding API decommission (forum) — https://community.ebay.com/t5/Traditional-APIs-Search/Alert-Finding-API-and-Shopping-API-to-be-decommissioned-in-2025/m-p/34631345
- eBay call limits — https://developer.ebay.com/develop/get-started/api-call-limits
- OpenClaw eBay research plugin — https://hub.openclaw.ai/jason-vaughan/plugins/openclaw-ebay-research · https://github.com/Jason-Vaughan/openclaw-ebay-research
- Vinted T&Cs (bots and scraping ban; versions of 18-12-2025 and 8 Oct 2026) — https://www.vinted.com/old-terms-and-conditions
- Vinted reseller enforcement — https://crosslist.com/blog/does-vinted-allow-resellers
- DataDome × Leboncoin — https://datadome.co/customers-stories/how-leboncoin-blocks-millions-of-malicious-requests-every-day/
- AmCompute GPU Resale Index — https://www.amcompute.com/gpu-resale-index
- changedetection.io — https://github.com/dgtlmoon/changedetection.io
- kleinanzeigen-agent.de — https://kleinanzeigen-agent.de/
- All GitHub repos in §3 (stats via `gh api`, 2026-10-29); URLs are inline in the tables.
- Own live probes, 2026-10-29: plain curl from a PL (WAW) egress against Allegro, Allegro Lokalnie, OLX (PL/RO/PT/BG/UA), Vinted, Kleinanzeigen, Marktplaats, 2dehands, Bazoš (cz/sk/at), sbazar, aukro, Subito, Mercari JP, Leboncoin, Wallapop, Geizhals, Ceneo, eBay, Facebook, hardverapro, jofogas, willhaben, tweakers, hardwareluxx, tori, finn, blocket, dba, ss.lv, skelbiu, okidoki, njuskalo, bolha, kupujemprodajem, ricardo, tutti, sprzedajemy and quoka. Results are in §4b and §4c.

Secondary:
- eBay sold login wall — https://www.openwebninja.com/blog/how-to-get-ebay-sold-and-completed-listings · https://scavio.dev/blog/ebay-sold-listings-api-login-wall-2026 · https://quicksync.pro/blog/how-to-see-sold-items-on-ebay/ · https://flowlister.com/blog/ebay-sold-listings/
- eBay sold-data alternatives — https://dev.to/sam_compsapi/how-to-get-ebay-sold-listings-data-in-2026-findcompleteditems-is-dead-3l2j · https://sold-comps.com/alternatives
- Allegro popularity = 30-day purchases — https://spolecznosc.allegro.pl/t5/zaawansowani-sprzedawcy/dlaczego-informacja-o-liczbie-sprzedanych-przedmiot%C3%B3w-nie/td-p/116194
- Allegro Lokalnie: no API — https://spolecznosc.allegro.pl/t5/zaawansowani-sprzedawcy/allegro-lokalnie-api/m-p/23769
- Allegro scraping / DataDome — https://scrape.do/blog/allegro-scraping.md · https://scrapfly.io/blog/posts/how-to-scrape-allegro
- Vinted API mechanics — https://dev.to/datakaz/vinted-scraping-at-scale-100k-listings-per-day-2026-guide-14d9 · https://apify.com/apium/vinted-scraper
- Vinted electronics category — https://us.fashionnetwork.com/news/Vinted-launches-electronics-category,1665225.html · https://cross-border-magazine.com/vinted-reports-330-increased-net-profit/
- Vinted Pro in PL — https://bezprawnik.pl/vinted-pro-w-polsce/ · https://biznes.interia.pl/finanse/news-zapytalismy-vinted-o-uruchomienie-nowej-funkcji-nie-mamy-pot,nId,22506936
- OLX JSON endpoint behaviour — https://apify.com/lowlanddata/olx-pl-scraper.md
- Wallapop v3 search — https://apify.com/nogards95/wallapop-scraper.md
- Mercari Global App — https://www.valueaddedresource.net/mercari-opens-global-app-to-us/ · https://www.digitalcommerce360.com/2026/06/19/mercari-launches-us-app/
- Mercari "相場検索" — https://www.commercepick.com/archives/69464 · https://news.allabout.co.jp/articles/o/54939/
- Aucfan — https://minhyo.jp/aucfan
- ZenMarket / Buyee fees — https://friptadium.com/blogs/actualites/acheter-seconde-main-japon-buyee-zenmarket-mercari
- BGH screen scraping — https://www.ra-plutte.de/bgh-zum-automatisierten-auslesen-fremder-websites-via-screen-scraping/
- PL scraping law overview — https://rpms.pl/web-scraping-czym-jest-czy-jest-legalny/
- Meta v Bright Data — https://techcrunch.com/2024/01/24/court-rules-in-favor-of-a-web-scraper-bright-data-which-meta-had-used-and-then-sued
- Proxy pricing — https://aimultiple.com/proxy-pricing · https://use-apify.com/blog/iproyal-pricing-plans-2026 · https://costbench.com/compare/bright-data-vs-oxylabs/
- EAN APIs — https://shopsavvy.com/compare/product-data-apis.md
- Geizhals data — https://readme.priceapi.com/reference/geizhals-1 · https://pypi.org/project/geizhals/0.0.9/
- GPU market — https://bestvaluegpu.com/gpu-price-trend/ · https://gpupoet.com/gpu/market-report/gpu-market-report-january-2026 · https://pcserverandparts.com/news/used-graphics-card-buying-guide-2026/ · https://www.tomshardware.com/news/gpus-historical-ebay-pricing
- LLM entity matching — https://arxiv.org/abs/2310.11244v3 (Peeters, Steiner, Bizer; EDBT 2025)

**To verify before relying on them** (sections 2 and 5–7, where research threads did not complete):
- vendor pricing pages: Swoopa, Flipify, Tactical Arbitrage, SourceMogul, Keepa;
- ISZTAR/TARIC for GPU and monitor CN codes;
- the Council press release on the low-value-parcel duty from 1 Jul 2026;
- biznes.gov.pl on the unregistered-activity limit for 2026;
- the art. 113 ust. 13 VAT exemption exclusions;
- Allegro's cennik for the GPU category;
- eBay.de / eBay.com fee pages;
- InPost and Furgonetka price lists.
