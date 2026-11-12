---
name: setup-deal-hunter
description: Set up Deal Hunter end to end on this machine after cloning the repo - install, secrets, model mapping, notifications, log repo, systemd unit, first live round, verification. Use when the user asks to set up, install, configure, onboard or "run" Deal Hunter / autonomous-deal-hunter, or clones this repo and wants it working. Only manual step: cloning. Input - none (it interviews the owner for choices).
---

# /setup-deal-hunter

**Goal:** from a bare clone to a running agent the owner can trust, with every secret stored safely and every
claim verified by a command, not assumed.

The owner's only manual step is `git clone`. Everything else you do with your shell tools. Ask the owner only
for *decisions* (which models, optional sources, where the log repo lives) and for secret *values*, securely
(step 4). Never skip a verification; never claim success without the printed evidence.

Paths: `R` = this repo's root (the directory containing `pyproject.toml` - use the directory you were invoked
from; if unsure, `pwd` and confirm it contains `dh/` and `pyproject.toml`). `S` = `$R/.claude/skills/setup-deal-hunter`.

Copy this checklist into your reply and keep it updated as you work:

```
Setup progress:
- [ ] 1 Preflight        - [ ] 6 Push notifications
- [ ] 2 Install + tests  - [ ] 7 Start and verify
- [ ] 3 Model mapping    - [ ] 8 (optional) supervised round
- [ ] 4 Access + secrets - [ ] 9 Owner-only items
- [ ] 5 Owner settings   - [ ] 10 Report
```

Read now (short, they shape your questions): `$R/README.md`, `$R/OWNER-TODO.md`.
Read on demand, not before: `$R/docs/CONFIGURATION.md` (every key, before step 5), `$R/docs/RUNNING.md`
(before step 7), `$R/docs/SOURCES.md` (step 9 mailbox), and `reference/steps.md` here (expanded facts per step -
the one file written for you).

## Rules

1. **Do not edit the agent's code or its frozen files.** Configuration goes in `config/local/` (gitignored,
   deep-merged over the shipped file). `config/rules.yaml`, `config/economics.yaml`, `data/*` are frozen:
   edit only while the agent is stopped, and only if the owner asks.
2. **Secrets never appear in your context, the shell history, a log or a commit.** Store them through
   `bash $S/scripts/put_secret.sh item#field`, which prompts the owner on their own terminal (`read -rs`) so
   the value never reaches the transcript. If no terminal is available to that command (headless agent), ask the
   owner to run that one command themselves, or warn them that pasting the value into the chat makes it visible
   in the transcript before you use it: `printf '%s' "$VALUE" | bash $S/scripts/put_secret.sh item#field --stdin`.
   Never `cat` a secrets file, never echo a value back.
3. **Never evict another workload.** `dh start` refuses instead; treat a refusal as information, not an error to
   force through. Check `free -g` yourself too - on a rig, RAM is the binding limit, not disk.
4. Long commands (a first round, the test suite) run in the background with the tool's background mode or with
   `nohup ... &`, then poll; do not let a tool timeout end the run.
5. The agent never buys anything and needs no autostart: no boot service, no timer, no cron. If the owner wants
   autostart, that is a separate decision - do not add it silently.

## Steps

Work through them in order. Before the first one, print the plan (steps 1-10, with what you will ask for) so the
owner knows what is coming, and keep a visible task list of the steps.

### 1. Preflight
`bash $S/scripts/setup.sh check` (run from anywhere; it finds the repo). It prints `KEY=VALUE` lines: Python
version, whether a systemd user session is usable, RAM, disk, llama-swap reachability and the models it runs,
whether `bw-secret` exists, Chrome for screenshots. Fix blockers (FAIL lines) before continuing. If it says
`user-bus=exported`, the shell lacks the D-Bus environment: export `XDG_RUNTIME_DIR=/run/user/$(id -u)` and
`DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus` in every later shell command and tell the owner.
Report anything you had to install and get a yes first for third-party packages.

