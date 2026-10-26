"""Fetch-budget bandit over source x query arms (spec §9.2, fast loop).

Each arm keeps a discounted Gamma posterior of its value per fetch:
    rate ~ Gamma(shape = a0 + reward, rate = b0 + fetches)
where `reward` is the discounted sum of value units and `fetches` the discounted number of fetches. `discount()`
multiplies both by `gamma` once per round, so old evidence fades (the market moves). The prior (1, 10) means
0.1 value units ($10 of expected net) per fetch: optimistic enough that a new arm is tried.

Selection used by the scheduler (`select`, I5 ruling): multiple-play Thompson, one search per arm per round: new arms
first, then the top draws, with ceil(floor x k) uniformly random exploration slots. Proportional allocation
(`choose`): new arms (fewer than `new_pulls` updates) first get their guaranteed pulls; the rest of the
budget is shared by one Thompson draw per arm, mixed with a floor so every arm keeps at least `floor`/len of it:
    share_i = floor/len + (1 - floor) * draw_i / sum(draws)
Integer allocation is systematic sampling on the cumulative shares (unbiased: E[alloc_i] = budget x share_i).
`order` gives the scheduler a distinct, prioritised arm list from the same shares (weighted sampling without
replacement), new arms first.

Arms with blocks or errors are throttled by the fetcher's health state, not here (spec §9.2).
State lives in the `arm_stats` table: `pulls` = number of updates (undiscounted), `reward`, plus `fetches`
(added by `migrate`).
"""
from __future__ import annotations

import math
import random
from typing import Iterable

from .store import Store

VALUE_DIVISOR = 100.0
TIER4_WEIGHT = 0.2


def value_units(deals: Iterable[tuple[int | None, float]]) -> float:
    """(Σ expected net USD of tier 1-3 deals + 0.2 × Σ tier 4) / 100. Input: (tier, expected net USD) pairs."""
    total = 0.0
    for tier, exp_usd in deals:
        if tier in (1, 2, 3):
            total += float(exp_usd)
        elif tier == 4:
            total += TIER4_WEIGHT * float(exp_usd)
    return total / VALUE_DIVISOR


def migrate(store: Store) -> None:
    have = {r["name"] for r in store.conn.execute("pragma table_info(arm_stats)")}
    if "fetches" not in have:
        store.ensure_table("alter table arm_stats add column fetches real default 0")


