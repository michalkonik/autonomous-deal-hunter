# Sources

Every source Deal Hunter uses or has rejected, how it is accessed, the robots decision, and its status.

## The rule

No bot-check circumvention of any kind: no TLS or browser fingerprint impersonation, no stealth browsers, no
challenge solving, no proxy rotation, no logging in to scrape (spec 6.4, amended 2026-10-29). All HTTP goes through
`dh/fetch.py`, which sends an honest User-Agent (`DealHunter/0.1 (personal price research; ...)`), paces requests
per domain (4 s plus 0 to 2 s of jitter), and backs off on 403, 429 and challenge pages. `fetch.obey_robots` is
false in this install (owner, 2026-11-12): robots.txt is not fetched and a Disallow line is not a reason to skip
a public page. A site that answers a plain request with 403, 429 or a challenge page is still dropped, not worked
around. There is no browser impersonation, no proxy and no logged-in scraping.

The registry is in `dh/sources/__init__.py` (`ADAPTER_NAMES`, `DROPPED`); the switches are in
`config/sources.yaml`. To add one, read [`ADDING-A-SOURCE.md`](ADDING-A-SOURCE.md).

## Status at a glance

| Source | Adapter name | Access | Status |
|---|---|---|---|
| Allegro Lokalnie (PL) | `allegro_lokalnie` | Plain HTML search and detail | Enabled |
| sprzedajemy.pl (PL) | `sprzedajemy` | Allowed category listing pages, filtered locally | Enabled |
| refurbed.pl (PL) | `refurbed` | Plain HTML search, page 1 | Enabled |
| taniekomputery.pl (PL) | `taniekomputery` | Plain HTML category page | Enabled |
| asgoodasnew.de (DE) | `asgoodasnew` | Plain HTML search, one page | Enabled |
| amso.pl (PL) | `amso` | Plain HTML search, one page | Enabled |
| luxtrade.pl (PL) | `luxtrade` | Plain HTML search, one page | Enabled |
| device.store (PL) | `devicestore` | Plain HTML search, one page | Enabled |
| gebrauchte-technik.de (DE) | `gebrauchtetechnik` | Plain HTML search, one page | Enabled |
| Kleinanzeigen.de (DE) | `kleinanzeigen` | Plain HTML at low rate | Enabled |
| HardverApro (HU) | `hardverapro` | Plain HTML, page 1 only | Enabled |
| Marktplaats (NL) | `marktplaats` | HTML search page (embedded page data) | Enabled |
| 2ememain.be (BE) | `2ememain` | HTML search page (embedded page data; same platform as Marktplaats) | Enabled |
| Tori.fi (FI) | `tori` | JSON search endpoint allowed by robots.txt (Adevinta BAP API) | Enabled |
| Bazos CZ, SK | `bazos_cz`, `bazos_sk` | Allowed category pages, filtered locally | Enabled |
| eBay DE and PL | `ebay_de`, `ebay_pl` | Official Browse API | **Disabled until keys exist** |
| eBay EU/EU-adjacent (GB, FR, IT, ES, IE, AT, CH, BE) | `ebay_gb`, `ebay_fr`, `ebay_it`, `ebay_es`, `ebay_ie`, `ebay_at`, `ebay_ch`, `ebay_be` | Official Browse API, filtered to sellers shipping to PL | **Disabled until keys exist** |
| eBay sold prices | (Marketplace Insights) | Official API, limited release | **Disabled until the scope is granted** |
| Pepper.pl, hotukdeals | `pepper_pl`, `hotukdeals` | RSS "hot" feeds | Enabled (feed sources) |
| Retail price watch | `retail` | JSON-LD on shop pages: morele.net, x-kom.pl | Enabled (feed source) |
| Allegro.pl | `allegro` | Official REST `GET /offers/listing` | **Disabled until `allegro#client_id` and `allegro#client_secret` exist. Allegro must also have verified the app; otherwise the call is `not_verified` and nothing is scraped** |
| OLX.pl | (via mailbox) | Saved-search alert e-mail | **Disabled until the mailbox is set up** |
| Alert mailbox | `mailbox` | IMAP over SSL | **Disabled pending credentials** (`enabled: false` by default) |
| Vinted | none | none | Off. No adapter exists. Its terms forbid bots and reselling for profit on standard accounts, and it blocks polite requests |
| Bazos AT | `bazos_at` | Category pages | Disabled: almost no volume (the GPU and notebook categories list 0 ads) |
| willhaben | none | none | **Dropped** |
| sbazar.cz | none | none | **Dropped** |
| OLX ro, bg, pt | none | none | **Dropped** |
| MediaExpert, Komputronik | none | none | **Dropped** (retail watch) |

