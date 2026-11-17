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
| >= 12 GB free RAM at start (`models.min_free_ram_gb`) | yes, while any role is local | `dh start` refuses and explains. The floor is skipped only when every role is on a remote provider, because nothing is loaded here |
| `/usr/bin/google-chrome` | optional | tier-1 evidence screenshots are skipped (`dh/verify.py` hardcodes that path; failure returns False, never an error) |
| `rocm-smi` | optional | the books count `base_w` only (no per-round GPU power) |
| `bw-secret` on PATH | only for optional secrets | eBay and Allegro each record one `no_keys` health event. The mailbox is quieter: `adapters.mailbox.enabled: false` (the shipped default) means NO adapter is built, so `dh status` and `/health` stay completely clean and nothing hints that OLX.pl is dark. `setup.sh access` is what proves it |
| ntfy app on the owner's phone | optional | pushes go nowhere until subscribed; tier-1 pushes are queued and retried for 6 h |
| a private log repo on GitHub | optional | cards stay local in `var/cards/`, a `no_clone` health event is recorded |

## config/local/ shapes (deep-merged over the shipped file, gitignored)

`config/local/models.yaml` is written only after the owner answers step 3. The yaml to write, and what `dh start`
loads or leaves alone, is in "Deciding the model mapping" below. Do not copy model names from anywhere else.
Deleting that file returns to the shipped roles.

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
| `ebay#client_id` | eBay App ID (first application) |
| `ebay#client_secret` | eBay Cert ID (first application) |
| `ebay#dev_id` | eBay Dev ID. Stored, never sent |
| `ebay2#client_id`, `ebay2#client_secret`, `ebay2#dev_id` | second application. Then `ebay3` … `ebay8`. A missing `client_id` ends the list. A quota on one switches to the next |
| `allegro#client_id` | Allegro REST Client ID |
| `allegro#client_secret` | Allegro REST Client Secret |
| `dh-mail#imap` | `{"host":"imap.example.com","port":993,"user":"alerts@example.com","password":"...","folder":"INBOX"}` |
| `dh-mail#smtp` | `{"host":"smtp.example.com","port":587,"user":"alerts@example.com","password":"...","to":"you@example.com"}` |
| `paid-llm#api_key` | only if `paid_api.enabled` and `models.paid_api.api_key_secret` points at it |
| `grok#api_key` | xAI key, when a role's provider is `grok` |
| `gemini#api_key` | Google AI key, when a role's provider is `gemini` |
| `claude#api_key` | Anthropic key, when a role's provider is `claude` |
| `openai#api_key` | OpenAI key, when a role's provider is `chatgpt` |

After storing `ebay#*`, `ebay2#*`, `allegro#*` or `dh-mail#imap`: `dh stop && dh start` (they are read once per process).

Allegro calls send this User-Agent unless `adapters.allegro.user_agent` overrides it in config (not a secret):
`deal-hunter/1.0 (+https://github.com/michalkonik/family-finance-planner)`. A different registered header must be
set before the first call. `/offers/listing` may return `not_verified` until Allegro grants the application.
The website is never fetched. OLX stays mailbox-only.

## Deciding the model mapping (step 3)

Ask first. The shipped setup is local llama-swap, and that is the answer to recommend. A commercial model is
only for the roles the owner names. Read `curl -s localhost:8080/running` and `config/models.yaml` so the
question can say what is loaded, then wait for the answer.

Local yaml, only when they want different local names than the shipped file (names from
`curl -s localhost:8080/v1/models`, not invented):

```yaml
# config/local/models.yaml
roles:
  worker: gemma-4-26b-a4b-pool
  senior: qwen3.8-flash-next-uncensored
  advisor: qwen3.8-flash-next-uncensored
```

`dh start` loads those models. `dh stop` unloads them only when this run loaded them and nothing else is loaded.

