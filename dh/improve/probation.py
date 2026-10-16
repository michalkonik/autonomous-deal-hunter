"""Promotion, probation and automatic rollback (spec §9.3 step 3.6, §9.4; research §3.8, §4.5).

After a promotion the new champion is on probation for PROBATION_WATCHED_HOURS (48). Every round updates two
Poisson CUSUMs, both baselined on the rounds BEFORE the promotion: the champion's count and the sentinel's count.
`Probation.observe` returns
- 'rollback'      champion alarm and the champion fell significantly more than the sentinel since the promotion
                  (cusum.attribute: Fisher exact on baseline vs since-promotion counts; an alarm no test explains is
                  held SENTINEL_HOLD_ROUNDS, then the chart is reset as a false alarm);
- 'market_shift'  champion alarm while the sentinel dropped too (its CUSUM alarms, or its count since the promotion
                  is significantly below its baseline): the world changed, never roll back. The alarm charts are
                  rebuilt from the next CUSUM_MIN_BASELINE_ROUNDS rounds (the new market's rates); the attribution
                  counts keep the pre-promotion baseline, so a later breakage is still caught;
- 'passed'        probation over without a breakage;
- 'continue'      otherwise.
The controller also rolls back when the daily must-pass re-run fails during probation.
`rollback` marks the failed version `failed_in_prod`, re-activates the previous one and returns it.
"""
from __future__ import annotations

from pathlib import Path

from . import gates
from .cusum import PoissonCusum, attribute, make
from .playbook import ROOT, Playbook, PlaybookError, active_version, set_status


class Probation:
    def __init__(self, state: dict):
        self.state = state
        self.champion = PoissonCusum.from_dict(state.get("champion"))
        self.sentinel = PoissonCusum.from_dict(state.get("sentinel"))

    @classmethod
    def start(cls, version: int, previous: int | None, started_wh: float, champion_counts: list[int],
              sentinel_counts: list[int]) -> "Probation":
        c, s = make(champion_counts), make(sentinel_counts)
        return cls({"version": version, "previous": previous, "started_wh": float(started_wh), "hold": 0,
                    "events": [], "champion": c.to_dict() if c else None, "sentinel": s.to_dict() if s else None,
                    "last_must_pass_wh": float(started_wh), "c_base": int(sum(champion_counts)),
                    "s_base": int(sum(sentinel_counts)), "rounds_base": len(champion_counts),
                    "c_after": 0, "s_after": 0, "rounds_after": 0})

    def to_dict(self) -> dict:
        self.state["champion"] = self.champion.to_dict() if self.champion else None
        self.state["sentinel"] = self.sentinel.to_dict() if self.sentinel else None
        return self.state

    def observe(self, champion_n: int, sentinel_n: int, watched_h: float) -> str:
        ev = self.state.setdefault("events", [])
        over = watched_h - float(self.state["started_wh"]) >= gates.PROBATION_WATCHED_HOURS
        st = self.state
        rb = st.get("rebaseline")
        if rb is not None:  # after a market shift the alarm thresholds are rebuilt on the new market's rates
            rb["c"].append(int(champion_n))
            rb["s"].append(int(sentinel_n))
            st["c_after"] = int(st.get("c_after", 0)) + int(champion_n)  # attribution keeps counting
            st["s_after"] = int(st.get("s_after", 0)) + int(sentinel_n)
            st["rounds_after"] = int(st.get("rounds_after", 0)) + 1
            if len(rb["c"]) >= gates.CUSUM_MIN_BASELINE_ROUNDS:
                self.champion, self.sentinel = make(rb["c"]), make(rb["s"])
                st.update(rebaseline=None, hold=0)
                ev.append({"wh": round(watched_h, 2), "event": "rebaselined"})
            return self._end(watched_h) if over else "continue"
        if self.champion is not None:
            self.champion.update(champion_n)
        if self.sentinel is not None:
            self.sentinel.update(sentinel_n)
        st["c_after"] = int(st.get("c_after", 0)) + int(champion_n)
        st["s_after"] = int(st.get("s_after", 0)) + int(sentinel_n)
        st["rounds_after"] = int(st.get("rounds_after", 0)) + 1
        a = attribute(self.champion, self.sentinel, c_after=st["c_after"], s_after=st["s_after"],
                      c_base=int(st.get("c_base", 0)), s_base=int(st.get("s_base", 0)),
                      rounds_after=st["rounds_after"], rounds_base=int(st.get("rounds_base", 0)))
        if a == "breakage":
            ev.append({"wh": round(watched_h, 2), "event": "breakage"})
            return "rollback"
        if a == "hold":
            st["hold"] = int(st.get("hold", 0)) + 1
            if st["hold"] >= gates.SENTINEL_HOLD_ROUNDS:  # no test explains the alarm: a false alarm, watch on
                ev.append({"wh": round(watched_h, 2), "event": "unexplained_alarm_reset"})
                self.champion.reset()
                st["hold"] = 0
            return self._end(watched_h) if over else "continue"
        self.state["hold"] = 0
        if a == "market_shift":
            ev.append({"wh": round(watched_h, 2), "event": "market_shift"})
            self.champion = self.sentinel = None
            self.state["rebaseline"] = {"c": [], "s": []}
            return "market_shift"
        if a == "sentinel_drift":
            ev.append({"wh": round(watched_h, 2), "event": "sentinel_drift"})
            self.sentinel.reset()
        return self._end(watched_h) if over else "continue"

    def _end(self, watched_h: float) -> str:
        """Probation is over: one last single-look Fisher test on everything since the promotion, so a breakage
        that no alarm caught (e.g. absorbed while the charts were rebuilt after a market shift) still rolls back."""
        st = self.state
        if breakage_test(st) < gates.BREAKAGE_P_FINAL:
            st.setdefault("events", []).append({"wh": round(watched_h, 2), "event": "breakage_at_end"})
            return "rollback"
        return "passed"

    def must_pass_due(self, watched_h: float) -> bool:
        return watched_h - float(self.state.get("last_must_pass_wh", 0.0)) >= gates.MUST_PASS_RECHECK_WATCHED_HOURS


def breakage_test(st: dict) -> float:
    """One-sided Fisher exact p that the champion fell more than the sentinel since the promotion (1.0 = no data)."""
    c, s_, cb, sb = (int(st.get(k, 0)) for k in ("c_after", "s_after", "c_base", "s_base"))
    if cb + sb == 0 or c + s_ == 0 or cb == 0:
        return 1.0
    return gates.hypergeom_cdf(c, c + s_ + cb + sb, c + cb, c + s_)


def promote(store, version: int, root: Path = ROOT) -> Playbook:
    """Make `version` the active playbook (store status + playbook/items.yaml)."""
    set_status(store, version, "promoted")
    pb = Playbook.load(version, store=store)
    pb.activate(root)
    return pb


def rollback(store, failed: int, root: Path = ROOT) -> Playbook:
    """Mark `failed` failed_in_prod and re-activate the latest remaining promoted (or baseline) version."""
    set_status(store, failed, "failed_in_prod")
    vid = active_version(store)
    if vid is None:
        raise PlaybookError("no promoted or baseline version to roll back to")
    pb = Playbook.load(vid, store=store)
    pb.activate(root)
    return pb
