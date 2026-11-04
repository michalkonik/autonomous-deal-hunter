"""Bandit over source x query arms (spec §9.2): discounted Gamma posterior of value per fetch."""
import random
from collections import Counter

import pytest

from dh.bandit import Bandit, value_units
from dh.store import Store


def _simulate(b: Bandit, rates: dict[str, float], rounds: int, budget: int, seed: int = 7) -> Counter:
    """Each round: allocate `budget` fetches; an arm returns Poisson(rate x fetches) value units."""
    rng = random.Random(seed)
    got: Counter = Counter()
    arms = list(rates)
    for _ in range(rounds):
        alloc = Counter(b.choose(arms, budget, rng))
        assert sum(alloc.values()) == budget
        for a in arms:
            n = alloc[a]
            got[a] += n
            # Poisson by summing exponential gaps (stdlib only)
            lam, k, t = rates[a] * n, 0, rng.expovariate(1.0) if rates[a] * n > 0 else float("inf")
            while t < lam:
                k += 1
                t += rng.expovariate(1.0)
            b.update(a, float(k), n)
        b.discount()
    return got


def test_bandit_shifts_budget_to_productive_arm(tmp_path):
    b = Bandit(Store(tmp_path / "t.db"))
    got = _simulate(b, {"good": 0.3, "poor": 0.05}, rounds=200, budget=20)
    assert got["good"] >= 3 * got["poor"], got


def test_floor_keeps_exploring(tmp_path):
    """A worthless arm keeps at least ~floor/len of the budget; it is never starved."""
    b = Bandit(Store(tmp_path / "t.db"), floor=0.10)
    got = _simulate(b, {"a": 0.5, "b": 0.5, "dead": 0.0, "d": 0.4}, rounds=200, budget=40)
    total = sum(got.values())
    assert got["dead"] >= 0.8 * (0.10 / 4) * total, got
    assert got["dead"] < got["a"]


def test_new_arms_get_three_guaranteed_pulls(tmp_path):
    b = Bandit(Store(tmp_path / "t.db"))
    for _ in range(50):  # a strong incumbent
        b.update("old", 50.0, 10)
    rng = random.Random(1)
    alloc = Counter(b.choose(["old", "new"], 10, rng))
    assert alloc["new"] >= 3
    for _ in range(3):
        b.update("new", 0.0, 1)
    assert b.is_new("new") is False


def test_state_persists_in_arm_stats_and_discount(tmp_path):
    st = Store(tmp_path / "t.db")
    b = Bandit(st, gamma=0.5)
    b.update("x", 4.0, 8)
    b.discount()
    b2 = Bandit(st, gamma=0.5)
    s = b2.stats("x")
    assert s["pulls"] == 1 and s["reward"] == pytest.approx(2.0) and s["fetches"] == pytest.approx(4.0)
    row = st.conn.execute("select * from arm_stats where arm='x'").fetchone()
    assert row["reward"] == pytest.approx(2.0)


def test_order_is_distinct_and_new_first(tmp_path):
    b = Bandit(Store(tmp_path / "t.db"))
    for a in ("a", "b", "c"):
        for _ in range(3):
            b.update(a, 1.0, 1)
    order = b.order(["a", "b", "c", "fresh"], random.Random(3))
    assert order[0] == "fresh" and sorted(order) == ["a", "b", "c", "fresh"]


def test_choose_edge_cases(tmp_path):
    b = Bandit(Store(tmp_path / "t.db"))
    assert b.choose([], 5, random.Random(0)) == []
    assert b.choose(["a"], 0, random.Random(0)) == []
    assert Counter(b.choose(["a", "b"], 2, random.Random(0)))  # new arms, budget smaller than guarantees


def test_value_units():
    # (sum expected net USD of tier 1-3 + 0.2 x sum tier 4) / 100
    assert value_units([(1, 300.0), (3, 100.0), (4, 50.0), (None, 999.0)]) == pytest.approx(4.1)
    assert value_units([]) == 0.0

