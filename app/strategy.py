from statistics import mean
from app.models import Opportunity

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = mean(values[:period])
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    return mean(
        max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        for p, c in zip(candles[-period-1:-1], candles[-period:])
    )

def _tp_multipliers(count):
    # The number of TP stages changes how the risk is distributed.
    # A single TP is deliberately farther away; multiple TPs spread
    # the exits while keeping the average target meaningfully above 1R.
    return {
        1: [2.6],
        2: [1.8, 3.4],
        3: [1.5, 2.5, 3.5],
        4: [1.5, 2.2, 2.9, 3.6],
        5: [1.4, 2.1, 2.8, 3.5, 4.2],
    }.get(max(1, min(5, int(count))), [1.5, 2.5, 3.5])

class SmartStrategy:
    def analyze(self, m, candles, tp_count=3):
        if len(candles) < 60:
            return None

        closes = [c.close for c in candles]
        fast, slow = ema(closes, 20), ema(closes, 50)
        a = atr(candles)
        if not fast or not slow or not a or a <= 0:
            return None

        last, prev = candles[-1], candles[-2]
        hi = max(c.high for c in candles[-21:-1])
        lo = min(c.low for c in candles[-21:-1])
        va = mean(c.volume for c in candles[-21:-1])
        vr = last.volume / max(va, 1e-9)

        regime = (
            "IMPULSE" if abs(last.close - prev.close) / a > 1.5
            else ("TREND" if abs(fast - slow) / last.close > .0015 else "RANGE")
        )

        side = setup = None
        if last.close > hi and vr >= 1.15 and fast > slow:
            side, setup = "LONG", "BREAKOUT"
        elif last.close < lo and vr >= 1.15 and fast < slow:
            side, setup = "SHORT", "BREAKOUT"
        elif last.close > fast and prev.close <= fast and fast > slow:
            side, setup = "LONG", "IMPULSE_CONTINUATION"
        elif last.close < fast and prev.close >= fast and fast < slow:
            side, setup = "SHORT", "IMPULSE_CONTINUATION"

        if not side and regime == "RANGE":
            if last.close <= lo + .25 * a and last.close > prev.close:
                side, setup = "LONG", "RANGE_REVERSAL"
            elif last.close >= hi - .25 * a and last.close < prev.close:
                side, setup = "SHORT", "RANGE_REVERSAL"

        if not side:
            return None

        pressure = sum([
            fast > slow if side == "LONG" else fast < slow,
            last.close > fast if side == "LONG" else last.close < fast,
            vr > 1.2,
        ])
        confidence = min(.98, .52 + .08 * pressure + .04 * min(vr, 3))

        entry = m.ask if side == "LONG" else m.bid

        # Structure + volatility SL. Avoid absurdly wide risk.
        if side == "LONG":
            sl = min(lo, entry - 1.2 * a)
            risk = entry - sl
        else:
            sl = max(hi, entry + 1.2 * a)
            risk = sl - entry

        if risk <= 0 or risk / entry > .02:
            return None

        # Dynamic targets: risk (R), ATR and nearby market structure all
        # influence the final target distance. We never use a fixed percent.
        multipliers = _tp_multipliers(tp_count)
        recent_high = max(c.high for c in candles[-60:-1])
        recent_low = min(c.low for c in candles[-60:-1])

        if side == "LONG":
            structure_room = max(recent_high - entry, a)
            max_reasonable = max(structure_room, 2.5 * risk)
            tp = []
            for r_mult in multipliers:
                target = entry + risk * r_mult
                # Do not force an arbitrary ceiling; if structure gives room,
                # allow the target to extend with the move.
                if setup == "BREAKOUT" and target < recent_high:
                    target = max(target, recent_high)
                target = max(target, entry + a * min(r_mult, 4.2))
                tp.append(target)
            tp = sorted(set(round(x, 10) for x in tp))
        else:
            structure_room = max(entry - recent_low, a)
            max_reasonable = max(structure_room, 2.5 * risk)
            tp = []
            for r_mult in multipliers:
                target = entry - risk * r_mult
                if setup == "BREAKOUT" and target > recent_low:
                    target = min(target, recent_low)
                target = min(target, entry - a * min(r_mult, 4.2))
                tp.append(target)
            tp = sorted(set((round(x, 10) for x in tp), reverse=True))

        if not tp:
            return None

        # Conservative fee/slippage allowance for PAPER expectancy filtering.
        estimated_round_trip_cost = max(0.0012, 2 * (m.spread_bps / 10000) + abs(m.funding_rate))
        expected_move = abs(tp[-1] - entry) / entry
        if expected_move <= estimated_round_trip_cost * 1.5:
            return None

        reasons = [
            f"volume_x={vr:.2f}",
            f"spread_bps={m.spread_bps:.2f}",
            f"risk_R=1.00",
            f"tp_count={len(tp)}",
            f"tp_max_R={max(multipliers):.1f}",
        ]
        return Opportunity(
            m.symbol, side, regime, setup, confidence,
            expected_move, entry, sl, tp, reasons
        )
