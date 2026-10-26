# Operations

Day-to-day running of Deal Hunter on the rig. The command table is also in [`RUNNING.md`](RUNNING.md).

## Start and stop

The owner starts and stops the agent by hand. Nothing starts at boot.

```bash
dh start     # guards, then a transient systemd user unit named deal-hunter
dh status    # is it up, what is it doing
dh stop      # stop, close observation windows, release the models it loaded
```

`dh start` refuses, and prints why, when:
- free RAM is below `models.min_free_ram_gb` (12 GB): another workload is using the rig;
- llama-swap runs a model outside the configured roles: loading the role models would evict it;
- a foreground `dh round` is running.

If llama-swap cannot be reached, `dh start` only warns: rounds run on the rules matcher alone and the LLM breaker
opens. Before the first round the agent warms the role models with one 1-token request each (up to 300 s per
model); a failed warm-up is recorded as a `model_warmup` health event.

`dh stop` stops the unit, closes every observation window, calls llama-swap `/unload` only when nothing but role
models is loaded and none of them was loaded before `dh start`, then checks that nothing is left. It prints
`nothing left running` on success and `STILL RUNNING: ...` (exit 1) otherwise.

While the agent runs, the rig is dedicated to it. The only promise the owner has made is that no other heavy
process runs at the same time.

A single round in the foreground, for testing: `dh round --once [--dry] [--no-llm]`. `--dry` sends no push.
`--no-llm` uses rules only and does not touch llama-swap. It refuses while the service runs.

## Status

`dh status` (or `dh status --json`) shows: running or stopped, the unit, uptime, rounds total and since start,
watched hours, the last round, deals today by tier, tokens and cost today, blocked and disabled sources (last
24 h), llama-swap and its loaded models, the ntfy topic and the log repo (clone path, last commit, remote).

Read the deals with `dh deals [--tier N] [--limit N] [--json]`, `dh show <id>` (the card), `dh verify <id>`
(recompute the net from the stored evidence and re-check that the listing is live) and `dh books`.

The web page on port 8711 shows the same, with the Bought, Skipped and Sold buttons on each deal. Marks are the
agent's main source of verified outcomes: use them.

## Logs

The agent logs to the systemd journal:

```bash
journalctl --user -u deal-hunter -f          # follow
journalctl --user -u deal-hunter --since today
journalctl --user -u deal-hunter -p err      # errors only
```

Each round logs one summary line (sources, listings, candidates, LLM items, deals by tier, energy). A failed round
logs a traceback and a `round_failed` health event, and the next round runs normally. The same facts are in the
`health` and `rounds` tables and on the web pages `/health` and `/rounds`.

## Health

Open `/health` or run `dh status`. The events that need action:

