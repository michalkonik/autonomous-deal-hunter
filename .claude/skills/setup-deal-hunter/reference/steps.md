# /setup-deal-hunter - expanded notes

## Contents
- What the agent actually needs (table)
- config/local/ shapes (models, pickup, publish, web, scheduler, notify, sources) + secret formats
- Deciding the model mapping (step 3)
- What a healthy start looks like (step 7) + health-event glossary
- Cold start (what to promise the owner)
- If the owner wants it gone
- Dev clone on the same host

Everything here is fact, verified against this repo before publishing. When it disagrees with `docs/`, the docs win -
and say so.

## What the agent actually needs (checked)

| Need | Required? | What happens without it |
|---|---|---|
| Linux with a usable systemd user session | yes | `dh start` uses `systemd-run --user`; without a user bus it cannot start |
| Python 3.12+ (`requires-python: >=3.12`) | yes | `pip install` fails on 3.10/3.11 |
| ~2 GB disk + a few GB for `var/` (SQLite DB, screenshots, snapshots) | yes | disk-full breaks a round |
| llama-swap (or any OpenAI-compatible endpoint) on `localhost:8080` | for LLM features | rounds run on the rules matcher only; `dh round --once --dry --no-llm` is the explicit no-model mode |
| >= 12 GB free RAM at start (`models.min_free_ram_gb`) | yes, checked by `dh start` | `dh start` refuses and explains |
| `/usr/bin/google-chrome` | optional | tier-1 evidence screenshots are skipped (`dh/verify.py` hardcodes that path; failure returns False, never an error) |
| `rocm-smi` | optional | the books count `base_w` only (no per-round GPU power) |
| `bw-secret` on PATH | only for optional secrets | eBay disables itself + one `no_keys` health event. The mailbox is quieter: `adapters.mailbox.enabled: false` (the shipped default) means NO adapter is built, so `dh status` and `/health` stay completely clean and nothing hints that Allegro.pl and OLX.pl are dark. `setup.sh access` is what proves it |
| ntfy app on the owner's phone | optional | pushes go nowhere until subscribed; tier-1 pushes are queued and retried for 6 h |
| a private log repo on GitHub | optional | cards stay local in `var/cards/`, a `no_clone` health event is recorded |

## config/local/ shapes (deep-merged over the shipped file, gitignored)

```yaml
# config/local/models.yaml - use names exactly as listed by: curl -s localhost:8080/running
roles:
  worker: qwen3.6-35b-a3b          # extraction, part-out steps (parallel: models.worker.parallel)
  senior: qwen3.8-flash-next       # triage review, round reflection
  advisor: qwen3.8-flash-next      # playbook proposals
```
With a mapping like this the models were loaded by someone else, so `dh stop` leaves them loaded (it only calls
`/unload` when the roles loaded nothing at `dh start`). Deleting the file returns to the shipped `pedro` roles.

```yaml
# config/local/pickup.yaml - pickup-only listings are valued only inside max_km of origin
max_km: 60
origin: {name: "Warsaw, city centre", lat: 52.2297, lon: 21.0122}
pln_per_km: 1.2
pln_per_trip: 40
```

```yaml
# config/local/publish.yaml - your private log repo (create it EMPTY, any default branch)
remote: git@github.com:<you>/deal-hunter-log.git
repo_web_url: https://github.com/<you>/deal-hunter-log   # optional: adds the "Card" button to pushes
# or, to keep cards local only:
remote: null
```

```yaml
# config/local/web.yaml
port: 8711
allowed_hosts: [192.168.0.39, rig.local]   # host names accepted on POST (the Bought/Skipped/Sold buttons)
```

```yaml
# config/local/scheduler.yaml - only for a fast first test round; remove afterwards
round_interval_s: 120
```

```yaml
# config/local/notify.yaml - the ntfy topic lives here once generated; you may set quiet hours
ntfy:
  quiet_hours: {start: "23:00", end: "07:00", tz: Europe/Warsaw}
```

```yaml
# config/local/sources.yaml - e.g. after storing dh-mail#imap
adapters:
  mailbox: {enabled: true}
```

Secret formats (the agent parses these exactly - see `docs/CONFIGURATION.md#secrets`):

