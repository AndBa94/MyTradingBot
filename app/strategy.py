from math import isfinite
from statistics import median
from app.models import Opportunity


def _f(value, default=0.0):
    try:
        x = float(value)
        return x if isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _book(levels):
    out = []
    for row in levels or []:
        try:
            price = _f(row[0])
            qty = _f(row[1])
        except (IndexError, TypeError):
            continue
        if price > 0 and qty > 0:
            out.append((price, qty, price * qty))
    return out


def _median(values, default=0.0):
    vals = sorted(v for v in values if v > 0 and isfinite(v))
    return median(vals) if vals else default


def _atr(candles, period=20):
    rows = candles[-period - 1:]
    if len(rows) < 3:
        return 0.0
    trs = []
    for i in range(1, len(rows)):
        c = rows[i]
        prev_close = rows[i - 1].close
        trs.append(max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close)))
    return _median(trs, 0.0)


def _wall_levels(levels, current, direction, max_distance_pct=0.012):
    rows = _book(levels)
    if not rows or current <= 0:
        return []

    if direction == "ABOVE":
        candidates = [
            r for r in rows
            if r[0] > current and (r[0] - current) / current <= max_distance_pct
        ]
    else:
        candidates = [
            r for r in rows
            if r[0] < current and (current - r[0]) / current <= max_distance_pct
        ]

    if len(candidates) < 3:
        return []

    baseline = _median([r[2] for r in candidates], 0.0)
    if baseline <= 0:
        return []

    # A wall must be meaningfully larger than nearby visible liquidity.
    # We do not use rounded/whole-number prices because Bybit contracts have
    # different tick sizes and many liquid coins trade far below $10.
    walls = [
        (price, notional, notional / baseline)
        for price, _, notional in candidates
        if notional >= baseline * 2.5
    ]

    if direction == "ABOVE":
        walls.sort(key=lambda x: x[0])
    else:
        walls.sort(key=lambda x: x[0], reverse=True)
    return walls


def _nearest_level(levels, current, direction, max_distance_pct):
    rows = _book(levels)
    if direction == "ABOVE":
        candidates = [
            r for r in rows
            if r[0] > current and (r[0] - current) / current <= max_distance_pct
        ]
        return min(candidates, key=lambda r: r[0]) if candidates else None

    candidates = [
        r for r in rows
        if r[0] < current and (current - r[0]) / current <= max_distance_pct
    ]
    return max(candidates, key=lambda r: r[0]) if candidates else None


def _body_strength(candle):
    rng = max(candle.high - candle.low, candle.close * 1e-9)
    return abs(candle.close - candle.open) / rng


def _close_location(candle):
    rng = max(candle.high - candle.low, candle.close * 1e-9)
    return (candle.close - candle.low) / rng


def _book_stats(orderbook):
    bids = _book(orderbook.get("bids", []))
    asks = _book(orderbook.get("asks", []))
    bid_total = sum(x[2] for x in bids[:12])
    ask_total = sum(x[2] for x in asks[:12])
    total = bid_total + ask_total
    imbalance = bid_total / total if total > 0 else 0.5
    return bids, asks, imbalance


def _previous_wall(previous_orderbook, current, direction):
    if not previous_orderbook:
        return None

    side = "asks" if direction == "ABOVE" else "bids"
    walls = _wall_levels(
        previous_orderbook.get(side, []),
        current,
        direction,
        max_distance_pct=0.015,
    )
    return walls[0] if walls else None


def _previous_imbalance(previous_orderbook):
    if not previous_orderbook:
        return None
    _, _, imbalance = _book_stats(previous_orderbook)
    return imbalance


def _flow_delta(previous_orderbook, current_imbalance):
    previous = _previous_imbalance(previous_orderbook)
    if previous is None:
        return 0.0
    return current_imbalance - previous


def _trend(candles):
    closes = [c.close for c in candles[-24:]]
    if len(closes) < 12:
        return 0

    fast = sum(closes[-6:]) / 6
    slow = sum(closes[-18:]) / 18

    if fast > slow * 1.0008:
        return 1
    if fast < slow * 0.9992:
        return -1
    return 0



