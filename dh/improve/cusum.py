"""Poisson CUSUM for a drop in a per-round count, and the sentinel attribution rule (research §3.8; spec §9.2).

S_t = max(0, S_{t-1} + x_t * ln(lambda1/lambda0) - (lambda1 - lambda0)); alarm when S_t > h. An empty round adds
lambda0 - lambda1, every counted event subtracts. With lambda1 = lambda0 / 2 and h = 4 the false-alarm run length at
lambda0 = 0.3 is ~1300 rounds and the detection delay ~70 rounds: rare-event monitoring is slow, honestly.

Attribution when the champion's CUSUM alarms (difference in differences against the sentinel, a never-modified
control: fixed queries judged by the pinned playbook). Counts: B_c, B_s = champion and sentinel over the baseline
rounds before the promotion; C, S = the same since the promotion (never reset during probation).
1. breakage      if the champion fell MORE than the sentinel: one-sided Fisher exact test on the 2x2 Poisson table
                 [[C, B_c], [S, B_s]] (conditioning on all margins removes the baseline rates' own noise),
                 P(X <= C) < BREAKAGE_P. Under a market shift both rates fall by the same factor and the table has no
                 interaction; under a breakage only the champion falls. This runs first, so a sentinel that drifts by
                 chance cannot hide a breakage, and it keeps the pre-promotion baseline, so a market shift declared
                 by mistake cannot absorb a breakage either.
2. market_shift  else, if the sentinel's own CUSUM alarms or S is significantly low against B_s (conditional
                 binomial on the exposures), P < SENTINEL_DROP_P.
3. hold          else (evidence still accumulating; the counts grow every round).
No sentinel events in the baseline -> breakage (a change point right after a promotion is attributed to it; research §3.8).
"""
from __future__ import annotations

import math
from statistics import mean

from . import gates


class PoissonCusum:
    def __init__(self, lambda0: float, lambda1: float | None = None, h: float = gates.CUSUM_H, s: float = 0.0,
                 n: int = 0):
        lambda0 = float(lambda0)
        lambda1 = lambda0 * gates.CUSUM_DROP_RATIO if lambda1 is None else float(lambda1)
        if lambda0 <= 0 or not 0 < lambda1 < lambda0:
            raise ValueError(f"a drop CUSUM needs 0 < lambda1 < lambda0, got {lambda0}, {lambda1}")
        self.lambda0, self.lambda1, self.h = lambda0, lambda1, float(h)
        self._a = math.log(lambda1 / lambda0)
        self._b = lambda1 - lambda0
        self.s, self.n = float(s), int(n)

    def update(self, count: int) -> bool:
        """Add one round's count; True when the chart alarms."""
        if count < 0:
            raise ValueError("count must be >= 0")
        self.s = max(0.0, self.s + count * self._a - self._b)
        self.n += 1
        return self.alarm

    @property
    def alarm(self) -> bool:
        return self.s > self.h

    def reset(self) -> None:
        self.s = 0.0

    def to_dict(self) -> dict:
        return {"lambda0": self.lambda0, "lambda1": self.lambda1, "h": self.h, "s": round(self.s, 6), "n": self.n}

    @classmethod
    def from_dict(cls, d: dict | None) -> "PoissonCusum | None":
        if not d:
            return None
        return cls(d["lambda0"], d["lambda1"], d["h"], d.get("s", 0.0), d.get("n", 0))


def baseline(counts: list[int], min_rounds: int = gates.CUSUM_MIN_BASELINE_ROUNDS) -> float | None:
    """lambda0 = mean count per round over the baseline window; None when too short or zero (nothing to watch)."""
    if len(counts) < min_rounds:
        return None
    lam = mean(counts)
    return lam if lam > 0 else None


def make(counts: list[int]) -> PoissonCusum | None:
    lam = baseline(counts)
    return None if lam is None else PoissonCusum(lam)


def attribute(champion: PoissonCusum | None, sentinel: PoissonCusum | None, *, c_after: int = 0, s_after: int = 0,
              c_base: int = 0, s_base: int = 0, rounds_after: int = 0, rounds_base: int = 0) -> str:
    """'quiet' | 'sentinel_drift' | 'breakage' | 'market_shift' | 'hold' (module doc)."""
    if champion is None or not champion.alarm:
        return "sentinel_drift" if sentinel is not None and sentinel.alarm else "quiet"
    if s_base <= 0 or rounds_base <= 0:
        return "breakage"
    total = c_after + s_after + c_base + s_base
    if gates.hypergeom_cdf(c_after, total, c_after + c_base, c_after + s_after) < gates.BREAKAGE_P:
        return "breakage"
    share_after = rounds_after / (rounds_after + rounds_base) if rounds_after > 0 else 0.0
    if (sentinel is not None and sentinel.alarm) or (share_after > 0 and gates.binom_cdf(s_after, s_after + s_base, share_after) < gates.SENTINEL_DROP_P):
        return "market_shift"
    return "hold"
