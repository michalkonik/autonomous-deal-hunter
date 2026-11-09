# Sources

Every source Deal Hunter uses or has rejected, how it is accessed, the robots decision, and its status.

## The rule

No bot-check circumvention of any kind: no TLS or browser fingerprint impersonation, no stealth browsers, no
challenge solving, no proxy rotation, no logging in to scrape (spec 6.4, amended 2026-10-29). All HTTP goes through
`dh/fetch.py`, which sends an honest User-Agent (`DealHunter/0.1 (personal price research; ...)`), obeys robots.txt
(an unreachable robots.txt counts as disallowed), paces requests per domain (4 s plus 0 to 2 s of jitter), and backs
off on 403, 429 and challenge pages. A site that blocks plain polite requests is reached only through an official
API, RSS or the alert mailbox, or it is dropped with a recorded reason.

The registry is in `dh/sources/__init__.py` (`ADAPTER_NAMES`, `DROPPED`); the switches are in
`config/sources.yaml`. To add one, read [`ADDING-A-SOURCE.md`](ADDING-A-SOURCE.md).

## Status at a glance

| Source | Adapter name | Access | Status |
|---|---|---|---|
| Allegro Lokalnie (PL) | `allegro_lokalnie` | Plain HTML search and detail | Enabled |
| sprzedajemy.pl (PL) | `sprzedajemy` | Allowed category listing pages, filtered locally | Enabled |
| Kleinanzeigen.de (DE) | `kleinanzeigen` | Plain HTML at low rate | Enabled |
| HardverApro (HU) | `hardverapro` | Plain HTML, page 1 only | Enabled |
| Marktplaats (NL) | `marktplaats` | HTML search page (embedded page data) | Enabled |
| Bazos CZ, SK | `bazos_cz`, `bazos_sk` | Allowed category pages, filtered locally | Enabled |
| eBay DE and PL | `ebay_de`, `ebay_pl` | Official Browse API | **Disabled until keys exist** |
| eBay sold prices | (Marketplace Insights) | Official API, limited release | **Disabled until the scope is granted** |
| Pepper.pl, hotukdeals | `pepper_pl`, `hotukdeals` | RSS "hot" feeds | Enabled (feed sources) |
| Retail price watch | `retail` | JSON-LD on shop pages: morele.net, x-kom.pl | Enabled (feed source) |
| Allegro.pl | (via mailbox) | Saved-search alert e-mail | **Disabled until the mailbox is set up** |
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

**Kleinanzeigen.de.** Server-rendered pages, allowed for plain requests at low rate. Selectors are structural
because the CSS is utility classes. A saved search by alert mail also works (see below).

**HardverApro.** robots.txt forbids only `keres.php?...offset=`, so only page 1 of a search (newest first) is read.
Hungary's hardware market.

**Marktplaats.** robots.txt forbids the JSON endpoint `/lrp/api/search*` and `/search?q=*`, so they are not used.
The HTML search page `/q/<query>/` is allowed and embeds the listings in `__NEXT_DATA__`, which the adapter parses.

**Bazos.** robots.txt disallows search on every Bazos domain, so search is never used. The adapter reads the newest
`pages` pages (20 ads each) of the matching category (for example `pc.bazos.cz/graficka/`) and filters locally.
The Bazos dataset can also seed price history (`python -m dh.seed bazos`).

**eBay (DE, PL).** The official Browse API with your keys: `bw-secret ebay#client_id` and `ebay#client_secret`
(read once per run: restart the agent after adding them).
eBay web pages are never fetched. eBay's terms ban AI shopping agents and scrapers since February 2026, so the API
is the only route. The API client is a documented exception to the web fetcher (it needs a POST token call and does
not serve robots.txt): it still sends the honest User-Agent, paces calls, backs off 60 s doubling to 1 h on 429 or
403, and disables itself for the session after 3 consecutive refusals. Active asks become comps. Marketplace
Insights (sold prices) needs the application and scope grant from eBay; without it the source disables itself with
`not_granted` and valuation relies on the ledger. Logged-in eBay Product Research is not implemented.

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

The spec also named Geizhals, Leboncoin and Wallapop as sites that block plain polite requests. None has an
adapter. They are out of scope for the same reason. Facebook Marketplace is out of scope: it needs a login and the
ban risk falls on the owner's account.

## Allegro.pl and OLX.pl

Both block polite polling (Allegro.pl uses DataDome; OLX uses CloudFront). The only legitimate route is the
platforms' own saved-search alert e-mails. The mailbox adapter reads those mails. It never fetches an Allegro, OLX
or eBay page, so Allegro and OLX listings are **never re-checked live** and cannot reach tier 1. They can reach tier 2.
The human acting on a deal opens the link and checks it by hand.

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