def _higher_timeframe_trend(candles, bucket=3):
    """Build a completed higher-timeframe trend from closed 5m candles."""
    if len(candles) < 36:
        return 0

    rows = []
    for i in range(0, len(candles) - bucket + 1, bucket):
        chunk = candles[i:i + bucket]
        if len(chunk) != bucket:
            continue
        rows.append(
            (
                chunk[0].open,
                max(c.high for c in chunk),
                min(c.low for c in chunk),
                chunk[-1].close,
                sum(c.volume for c in chunk),
            )
        )

    if len(rows) < 12:
        return 0

    closes = [row[3] for row in rows[-12:]]
    fast = sum(closes[-4:]) / 4
    slow = sum(closes[-9:]) / 9

    if fast > slow * 1.0010:
        return 1
    if fast < slow * 0.9990:
        return -1
    return 0

def _market_regime(candles, atr, trend):
    """Classify the recent 5m market without using the forming candle."""
    if not candles or atr <= 0:
        return "UNKNOWN"

    price = max(candles[-1].close, 1e-9)
    atr_pct = atr / price

    if atr_pct >= 0.006:
        return "HIGH_VOL"
    if trend > 0:
        return "TREND_UP"
    if trend < 0:
        return "TREND_DOWN"
    return "RANGE"


def _rising_lows(candles, n=3):
    rows = candles[-n:]
    return len(rows) == n and all(rows[i].low >= rows[i - 1].low for i in range(1, n))


def _falling_highs(candles, n=3):
    rows = candles[-n:]
    return len(rows) == n and all(rows[i].high <= rows[i - 1].high for i in range(1, n))


def _make_targets(side, entry, stop, first_level, second_level, atr):
    risk = abs(entry - stop)
    if risk <= 0:
        return []

    min_step = max(entry * 0.0007, atr * 0.15)

    if side == "LONG":
        candidates = [
            entry + max(risk * 1.40, min_step),
            entry + max(risk * 2.40, min_step * 1.5),
            entry + max(risk * 3.50, min_step * 2.0),
        ]
        if first_level and first_level > entry:
            candidates[0] = min(candidates[0], first_level * 0.999)
        if second_level and second_level > entry:
            candidates[1] = min(candidates[1], second_level * 0.999)
            candidates[2] = min(candidates[2], second_level * 0.999)

        targets = []
        for x in candidates:
            if x > entry and (not targets or x > targets[-1]):
                targets.append(x)
        return targets

    candidates = [
        entry - max(risk * 1.40, min_step),
        entry - max(risk * 2.40, min_step * 1.5),
        entry - max(risk * 3.50, min_step * 2.0),
    ]
    if first_level and first_level < entry:
        candidates[0] = max(candidates[0], first_level * 1.001)
    if second_level and second_level < entry:
        candidates[1] = max(candidates[1], second_level * 1.001)
        candidates[2] = max(candidates[2], second_level * 1.001)

    targets = []
    for x in candidates:
        if x < entry and (not targets or x < targets[-1]):
            targets.append(x)
    return targets


