# Running Deal Hunter

The owner starts and stops the agent by hand. Nothing starts at boot.

> Setting this checkout up for the first time? Run `/setup-deal-hunter` from it with Claude Code (or another
> Agent-Skills tool) and it performs the whole first-run sequence - install, secrets, model mapping, systemd
> unit, first round, verification - and reports the commands it ran. This file stays the manual reference.

| Command | What it does |
|---|---|
| `dh start` | Checks free RAM (>= `models.min_free_ram_gb`, 12 GB) and that llama-swap runs no model outside the configured roles, then starts the transient user unit `deal-hunter` (rounds + log publisher) **and** the page server unit `deal-hunter-web` (the web page on :8711). Refuses, and says why, instead of evicting another workload. Also refuses while a foreground `dh round` runs. |
| `dh page start\|stop\|status` | The page server alone: browse the deals with the hunter stopped. It is its own transient unit `<unit>-web` running `python -m dh.runtime serve-web`: same database, no agent, no lock, no models, and only the mark forms and the "stop the page" button ever write. `dh start` starts it next to the hunter; `dh stop` stops both. The nav badge shows whether the **hunter** is running while you browse. |
| `dh status [--json]` | Running?, uptime, rounds, last round, deals today, health, llama-swap, ntfy topic, log repo, and the improvement loop (phase, cooldown, owner reviews; read-only). |
| `dh stop` | Stops the unit and the page server, closes observation windows, unloads the role models only if nothing else is loaded and they were not already loaded before `dh start`, then checks that no process is left. |
| `dh round --once [--dry] [--no-llm]` | One round in the foreground (testing). `--dry`: no push. `--no-llm`: rules only, llama-swap untouched. |
| `dh deals`, `dh show <id>`, `dh verify <id>`, `dh books` | Read the results. |
| `dh playbook` | The active playbook items and every version. Owner actions on the improvement loop, one at a time: `--rollback N`, `--maintenance on\|off`, `--pending` (what waits for you), `--label ID TRUTH` (`deal`, `not_deal`, `key:<key>`), `--apply ID` (a prompt proposal; needs the running agent, refused otherwise). While the agent runs these are **queued** and applied at its next round; while it is stopped they apply at once (except `--apply`). |
| `dh eval [--version N]` | Must-pass and golden scores of the active (or a given) playbook; exit 1 if a must-pass case fails. `--add-from-marks` extends the golden set from verified marks (owner only; refused while the agent runs). |
| `dh report --weekly [--publish]` | The weekly report as Markdown. `--publish` writes it into the log repo clone (refused while the agent runs, which publishes it itself every 168 watched hours). |

## Sharing the rig with another workload

If llama-swap already runs models for something else, `dh start` refuses (the role models would evict them). And
every round re-checks llama-swap: if a foreign model appears while the agent runs, that round runs on the rules alone
and the LLM listings wait (`llm_pending`). The check is repeated before every LLM call of a round as well, so a
model another workload loads mid-round is never swapped out by the agent.

To use models that are already loaded instead, map the roles to them in `config/local/models.yaml` (gitignored).
`dh stop` then leaves them loaded, because they were loaded before `dh start`. Example (the "duo" group):

```yaml
# config/local/models.yaml
roles:
  worker: qwen3.6-35b-a3b
  senior: qwen3.8-flash-next
  advisor: qwen3.8-flash-next
```

Check `curl -s localhost:8080/running` first and use the names it lists. Remove the file to go back to the default
`pedro` roles in `config/models.yaml`.

Round pacing for a quick test (`config/local/scheduler.yaml`): `round_interval_s: 120` (default 600).

## A second checkout on the same host

A development clone next to the owner's checkout must not take the owner's unit for its own. Give it its own unit
name in `config/local/runtime.yaml` (`unit_name: deal-hunter-dev`) or the environment (`DH_UNIT_NAME`); its
lock, pid and state files are under its own `var/run/` already. Then `dh round --once --dry` works there while the
owner's `deal-hunter` unit runs. See [`CONFIGURATION.md`](CONFIGURATION.md#runtimeyaml).

Before you `dh start` a second checkout (rather than only `dh round --once --dry`), also give it its own web port
and keep it away from the owner's log repo, in its `config/local/`:

```yaml
# config/local/web.yaml
port: 8712
# config/local/publish.yaml
remote: null
log_dir: ~/.local/share/deal-hunter/log-dev
```

A dry foreground round never publishes or pushes; `serve` does both.
