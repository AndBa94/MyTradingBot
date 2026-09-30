"""Deterministic, candle-level research backtest primitives.

This is intentionally separate from the PAPER execution engine. It can score
a generated Opportunity against candles that occur strictly after the signal.
If a candle touches both stop and target, the stop is counted first because
OHLC data cannot reveal the intrabar order.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SignalOutcome:
    result: str
    exit_price: float
    bars_held: int
    gross_return: float
    net_return: float
    reason: str


def _fee(entry, exit_price, fee_rate):
    return (abs(float(entry)) + abs(float(exit_price))) * max(0.0, float(fee_rate))


def evaluate_opportunity(opportunity, future_candles, fee_rate=0.00055, max_bars=8):
    """Evaluate one signal without using any candle at/before the signal."""
    if not opportunity or not future_candles:
        return None

    entry = float(opportunity.entry)
    stop = float(opportunity.stop_loss)
    tps = [float(x) for x in (opportunity.take_profits or [])]
    if entry <= 0 or stop <= 0 or not tps:
        return None

    side = str(opportunity.side).upper()
    if side not in {"LONG", "SHORT"}:
        return None

    risk = abs(entry - stop)
    if risk <= 0:
        return None

    for index, candle in enumerate(list(future_candles)[:max(1, int(max_bars))], start=1):
        high = float(candle.high)
        low = float(candle.low)

        stop_hit = low <= stop if side == "LONG" else high >= stop
        target = tps[0]
        target_hit = high >= target if side == "LONG" else low <= target

        # With OHLC only, intrabar order is unknowable. Stop-first avoids
        # manufacturing profits from an optimistic assumption.
        if stop_hit:
            gross = ((stop - entry) / entry) if side == "LONG" else ((entry - stop) / entry)
            net = gross - _fee(entry, stop, fee_rate) / entry
            return SignalOutcome("LOSS", stop, index, gross, net, "STOP_LOSS")

        if target_hit:
            gross = ((target - entry) / entry) if side == "LONG" else ((entry - target) / entry)
            net = gross - _fee(entry, target, fee_rate) / entry
            return SignalOutcome("WIN", target, index, gross, net, "TP1")

    last = list(future_candles)[:max(1, int(max_bars))][-1]
    exit_price = float(last.close)
    gross = ((exit_price - entry) / entry) if side == "LONG" else ((entry - exit_price) / entry)
    net = gross - _fee(entry, exit_price, fee_rate) / entry
    return SignalOutcome("TIMEOUT", exit_price, min(len(future_candles), int(max_bars)), gross, net, "TIMEOUT")


def walk_forward_slices(length, train_fraction=0.60, validation_fraction=0.20):
    """Return chronological train/validation/test index ranges."""
    n = int(length)
    if n < 3:
        return {"train": (0, n), "validation": (n, n), "test": (n, n)}
    train_end = max(1, min(n - 2, int(n * float(train_fraction))))
    validation_end = max(train_end + 1, min(n - 1, int(n * (float(train_fraction) + float(validation_fraction)))))
    return {
        "train": (0, train_end),
        "validation": (train_end, validation_end),
        "test": (validation_end, n),
    }
