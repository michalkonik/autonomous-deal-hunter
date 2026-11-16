# Agent change workflow (rule 1)

Any code change an AI agent develops for this project follows exactly this path:

1. **Dev clone, never the live checkout.** Development happens in a separate clone
   (on the rig: `~/Desktop/github_repos/deal-hunter-dev`). The live checkout that the
   systemd unit runs from is not edited and its unit is not restarted by an agent.
2. **Branch off `feat/build`.** One branch per change, named for what it does.
3. **Full test suite, zero failures required** before the change is offered
   (`.venv/bin/python -m pytest tests -q`), plus a smoke run against a copy of the
   live database when the change touches the web page or runtime.
4. **Push the branch and open a PR** to `feat/build` (`gh pr create`); the PR link is
   sent to the owner for review. An agent never merges its own PR and never pushes
   directly to `feat/build` or `main`.
5. The owner merges; the live checkout is updated separately (operator action).

Judgment/self-improvement data (the playbook DB, `playbook/items.yaml`) is runtime
state, not code: it stays in the live checkout and is delivered as a PR by the same
rule when it is proposed as a durable change.
