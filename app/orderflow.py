"""Order-flow analytics independent from trade execution.

The functions here operate only on Bybit order-book snapshots and can later be
fed by either REST snapshots or the public WebSocket stream. They never place
orders and contain no trading decisions.
"""
from __future__ import annotations

from math import isfinite


def _f(value, default=0.0):
    try:
        x = float(value)
        return x if isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _levels(rows):
    out = []
    for row in rows or []:
        try:
            price, qty = _f(row[0]), _f(row[1])
        except (IndexError, TypeError):
            continue
        if price > 0 and qty > 0:
            out.append((price, qty, price * qty))
    return out


def _notional(rows, depth):
    return sum(level[2] for level in _levels(rows)[:max(1, int(depth))])


def _top_price(book, side):
    rows = _levels(book.get(side, []))
    if not rows:
        return 0.0
    return rows[0][0]


def _weighted_imbalance(book, depth=12):
    bid = _notional(book.get("bids", []), depth)
    ask = _notional(book.get("asks", []), depth)
    total = bid + ask
    return bid / total if total > 0 else 0.5


def _relative_change(current, previous):
    if previous <= 0:
        return 0.0
    return (current - previous) / previous


def analyze_orderflow(current, previous=None, depth=12):
    """Return normalized microstructure features.

    imbalance is bid-notional / total-notional, so 0.5 is neutral.
    flow_delta measures change in that imbalance between snapshots.
    bid_change/ask_change describe near-book notional changes.
    pressure combines imbalance persistence and directional change.
    """
    current = current or {}
    previous = previous or {}

    imbalance = _weighted_imbalance(current, depth)
    previous_imbalance = _weighted_imbalance(previous, depth) if previous else 0.5
    flow_delta = imbalance - previous_imbalance if previous else 0.0

    bid_now = _notional(current.get("bids", []), depth)
    ask_now = _notional(current.get("asks", []), depth)
    bid_prev = _notional(previous.get("bids", []), depth) if previous else bid_now
    ask_prev = _notional(previous.get("asks", []), depth) if previous else ask_now

    bid_change = _relative_change(bid_now, bid_prev)
    ask_change = _relative_change(ask_now, ask_prev)

    bid = _top_price(current, "bids")
    ask = _top_price(current, "asks")
    spread_bps = ((ask - bid) / ((ask + bid) / 2.0) * 10000.0) if bid > 0 and ask > 0 else 0.0

    pressure = (imbalance - 0.5) * 2.0 + flow_delta * 2.0

    return {
        "imbalance": imbalance,
        "previous_imbalance": previous_imbalance,
        "flow_delta": flow_delta,
        "bid_notional": bid_now,
        "ask_notional": ask_now,
        "bid_change": bid_change,
        "ask_change": ask_change,
        "spread_bps": spread_bps,
        "pressure": pressure,
        "healthy": bool(bid > 0 and ask > 0 and spread_bps >= 0),
    }


def directional_pressure(features, side):
    """Map pressure to a 0..1 directional strength."""
    pressure = _f(features.get("pressure"))
    side = str(side).upper()
    if side == "SHORT":
        pressure = -pressure
    return max(0.0, min(1.0, 0.5 + pressure * 0.5))
