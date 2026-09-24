from math import isfinite
from statistics import mean, median
from app.models import Opportunity


def _f(v, default=0.0):
    try:
        x = float(v)
        return x if isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    e = mean(values[:period])
    for v in values[period:]:
        e = v * k + e * (1.0 - k)
    return e


def _rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for a, b in zip(values[-period - 1:-1], values[-period:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    gain, loss = mean(gains), mean(losses)
    if loss <= 1e-12:
        return 100.0 if gain > 0 else 50.0
    rs = gain / loss
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for prev, cur in zip(candles[-period - 1:-1], candles[-period:]):
        trs.append(max(cur.high - cur.low,
                       abs(cur.high - prev.close),
                       abs(cur.low - prev.close)))
    return mean(trs)


def _book(levels):
    out = []
    for row in levels or []:
        try:
            price, qty = _f(row[0]), _f(row[1])
        except (IndexError, TypeError):
            continue
        if price > 0 and qty > 0:
            out.append((price, qty, price * qty))
    return out


def _book_imbalance(orderbook):
    bids = _book(orderbook.get("bids", []))
    asks = _book(orderbook.get("asks", []))
    if not bids or not asks:
        return None
    bid = sum(x[2] for x in bids[:12])
    ask = sum(x[2] for x in asks[:12])
    total = bid + ask
    return bid / total if total > 0 else 0.5


def _flow_delta(previous_orderbook, current):
    if not previous_orderbook or current is None:
        return 0.0
    previous = _book_imbalance(previous_orderbook)
    return 0.0 if previous is None else current - previous


def _volume_ratio(candles):
    if len(candles) < 21:
        return 0.0
    baseline = median(c.volume for c in candles[-21:-1])
    return candles[-1].volume / max(baseline, 1e-9)


def _momentum(values, lookback=3):
    if len(values) <= lookback:
        return 0.0
    return (values[-1] - values[-1 - lookback]) / max(abs(values[-1 - lookback]), 1e-9)


def _trend_direction(closes):
    fast, slow = _ema(closes, 20), _ema(closes, 50)
    if fast is None or slow is None:
        return 0, None, None
    if fast > slow * 1.0004:
        return 1, fast, slow
    if fast < slow * 0.9996:
        return -1, fast, slow
    return 0, fast, slow


def _higher_trend(closes, bucket=3):
    if len(closes) < 60:
        return 0
    rows = []
    for i in range(0, len(closes) - bucket + 1, bucket):
        chunk = closes[i:i + bucket]
        if len(chunk) == bucket:
            rows.append(chunk[-1])
    if len(rows) < 20:
        return 0
    fast, slow = _ema(rows[-30:], 8), _ema(rows[-30:], 18)
    if fast is None or slow is None:
        return 0
    if fast > slow * 1.0007:
        return 1
    if fast < slow * 0.9993:
        return -1
    return 0


def _score_components(side, imbalance, flow, volume_ratio, momentum, rsi,
                      trend, trend15, spread_bps, atr_pct, rr, body):
    if side == "LONG":
        book = 25 if imbalance >= 0.60 else 20 if imbalance >= 0.56 else 12 if imbalance >= 0.53 else 0
        flow_score = 10 if flow >= 0.025 else 7 if flow >= 0.010 else 4 if flow >= -0.005 else 0
        rsi_score = 5 if 52 <= rsi <= 68 else 3 if 50 <= rsi <= 72 else 0
    else:
        book = 25 if imbalance <= 0.40 else 20 if imbalance <= 0.44 else 12 if imbalance <= 0.47 else 0
        flow_score = 10 if flow <= -0.025 else 7 if flow <= -0.010 else 4 if flow <= 0.005 else 0
        rsi_score = 5 if 32 <= rsi <= 48 else 3 if 28 <= rsi <= 50 else 0

    mom = abs(momentum)
    momentum_score = 15 if mom >= 0.0025 else 11 if mom >= 0.0015 else 7 if mom >= 0.0008 else 0
    trend_score = 12 if trend == (1 if side == "LONG" else -1) else 0
    volume_score = 10 if volume_ratio >= 1.50 else 8 if volume_ratio >= 1.25 else 5 if volume_ratio >= 1.05 else 0
    spread_score = 5 if spread_bps <= 5 else 3 if spread_bps <= 8 else 1 if spread_bps <= 12 else 0
    volatility_score = 5 if 0.10 <= atr_pct <= 0.70 else 3 if atr_pct < 0.90 else 0
    rr_score = 5 if rr >= 2.4 else 4 if rr >= 2.0 else 2 if rr >= 1.6 else 0
    candle_score = 5 if body >= 0.65 else 3 if body >= 0.50 else 0
    if trend15 == (1 if side == "LONG" else -1):
        trend_score = min(12, trend_score + 1)

    return {
        "order_book": min(25, book),
        "momentum": min(15, momentum_score),
        "trend": min(12, trend_score),
        "rsi": min(5, rsi_score),
        "flow": min(10, flow_score),
        "volume": min(10, volume_score),
        "spread": min(5, spread_score),
        "volatility": min(5, volatility_score),
        "risk_reward": min(5, rr_score),
        "candle": min(5, candle_score),
    }


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
                and flow_delta >= -0.005
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
                    if trend15 == 1:
                        score += 0.03
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
                and flow_delta <= 0.005
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
                    if trend15 == -1:
                        score += 0.03
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
            and trend15 >= 0
            and volume_ratio >= (1.55 if regime == "HIGH_VOL" else 1.30)
            and imbalance >= (0.57 if regime == "HIGH_VOL" else 0.55)
            and flow_delta >= -0.005
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
            and trend15 <= 0
            and volume_ratio >= (1.55 if regime == "HIGH_VOL" else 1.30)
            and imbalance <= (0.43 if regime == "HIGH_VOL" else 0.45)
            and flow_delta <= 0.005
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

        # -------------------- MOMENTUM CONTINUATION --------------------
        # Independent from wall/breakout triggers: trend + candle impulse +
        # participation + order-book agreement. It is intentionally scored,
        # not forced to pass every feature at once.
        momentum_long = (
            trend == 1
            and trend15 >= 0
            and bullish
            and body >= 0.55
            and close_loc >= 0.72
            and last.close > prev.close
            and volume_ratio >= 1.15
            and imbalance >= 0.55
            and flow_delta >= -0.01
            and not funding_long_block
        )
        if momentum_long:
            stop = min(
                prev.low,
                last.low,
            ) - max(atr * 0.50, spread * 2.0, entry_long * 0.0009)
            targets = _make_targets(
                "LONG", entry_long, stop,
                resistance[0] if resistance else recent_high,
                recent_high,
                atr,
            )
            if targets:
                score = 0.54
                if trend15 == 1: score += 0.06
                if volume_ratio >= 1.40: score += 0.07
                if imbalance >= 0.58: score += 0.06
                if body >= 0.70: score += 0.04
                candidates.append(
                    ("LONG", "MOMENTUM_CONTINUATION", score, stop, targets, support, resistance)
                )

        momentum_short = (
            trend == -1
            and trend15 <= 0
            and bearish
            and body >= 0.55
            and close_loc <= 0.28
            and last.close < prev.close
            and volume_ratio >= 1.15
            and imbalance <= 0.45
            and flow_delta <= 0.01
            and not funding_short_block
        )
        if momentum_short:
            stop = max(
                prev.high,
                last.high,
            ) + max(atr * 0.50, spread * 2.0, entry_short * 0.0009)
            targets = _make_targets(
                "SHORT", entry_short, stop,
                support[0] if support else recent_low,
                recent_low,
                atr,
            )
            if targets:
                score = 0.54
                if trend15 == -1: score += 0.06
                if volume_ratio >= 1.40: score += 0.07
                if imbalance <= 0.42: score += 0.06
                if body >= 0.70: score += 0.04
                candidates.append(
                    ("SHORT", "MOMENTUM_CONTINUATION", score, stop, targets, support, resistance)
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

        setup_confidence = min(0.92, max(0.50, confidence))
        confidence = setup_confidence
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

        opportunity = Opportunity(
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

        score_10, components = evaluate_opportunity(opportunity)
        opportunity.score_10 = score_10
        opportunity.score_components = components
        opportunity.confidence = score_10 / 10.0
        opportunity.decision = "ENTER" if score_10 >= 7.5 else "WAIT"
        opportunity.reasons.extend([
            f"setup_confidence={setup_confidence:.3f}",
            f"score_10={score_10:.2f}",
            f"trend_score={components['trend']:.1f}",
            f"volume_score={components['volume']:.1f}",
            f"order_book_score={components['order_book']:.1f}",
            f"momentum_score={components['momentum']:.1f}",
            f"volatility_score={components['volatility']:.1f}",
            f"risk_reward_score={components['risk_reward']:.1f}",
            f"cost_score={components['cost']:.1f}",
        ])
        return opportunity
