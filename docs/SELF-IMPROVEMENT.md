# How Deal Hunter improves itself, and when it stops

Deal Hunter may change a small part of itself, the playbook, but only one gated change at a time. It must prove
that each change does not make things worse, and it stops changing once the evidence runs out. This page explains
the loop, how to read what it did, and how to overrule it. The design follows spec §9 and the research report
`docs/research/self-improving-agents.md` (§1, §3, §4). The code is in `dh/improve/`.

## The short version

1. **Only the playbook can change.** That is `playbook/items.yaml`: search queries, product aliases, scam keywords,
   two LLM prompts and per-source cadence. Prices, fees, tiers, floors, adapters, the gates and the evaluation sets
   are frozen and can only be changed by a human through git. Prompt changes may be *proposed* but only you apply
  them.
2. **One small typed change at a time.** A change is a *delta*: add, update or remove ONE item. Code computes its
   inverse, so every change can be undone exactly. The agent never rewrites the playbook (research §1 rule 5: a
   full rewrite shrank one playbook from 18,282 tokens to 122 and scored below having none).
3. **Cheapest test first.** Schema and caps, then the must-pass set (100%), the golden set (fix more than you
   break), replay on archived listings, shadow mode on the live stream, and finally probation with automatic
   rollback.
4. **Learn only from verified outcomes.** Your Bought / Skipped / Sold marks, your labels on disagreements, and the
   clears-at ledger. The agent's own opinion of a deal is never a label.
5. **Stop when it works.** Cooldowns double after each rejection. After 3 rejections in a row, or when two weeks of
   data could not detect any plausible gain, the playbook freezes (*maintenance mode*). The weekly report then says
   "working well; no changes proposed".

All waits are in **watched hours**: hours the agent was actually running (`dh start` to `dh stop`), not calendar
time. They are computed as the union of the ledger's observation windows (`dh/improve/monitor.py`).

## What may change, and how it is tested