| Reference | Content |
|---|---|
| `ebay#client_id` | eBay App ID |
| `ebay#client_secret` | eBay Cert ID |
| `dh-mail#imap` | `{"host":"imap.example.com","port":993,"user":"alerts@example.com","password":"...","folder":"INBOX"}` |
| `dh-mail#smtp` | `{"host":"smtp.example.com","port":587,"user":"alerts@example.com","password":"...","to":"you@example.com"}` |
| `paid-llm#api_key` | only if `paid_api.enabled` and `models.paid_api.api_key_secret` points at it |

After storing `ebay#*` or `dh-mail#imap`: `dh stop && dh start` (they are read once per process).

## Deciding the model mapping (step 3)

Read both sides first: `curl -s localhost:8080/running` (what is loaded) and `config/models.yaml` +
`config/local/models.yaml` (what the roles want). Then:

1. **Nothing loaded, or only role models** - keep defaults; `dh start` loads the role models and `dh stop`
   releases them (only when it loaded them and nothing else is in play).
2. **A foreign model is loaded** (a chat model, another agent): `dh start` will refuse. Offer to map the roles to
   the loaded models (fastest, no eviction, `dh stop` leaves them loaded), or to wait until the other workload is
   done. On a rig, mapping is almost always right.
3. **No endpoint at all**: rounds still run (rules only): the matcher, valuation, tiering and the ledger work;
   unclear titles are not resolved and no triage review happens, so expect fewer and blunter deals. Wire an
   endpoint later by setting `base_url` in `config/local/models.yaml` (any OpenAI-compatible server; llama-swap
   needs no key).
4. Paid APIs are **off by default and stay off** unless the owner asks (`paid_api.monthly_cap_usd: 0` means no
   paid call is ever made). Never turn them on by accident.

## What a healthy start looks like (step 7)

```
deal-hunter: RUNNING (unit deal-hunter, state ..., pid ..., up 1 min)
  rounds: 1 total, 1 since start; watched hours 0.0
  ...
  ntfy topic: <24 chars>
  log repo: ... clone=True/False ...
```
- `PAUSED: frozen file changed (...)` means a frozen file (`config/rules.yaml`, `config/economics.yaml`, `data/`)
  changed while the agent ran. Undo it, or accept with `dh stop && dh start`.
- Health kinds that mean something: `no_credentials` (mailbox), `no_keys` (eBay), `no_clone` (log repo),
  `blocked`/`disabled` (a site refused polite requests; backoff or session-disabled), `alert_parse_empty` (a real
  platform alert did not match the parser - keep the .eml, report it), `improve_error` (never stops rounds).
- Port 8711 has no login: LAN-readable and LAN-writable. Never expose it beyond a trusted network.

## Cold start (what to promise the owner)

Tier 1 needs >= 12 effective *sold* comparables per product, younger than 30 days, plus a live re-check - active
asks never count. The clears-at ledger needs 4-8 weeks of **watched** time (time the agent was running). Until
then: few or no tier-1 pushes, tiers 2-4 carry the value, and eBay sold data (Marketplace Insights scope) is the
fast shortcut. `python -m dh.seed bazos --db var/dh.db` seeds history that ends in 2024 - it does not feed
current estimates.

## If the owner wants it gone

```bash
cd <repo> && .venv/bin/dh stop
gio trash <repo>                       # checkout, config/local, DB, venv
rm -f ~/.local/bin/bw-secret           # only if the setup skill installed it
# ~/.config/deal-hunter/secrets and ~/.local/share/deal-hunter/log are the only things outside the checkout
```
Nothing autostarts: after `dh stop` nothing runs, no timer or cron exists.

## Dev clone on the same host

A second checkout needs its own unit and port, or it will refuse (the owner's unit is active) and `dh stop` would
stop the owner's agent:
```yaml
# config/local/runtime.yaml
unit_name: deal-hunter-dev
# config/local/web.yaml
port: 8712
# config/local/publish.yaml
remote: null
log_dir: ~/.local/share/deal-hunter/log-dev
```
(or `DH_UNIT_NAME=deal-hunter-dev`). Its `var/` is already per-checkout.