"Disabled pending credentials" sources cost nothing: they request nothing and record one health event.

## Enabled sources, in detail

**Allegro Lokalnie.** robots.txt blocks only `/oferty/wystaw/*`, so search and detail pages are readable. Polish
prices in PLN; the exit channel `allegro_lokalnie` is a comparable source.

**sprzedajemy.pl.** robots.txt has `Disallow: *inp_*`, which covers every search URL and sort option. The search
form is never used: the adapter reads the allowed category listing pages (page 2 with `?offset=30`) and filters the
titles locally against the query words. Detail pages carry schema.org Product data.

**taniekomputery.pl.** Added 2026-11-12. A Polish shop for refurbished post-lease laptops, desktops and
tablets, with a shop warranty and delivery in Poland. The adapter reads the first screen of the matching
category (`/en/refurbished-laptops`, tablets or computers) and keeps rows whose title matches the query.
The struck `.price-old` amount is not the offer. Listings are marked refurbished, so they are compared
with used prices.

**asgoodasnew.de.** Added 2026-11-13. German shop for refurbished phones, laptops and other electronics.
The shipping page prices Poland at 10,90 EUR and names DHL for Germany and Poland. The adapter reads
one search page (32 articles). Each in-stock grade is its own offer. The price is the grade amount
in `data-variant`, not the voucher amount shown next to a code. Out-of-stock grades are skipped.
Grade "neu" is still refurbished stock of the same device, so that word is left out of the title and
the row is compared with used prices. The shop warranty line is 30 months.

**amso.pl.** Added 2026-11-13. Kraków shop for post-lease and ex-display computers, with delivery in
Poland. The adapter reads the first page of `search.php?text=`. `strong.price` is the current price.
The struck regular price, the omnibus 30-day price and the points price are not the offer. Rows
marked unavailable are skipped. Listings are marked refurbished.

**luxtrade.pl.** Added 2026-11-13. Polish shop for refurbished phones, with delivery in Poland and a
shop banner of up to 24 months of warranty. The adapter reads the first page of
`/szukaj?controller=search&s=`. The current price is `span.price`. The link's `content` URL is the
specific grade. A card marked unavailable is skipped. On the product page the offer is
`.current-price-value`; other `span.price` nodes on that page are not the offer.

**device.store.** Added 2026-11-13. Polish shop in Bydgoszcz for ex-lease laptops and phones, priced
in euro, with delivery in Poland and a shop warranty of up to 2 years. The adapter reads the first
page of `/en/search.html?text=`. `strong.price` is the current gross price. Empty skeleton cards and
unavailable rows are skipped. The product page price is `#projector_price_value`.

**gebrauchte-technik.de.** Added 2026-11-13. German shop for refurbished computers. The shipping page
lists Poland and charges 9,99 EUR for delivery inside the EU. The adapter reads the first page of
`/search?search=`. `span.product-price` is the offer. The list price next to it is not. A grade of
"neu" is left out of the title, and the row is compared with used prices. The product page price is
`.product-detail-price`.

**refurbed.pl.** Added 2026-11-12. Professional refurbished computers and phones. The Polish store
(`www.refurbed.pl`) prices in PLN and includes delivery in Poland, with at least 12 months of shop warranty.
robots.txt allows `/search/?query=`. The first page is server-rendered product cards, so that is what the
adapter reads. The "show more" control is a javascript button (`href="#"`), so later pages are not fetched.
The struck "(Nowa)" amount is the new-shop comparison and is not the offer; a voucher price
(`product-price-discounted`) is not the offer either. Listings are marked `condition: refurbished`, so they
are compared with used prices. Every shop already in the registry stays enabled.

**Kleinanzeigen.de.** Server-rendered pages, allowed for plain requests at low rate. Selectors are structural
because the CSS is utility classes. A saved search by alert mail also works (see below).

**HardverApro.** robots.txt forbids only `keres.php?...offset=`, so only page 1 of a search (newest first) is read.
Hungary's hardware market.

**Marktplaats.** robots.txt forbids the JSON endpoint `/lrp/api/search*` and `/search?q=*`, so they are not used.
The HTML search page `/q/<query>/` is allowed and embeds the listings in `__NEXT_DATA__`, which the adapter parses.