### 2. Install
`bash $S/scripts/setup.sh install`. Creates `.venv`, installs the package (`dh` entry point), checks imports,
prints the version. Then run the test suite once, in the background:
`.venv/bin/pytest -q` (network tests are skipped by default). Report passed/failed; a failure here means the
environment is wrong, not the agent - fix it before going on.

### 3. Which models do the LLM jobs
`bash $S/scripts/setup.sh models` shows what llama-swap serves and what `config/models.yaml` wants.
- The roles match what is loaded or loadable and RAM allows it: keep the defaults.
- Something else is loaded (a chat model, another agent): map the roles to it in `config/local/models.yaml`
  (see `reference/steps.md` for the file shape and for how to phrase the choice). `dh stop` then leaves those
  models loaded.
- No llama-swap at all: ask whether the owner wants LLM features now (they can point `base_url` at any
  OpenAI-compatible endpoint) or start rules-only (`dh round --once --dry --no-llm`) and wire models later.
Never guess a model name: use exactly what `curl -s localhost:8080/running` lists.

### 4. Access: every source the agent cannot see yet (ask about ALL of them)
**A disabled source is invisible in `dh status`** - with no credentials the mailbox adapter is not even built, so
no health event is recorded and a clean-looking status proves nothing about Allegro or OLX. Never assume the owner
does not have a credential, and never move on silently: run the matrix, show it, ask once per gap, record the answer.

```bash
bash $S/scripts/setup.sh access   # one line per source: live=yes|no, via=http|rss|imap|api, and the reason why not
```

Ask the owner about each `access-gap` line, offering the concrete choice (this is the step a new owner is judged
on - the agent runs fine without these, and is blind without them):
- **`dh-mail#imap`** - JSON `{"host","port","user","password","folder"}` for the alert mailbox. Without it
  **Allegro.pl and OLX.pl are invisible** (both block polite polling; their saved-search alert mail is the only
  legitimate route) and eBay/Kleinanzeigen alerts are lost. It takes a dedicated mailbox with IMAP SSL 993, accounts
  on the platforms registered to *that* address, and saved searches - about an hour of the owner's clicking, so ask
  early even if they cannot do it today. Check a live mailbox with `bash $S/scripts/setup.sh mailbox` (read-only:
  it shows the unseen count and whether each mail would pass the DKIM rule; nothing is marked read).
- **`ebay#client_id`, `ebay#client_secret`** - eBay developer keys (eBay bans scrapers, the API is the only route).
  Also tell the owner to apply for the **Marketplace Insights** scope in the same portal: sold prices are the fastest
  way to reach tier 1 before the ledger matures.
- **`dh-mail#smtp`** - JSON, optional e-mail fallback for pushes.
Store each with `bash $S/scripts/put_secret.sh <item>#field [--stdin]`: on a Bitwarden-backed machine it writes the
vault item itself (folder from `BW_AGENT_FOLDERS`, using `~/.config/bw/session`; if the vault is locked it fails
loudly and tells the owner to run `bw-agent-unlock` - nothing is half-written), otherwise it installs the file-backed
`bw-secret` shim (`~/.config/deal-hunter/secrets`, mode 600). Verify with `bash $S/scripts/setup.sh secrets`
(present/absent and JSON validity, never values), and re-run `setup.sh access` to see the source go live.
Secrets are read once per process: after storing one, `dh stop && dh start`.

An owner who declines for now is fine - but then the final report must name each source that stays dark and what
would unlock it. "It started and found deals" is not the same as "it has access".

### 5. Owner settings the defaults get wrong for a new user
Write `config/local/` files (create the directory; it is gitignored) for what the owner tells you - exact shapes
in `reference/steps.md` ("config/local/ shapes"). At minimum ask about:
- **pickup origin** (`config/local/pickup.yaml`: your city + coordinates, radius) - the default is Krakow;
- **the private log repo** (`config/local/publish.yaml`: `remote`, `repo_web_url`) - if they have one, or `null`
  to keep cards local only;
- **the exit channels and tax mode** they actually use (`config/economics.yaml` is frozen: if they want it
  changed, stop the agent first, edit, start);
