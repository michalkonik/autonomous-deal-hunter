# Adding a source

1. Prefer, in this order: an official API you hold keys for, an RSS/Atom feed, the platform's saved-search alert mailbox, and only then plain polite HTML.
2. Subclass `dh.sources.base.Adapter` (`search`, `parse_detail`), register it in `dh/sources/__init__.py` (`_build`, `ADAPTER_NAMES`) and add `adapters.<name>.enabled` to `config/sources.yaml`.
3. All HTTP goes through `dh.fetch.Fetcher` (honest User-Agent, robots.txt, pacing, block backoff). Never impersonate a browser, rotate proxies or solve challenges. A site that answers 403/429 or a challenge page to a plain request is dropped (record why next to its `sources.yaml` entry), not worked around.
4. Capture fixtures on the rig (`tests/fixtures/capture.py`), parse them in a pure function, and test prices in every currency format the site uses (price <= 0 is skipped).
5. Store sellers only as `base.seller_hash(...)`; never names or phones.

## Exception: official, keyed APIs (eBay Browse)

`dh/sources/ebay.py` does not use `Fetcher`, because the OAuth token call is a POST and the API answers unauthenticated GETs with a 403 error page that the web Fetcher would read as a bot block. It uses `ApiClient`, which still: sends the honest User-Agent; paces calls per host (`min_interval_s`); backs off 60 s doubling (cap 1 h) on 429/403; and disables itself for the session after 3 consecutive refusals, recording health events. It skips robots.txt only because it is the documented, keyed API (api.ebay.com serves no robots.txt). eBay web pages are never fetched. Without `bw-secret ebay#client_id` / `ebay#client_secret` the adapter is disabled and records one `no_keys` health event.

## Adapter kinds added in M1 task 10

- `rss.RssAdapter(name, url, country, currency)`: Pepper.pl and hotukdeals "hot" feeds; price from the title, else the `pepper:merchant` price.
- `retail.RetailWatch`: `retail.watch` list in `sources.yaml`; JSON-LD `offers.price`; emits on a >= 10% drop or below the key's clears-at P25 and writes `retail_new` comps.
