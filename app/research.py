"""Research Lab for MyTradingBot.

This module is deliberately independent from live/PAPER execution. It turns the
closed-trade journal into an evidence report and only marks a parameter bucket
as eligible for optimization when the sample is large enough.

Wolfram is used during development as an independent numerical reference; the
runtime bot does not depend on Wolfram.
"""
from __future__ import annotations

from math import sqrt
from collections import defaultdict
import random
from statistics import mean


Z95 = 1.95996398454005


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def wilson_interval(wins: int, trials: int, z: float = Z95) -> tuple[float, float]:
    """Wilson 95% interval for a binomial win rate."""
    n = int(trials)
    if n <= 0:
        return 0.0, 0.0
    p = max(0.0, min(1.0, int(wins) / n))
    den = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / den
    margin = z * sqrt((p * (1.0 - p) / n) + (z * z / (4.0 * n * n))) / den
    return max(0.0, center - margin), min(1.0, center + margin)


def _bucket(score: float) -> str:
    if score < 7.5:
        return "<7.5"
    if score < 8.0:
        return "7.5-8.0"
    if score < 8.5:
        return "8.0-8.5"
    if score < 9.0:
        return "8.5-9.0"
    return "9.0-10"


def _summarize(rows):
    pnls = [_float(r.get("pnl")) for r in rows]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    n = len(pnls)
    wr = len(wins) / n if n else 0.0
    lo, hi = wilson_interval(len(wins), n)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": wr * 100.0,
        "win_rate_ci95": [lo * 100.0, hi * 100.0],
        "net_pnl": sum(pnls),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": (gross_profit / gross_loss) if gross_loss else None,
        "expectancy": (sum(pnls) / n) if n else 0.0,
        "avg_win": (mean(wins) if wins else 0.0),
        "avg_loss": (mean(losses) if losses else 0.0),
    }


def _group(rows, key_fn):
    groups = defaultdict(list)
    for row in rows:
        groups[key_fn(row)].append(row)
    return groups


def _group_summary(rows, minimum_sample):
    out = {}
    for key, bucket in rows.items():
        summary = _summarize(bucket)
        summary["eligible_for_optimization"] = summary["trades"] >= minimum_sample
        out[str(key)] = summary
    return out



def equity_max_drawdown(pnls):
    curve = 0.0
    peak = 0.0
    drawdown = 0.0
    for pnl in pnls:
        curve += _float(pnl)
        peak = max(peak, curve)
        drawdown = max(drawdown, peak - curve)
    return drawdown


def monte_carlo(pnls, simulations=2000, seed=42):
    """Shuffle observed trade outcomes to estimate sequence risk.

    This does not create new returns or predict the future. It only tests how
    sensitive the observed outcomes are to trade ordering.
    """
    values = [_float(x) for x in (pnls or [])]
    n = len(values)
    if n == 0:
        return {
            "simulations": 0,
            "trades": 0,
            "median_final_pnl": 0.0,
            "p05_final_pnl": 0.0,
            "p95_final_pnl": 0.0,
            "median_max_drawdown": 0.0,
            "p95_max_drawdown": 0.0,
        }

    rng = random.Random(int(seed))
    finals = []
    drawdowns = []
    count = max(1, int(simulations))
    for _ in range(count):
        sample = values[:]
        rng.shuffle(sample)
        finals.append(sum(sample))
        drawdowns.append(equity_max_drawdown(sample))

    finals.sort()
    drawdowns.sort()

    def percentile(items, p):
        if len(items) == 1:
            return items[0]
        index = (len(items) - 1) * float(p)
        lo = int(index)
        hi = min(lo + 1, len(items) - 1)
        weight = index - lo
        return items[lo] * (1.0 - weight) + items[hi] * weight

    return {
        "simulations": count,
        "trades": n,
        "median_final_pnl": percentile(finals, 0.50),
        "p05_final_pnl": percentile(finals, 0.05),
        "p95_final_pnl": percentile(finals, 0.95),
        "median_max_drawdown": percentile(drawdowns, 0.50),
        "p95_max_drawdown": percentile(drawdowns, 0.95),
    }

def build_research_report(rows, minimum_sample=30):
    """Build a conservative research report from closed trades.

    No parameter is changed here. A bucket is merely marked as eligible for
    further walk-forward optimization once it has enough observations.
    """
    rows = list(rows or [])
    base = _summarize(rows)

    by_setup = _group(rows, lambda r: str(r.get("setup") or "UNKNOWN"))
    by_score = _group(rows, lambda r: _bucket(_float(r.get("score_10"))))
    by_reason = _group(rows, lambda r: str(r.get("reason") or "UNKNOWN"))

    report = {
        "version": 1,
        "sample": base,
        "minimum_sample_for_optimization": int(minimum_sample),
        "by_setup": _group_summary(by_setup, minimum_sample),
        "by_score": _group_summary(by_score, minimum_sample),
        "by_exit_reason": _group_summary(by_reason, minimum_sample),
        "monte_carlo": monte_carlo([_float(r.get("pnl")) for r in rows]),
        "guardrails": {
            "no_automatic_parameter_change": True,
            "requires_out_of_sample_validation": True,
            "requires_fees_and_slippage": True,
            "minimum_sample_before_optimization": int(minimum_sample),
        },
    }

    if len(rows) < minimum_sample:
        report["status"] = "COLLECT_MORE_DATA"
        report["message"] = (
            "Sample is too small for automatic strategy changes. "
            "Continue PAPER collection and then run walk-forward validation."
        )
    else:
        report["status"] = "READY_FOR_WALK_FORWARD"
        report["message"] = (
            "Enough trades exist for research, but parameters still require "
            "out-of-sample validation before deployment."
        )
    return report
