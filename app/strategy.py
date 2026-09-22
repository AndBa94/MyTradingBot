import math
from statistics import mean
from app.models import Opportunity


def _whole_price(value, side=None):
    """Return a tradable whole-number price.

    LONG entries are rounded upward and SHORT entries downward so the paper
    fill is not more optimistic than the live bid/ask.
    """
    if side == "LONG":
        return float(math.ceil(value))
    if side == "SHORT":
        return float(math.floor(value))
    return float(round(value))


def _aggregate_book(levels):
    """Aggregate order-book quantity by whole-number price."""
    out = {}
    for row in levels or []:
        try:
            price = float(row[0])
            qty = float(row[1])
            if price <= 0 or qty <= 0:
                continue
            level = int(round(price))
            if level <= 0:
                continue
            out[level] = out.get(level, 0.0) + price * qty
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _wall_levels(levels, current, direction):
    """Find strong whole-price liquidity walls without requiring extreme outliers."""
    book = _aggregate_book(levels)
    candidates = [
        (price, notional)
        for price, notional in book.items()
        if (price > current if direction == "ABOVE" else price < current)
    ]
    if len(candidates) < 3:
        return []

    sizes = sorted(x[1] for x in candidates)
    median = sizes[len(sizes) // 2]

    # 3x the median was too restrictive for a live book: a valid wall can be
    # only 2x the surrounding liquidity. Keep the wall definition selective,
    # but do not require an extreme single-order outlier.
    threshold = max(median * 2.0, 1.0)

    walls = [(price, size) for price, size in candidates if size >= threshold]
    walls.sort(key=lambda x: x[0])
    return walls


def _select_support(walls, entry):
    below = [(p, v) for p, v in walls if p < entry]
    if not below:
        return None
    return min(below, key=lambda x: (entry - x[0], -x[1]))


def _select_resistance(walls, entry):
    above = [(p, v) for p, v in walls if p > entry]
    if not above:
        return None
    return min(above, key=lambda x: (x[0] - entry, -x[1]))


class SmartStrategy:
    """Liquidity-wall scalper.

    Order-book liquidity remains the primary signal. EMA/RSI/MACD are not used
    for entry. A trade requires a visible wall, an actual price reaction at
    that wall, and enough liquidity ahead to build the TP path.
    """

    def analyze(self, m, candles, orderbook=None, tp_count=3):
        if len(candles) < 30 or not orderbook:
            return None

        # Whole-number price logic becomes too coarse on very cheap coins.
        if m.last < 10:
            return None

        last, prev = candles[-1], candles[-2]
        avg_volume = mean(c.volume for c in candles[-21:-1])
        volume_ratio = last.volume / max(avg_volume, 1e-9)

        entry_long = _whole_price(m.ask, "LONG")
        entry_short = _whole_price(m.bid, "SHORT")

        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])

        bid_walls = _wall_levels(bids, m.last, "BELOW")
        ask_walls = _wall_levels(asks, m.last, "ABOVE")

        support = _select_support(bid_walls, entry_long)
        resistance = _select_resistance(ask_walls, entry_short)

        # A reaction is now tied to the wall being touched/reclaimed, not just
        # a green/red candle somewhere above/below it.
        long_tolerance = max(1.0, entry_long * 0.003)
        short_tolerance = max(1.0, entry_short * 0.003)

        long_reaction = (
            support is not None
            and last.low <= support[0] + long_tolerance
            and last.close >= support[0]
            and last.close > last.open
            and last.close >= prev.close
            and volume_ratio >= 0.8
        )

        short_reaction = (
            resistance is not None
            and last.high >= resistance[0] - short_tolerance
            and last.close <= resistance[0]
            and last.close < last.open
            and last.close <= prev.close
            and volume_ratio >= 0.8
        )

        side = None
        setup = None
        entry = None

        if long_reaction:
            side = "LONG"
            setup = "BID_WALL_BOUNCE"
            entry = entry_long
        elif short_reaction:
            side = "SHORT"
            setup = "ASK_WALL_REJECTION"
            entry = entry_short

        if side is None:
            return None

        # TP structure:
        # TP1 before the first large wall.
        # TP2 after that wall but before the next large wall.
        # TP3 before the next large wall.
        if side == "LONG":
            above_walls = sorted(
                [(p, v) for p, v in ask_walls if p > entry],
                key=lambda x: x[0],
            )
            if len(above_walls) < 2:
                return None
            wall1 = above_walls[0]
            wall2 = above_walls[1]
            tp1 = wall1[0] - 1
            tp2 = wall1[0] + max(1, (wall2[0] - wall1[0]) // 3)
            tp2 = min(tp2, wall2[0] - 1)
            tp3 = wall2[0] - 1
            tp = [float(tp1), float(tp2), float(tp3)]

            if support is None:
                return None
            sl = float(support[0] - 1)
        else:
            below_walls = sorted(
                [(p, v) for p, v in bid_walls if p < entry],
                key=lambda x: x[0],
                reverse=True,
            )
            if len(below_walls) < 2:
                return None
            wall1 = below_walls[0]
            wall2 = below_walls[1]
            tp1 = wall1[0] + 1
            tp2 = wall1[0] - max(1, (wall1[0] - wall2[0]) // 3)
            tp2 = max(tp2, wall2[0] + 1)
            tp3 = wall2[0] + 1
            tp = [float(tp1), float(tp2), float(tp3)]

            if resistance is None:
                return None
            sl = float(resistance[0] + 1)

        tp = [float(int(round(x))) for x in tp]
        sl = float(int(round(sl)))
        entry = float(int(round(entry)))

        if side == "LONG":
            tp = sorted(set(x for x in tp if x > entry))
            if sl >= entry or not tp:
                return None
        else:
            tp = sorted(set((x for x in tp if x < entry), reverse=True))
            if sl <= entry or not tp:
                return None

        risk = abs(entry - sl)
        if risk <= 0 or risk / entry > 0.02:
            return None

        expected_move = abs(tp[-1] - entry) / entry
        costs = m.spread_bps / 10000 + abs(m.funding_rate)

        # PAPER uses taker fees on both entry and exit. The old filter only
        # considered spread/funding, so a TP could be labelled TAKE_PROFIT
        # while still losing money after fees. Require the FIRST target itself
        # to clear the round-trip trading cost with a safety buffer, not only
        # the final TP3.
        paper_fee = 0.00055
        round_trip_cost = costs + (2.0 * paper_fee)
        first_move = abs(tp[0] - entry) / entry
        if first_move <= round_trip_cost * 1.05:
            return None
        if expected_move <= round_trip_cost * 1.20:
            return None

        # Confidence is based on setup quality, not an arbitrary need for a
        # huge volume spike. A valid wall reaction starts above the auto-entry
        # threshold; extra volume/wall dominance increases confidence.
        confidence = 0.62
        if volume_ratio >= 1.2:
            confidence += 0.08
        if wall1[1] >= max(
            support[1] if support else 0,
            resistance[1] if resistance else 0,
        ):
            confidence += 0.04
        confidence = min(0.92, confidence)

        reasons = [
            f"book_support={support[0] if support else 0:.0f}",
            f"book_resistance={resistance[0] if resistance else 0:.0f}",
            f"wall1={wall1[0]:.0f}",
            f"wall2={wall2[0]:.0f}",
            f"volume_x={volume_ratio:.2f}",
            f"spread_bps={m.spread_bps:.2f}",
            f"tp1_move_pct={first_move*100:.3f}",
            f"round_trip_cost_pct={round_trip_cost*100:.3f}",
            "price_levels=WHOLE",
        ]

        return Opportunity(
            m.symbol,
            side,
            "LIQUIDITY",
            setup,
            confidence,
            expected_move,
            entry,
            sl,
            tp[:max(1, min(5, int(tp_count)))],
            reasons,
        )