class SmartStrategy:
    """Microstructure scalper using liquidity walls, levels, pressure and volume.

    Two setups are supported:
    - WALL_BOUNCE: price tests visible liquidity and rejects it.
    - LEVEL_BREAKOUT: price breaks a recent level/wall with pressure and volume.

    A single visible wall is never enough to enter.
    """

    def analyze(
        self,
        m,
        candles,
        orderbook=None,
        tp_count=3,
        previous_orderbook=None,
    ):
        if len(candles) < 40 or not orderbook or m.last <= 0:
            return None

        last = candles[-1]
        prev = candles[-2]
        prev2 = candles[-3]

        avg_volume = _median([c.volume for c in candles[-21:-1]], 0.0)
        volume_ratio = last.volume / max(avg_volume, 1e-9)
        atr = _atr(candles, 20)
        if atr <= 0:
            return None

        bids, asks, imbalance = _book_stats(orderbook)
        if not bids or not asks:
            return None

        entry_long = max(_f(m.ask), _f(m.last))
        entry_short = min(_f(m.bid), _f(m.last))
        spread = max(_f(m.ask) - _f(m.bid), 0.0)
        spread_pct = spread / max(m.last, 1e-9)

        # The signal is based on a completed candle, so do not chase a move that
        # has already travelled too far before the live entry price is reached.
        if max(
            abs(entry_long - last.close) / entry_long,
            abs(entry_short - last.close) / entry_short,
        ) > 0.0045:
            return None

        # Extremely wide spreads make a few-minute scalp economically fragile.
        if spread_pct > 0.0012:
            return None

        bid_walls = _wall_levels(bids, m.last, "BELOW")
        ask_walls = _wall_levels(asks, m.last, "ABOVE")
        support = bid_walls[0] if bid_walls else None
        resistance = ask_walls[0] if ask_walls else None

        # 24 previous 5m candles = roughly two hours. This is deliberately
        # more responsive than the old 4h-only breakout level.
        recent_high = max(c.high for c in candles[-25:-1])
        recent_low = min(c.low for c in candles[-25:-1])

        trend = _trend(candles)
        trend15 = _higher_timeframe_trend(candles)
        regime = _market_regime(candles, atr, trend)
        flow_delta = _flow_delta(previous_orderbook, imbalance)
        funding_rate = _f(m.funding_rate)

        # Funding is a secondary crowding filter, never a standalone signal.
        # Avoid opening into unusually crowded positioning when the order-flow
        # is already marginal.
        funding_long_block = funding_rate >= 0.0008
        funding_short_block = funding_rate <= -0.0008
        body = _body_strength(last)
        close_loc = _close_location(last)
        bullish = last.close > last.open
        bearish = last.close < last.open
        pressure_long = _rising_lows(candles, 3) and last.close >= prev.close >= prev2.close
        pressure_short = _falling_highs(candles, 3) and last.close <= prev.close <= prev2.close

        candidates = []

        # -------------------- WALL BOUNCE: LONG --------------------
        if support:
            dist = (entry_long - support[0]) / entry_long
            bounce = (
                dist <= 0.0045
                and last.low <= support[0] * 1.0015
                and last.close > support[0]
                and bullish
                and body >= 0.45
                and close_loc >= 0.60
                and (
                    (trend == 1 and trend15 >= 0)
                    or (
                        regime == "RANGE"
                        and trend15 == 0
                        and volume_ratio >= 1.15
                        and imbalance >= 0.58
                    )
                )
                and volume_ratio >= 1.10
                and imbalance >= 0.56
                and flow_delta >= 0.005
                and not funding_long_block
            )
            if bounce:
                stop = support[0] - max(
                    atr * 0.45,
                    spread * 2.0,
                    entry_long * 0.0008,
                )
                resistance_level = resistance[0] if resistance else recent_high
                targets = _make_targets(
                    "LONG",
                    entry_long,
                    stop,
                    resistance_level,
                    recent_high,
                    atr,
                )
                if targets:
                    score = 0.55
                    if imbalance >= 0.58:
                        score += 0.07
                    if volume_ratio >= 1.15:
                        score += 0.06
                    if trend == 1:
                        score += 0.04
                    elif regime == "RANGE":
                        score += 0.02
                    if support[2] >= 3:
                        score += 0.05
                    if flow_delta >= 0.02:
                        score += 0.04
                    candidates.append(
                        ("LONG", "WALL_BOUNCE", score, stop, targets, support, resistance)
                    )

        # -------------------- WALL REJECTION: SHORT --------------------
        if resistance:
            dist = (resistance[0] - entry_short) / entry_short
            bounce = (
                dist <= 0.0045
                and last.high >= resistance[0] * 0.9985
                and last.close < resistance[0]
                and bearish
                and body >= 0.45
                and close_loc <= 0.40
                and (
                    (trend == -1 and trend15 <= 0)
                    or (
                        regime == "RANGE"
                        and trend15 == 0
                        and volume_ratio >= 1.15
                        and imbalance <= 0.42
                    )
                )
                and volume_ratio >= 1.10
                and imbalance <= 0.44
                and flow_delta <= -0.005
                and not funding_short_block
            )
            if bounce:
                stop = resistance[0] + max(
                    atr * 0.45,
                    spread * 2.0,
                    entry_short * 0.0008,
                )
                support_level = support[0] if support else recent_low
                targets = _make_targets(
                    "SHORT",
                    entry_short,
                    stop,
                    support_level,
                    recent_low,
                    atr,
                )
                if targets:
                    score = 0.55
                    if imbalance <= 0.42:
                        score += 0.07
                    if volume_ratio >= 1.15:
                        score += 0.06
                    if trend == -1:
                        score += 0.04
                    elif regime == "RANGE":
                        score += 0.02
                    if resistance[2] >= 3:
                        score += 0.05
                    if flow_delta <= -0.02:
                        score += 0.04
                    candidates.append(
                        ("SHORT", "WALL_REJECTION", score, stop, targets, support, resistance)
                    )

        # -------------------- LEVEL BREAKOUT: LONG --------------------
        prior_resistance = _previous_wall(previous_orderbook, m.last, "ABOVE")
        breakout_level = max(
            [
                x
                for x in [
                    prior_resistance[0] if prior_resistance else 0,
                    recent_high,
                ]
                if x > 0
            ],
            default=0,
        )

        long_break = (
            breakout_level > 0
            and last.close > breakout_level * 1.0005
            and prev.close <= breakout_level * 1.0008
            and last.low <= breakout_level * 1.0015
            and bullish
            and body >= (0.60 if regime == "HIGH_VOL" else 0.50)
            and close_loc >= (0.78 if regime == "HIGH_VOL" else 0.70)
            and pressure_long
            and trend == 1
            and trend15 == 1
            and volume_ratio >= (1.55 if regime == "HIGH_VOL" else 1.30)
            and imbalance >= (0.57 if regime == "HIGH_VOL" else 0.55)
            and flow_delta >= 0.005
            and not funding_long_block
            and (last.close - breakout_level) / breakout_level <= 0.006
        )

        if long_break:
            stop = breakout_level - max(
                atr * 0.45,
                spread * 2.0,
                entry_long * 0.0009,
            )
            targets = _make_targets(
                "LONG",
                entry_long,
                stop,
                resistance[0] if resistance else 0,
                breakout_level + atr * 1.5,
                atr,
            )
            if targets:
                score = 0.58
                if volume_ratio >= 1.50:
                    score += 0.08
                if imbalance >= 0.58:
                    score += 0.06
                if prior_resistance:
                    score += 0.05
                if trend >= 0:
                    score += 0.04
                if flow_delta >= 0.02:
                    score += 0.04
                candidates.append(
                    ("LONG", "LEVEL_BREAKOUT", score, stop, targets, support, resistance)
                )

        # -------------------- LEVEL BREAKOUT: SHORT --------------------
        prior_support = _previous_wall(previous_orderbook, m.last, "BELOW")
        breakout_level = min(
            [
                x
                for x in [
                    prior_support[0] if prior_support else 0,
                    recent_low,
                ]
                if x > 0
            ],
            default=0,
        )

        short_break = (
            breakout_level > 0
            and last.close < breakout_level * 0.9995
            and prev.close >= breakout_level * 0.9992
            and last.high >= breakout_level * 0.9985
            and bearish
            and body >= (0.60 if regime == "HIGH_VOL" else 0.50)
            and close_loc <= (0.22 if regime == "HIGH_VOL" else 0.30)
            and pressure_short
            and trend == -1
            and trend15 == -1
            and volume_ratio >= (1.55 if regime == "HIGH_VOL" else 1.30)
            and imbalance <= (0.43 if regime == "HIGH_VOL" else 0.45)
            and flow_delta <= -0.005
            and not funding_short_block
            and (breakout_level - last.close) / breakout_level <= 0.006
        )

        if short_break:
            stop = breakout_level + max(
                atr * 0.45,
                spread * 2.0,
                entry_short * 0.0009,
            )
            targets = _make_targets(
                "SHORT",
                entry_short,
                stop,
                support[0] if support else 0,
                breakout_level - atr * 1.5,
                atr,
            )
            if targets:
                score = 0.58
                if volume_ratio >= 1.50:
                    score += 0.08
                if imbalance <= 0.42:
                    score += 0.06
                if prior_support:
                    score += 0.05
                if trend <= 0:
                    score += 0.04
                if flow_delta <= -0.02:
                    score += 0.04
                candidates.append(
                    ("SHORT", "LEVEL_BREAKOUT", score, stop, targets, support, resistance)
                )

        if not candidates:
            return None

        # Only the strongest setup is allowed. This prevents contradictory
        # LONG/SHORT entries from the same candle.
        side, setup, confidence, sl, tp, support, resistance = max(
            candidates,
            key=lambda x: x[2],
        )
        entry = entry_long if side == "LONG" else entry_short

        if side == "LONG" and sl >= entry:
            return None
        if side == "SHORT" and sl <= entry:
            return None

        risk = abs(entry - sl)
        if risk <= max(spread, entry * 0.0003):
            return None
        if risk / entry > 0.012:
            return None

        tp = [
            x for x in tp
            if (x > entry if side == "LONG" else x < entry)
        ]
        tp = tp[:max(1, min(5, int(tp_count)))]
        if not tp:
            return None

        first_move = abs(tp[0] - entry) / entry
        final_move = abs(tp[-1] - entry) / entry
        first_r = abs(tp[0] - entry) / risk

        # Do not enter when the first partial exit is closer than the risk.
        # A previous version could cap TP1 at a nearby level and accidentally
        # create a sub-1R first target, which is unfavorable for a fast scalp.
        # 1.20R leaves room for fees/slippage while still allowing frequent
        # partial exits. The target curve itself is wider (1.4R / 2.4R / 3.5R)
        # so the runner has enough distance to matter.
        if first_r < 1.20:
            return None

        # Paper/live taker economics: spread + estimated round-trip fees.
        # This is deliberately checked against TP1, not only the final target.
        round_trip_cost = spread_pct + 2.0 * 0.00055
        if first_move <= round_trip_cost * 1.20:
            return None
        if final_move <= round_trip_cost * 1.60:
            return None

        confidence = min(0.92, max(0.50, confidence))
        reasons = [
            f"setup={setup}",
            f"regime={regime}",
            f"trend5={'UP' if trend > 0 else 'DOWN' if trend < 0 else 'FLAT'}",
            f"trend15={'UP' if trend15 > 0 else 'DOWN' if trend15 < 0 else 'FLAT'}",
            f"volume_x={volume_ratio:.2f}",
            f"book_imbalance={imbalance:.3f}",
            f"flow_delta={flow_delta:+.3f}",
            f"funding={funding_rate:+.5f}",
            f"spread_bps={m.spread_bps:.2f}",
            f"atr_pct={atr / m.last * 100:.3f}",
            f"risk_pct={risk / entry * 100:.3f}",
            f"tp1_move_pct={first_move * 100:.3f}",
            f"tp1_r={first_r:.2f}",
            f"net_cost_est_pct={round_trip_cost * 100:.3f}",
            f"breakout_retest={'YES' if setup == 'LEVEL_BREAKOUT' else 'N/A'}",
        ]

        if support:
            reasons.append(f"support={support[0]:.8g}")
        if resistance:
            reasons.append(f"resistance={resistance[0]:.8g}")
        if prior_resistance:
            reasons.append(f"prev_ask_wall={prior_resistance[0]:.8g}")
        if prior_support:
            reasons.append(f"prev_bid_wall={prior_support[0]:.8g}")

        return Opportunity(
            m.symbol,
            side,
            "MICROSTRUCTURE",
            setup,
            confidence,
            final_move,
            entry,
            sl,
            tp,
            reasons,
        )