class Bandit:
    def __init__(self, store: Store, gamma: float = 0.98, floor: float = 0.10, prior: tuple[float, float] = (1.0, 10.0),
                 new_pulls: int = 3):
        if not 0 < gamma <= 1 or not 0 <= floor < 1:
            raise ValueError("gamma must be in (0, 1], floor in [0, 1)")
        self.store, self.gamma, self.floor, self.prior, self.new_pulls = store, gamma, floor, prior, new_pulls
        migrate(store)

    # ---------------------------------------------------------------- state
    def stats(self, arm: str) -> dict:
        r = self.store.conn.execute("select pulls, reward, fetches from arm_stats where arm=?", (arm,)).fetchone()
        if r is None:
            return {"pulls": 0, "reward": 0.0, "fetches": 0.0}
        return {"pulls": int(r["pulls"] or 0), "reward": float(r["reward"] or 0.0), "fetches": float(r["fetches"] or 0.0)}

    def _all(self, arms: list[str]) -> dict[str, dict]:
        out = {a: {"pulls": 0, "reward": 0.0, "fetches": 0.0} for a in arms}
        rows = self.store.conn.execute("select arm, pulls, reward, fetches from arm_stats").fetchall()
        for r in rows:
            if r["arm"] in out:
                out[r["arm"]] = {"pulls": int(r["pulls"] or 0), "reward": float(r["reward"] or 0.0),
                                 "fetches": float(r["fetches"] or 0.0)}
        return out

    def is_new(self, arm: str) -> bool:
        return self.stats(arm)["pulls"] < self.new_pulls

    def update(self, arm: str, value_units: float, fetches: int) -> None:
        """One pull of `arm` this round: it used `fetches` fetches and produced `value_units`."""
        if value_units < 0 or fetches < 0:
            raise ValueError("value_units and fetches must be >= 0")
        self.store.conn.execute(
            """insert into arm_stats (arm, pulls, reward, fetches) values (?, 1, ?, ?)
               on conflict(arm) do update set pulls=pulls+1, reward=reward+excluded.reward,
               fetches=coalesce(fetches,0)+excluded.fetches""", (arm, float(value_units), float(fetches)))
        self.store.conn.commit()

    def discount(self) -> None:
        self.store.conn.execute("update arm_stats set reward=reward*?, fetches=coalesce(fetches,0)*?",
                                (self.gamma, self.gamma))
        self.store.conn.commit()

    # ---------------------------------------------------------------- allocation
    def shares(self, arms: list[str], rng: random.Random) -> dict[str, float]:
        """One Thompson draw per arm, floor-mixed; sums to 1, each >= floor/len(arms)."""
        if not arms:
            return {}
        st = self._all(arms)
        a0, b0 = self.prior
        draws = {a: rng.gammavariate(a0 + st[a]["reward"], 1.0 / (b0 + st[a]["fetches"])) for a in arms}
        total = sum(draws.values())
        n = len(arms)
        if total <= 0:
            return {a: 1.0 / n for a in arms}
        return {a: self.floor / n + (1 - self.floor) * draws[a] / total for a in arms}

    def choose(self, arms: list[str], budget: int, rng: random.Random) -> list[str]:
        """`budget` fetch slots as a list of arm names (an arm appears once per slot)."""
        arms = list(dict.fromkeys(arms))
        if not arms or budget <= 0:
            return []
        st = self._all(arms)
        out: list[str] = []
        for a in arms:  # guaranteed trial budget for new arms
            need = max(0, self.new_pulls - st[a]["pulls"])
            take = min(need, budget - len(out))
            out += [a] * take
        rest = budget - len(out)
        if rest <= 0:
            return out
        sh = self.shares(arms, rng)
        # systematic sampling: positions u, u+1, ..., u+rest-1 on the cumulative axis scaled to `rest`
        u = rng.random()
        cum, k = 0.0, 0
        for a in arms:
            cum += sh[a] * rest
            while k < rest and u + k < cum - 1e-12:
                out.append(a)
                k += 1
        while k < rest:  # float residue: give it to the largest share
            out.append(max(arms, key=lambda x: sh[x]))
            k += 1
        return out

    def select(self, arms: list[str], k: int, rng: random.Random) -> list[str]:
        """Multiple-play Thompson selection (I5 ruling) for a scheduler that runs each arm at most once per round.

        Every arm is ordered (all of them are returned; the caller takes the first k it can use):
        1. new arms (fewer than `new_pulls` updates), in random order: their guaranteed trial pulls;
        2. the arms with the highest Thompson draws, filling k minus the exploration slots;
        3. ceil(floor x k) exploration slots: arms not chosen above, uniformly at random;
        4. the rest by draw (fallback when the caller skips arms, e.g. a per-source cap).
        """
        arms = list(dict.fromkeys(arms))
        if not arms:
            return []
        k = max(0, min(int(k), len(arms)))
        st = self._all(arms)
        a0, b0 = self.prior
        new = [a for a in arms if st[a]["pulls"] < self.new_pulls]
        rng.shuffle(new)
        draws = {a: rng.gammavariate(a0 + st[a]["reward"], 1.0 / (b0 + st[a]["fetches"])) for a in arms}
        explore = math.ceil(self.floor * k) if k else 0
        out = new[:k]
        by_draw = sorted((a for a in arms if a not in set(out)), key=lambda a: draws[a], reverse=True)
        n_top = max(0, k - len(out) - explore)
        out += by_draw[:n_top]
        rest = [a for a in arms if a not in set(out)]
        n_exp = max(0, k - len(out))
        out += rng.sample(rest, min(n_exp, len(rest)))
        chosen = set(out)
        out += [a for a in by_draw if a not in chosen]
        return out

    def order(self, arms: list[str], rng: random.Random) -> list[str]:
        """Every arm once, in priority order: new arms first, then weighted sampling without replacement by share
        (Efraimidis-Spirakis keys u^(1/w)). The scheduler takes arms from the front until its budget is spent."""
        arms = list(dict.fromkeys(arms))
        if not arms:
            return []
        st = self._all(arms)
        new = [a for a in arms if st[a]["pulls"] < self.new_pulls]
        old = [a for a in arms if st[a]["pulls"] >= self.new_pulls]
        rng.shuffle(new)
        sh = self.shares(old, rng) if old else {}
        keyed = sorted(old, key=lambda a: math.log(max(rng.random(), 1e-300)) / max(sh[a], 1e-12), reverse=True)
        return new + keyed
