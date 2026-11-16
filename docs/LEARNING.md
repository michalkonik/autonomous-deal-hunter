# Learning from Deal Hunter

Deal Hunter is a working example of an autonomous agent that must make money, not look clever. Each subsystem
teaches one lesson about building such agents. Read the code next to this page; every lesson names the file.

Further reading:
- [`research/deal-tools-landscape.md`](research/deal-tools-landscape.md): what existing tools do, platform access,
  economics, scam patterns.
- [`research/self-improving-agents.md`](research/self-improving-agents.md): the literature on self-improving
  agents and the statistics of tuning on rare events.
- The public `llm-education` repository has the generic guide **"Self-improving agents without regressions"**
  (English and Polish), written from the same research. Use it for the general method; this page is the worked
  example.

## 1. Most of an agent is code, not an LLM

*See `dh/scheduler.py`, `dh/match.py`.* Collectors, the ledger, the economics engine and the bandit are plain
code. The LLM sees a listing only after cheap filters and only when the rules matcher is under 0.9 certain; the
target is under 5% of listings. This is cheaper, faster and more predictable than an LLM loop, and it leaves the
rig free. **Lesson:** use an LLM for the ambiguous residue and let deterministic code do the bulk.

## 2. Make the LLM safe by construction

*See `dh/llm.py`, `dh/extract.py`, `playbook/items.yaml` (the prompts).* Listing text is untrusted: it is wrapped
in `<listing>` delimiters, tag-stripped, truncated, and the prompt says it is never an instruction. The model has
no tools in this step and its JSON output is checked against a schema, with retries and a circuit breaker.
**Lesson:** treat every web page as hostile input, give the model no authority, and validate what comes back.

## 3. Measure value in money, with an honest ledger

*See `dh/ledger.py`, `dh/valuation.py`.* The agent infers sold prices from listings that vanish, but only inside
watched time, and only when a sweep succeeded and returned results. A crash or a stop means "unknown", not
"sold". Comparables carry weights by how much they can be trusted (owner sale, eBay sold, probably sold, ask).
**Lesson:** an agent's data is only as good as its model of what it did not see. Record absence carefully.

## 4. Put the money rules in a frozen, tested engine

*See `dh/economics.py`, `docs/ECONOMICS.md`, `dh/tiering.py`.* The profit formula is pure functions with ten
hand-worked examples. Floors and tiers read thresholds from a config the agent cannot write, and the rounds pause
if a frozen file changes. **Lesson:** the scorer and the rules of the game must be outside the agent's reach.
Systems that could reach their own scorer gamed it (research report section 3.7).

## 5. Be conservative where mistakes cost trust

*See `dh/tiering.py`, `dh/risk.py`, `dh/verify.py`.* Floors use the P25 exit price. Tier 1 needs high confidence:
a certain match, enough sold comparables, a low scam score, and a live re-check at the same price. A deal that
cannot be re-checked cannot be tier 1. Every card lists the unmet conditions. **Lesson:** a noisy agent is
ignored. Gate the loudest action (a phone push) on the strictest evidence, and explain every decision.

## 6. Spend exploration deliberately: the bandit

*See `dh/bandit.py`.* Each source and query is an arm with a discounted posterior of value per fetch. Thompson
sampling spends more on what works, an exploration floor keeps trying the rest, and discounting lets old
evidence fade as the market moves. Sentinel queries, never edited, give a control group. **Lesson:** adapt
continuously with a method that cannot rewrite itself, and keep a control to tell "the market changed" from
"I broke something" (research report section 3.5 and 3.8).

## 7. Respect the platforms, and keep the owner out of trouble

*See `dh/fetch.py`, `docs/SOURCES.md`.* The agent never circumvents a bot check. It identifies itself, obeys
robots.txt, paces requests, and when a site says no it uses an official API, RSS or the alert mailbox, or drops
the site and writes down why. **Lesson:** an agent that gets its owner's accounts banned has failed even if it
finds deals. Make etiquette a property of the fetch layer, not a habit of each adapter.

## 8. Self-improvement needs gates, and the courage to stop

*See `dh/improve/`, `docs/SELF-IMPROVEMENT.md`.* The loop may change one small item at a time and must pass,
cheapest first: a schema check, a must-pass set, a paired golden-set comparison, replay on archived data, shadow
mode on live data, and probation with automatic rollback. Waits grow after each rejection and the playbook freezes
when the evidence runs out. Labels come only from verified outcomes (your marks, the ledger), never the agent's
own opinion. **Lessons:** deals are rare, so three empty rounds prove nothing; a full rewrite destroys a playbook;
a reachable scorer gets gamed; and not changing is a valid output. The statistics are in the research report
sections 3 and 4.

Note that in this repository the loop exists as a tested library but is not yet called by the running agent (see
[`ARCHITECTURE.md`](ARCHITECTURE.md#what-is-and-is-not-wired-yet)). That is itself a lesson: build the gates first,
then connect the loop.

## 9. Books close the loop

*See `dh/books.py`, the web page `/books`.* Energy, tokens and fetches per round are recorded, and realised
profit from your Sold marks is compared with the forecast. **Lesson:** an agent should know what it costs and
whether it pays for itself. The owner's marks are the only labels that cannot be argued with.

## 10. Honest documentation

*See `docs/ARCHITECTURE.md`, the "not wired yet" section.* Where code and design differ, the docs say so. A
config key that nothing reads is marked as such. **Lesson:** an owner can only tune what they correctly
understand.

## Exercises

1. Run `dh round --once --dry --no-llm`, then `dh deals`. Pick one deal and read its card. Which condition kept it
   from tier 1?
2. Raise `confidence.min_comps.base` in a scratch copy of `rules.yaml` and recompute a deal by hand with the
   confidence function. What changes?
3. Read `dh/bandit.py` and change `gamma` in a test. How many rounds until an arm's old reward counts for half?
4. Write a new RSS source by following [`ADDING-A-SOURCE.md`](ADDING-A-SOURCE.md).
5. Read the must-pass cases in `evals/must_pass.jsonl` and invent a new bait pattern. Which gate would catch it?
