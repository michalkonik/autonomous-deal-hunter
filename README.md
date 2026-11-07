# Deal Hunter

Deal Hunter is an autonomous agent that runs on the owner's local LLM rig, while the owner has switched it on, and
looks for **electronics that can be resold at a profit**: GPUs first, then RAM, SSDs and gaming PCs to part out. It
finds listings, values them against real clearing prices, computes the full net profit for a Polish VAT-registered
sole trader (JDG), screens for scams, and writes an evidence card for every deal that clears its floor. Tier-1
deals are pushed to the owner's phone. Humans do the buying, shipping and reselling: the agent never buys, bids,
messages sellers or lists items.

Precision matters more than volume. One false tier-1 push costs more trust than five missed deals.

## What makes it different

The research in [`docs/research/`](docs/research/) found no public tool that does this combination.
Existing tools are US Facebook-Marketplace alerting apps, Amazon arbitrage tools, new-price comparison sites and
single-platform bots. Deal Hunter's edge:

- **A clears-at ledger.** It records every tracked listing's first price, every price cut and the moment it
  disappears. A listing that vanishes within 72 h at or below the median ask counts as "probably sold". Only
  *watched* time counts: a listing that disappears while the agent is stopped is never counted. The ledger needs
  4-8 weeks of watched time to mature (see [Cold start](#cold-start)).
- **EU long-tail coverage.** Sources that nobody else watches: Allegro Lokalnie, sprzedajemy.pl, Kleinanzeigen,
  HardverApro (HU), Bazos (CZ, SK), Marktplaats (NL), eBay DE/PL through the official API, Pepper.pl and
  hotukdeals feeds, and Polish retail shops for price errors. Allegro.pl and OLX come through an alert mailbox.
- **PL JDG economics.** The net calculation covers margin-scheme VAT, import VAT and duty, the 2026 EU
  consignment and handling fees, platform fees, FX spread, returns reserve (2-year B2C warranty), a scam/DOA
  haircut and income tax. Every figure on a card shows its inputs. See [`docs/ECONOMICS.md`](docs/ECONOMICS.md).
- **Scam screening.** Hard excludes (box only, for parts, off-platform payment), a logistic scam score, photo
  reuse detection by perceptual hash, and a "how to buy safely" checklist on each card.
- **Gated self-improvement.** The agent may change only its playbook, one typed change at a time, behind frozen
  gates, and it stops changing when it works. See [`docs/SELF-IMPROVEMENT.md`](docs/SELF-IMPROVEMENT.md) and the
  [status note](docs/ARCHITECTURE.md#what-is-and-is-not-wired-yet) on what is wired into the running agent.

**Fetching etiquette (frozen).** No bot-check circumvention of any kind: no TLS or browser fingerprint
impersonation, no stealth browsers, no challenge solving, no proxy rotation. Requests are plain, identify the agent
honestly and respect robots.txt. Sites that block polite requests are used only through official APIs, RSS or the
alert mailbox, or are dropped. See [`docs/SOURCES.md`](docs/SOURCES.md).

## Quick start

Requirements: Linux with systemd user sessions, Python 3.12, a llama-swap endpoint on `localhost:8080` serving the
role models (or use `--no-llm`), `bw-secret` for secrets, and optionally `/usr/bin/google-chrome` for evidence
screenshots and `rocm-smi` for energy accounting.

```bash
git clone <your deal-hunter repo> && cd deal-hunter
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
export PATH="$PWD/.venv/bin:$PATH"          # puts `dh` on the PATH

dh round --once --dry                        # one foreground round, no push (needs models, or add --no-llm)
dh deals                                     # list what it found
dh start                                     # start the agent as the systemd user unit `deal-hunter`
dh status                                    # running?, rounds, health, ntfy topic, log repo, improvement loop
dh playbook                                  # active playbook; --pending, --rollback N, --maintenance on|off ...
dh eval                                      # must-pass and golden scores of the active playbook
dh report --weekly                           # the weekly report
dh stop                                      # stop everything, release the models it loaded
```

- The web page is on **http://&lt;rig&gt;:8711** while the agent runs (`config/web.yaml`). Deals, deal cards with
  Bought / Skipped / Sold buttons, rounds, playbook, books and health.
- Push notifications: install the **ntfy** app and subscribe to the topic that `dh status` prints (a random
  24-character topic, generated on first use and kept in `config/local/notify.yaml`). A tier-1 push is titled
  `T1 ★ +$640 RTX 4090 24GB · OLX Kraków`; its body has the buy price, the exit range (P25-median and channel),
  the conservative and expected net and the confidence. Tapping it opens the listing; the "Card" button opens the
  deal card in the log repo, and is left out when `publish.repo_web_url` is not set.
- There is no autostart: no boot service, timer or cron. The owner starts and stops the agent by hand.
- `dh start` refuses, and says why, when free RAM is below `models.min_free_ram_gb` (12 GB) or llama-swap runs a
  model outside the configured roles. It never evicts another workload.
- Run the tests with `.venv/bin/pytest`. Tests that touch the network are skipped unless you pass `--live`.

Daily use is in [`docs/OPERATIONS.md`](docs/OPERATIONS.md); the command table is in
[`docs/RUNNING.md`](docs/RUNNING.md).

## Cold start

Tier 1 needs at least 12 effective *sold* comparables (more for larger capital) younger than 30 days, plus a live
re-check. Active asking prices never count towards that. Until the ledger has matured, or eBay sold data is
available, expect few or no tier-1 pushes. Tiers 2-3 carry the early value. Run the agent for many watched hours
before judging it. You can seed price history from the public Bazos dataset
(`python -m dh.seed bazos --db var/dh.db`), but that data ends in 2024 and does not feed current estimates.

## Owner set-up checklist

Spec section 15, updated for what was built. Items marked **gated** switch a feature on; without them the feature
stays off and the rest of the agent still works.

1. **Create the private GitHub log repo `deal-hunter-log` (gated: log publishing).** The automated creation was
   denied by the permission system, so you must create it. An empty repo, or one with only `main`, is fine: the
   publisher creates the `log` branch itself on its first sync and pushes only `log`, never `main`. Set the clone
   URL in `config/local/publish.yaml`:
   ```yaml
   remote: git@github.com:<you>/deal-hunter-log.git
   repo_web_url: https://github.com/<you>/deal-hunter-log   # optional: makes push links point at the card
   ```
   Without a clone and a remote, the publisher records a `no_clone` health event and the agent keeps working.
2. **Create the private repo `deal-hunter`** for this code, if you have not already.
3. **Alert mailbox (gated: Allegro.pl and OLX; also Kleinanzeigen and eBay alerts).** Create a dedicated mailbox,
   store its IMAP login as `bw-secret dh-mail#imap` (JSON), save narrow searches on the platforms with that
   address, and enable the adapter. Full steps in [`docs/SOURCES.md`](docs/SOURCES.md#alert-mailbox-set-up).
4. **eBay developer keys (gated: eBay DE/PL).** Store `bw-secret ebay#client_id` and `ebay#client_secret`. Apply
   for the Marketplace Insights scope to get sold prices; without it eBay supplies active asks only.
5. **Email fallback for pushes (optional).** `bw-secret dh-mail#smtp` (JSON). Without it, a failed ntfy push is
   queued and retried for up to 6 hours.
6. **Install ntfy** and subscribe to the topic from `dh status`.
7. **Take [`ACCOUNTANT-QUESTIONS.md`](ACCOUNTANT-QUESTIONS.md) to your accountant**, then update
   `config/economics.yaml` (frozen layer: edit it only while the agent is stopped).
8. **Check the model roles** in `config/models.yaml` against `curl -s localhost:8080/running`, or map them to
   already-loaded models in `config/local/models.yaml` ([`docs/CONFIGURATION.md`](docs/CONFIGURATION.md#modelsyaml)).
9. **Decisions still open.** Vinted (no adapter exists, off), logged-in eBay Product Research (not implemented),
   and whether to allow paid LLM APIs (off; `models.paid_api.monthly_cap_usd` is 0, so no paid call is ever made).

## Documentation

| Document | What it is for |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Components, data flow, the round pipeline, the frozen layer, the store, the process model |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | Every config file and key, local overrides, secrets |
| [`docs/TUNING.md`](docs/TUNING.md) | Which knob moves what, how to read the scoreboard, symptoms and fixes |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Start, stop, logs, health, backups, blocks, retention, upgrades, crash recovery |
| [`docs/RUNNING.md`](docs/RUNNING.md) | Command table and sharing the rig |
| [`docs/SOURCES.md`](docs/SOURCES.md) | Every source: access, robots decision, status; alert mailbox set-up |
| [`docs/ADDING-A-SOURCE.md`](docs/ADDING-A-SOURCE.md) | Writing an adapter |
| [`docs/ECONOMICS.md`](docs/ECONOMICS.md) | The net-profit formula with worked examples |
| [`docs/SELF-IMPROVEMENT.md`](docs/SELF-IMPROVEMENT.md) | The gated change loop, how to read a proposal, how to overrule it |
| [`docs/LEARNING.md`](docs/LEARNING.md) | What each subsystem teaches about building autonomous agents |
| [`docs/research/`](docs/research/) | The two research reports behind the design |
| [`ACCOUNTANT-QUESTIONS.md`](ACCOUNTANT-QUESTIONS.md) | Open tax and legal questions |
| [`docs/superpowers/specs/2026-10-29-deal-hunter-design.md`](docs/superpowers/specs/2026-10-29-deal-hunter-design.md) | The design spec |
