# Owner to-do

Only you can do these. Work top to bottom: items 1 and 2 matter now, the rest unlock more sources and more
accurate numbers. Every command runs on the rig unless it says otherwise.

## 1. Restart the agent on the new code (5 minutes, do this first)

Your running unit has run no round since 21:40 on 29 October. It is still active, but at 21:48 a frozen file
changed under it (`evals/must_pass.jsonl`, from the last fix wave) and its rounds stopped. The code it runs
predates the "PAUSED" status line, so `.venv/bin/dh status` still says RUNNING. A restart puts it on the current
reviewed code (the final fix wave and the 30 October improvement wave, see `docs/VALIDATION.md`).

The restart also starts the log publisher against the log repo (item 4). Run the commands from that checkout:

```bash
cd ~/Desktop/github_repos/deal-hunter
.venv/bin/dh stop
git pull --ff-only origin feat/build
.venv/bin/pip install -e '.[dev]'      # no new dependencies this time; harmless to run
.venv/bin/dh start
.venv/bin/dh status                    # RUNNING, a new round within ~2 minutes, no PAUSED line
```

Once, after this restart, clear the comparables an older matcher got wrong (laptops and PCs read as bare cards,
water blocks; they otherwise stay comparables until re-evaluated, a gone listing for up to 90 days). It needs the
agent stopped, so do it between `stop` and `start` above, or later with a stop/start around it:

```bash
cd ~/Desktop/github_repos/deal-hunter && .venv/bin/dh recheck            # dry run: what would change, and which logged deals sit on such listings
cd ~/Desktop/github_repos/deal-hunter && .venv/bin/dh recheck --apply    # write it; the listings are re-evaluated with the LLM when next queued
```

If `.venv/bin/dh start` refuses because llama-swap runs another model, either wait for that workload to finish or map
the roles to the loaded models (docs/RUNNING.md, "Sharing the rig with another workload").

## 2. Subscribe to the push topic (2 minutes)

Install the **ntfy** app (Android or iOS) and subscribe, on the default server `ntfy.sh`, to the topic that
this prints (line `ntfy topic:`; it is kept in `config/local/notify.yaml`, which is not in git):

```bash
cd ~/Desktop/github_repos/deal-hunter && .venv/bin/dh status
```

Only tier-1 deals are pushed, plus one push if the rounds pause. Anyone who knows the topic can read the pushes.