| Event | Meaning | Action |
|---|---|---|
| `blocked` | A site answered 403, 429 or a challenge page | Nothing: the fetcher backs off from 60 s up to 1 h. See [On a block](#on-a-block) |
| `disabled` | A domain was disabled for the session after 3 consecutive blocks | Restart to retry later; or switch the source off |
| `no_keys`, `no_credentials`, `not_granted` | eBay keys, mailbox credentials or the Insights scope are missing | Add the secret (see [CONFIGURATION.md](CONFIGURATION.md#secrets)) or ignore: the source stays off |
| `unauthenticated_alert` | An alert mail failed the DKIM or SPF check and was skipped | Check the sender; this protects against forged "deals" |
| `alert_parse_empty` | A real alert mail parsed to zero offers and was left unread | The platform changed its mail format; see [SOURCES.md](SOURCES.md) |
| `sweep_timeout` | A source's sweep was cut after `sweep_timeout_s` | Usually a slow site; repeated cuts mean the source is unhealthy |
| `foreign_model` | Another model was loaded in llama-swap, so the round was rules-only | Free the model or remap the roles |
| `frozen_changed` | A frozen file changed while running; the rounds stopped | `dh stop` then `dh start` |
| `ntfy_failed`, `email_failed`, `push_expired` | Push delivery problems | Check the topic and network; pushes retry for 6 h, then expire |
| `no_clone`, `clone_failed`, `sync_error`, `bad_branch` | Log publishing problems | See [Log repo](#the-log-repo) |

The web page has no authentication. Keep port 8711 on a trusted LAN.

## On a block

A block means a site refused plain, honest, robots-respecting requests. **Never work around it.** No fingerprint
impersonation, stealth browsers, challenge solving or proxy rotation: those are excluded by design (spec 6.4).

1. Do nothing for a while. The fetcher backs off by itself (60 s, doubling to 1 h) and the bandit is not asked
   to retry a blocked domain (blocked and disabled domains are skipped when arms are planned).
2. After 3 consecutive blocks the domain is disabled until the next `dh start`. Restarting clears it.
3. If it blocks again, raise `fetch.min_interval_s` (more polite), or set the source to `enabled: false` in
   `config/local/sources.yaml` with a `reason`.
4. If the site now answers 403 or a challenge to every plain request, move to an official API, RSS or the alert
   mailbox, or drop the source and record why (see the dropped list in [`SOURCES.md`](SOURCES.md)).
5. You can test one source by hand without touching the real database: `dh probe --source <name> --query "rtx 4090"`.

## The log repo

Every `publish.interval_min` (30) minutes the publisher pulls `deal-hunter-log`, writes the deal cards (sanitised:
no seller data), `INDEX.md` (starred and tier-1 deals first), and daily and weekly summaries, commits, and pushes
to the `log` branch only. It also imports `outcome:` edits you made to a card's front matter in the GitHub app as
marks. A git or network failure is a health event, never a crash; local state is kept and the next cycle pushes
again.

This needs the private repo `deal-hunter-log`, which **you must create** (see the README checklist), with a `log`
branch, and `publish.remote` set. Without it, `dh status` shows `clone=False` and `/health` shows `no_clone`.

## Backups

The database `var/dh.db` is the source of truth: ledger history, comps, deals, marks and the bandit state. The
clears-at ledger takes weeks to build, so back it up.

```bash
# safe while the agent runs (SQLite online backup)
sqlite3 var/dh.db ".backup '/path/to/backup/dh-$(date +%F).db'"
```

If you copy the file instead, do it while the agent is stopped, or copy `dh.db`, `dh.db-wal` and `dh.db-shm`
together. Also keep `config/local/` (your overrides and the ntfy topic) and `playbook/items.yaml`. The cards under
`var/cards/` are regenerated from the database by `dh show`, and the log repo holds a sanitised copy.

To restore: `dh stop`, put the file at `var/dh.db`, delete any stale `dh.db-wal` and `dh.db-shm`, `dh start`.

## Retention

Deleted by the daily maintenance pass (at most every `scheduler.maintenance_hours`, 24 h):

| What | Kept | Setting |
|---|---|---|
| Raw page snapshots, `var/snapshots/` | 90 days | `scheduler.retention_days` |
| Saved alert mails (.eml) | 90 days | `scheduler.retention_days` |
| Photo hashes | 90 days | a constant in `dh/photos.py` (`WINDOW_DAYS`) |

Photos are never stored: the picture is downloaded, hashed and discarded, and only the 64-bit hash is kept (for
the photo-reuse scam check). The `retention.photos_days` and `retention.snapshots_days` keys in `sources.yaml`
exist but no code reads them.

Not deleted: deal cards, evidence screenshots in `var/screenshots/`, and the database rows. Delete old
screenshots by hand if the disk fills. Seller names and phone numbers are never stored (only a salted hash of the
seller ID, account age and ratings), and never reach the log repo.

## Upgrades

1. `dh stop`. Do not pull code while the agent runs: a changed frozen file stops the rounds.
2. Back up `var/dh.db`.
3. `git pull`, then `.venv/bin/pip install -e '.[dev]'`.
4. `.venv/bin/pytest` (add `--live` only if you want network tests).
5. Read the diff of `config/*.yaml` against `config/local/*.yaml`: new keys appear in the shipped files, and your
   local overrides still win.
6. `dh round --once --dry`, then `dh start`.

Schema changes are additive (`create table if not exists`, `add column`) and run on first open, so no manual
migration is needed.

## Recovery after a crash

If the process or the rig dies, the agent leaves no unsafe state:

- **Observation windows.** The ledger infers a disappearance only inside one watched window. A crash leaves a
  window open; the next `dh start` (startup recovery), `dh stop`, or `dh round` closes it at the last observation
  (`dh start` also records a `recovery` health event). Listings that disappeared during the gap are *not* counted as sold.
- **Pushes.** Queued pushes are in the database and retried each round for up to 6 hours. A tier-1 deal is
  pushed at most once.
- **Alert mail.** Mails are marked read only after their listings are saved, so a crash re-delivers them (the
  save is idempotent).
- **A stale unit.** `dh start` runs `systemctl --user reset-failed deal-hunter` first. If `dh status` shows
  `processes` after a stop, `dh stop` again, then check `pgrep -af "dh.runtime serve"`.
- **A stale foreground-round lock** (`var/run/round.pid`) is ignored when its process is gone.
- **A damaged database**: restore the latest backup (see above). The log repo keeps sanitised deal cards, not the
  ledger, so a backup is the only way to keep the price history.