Grok device login speaks the Responses API. Gemini, Claude, ChatGPT, and a Grok console key speak chat completions. One block per provider, in the same file. The
cap and the two prices in the example are `0` on purpose: replace them with the numbers the owner accepted.
A copied `0` cap refuses every call.

| Provider | base_url | key reference |
|---|---|---|
| Grok, device login (`dh xai-login`, the same approval OpenClaw uses) | `https://cli-chat-proxy.grok.com/v1` | none. `auth: xai_oauth`, `wire: responses`. Session file `~/.config/deal-hunter/xai-oauth.json`. Bills the Grok subscription |
| Grok, existing `grok login` only | `https://cli-chat-proxy.grok.com/v1` | none. `auth: grok_session`, `wire: responses` |
| Grok, console API key | `https://api.x.ai/v1` | `grok#api_key` |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini#api_key` |
| Claude | `https://api.anthropic.com/v1` | `claude#api_key` |
| ChatGPT | `https://api.openai.com/v1` | `openai#api_key` |

```yaml
# config/local/models.yaml - Grok via dh xai-login. No api_key_secret.
providers:
  grok:
    base_url: https://cli-chat-proxy.grok.com/v1
    auth: xai_oauth
    wire: responses
    monthly_cap_usd: 0          # replace with the USD cap the owner accepted; 0 leaves the provider off
    usd_per_mtok_in: 2
    usd_per_mtok_out: 6
    roles: {senior: grok-4.7, advisor: grok-4.7}
```

Console API key, only when the owner already has one. Worker stays local in this example:

```yaml
providers:
  grok:
    base_url: https://api.x.ai/v1
    api_key_secret: grok#api_key
    monthly_cap_usd: 0          # replace with the USD cap the owner accepted; 0 leaves the provider off
    usd_per_mtok_in: 0          # replace with the price-page number you showed the owner
    usd_per_mtok_out: 0
    roles: {senior: grok-4.7, advisor: grok-4.7}   # replace with ids from that provider's model list
```

Browser login, when the owner already ran `grok login` and wants that session instead of a console key.
This bills the Grok subscription. The cap is still a local safety stop, priced at the catalog rates:

```yaml
providers:
  grok:
    base_url: https://cli-chat-proxy.grok.com/v1
    auth: grok_session
    wire: responses
    monthly_cap_usd: 0          # replace with the USD cap the owner accepted; 0 leaves the provider off
    usd_per_mtok_in: 2
    usd_per_mtok_out: 6
    roles: {senior: grok-4.7, advisor: grok-4.7}
```

A second provider is another key under `providers` (for example `claude:` with `base_url` and `claude#api_key`).
Do not put the same role on two providers: startup refuses. A role that is not listed stays on llama-swap.
When every role is remote, startup does not load llama-swap and does not check the 12 GB floor.

1. **Owner chose local, and nothing foreign is loaded** - no provider block. `dh start` loads the role models.
2. **Owner chose local, and a foreign model is loaded** - `dh start` will refuse. Ask: map the local roles to
   the loaded names, or wait. Do not unload the other workload.
3. **No endpoint at all, and they declined a commercial provider** - rounds still run on the rules matcher.
   Unclear titles are not resolved. `dh round --once --dry --no-llm` is the explicit no-model command.
4. **Owner chose a commercial provider** - write only the roles, cap and prices they accepted. For Grok
   device login, run `dh xai-login` and write `auth: xai_oauth` with no key. For every other provider,
   `put_secret.sh` stores the key first. Never enable a provider they did not ask for.

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
- Health kinds that mean something: `no_credentials` (mailbox), `no_keys` (eBay or Allegro), `not_verified`
  (Allegro has not granted `/offers/listing`), `quota` (one eBay key hit a limit and another was used),
  `not_granted` (every stored eBay key lacks Marketplace Insights), `no_clone` (log repo),
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
rm -f ~/.config/deal-hunter/xai-oauth.json   # the device login, only if that session should go too
# ~/.config/deal-hunter/secrets, xai-oauth.json and ~/.local/share/deal-hunter/log are the files outside the checkout
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