- **web port** if 8711 is taken (`config/local/web.yaml`), and `allowed_hosts` for the mark buttons;
- **quiet hours** for pushes (`config/local/notify.yaml`).
Do not invent values: if the owner does not know a coordinate, look it up and show them what you will write.

### 6. Push notifications
`dh status` prints the ntfy topic (generated on first use into `config/local/notify.yaml`). The owner installs
the ntfy app and subscribes to `https://ntfy.sh/<topic>` - that tap-by-hand is the one thing no agent can do for
them. Tell them the topic is a shared secret: anyone who knows it reads their pushes.

### 7. Start and verify
```bash
cd $R && .venv/bin/dh start          # refuses with a reason if RAM or models are wrong
sleep 30 && .venv/bin/dh status
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8711/   # 200 = the web page is up (use your configured port)
journalctl --user -u deal-hunter --no-pager -n 30                 # no traceback in the first minutes (your unit name)
bash $S/scripts/setup.sh verify      # status + web (port from config) + health + rounds in one shot
```
Wait for the first round to finish (`dh status` -> `rounds_since_start` >= 1, no `PAUSED` line), then
`.venv/bin/dh deals` and, if there is nothing yet, say so honestly - an empty first day is normal (cold start).
Show the owner the web page URL with the machine's LAN address, not `localhost`, so they can open it from a phone.

Finish with `bash $S/scripts/setup.sh access` and quote its `live=N not-live=M` line: a round count proves the
agent runs, the matrix proves what it can actually see. If the owner stored `dh-mail#imap`, run
`bash $S/scripts/setup.sh mailbox` too - an empty folder is normal on day 1, a folder full of mail that all says
`mail=skip platform=unknown` means the alerts are landing in the INBOX and the Gmail filter is missing or wrong.

### 8. If the owner wants a supervised first round instead
`cd $R && .venv/bin/dh round --once --dry` (foreground, no push, no publish; add `--no-llm` if models are not
wired yet). This is also the command to re-check after any config edit.

### 9. The remaining owner-only items
Print the list from `OWNER-TODO.md` that still applies to them, tailored: alert mailbox set-up (a dedicated
mailbox + saved searches, ~1 h, the step that unlocks Allegro and OLX), eBay Marketplace Insights application,
the accountant questions file, and the cold-start rule (tier 1 needs >= 12 recent *sold* comparables; the ledger
needs 4-8 weeks of watched time - judge it after weeks, not hours). To re-check the mailbox later, run
`bash $S/scripts/setup.sh mailbox` (it prints the folder, the unseen count and per-mail DKIM verdict, plus the
6-step checklist when the folder is still empty).

### 10. Report
```
Deal Hunter: RUNNING (unit deal-hunter, web http://<lan-ip>:8711, stopped with `dh stop`)
Verified: rounds <n>, health <clean | list>, models <roles -> names>, tests <n passed>
Access: live <N>/<total> sources (setup.sh access). Dark: <source - what unlocks it>, ...
Secrets stored: <list | none> (never shown)
Local config: <files you created, one line each>
Optional next steps: <mailbox / eBay keys / log repo / ntfy subscribe / accountant>
Data: DB var/dh.db, screenshots var/screenshots/, log clone ~/.local/share/deal-hunter/log
Undo: .venv/bin/dh stop; then `gio trash $R` removes the checkout (config/local/ is inside it)
```
Then ask whether they want the state journalled or documented anywhere they keep notes.

## Before you finish
- [ ] Every verification command ran and its real output was quoted in the report (no assumed success).
- [ ] No secret value appeared in the transcript, a file with loose permissions, or a commit.
- [ ] Nothing was installed or edited outside `$R`, `config/local/`, `.venv`, `~/.local/bin/bw-secret`,
      `~/.config/deal-hunter/` and `~/.local/share/deal-hunter/`.
- [ ] `git status` in `$R` shows no tracked file changed (`config/local/` is gitignored).
- [ ] The owner knows: how to stop it, where the page is, how to mark a deal, and that tier 1 takes weeks.