**Bazos.** robots.txt disallows search on every Bazos domain, so search is never used. The adapter reads the newest
`pages` pages (20 ads each) of the matching category (for example `pc.bazos.cz/graficka/`) and filters locally.
The Bazos dataset can also seed price history (`python -m dh.seed bazos`).

**eBay (DE, PL, GB, FR, IT, ES, IE, AT, CH, BE).** The official Browse API with your keys: `bw-secret ebay#client_id` and `ebay#client_secret`
(read once per run: restart the agent after adding them). Foreign markets add `deliveryCountry:PL` to the search
filter, so only items the seller ships to Poland are returned. (eBay's PT, FI, SE, NO, DK markets are not supported
by the Browse API — those countries need their local marketplaces instead.)
eBay web pages are never fetched. eBay's terms ban AI shopping agents and scrapers since February 2026, so the API
is the only route. The API client is a documented exception to the web fetcher (it needs a POST token call and does
not serve robots.txt): it still sends the honest User-Agent and paces calls. One key pair backs off 60 s doubling
to 1 h on 429 or 403 and disables itself for the session after 3 consecutive refusals. Extra complete sets
(`ebay2` … `ebay8`) are used as well: a 429 cools that application and the next key is tried immediately.
Active asks become comps. Marketplace Insights (sold prices) uses the same key pool and needs the scope grant
on at least one application; if every key lacks it the source disables itself with `not_granted` and valuation
relies on the ledger. Logged-in eBay Product Research is not implemented.

**Pepper.pl and hotukdeals.** Public RSS "hot" feeds, fetched politely. They carry retailer promotions (price from
the title, or the merchant price), so they find shop price errors. They are *feed sources*: one query-less sweep per
round, and never ledger-swept, because a deal leaving a feed was not sold.

**Retail price watch.** A list of shop pages in `config/sources.yaml` (`retail.watch`): a morele.net category and
five x-kom.pl product pages. The pages carry JSON-LD `offers.price`. It records the new-retail price (the ceiling
for used prices) and emits a listing when a price falls 10% or more, or goes below the key's clears-at P25. Add
pages to the list for products you care about.

## Dropped sources and why

Evaluated on 2026-10-29 with the polite fetcher.

| Source | Reason |
|---|---|
| sbazar.cz | robots.txt: `User-agent: *  Disallow: /`. Only Googlebot-style agents are allowed |
| willhaben.at | robots.txt header says it is "expressively forbidden to use spiders, search robots or other automatic methods" without permission; keyword search and paging are also disallowed |
| OLX ro, bg, pt | Blocks polite polling like olx.pl. Dropped under the same mailbox-only rule |
| MediaExpert.pl | Answers 403 (a bot interstitial) to plain requests |
| Komputronik.pl | Category pages answer 403 (a challenge) |
| Vinted | Terms forbid bots; blocks plain requests; no adapter built |
| Swappie | Polish iPhone category returned 403 on 2026-11-13; not worked around |
| Subito.it | robots.txt returned 403 (access denied) on 2026-11-12 |
| Blocket.se | robots.txt prohibits automated crawling without written permission. Buyer-protection freight also requires a Swedish phone number |
| Back Market | laptop category returned 403 (checked 2026-11-12); not worked around |
| afbshop.de | Shipping page lists Germany, Austria, Belgium, Italy and Spain, not Poland (2026-11-13) |
| computeruniverse | Homepage returned 403 (2026-11-13); not worked around |
| clevertronic.de | Homepage returned 403, a challenge page (2026-11-13) |
| CeX Poland | Search page had no product cards; the catalog JSON returned 403 (2026-11-13) |
| itsco.de | Delivery-time list includes Poland, but the card shows the bulk from-price (one piece is a different row) and the search URL returned 500 |
| Wisetek Market | Shipping page lists Poland, but the search page and the catalog JSON returned 429 on 2026-11-13 |
| lapstore.de | Poland delivery is only for commercial resellers, not a private address (2026-11-13) |

Checked and not added, because a priced public page that ships to Poland was not shown:
rebuy (the Poland mention is a workshop in Poznań; the shop sites are DE, FR, ES, IT and NL),
ReMarkt.pl (the laptop category returned 429), Jófogás and Aukro (domestic classifieds; Hungary is already
covered by HardverApró), Ricardo (private Swiss classifieds, outside the EU). An existing source was not
turned off to make room for any of these.

