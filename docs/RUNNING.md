# Running Deal Hunter

The owner starts and stops the agent by hand. Nothing starts at boot.

| Command | What it does |
|---|---|
| `dh start` | Checks free RAM (>= `models.min_free_ram_gb`, 12 GB) and that llama-swap runs no model outside the configured roles, then starts the transient user unit `deal-hunter` (rounds + web page on :8711 + log publisher). Refuses, and says why, instead of evicting another workload. Also refuses while a foreground `dh round` runs. |
| `dh status [--json]` | Running?, uptime, rounds, last round, deals today, health, llama-swap, ntfy topic, log repo. |
| `dh stop` | Stops the unit, closes observation windows, unloads the role models only if nothing else is loaded and they were not already loaded before `dh start`, then checks that no process is left. |
| `dh round --once [--dry] [--no-llm]` | One round in the foreground (testing). `--dry`: no push. `--no-llm`: rules only, llama-swap untouched. |
| `dh deals`, `dh show <id>`, `dh verify <id>`, `dh books` | Read the results. |

## Sharing the rig with another workload

If llama-swap already runs models for something else, `dh start` refuses (the role models would evict them). And
every round re-checks llama-swap: if a foreign model appears while the agent runs, that round runs on the rules alone
and the LLM listings wait (`llm_pending`).

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
