"""Books: what each round costs (spec §4 component 13, §10 cost tracking).

Energy = (mean GPU power from `rocm-smi --showpower --json`, sampled during the round + a fixed base for CPU, RAM,
disks and PSU losses) x round seconds x the owner's tariff. GPU power is the sum over all cards (the owner runs no
other heavy work while the agent runs, spec §15a.1). Paid-API spend is 0 while `models.paid_api.enabled` is false.
Each round writes four `costs` rows: energy (PLN, kWh), llm (0 PLN, tokens), fetch (0 PLN, requests),
paid_api (PLN, 0).

Defaults live in config/books.yaml: tariff_pln_kwh 1.10, base_w 120, gpu_sample_s 15.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from statistics import mean

from .money import q2

DEFAULT_TARIFF = 1.10
DEFAULT_BASE_W = 120.0


def _books(cfg) -> dict:
    b = cfg.get("books") if cfg is not None else None
    return b.to_dict() if b is not None and hasattr(b, "to_dict") else (b or {})


def tariff(cfg) -> float:
    return float(_books(cfg).get("tariff_pln_kwh", DEFAULT_TARIFF))


def base_w(cfg) -> float:
    return float(_books(cfg).get("base_w", DEFAULT_BASE_W))


def parse_rocm_power(text: str) -> float | None:
    """Sum of every card's power reading ("... Power (W)") in rocm-smi JSON; None when nothing can be read."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    total, seen = 0.0, False
    for card in data.values():
        if not isinstance(card, dict):
            continue
        for k, v in card.items():
            if "Power (W)" in k:
                try:
                    total += float(v)
                    seen = True
                except (TypeError, ValueError):
                    pass
    return total if seen else None


async def sample_gpu_watts(timeout: float = 10.0) -> float | None:
    """One rocm-smi reading (W, all cards), or None when rocm-smi is missing or fails."""
    try:
        p = await asyncio.create_subprocess_exec("rocm-smi", "--showpower", "--json", stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
    except (FileNotFoundError, PermissionError, OSError):
        return None
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            p.kill()
        await p.wait()
        return None
    return parse_rocm_power(out.decode(errors="replace")) if p.returncode == 0 else None


class GpuSampler:
    """Samples GPU power every `interval_s` while a round runs: `start()`, then `await stop()` -> samples."""

    def __init__(self, interval_s: float = 15.0, sample=sample_gpu_watts):
        self.interval_s, self._sample = interval_s, sample
        self.samples: list[float] = []
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            w = await self._sample()
            if w is not None:
                self.samples.append(w)
            await asyncio.sleep(self.interval_s)

    async def stop(self) -> list[float]:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if not self.samples:  # a round shorter than one interval still gets one reading
            w = await self._sample()
            if w is not None:
                self.samples.append(w)
        return list(self.samples)


def energy_kwh(seconds: float, gpu_watts_samples: list[float], base: float = DEFAULT_BASE_W) -> float:
    gpu = mean(gpu_watts_samples) if gpu_watts_samples else 0.0
    return (gpu + base) * max(0.0, float(seconds)) / 3600.0 / 1000.0


def energy_pln(seconds: float, gpu_watts_samples: list[float], tariff_pln_kwh: float,
               base: float = DEFAULT_BASE_W) -> Decimal:
    kwh = energy_kwh(seconds, gpu_watts_samples, base)
    return q2(Decimal(str(kwh)) * Decimal(str(tariff_pln_kwh)))


def paid_month_usd(store, now: datetime | None = None) -> float:
    """Paid-API spend (USD, the `units` of the paid_api cost rows) of the calendar month so far (UTC)."""
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    r = store.conn.execute("select coalesce(sum(c.units),0) from costs c join rounds r on r.id=c.round_id "
                           "where c.kind='paid_api' and r.started_at >= ?", (start,)).fetchone()
    return float(r[0])


def record_round(store, round_id: int, cfg, *, seconds: float, gpu_watts: list[float], llm: dict,
                 fetches: int) -> dict:
    """Write this round's cost rows. `llm` = {calls, prompt_tokens, completion_tokens, seconds}."""
    b = base_w(cfg)
    kwh = energy_kwh(seconds, gpu_watts, b)
    e_pln = energy_pln(seconds, gpu_watts, tariff(cfg), b)
    tokens = int(llm.get("prompt_tokens", 0)) + int(llm.get("completion_tokens", 0))
    paid_usd = float(llm.get("paid_usd", 0.0))  # 0 unless a paid role ran (models.paid_api, capped per month)
    try:
        pln_usd = Decimal(str(cfg.models.paid_api.get("pln_per_usd", 4.0)))
    except AttributeError:
        pln_usd = Decimal("4.0")
    paid = q2(Decimal(str(paid_usd)) * pln_usd)
    # amount kept to 6 dp (a short round costs fractions of a grosz: q2 per row would sum to 0); summaries round
    store.add_cost(round_id, "energy", Decimal(str(round(kwh * tariff(cfg), 6))), round(kwh, 6))
    store.add_cost(round_id, "llm", Decimal("0.00"), tokens)
    store.add_cost(round_id, "fetch", Decimal("0.00"), int(fetches))
    store.add_cost(round_id, "paid_api", paid, round(paid_usd, 6))
    return {"energy_pln": e_pln, "kwh": kwh, "gpu_w_mean": mean(gpu_watts) if gpu_watts else None,
            "llm_tokens": tokens, "llm_calls": int(llm.get("calls", 0)), "fetches": int(fetches), "paid_api_pln": paid}


def summary(store, since: str | None = None, cfg=None) -> dict:
    """Totals by cost kind (all time, or rounds started at/after the ISO timestamp `since`). With `cfg`, energy is
    priced at summary time from the stored kWh x the current tariff."""
    sql = "select c.kind, c.amount_pln, c.units from costs c"
    args: list = []
    if since:
        sql += " join rounds r on r.id=c.round_id where r.started_at >= ?"
        args.append(since)
    by: dict[str, dict] = {}
    for r in store.conn.execute(sql, args):
        k = by.setdefault(r["kind"], {"pln": Decimal(0), "units": 0.0, "rows": 0})
        k["pln"] += Decimal(r["amount_pln"])
        k["units"] += float(r["units"] or 0)
        k["rows"] += 1
    if cfg is not None and "energy" in by:
        by["energy"]["pln"] = Decimal(str(by["energy"]["units"])) * Decimal(str(tariff(cfg)))
    rsql = "select count(*) from rounds" + (" where started_at >= ?" if since else "")
    rounds = store.conn.execute(rsql, args).fetchone()[0]
    total = sum((v["pln"] for v in by.values()), Decimal(0))
    return {"rounds": rounds, "total_pln": str(q2(total)),
            "by_kind": {k: {"pln": str(q2(v["pln"])), "units": v["units"], "rows": v["rows"]} for k, v in by.items()}}
