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
        1: [1.5],
        2: [1.1, 1.8],
        3: [1.0, 1.6, 2.2],
        4: [1.0, 1.5, 2.0, 2.5],
        5: [0.9, 1.3, 1.7, 2.1, 2.6],
    }.get(max(1, min(5, int(count))), [1.0, 1.6, 2.2])

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

        # Targets are exact R-multiples of the original entry-to-SL risk.
        # ATR must not silently turn a 1R scalping target into 4-5R.
        multipliers = _tp_multipliers(tp_count)
        if side == "LONG":
            tp = [round(entry + risk * r_mult, 10) for r_mult in multipliers]
        else:
            tp = [round(entry - risk * r_mult, 10) for r_mult in multipliers]
        tp = sorted(set(tp), reverse=(side == "SHORT"))

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