The spec also named Geizhals, Leboncoin and Wallapop as sites that block plain polite requests. None has an
adapter. They are out of scope for the same reason. Facebook Marketplace is out of scope: it needs a login and the
ban risk falls on the owner's account.

## Allegro.pl and OLX.pl

Both block polite polling (Allegro.pl uses DataDome; OLX uses CloudFront). The hunter never fetches those websites.

**Allegro.pl search** is the official REST API: client-credentials token, then `GET /offers/listing`. Every call
sends `User-Agent: deal-hunter/1.0 (+https://github.com/michalkonik/family-finance-planner)` unless
`adapters.allegro.user_agent` in config overrides it. A wrong User-Agent can get the key blocked. Keys are
`bw-secret allegro#client_id` and `allegro#client_secret` (read once per process: restart after storing them).
Allegro only allows verified applications on this endpoint and has not been granting that access to new apps
since 2024. A 403 `VerificationRequired` is recorded once as `not_verified` and the adapter stops for the session.
There is no HTML fallback. Buy-now offers only; auctions, empty stock and Allegro Lokalnie rows are dropped.
There is no per-offer public endpoint, so an Allegro listing is not re-checked live and cannot reach tier 1.

**OLX.pl** still has no such API in this agent. Saved-search alert mail is its only route.

The mailbox adapter reads those mails and never fetches an Allegro, OLX or eBay page. Mail listings are not
re-checked live. They can reach tier 2. The human acting on a deal opens the link and checks it by hand.

## Alert mailbox set-up

This is **gated on you**: until you do it, Allegro.pl, OLX.pl and the other alert sources stay off.

1. **Create a dedicated mailbox** that supports IMAP over SSL (port 993). Do not use a personal mailbox: the agent
   reads every unseen mail in the folder it is pointed at.
2. **Store the login** as a Bitwarden item `dh-mail` with a field `imap` holding JSON:
   ```json
   {"host": "imap.example.com", "port": 993, "user": "alerts@example.com", "password": "...", "folder": "INBOX"}
   ```
   Check it with `bw-secret dh-mail#imap`. `folder` is optional (default `INBOX`). The login is read once per run:
   if the agent is running, `dh stop` and `dh start` after storing it.
3. **Sign up** on Allegro, OLX, Kleinanzeigen and (optionally) eBay with that address.
4. **Save narrow searches** on each platform and turn on e-mail alerts for them, at the most frequent setting the
   platform offers. Good searches mirror the playbook queries: one per product, for example "rtx 4090", with a
   minimum price that excludes accessories. Allegro allows up to 300 followed searches. Narrow searches matter
   because each mail is a new-listing event: a broad search floods the rig with noise.
5. **Enable the adapter** in `config/local/sources.yaml`:
   ```yaml
   adapters:
     mailbox: {enabled: true}
   ```
6. `dh stop && dh start`. The mailbox is polled once per round. `dh status` and `/health` show `no_credentials` if
   step 2 failed.

How mail is handled:
- A mail is accepted only when the sender domain is a known platform (`allegro.pl`, `olx.pl`, `kleinanzeigen.de`,
  `ebay.de`, `ebay.pl`) **and** the receiving server's `Authentication-Results` shows DKIM or SPF passing for that
  platform. Anything else is skipped with an `unauthenticated_alert` health event, so a forged mail cannot create
  a deal. Your mail provider must add that header.
- Mails are read without marking them, and marked read only after their listings are saved. A crash re-delivers
  them. Already-read mail is ignored, so do not read the alert mailbox by hand (or use another folder for yours).
- A genuine alert that parses to zero offers raises `alert_parse_empty` and stays unread for inspection.
- Photos in a mail are kept only from the platform's image hosts, over https
  (`adapters.mailbox.image_hosts`).
- Saved `.eml` files are deleted after 90 days.
- **The mail parsers are built from each platform's typical alert layout, using synthetic fixtures**
  (`tests/fixtures/mailbox/README.md`). They have not yet met a real alert. Expect to adjust them after your first
  real mails: if `alert_parse_empty` appears, send a sample to a developer.
- Kleinanzeigen mails are enriched through the polite fetcher when its robots rules allow it. Allegro, OLX and eBay
  mails never are. Allegro and OLX mailbox listings are unverifiable and top out at tier 2. A Kleinanzeigen mail listing is routed to the real `kleinanzeigen` adapter for its detail page and its live re-check (when that adapter is enabled), and an eBay one to the eBay API adapter when keys exist; verified, they can reach tier 1.