Optional: rotate the topic. An earlier draft of this file (in the private repo's history) contained it; rotating
is optional, since the repo is private. To rotate: delete the `topic:` line from `config/local/notify.yaml`,
then `cd ~/Desktop/github_repos/deal-hunter && .venv/bin/dh stop`, `cd ~/Desktop/github_repos/deal-hunter && .venv/bin/dh start` (a new random topic is generated on first use), and subscribe to the new one shown by
`cd ~/Desktop/github_repos/deal-hunter && .venv/bin/dh status`.

## 3. Publish `main` on GitHub (2 minutes)

Only `feat/build` exists on `git@github.com:michal-koniu/deal-hunter.git`. To create `main` from it
(run this in any clone that has fetched `feat/build`):

```bash
cd ~/Desktop/github_repos/deal-hunter
git fetch origin
git push origin origin/feat/build:refs/heads/main
gh repo edit michal-koniu/deal-hunter --default-branch main   # optional: make main the default branch
```

Later, to bring `main` up to date with new work on `feat/build` (a fast-forward, no merge commit):

```bash
git fetch origin
git push origin origin/feat/build:refs/heads/main            # refused if main has commits feat/build lacks
```

Prefer a reviewed merge? Open a PR instead: `gh pr create --repo michal-koniu/deal-hunter --base main --head feat/build`.

## 4. The log repo: already configured, nothing to do

`git@github.com:michal-koniu/deal-hunter-log.git` exists and is empty. `config/publish.yaml` now points at it
(`remote`, plus `repo_web_url` so the "Card" button in a push opens the card on GitHub). After the restart in
item 1, the publisher clones it into `~/.local/share/deal-hunter/log`, creates the `log` branch itself and pushes
only `log`, every 30 minutes. Check with `cd ~/Desktop/github_repos/deal-hunter && .venv/bin/dh status` (`log repo: ... clone=True`). If you would rather keep the URL
out of the repository, delete the two lines from `config/publish.yaml` and put them in
`config/local/publish.yaml` instead (gitignored):

```yaml
remote: git@github.com:michal-koniu/deal-hunter-log.git
repo_web_url: https://github.com/michal-koniu/deal-hunter-log
```

To mark a deal from your phone, edit the card's `outcome:` block in the GitHub app. The next sync imports it.

## 5. The alert mailbox (gated: OLX.pl; also extra Allegro, Kleinanzeigen and eBay alerts), about 1 hour

OLX blocks polite automated requests, so the agent sees it only through e-mail alerts. Allegro.pl public
search is the official REST API when the application is verified (section 6b). Saved-search mail still
helps beside that API. The mailbox adapter never opens those websites.

1. Create a **dedicated** mailbox with IMAP over SSL (port 993). Not your personal one: the agent reads every
   unseen mail in the folder. Your provider must add an `Authentication-Results` header (Gmail, Fastmail and
   most big providers do). Without it every alert is rejected as unauthenticated.
2. Store the login in Bitwarden as item `dh-mail`, field `imap`, holding JSON:
   ```json
   {"host": "imap.example.com", "port": 993, "user": "alerts@example.com", "password": "...", "folder": "INBOX"}
   ```
   Check it with `bw-secret dh-mail#imap`.
3. Optional e-mail fallback for pushes: field `smtp` on the same item,
   `{"host": "...", "port": 587, "user": "...", "password": "...", "to": "you@example.com"}`.
4. Sign up with that address on **Allegro**, **OLX**, **Kleinanzeigen** and (optionally) **eBay**.
5. Create the saved searches below on each platform, with e-mail alerts at the most frequent setting, and the
   minimum price shown. They mirror the playbook queries (`playbook/items.yaml`); the minimum price keeps
   accessories and box-only ads out. Use "Used" condition where the platform offers it, category
   "Karty graficzne" / "Grafikkarten" / "Pamięć RAM" / "Dyski SSD" / "Komputery" where it applies.
   Allegro allows up to 300 followed searches; 42 searches x 4 platforms fits everywhere.

   | Search text | Min price PLN (Allegro, OLX, eBay.pl) | Min price EUR (Kleinanzeigen, eBay.de) |
   |---|---:|---:|
   | `rtx 3060` | 300 | 70 |
   | `rtx 3060 ti` | 300 | 70 |
   | `rtx 3070` | 300 | 70 |
   | `rtx 3070 ti` | 300 | 70 |
   | `rtx 3080` | 300 | 70 |
   | `rtx 3080 ti` | 300 | 70 |
   | `rtx 3090` | 1000 | 235 |
   | `rtx 3090 ti` | 1100 | 255 |
   | `rtx 4060` | 300 | 70 |
   | `rtx 4060 ti` | 300 | 70 |
   | `rtx 4070` | 300 | 70 |
   | `rtx 4070 super` | 300 | 70 |
   | `rtx 4070 ti` | 300 | 70 |
   | `rtx 4070 ti super` | 300 | 70 |
   | `rtx 4080` | 1500 | 350 |
   | `rtx 4080 super` | 1500 | 350 |
   | `rtx 4090` | 2500 | 585 |
   | `rtx 5060 ti` | 300 | 70 |
   | `rtx 5070` | 300 | 70 |
   | `rtx 5070 ti` | 300 | 70 |
   | `rtx 5080` | 1800 | 420 |
   | `rtx 5090` | 4000 | 940 |
   | `rx 6800` | 300 | 70 |
   | `rx 6800 xt` | 300 | 70 |
   | `rx 6900 xt` | 300 | 70 |
   | `rx 6950 xt` | 300 | 70 |
   | `rx 7800 xt` | 300 | 70 |
   | `rx 7900 gre` | 300 | 70 |
   | `rx 7900 xt` | 300 | 70 |
   | `rx 7900 xtx` | 1200 | 280 |
   | `rx 9070` | 300 | 70 |
   | `rx 9070 xt` | 300 | 70 |
   | `ddr4 32gb` | 80 | 15 |
   | `ddr4 64gb` | 80 | 15 |
   | `ddr5 32gb` | 80 | 15 |
   | `ddr5 64gb` | 80 | 15 |
   | `ddr5 96gb` | 80 | 15 |
   | `nvme 1tb` | 60 | 10 |
   | `nvme 2tb` | 60 | 10 |
   | `nvme 4tb` | 60 | 10 |
   | `komputer gamingowy` (Allegro, OLX only) | 1000 | - |
   | `gaming pc` (Kleinanzeigen, eBay.de only) | - | 235 |

   For RTX 30xx/40xx searches below 1000 PLN you may raise the minimum to about half the model's used price.
   It cuts noise without hiding real bargains. EUR minimums are the PLN ones at 4.25 PLN/EUR, rounded down.
6. Enable the adapter in `config/local/sources.yaml`:
   ```yaml
   adapters:
     mailbox: {enabled: true}
   ```
7. `cd ~/Desktop/github_repos/deal-hunter && .venv/bin/dh stop && .venv/bin/dh start` (secrets are read once per run). `cd ~/Desktop/github_repos/deal-hunter && .venv/bin/dh status` shows
   `no_credentials` if step 2 failed.
   If `alert_parse_empty` health events appear, the platform's real alert layout differs from the synthetic test
   fixtures: keep one such mail and send it to a developer.

## 6. eBay developer keys (gated: eBay DE and PL), about 30 minutes plus eBay's review

1. Sign in at <https://developer.ebay.com>, create an application and a **Production** keyset.
2. Store every application you have. First set: Bitwarden item `ebay`, fields `client_id` (App ID),
   `client_secret` (Cert ID) and `dev_id` (stored, never sent). Further sets are `ebay2`, `ebay3`, … up to
   `ebay8`, with the same fields. A missing `client_id` ends the list. A quota on one application switches
   to the next. Check with `bw-secret ebay#client_id` (prints the value: do not paste it into chat).
3. Apply for the **Marketplace Insights API** (sold prices) on **each** application. Until eBay grants it
   on every stored key, `cd ~/Desktop/github_repos/deal-hunter && .venv/bin/dh status` shows `ebay_insights`
   as disabled (`not_granted` only when every key lacks the scope, otherwise `no_keys`).
   Sold prices are the fastest way to reach tier 1 before the clears-at ledger matures.
4. `cd ~/Desktop/github_repos/deal-hunter && .venv/bin/dh stop && .venv/bin/dh start`.

## 6b. Allegro.pl REST keys (gated: public offer search)

1. In the Allegro developer apps page, copy the Client ID and Client Secret for the application whose
   User-Agent was validated. The shipped header is
   `deal-hunter/1.0 (+https://github.com/michalkonik/family-finance-planner)`. If you registered a different
   header, set `adapters.allegro.user_agent` to that exact string before the first call. A wrong header can
   get the key blocked. The header is not a secret.
2. Store item `allegro`, fields `client_id` and `client_secret`. Do not paste them into chat.
3. `cd ~/Desktop/github_repos/deal-hunter && .venv/bin/dh stop && .venv/bin/dh start`.
   Allegro has not been granting `/offers/listing` to new applications. Until they do, status shows
   `not_verified` and the website is not fetched. Saved-search mail (section 5) still works beside that.

## 7. The accountant (one meeting)

Take [`ACCOUNTANT-QUESTIONS.md`](ACCOUNTANT-QUESTIONS.md) (12 questions: PKD code, cash register, VAT-UE/OSS,
BDO/WEEE, margin-scheme bookkeeping, input VAT, shipping and customs bases, income tax and health contribution
under liniowy and ryczałt). Then update `config/economics.yaml` with the answers. It is a **frozen** file: edit it
only while the agent is stopped (`cd ~/Desktop/github_repos/deal-hunter && .venv/bin/dh stop`, edit, `.venv/bin/dh start`), otherwise the rounds pause.

## 8. Decisions still open (no deadline)

- Vinted: no adapter exists; off.
- Paid LLM APIs: off (`models.paid_api.monthly_cap_usd: 0`). Turn on only with a cap you accept.
- Let the agent run for many **watched** hours before judging it: tier 1 needs at least 12 sold comparables per
  product from the last 30 days, and the ledger needs 4-8 weeks of watched time (README, "Cold start").

## 9. Optional clean-up

The improvement wave of 30 October 2026 left a development clone at `~/Desktop/github_repos/deal-hunter-dev`
(unit name `deal-hunter-dev`, its own `var/`, validation data in `var/validation/`, web port 8712 and no log repo
in its `config/local/`, so it never collides with your agent on port 8711). Nothing runs from it. Delete
it when you no longer need the validation data:

```bash
gio trash ~/Desktop/github_repos/deal-hunter-dev
```