| Item kind | Example | Evaluation path ("door") |
|---|---|---|
| `alias` | `4090fe => 4090 fe` (the matcher missed a glued Founders Edition suffix) | judgment: all offline gates, then shadow, then probation |
| `scam_kw` | `sprzedam pudelko po` (box-only bait worded in a GPU listing's description; narrow, because an original box sold with the card is normal) | judgment: as above |
| `prompt` | `prompt.extract`, `prompt.triage` only (update only) | llm: **owner review only**. The gates cannot judge a prompt without live LLM calls, so the loop never promotes one. It stops at "owner review"; you apply it, which first runs the must-pass set with the live model, then probation |
| `query` | `rtx 5070 ti` on `all` sources | arm: schema and must-pass, then probation. The bandit's trial budget and the CUSUM judge it, because replay cannot score a query nobody ran (research §3.3) |
| `cadence` | minutes between searches of one source | arm: as above |

Queries and cadences are never *removed* by a delta: the bandit retires weak arms into its cold pool.
Every prompt must keep its prompt-injection guard (`<listing>` and "untrusted"). Caps: at most 400 items, a prompt
at most 1500 characters, other values at most 120.

**Sentinel items** (`sentinel: true`) can never be changed by a delta:
- the control-group queries `sentinel.*`;
- the reviewer prompts `prompt.reflect` and `prompt.propose`, so the agent can never rewrite its own reviewer.

## The gates (`dh/improve/gates.py`, frozen)

1. **schema**: the challenger parses, respects the caps and differs from the champion in exactly one item.
2. **must_pass**: all cases in `evals/must_pass.jsonl` must pass (39 cases). They cover box-only titles, wanted
   ads, comparisons, for-parts listings, accessories alone, off-platform scams, a stripped card, scam wording in
   descriptions, and true deals that must keep their tier. Each case is judged by the real pipeline: matcher with
   the playbook's aliases, risk with its scam keywords, economics, confidence and tiering, all with fixed FX and a
   fixed day.
3. **golden**: `evals/golden.jsonl` (152 cases, from the human-labelled matcher fixtures plus known misses).
   Champion and challenger are scored on the same cases. `b` counts regressions (the champion was right) and `c`
   counts fixes. The gate rejects when `b >= c`; the exact McNemar p is reported.
4. **replay**: the last 21 days of stored listings are judged again with both playbooks, using cached LLM output
   and no network. The gate rejects if the expected net of tier 1-3 falls more than 5%, if known false positives
   rise, or if a deal you bought or sold is lost.
5. **owner review** (prompt changes only): `cli_pending` shows the proposed text and `cli_apply(id)` applies it.
   The must-pass set runs again with the LIVE model and the new prompt. Only if it passes is the change promoted,
   and it then goes through probation like any other.
6. **shadow** (`dh/improve/shadow.py`): every round, both playbooks judge the same evaluated listings and only the
   champion acts. Disagreements are stored and labelled later by verified outcomes. The decision is one exact
   binomial test, made once, when at least 40 shadow rounds and at least 10 labelled disagreements exist:
   promote if `p < 0.1`, otherwise reject. Testing once means peeking cannot inflate the error. The challenger is
   also rejected early if the champion clearly wins, or after 336 watched hours with no decision. Twyman's law:
   if one item changes more than 25% of all judgments, the change is held for audit instead of promoted.
7. **probation** (`dh/improve/probation.py`): 48 watched hours. Two Poisson CUSUMs watch the champion's and the
   sentinel's per-round counts of deals and near-misses. On an alarm, the champion's and sentinel's counts since
   the promotion are compared with their baselines from before it (a one-sided Fisher exact test):
   - only the champion fell: **rollback** to the previous version;
   - both fell: a **market shift**, never a rollback;
   - neither test is conclusive: wait, then treat it as a false alarm.

   A final single-look test runs when probation ends. The must-pass set is re-run every 24 watched hours, and a
   failure there also rolls back. So does a **health rise**: a source and kind with at least 3 events since the
   promotion and more than twice as many as in the equal watched window before it, while the sentinel's sources
   stay healthy. An example is a cadence change that brings 429s.

All thresholds are constants at the top of `gates.py`. The rounds refuse to run if that file changes while the agent
runs (`config.FROZEN_PATHS`).

## When a proposal is made, and when the agent stops

The senior model writes a structured round report after every round. Code collects the evidence: failed searches,
health events, empty sweeps, deferred LLM work and your Skipped reasons. The model only sorts the free-text items
into a fixed list of causes, in a fresh context, using the immutable `prompt.reflect`. The report changes nothing.

A proposal is drafted (by the `advisor` role, using `prompt.propose`) only when one of these fires:
- 3 or more failures with the same cause;
- a health alarm (3 or more events of one source and kind);
- a CUSUM alarm;
- the weekly review, every 168 watched hours.

Empty rounds alone never trigger a proposal: at 0.1-0.3 deals per round, three empty rounds happen 41-74% of the
time by chance (research §3.1).

**Stop rules**:
- One change in flight: nothing new starts while a change is in shadow or probation.
- Cooldown after each rejection or rollback: 8, 16, 32, then 64 watched hours. An accepted change resets it.
  A trigger that yields no proposal starts the base 8-hour wait and stamps its evidence, so the advisor is not
  called every round. A weekly review that falls inside a cooldown waits for it to end and is not lost.
- Rejected ideas are shown to the reflector for 30 days, and code drops any repeat.
- **Maintenance mode** starts after 3 rejections in a row, or when two weeks of watched data could not detect an
  effect as large as the most one item can plausibly achieve (×1.5). In maintenance only the bandit adapts. A
  health or CUSUM alarm, or the weekly review, can reopen the playbook for one targeted change, which ends back in
  maintenance.

## Reading what it did

- `dh playbook` (`improve.cli_playbook_show`): the active items (`*` marks a sentinel) and every version with its
  status: `baseline`, `shadow`, `promoted`, `rejected` or `failed_in_prod`. The web page `/playbook` shows the
  diffs.
- `dh status` (`improve.cli_status`): the phase (idle, shadow, probation or maintenance), cooldown left, the
  shadow tally (wins, losses, unlabelled), probation hours left and recent events.
- `dh eval` (`improve.cli_eval`): the must-pass and golden scores of the active playbook. It exits 1 if a must-pass
  case fails.
- The weekly report (`improve.report.weekly_async`) is published to the log repo. It covers the scoreboard of spec
  §9.7, the week's proposals (the change, its trigger, the predicted effect and the outcome) and the prediction
  ledger (the share of proposals that passed the gates in 30 days). If that share stays below 20%, reflect less
  often or give the advisor role a stronger model.
- Proposals are stored in the `proposals` table, with their gate details, and shadow disagreements in
  `shadow_results`.

## Overruling it

- **Label disagreements**: `improve.cli_pending()` lists them. Label one with
  `improve.cli_label(id, "deal" | "not_deal" | "key:<key>")`. You state the truth and code decides which side was
  right. Your Bought / Skipped / Sold marks label disagreements automatically. A Skipped mark counts as a
  wrong-deal label only when its reason names the wrongness (scam, box, wrong model, broken...; see
  `evals/labels.py`).
- **Force a rollback**: `improve.cli_rollback(N)` makes version N active again and aborts any change in flight.
- **Force maintenance on or off**: `improve.cli_maintenance(True | False)`.
- **Apply a prompt change**: `improve.cli_apply(id)`, after reading it in `cli_pending`.
- These owner commands are **queued** (`improve_commands`). The running agent applies them at the start of its
  next round, and again just before saving if one arrives mid-round. When the agent is stopped, the CLI applies
  them at once. The controller's state is saved with a compare-and-swap, so no command is ever overwritten.
- **Edit `playbook/items.yaml` by hand** if you like. The next round notices that the file matches no stored
  version and records it as a new baseline version (note "owner edit"). It aborts any change in flight, logs a
  health event and carries on.
- **Inject a change yourself**: `Controller.submit(delta)` runs a delta through the same gates. It is used by the
  acceptance tests.
- **Extend the golden set**: `dh eval --add-from-marks` (`evals.marks.add_from_marks`) appends one case per new
  verified mark. Only you run it: the improvement loop has no write path to `evals/`. It refuses while the agent
  runs, because `evals/` is in the frozen digest and changing it mid-run would stop the rounds. Stop the agent
  first.

## What is frozen, and why

Frozen: `config/rules.yaml`, `config/economics.yaml`, all of `data/` (fees, shipping, taxonomy), `dh/economics.py`,
`dh/customs.py`, `dh/tiering.py`, `dh/improve/gates.py` and `evals/`.

Systems that could reach their own scorer gamed it. DGM deleted its hallucination markers, STOP disabled its
sandbox, AlphaEvolve exploited leaky verifiers, Codex hard-coded evaluation answers, and o3 patched graders.
"Please do not cheat" still produced 80% reward hacking (METR; research §1 rule 4, §3.7). Instructions are not a
control, so the freeze is enforced by construction:
- The only file `dh/improve` can write is `playbook/items.yaml`, through one guarded function that refuses every
  other path (`playbook._guarded_write`).
- A static test parses `dh/improve/*.py` and fails on any other write, any shell-out, and any frozen path named
  outside `gates.py` (`tests/improve/test_improve_frozen.py`).
- The rounds refuse to run if a frozen file changes while the agent runs.
- The reflector never sees eval cases or labels; rejected ideas reach it only as a stage and a summary
  (research R2).

## Known limits

- Replay judges stored listings: it can test a judgment change, never a query nobody ran.
- Prompt changes cannot be judged offline, because the gates use cached LLM output, so they are owner-review only.
  The sentinel's pinned playbook only ever uses cached extractions made under its own extraction prompt (prompt
  epochs). An approved prompt change therefore never contaminates the control group.
- Attribution assumes the sentinel queries see the same market as the champion. A shift that hits only the
  champion's queries (say, one product line drying up) looks like breakage, and the change is rolled back. That is
  the cheaper error: an improvement can be proposed again after 30 days, but a regression costs deals every round.
- Detection is slow at rare-event rates: a CUSUM needs about 50-100 rounds to confirm that a 0.3-per-round rate
  has halved (research §3.8). Probation therefore also ends with a final test on everything seen since the
  promotion.
