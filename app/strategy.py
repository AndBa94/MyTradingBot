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
    if trend15 == (1 if side == "LONG" else -1):
        trend_score = min(12, trend_score + 1)
    volume_score = 10 if volume_ratio >= 1.50 else 8 if volume_ratio >= 1.25 else 5 if volume_ratio >= 1.05 else 0
    spread_score = 5 if spread_bps <= 5 else 3 if spread_bps <= 8 else 1 if spread_bps <= 12 else 0
    volatility_score = 5 if 0.10 <= atr_pct <= 0.70 else 3 if atr_pct < 0.90 else 0
    rr_score = 5 if rr >= 2.4 else 4 if rr >= 2.0 else 2 if rr >= 1.6 else 0
    candle_score = 5 if body >= 0.65 else 3 if body >= 0.50 else 0

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
    """First-bot style microstructure scorer, lightly hardened."""

    def analyze(self, m, candles, orderbook=None, tp_count=3, previous_orderbook=None):
        if len(candles) < 60 or not orderbook or m.last <= 0:
            return None

        closes = [c.close for c in candles]
        last, prev = candles[-1], candles[-2]
        imbalance = _book_imbalance(orderbook)
        if imbalance is None:
            return None

        volume_ratio = _volume_ratio(candles)
        momentum = _momentum(closes, 3)
        rsi = _rsi(closes, 14)
        atr = _atr(candles, 14)
        trend, fast, slow = _trend_direction(closes)
        trend15 = _higher_trend(closes)
        flow = _flow_delta(previous_orderbook, imbalance)
        if atr is None or rsi is None or fast is None:
            return None

        spread = max(m.ask - m.bid, 0.0)
        spread_bps = _f(m.spread_bps)
        if spread_bps > 12:
            return None

        entry_long = m.ask
        entry_short = m.bid
        if abs(entry_long - last.close) / max(last.close, 1e-9) > 0.0030:
            return None
        if abs(entry_short - last.close) / max(last.close, 1e-9) > 0.0030:
            return None

        body = abs(last.close - last.open) / max(last.high - last.low, last.close * 1e-9)
        close_loc = (last.close - last.low) / max(last.high - last.low, last.close * 1e-9)

        long_ok = (
            trend == 1 and trend15 >= 0
            and momentum >= 0.0010 and 51 <= rsi <= 70
            and imbalance >= 0.55 and flow >= 0.000
            and volume_ratio >= 1.15 and last.close > last.open
            and body >= 0.55 and close_loc >= 0.65
        )
        short_ok = (
            trend == -1 and trend15 <= 0
            and momentum <= -0.0012 and 30 <= rsi <= 49
            and imbalance <= 0.45 and flow <= 0.000
            and volume_ratio >= 1.20 and last.close < last.open
            and body >= 0.55 and close_loc <= 0.35
        )

        funding = _f(m.funding_rate)
        if long_ok and funding >= 0.0008:
            long_ok = False
        if short_ok and funding <= -0.0008:
            short_ok = False

        if long_ok == short_ok:
            return None

        side = "LONG" if long_ok else "SHORT"
        entry = entry_long if side == "LONG" else entry_short

        if side == "LONG":
            stop = min(last.low, prev.low, entry - atr * 0.85)
            risk = entry - stop
            tp = [entry + risk * 1.35, entry + risk * 2.20, entry + risk * 3.00]
        else:
            stop = max(last.high, prev.high, entry + atr * 0.85)
            risk = stop - entry
            tp = [entry - risk * 1.35, entry - risk * 2.20, entry - risk * 3.00]

        if risk <= 0:
            return None

        rr = abs(tp[-1] - entry) / risk
        first_r = abs(tp[0] - entry) / risk
        if first_r < 1.20 or rr < 2.0:
            return None

        round_trip_cost = spread / max(entry, 1e-9) + 2.0 * 0.00055
        if abs(tp[0] - entry) / entry <= round_trip_cost * 1.20:
            return None

        atr_pct = atr / max(m.last, 1e-9) * 100.0
        components = _score_components(
            side, imbalance, flow, volume_ratio, momentum, rsi,
            trend, trend15, spread_bps, atr_pct, rr, body
        )
        raw_score = sum(components.values())

        # Keep the legacy score gate, but make the entry gate harder: the overnight
        # history showed too many late/weak entries reaching the stop before follow-through.
        if raw_score < 86:
            return None

        score_10 = raw_score / 10.0
        reasons = [
            f"setup=PULSE_LEGACY",
            f"legacy_score={raw_score:.0f}/100",
            f"book_score={components['order_book']:.0f}/25",
            f"momentum_score={components['momentum']:.0f}/15",
            f"ema_score={components['trend']:.0f}/12",
            f"rsi_score={components['rsi']:.0f}/5",
            f"flow_score={components['flow']:.0f}/10",
            f"volume_score={components['volume']:.0f}/10",
            f"spread_bps={spread_bps:.2f}",
            f"volume_x={volume_ratio:.2f}",
            f"rsi={rsi:.1f}",
            f"momentum={momentum * 100:.3f}%",
            f"book_imbalance={imbalance:.3f}",
            f"flow_delta={flow:+.3f}",
            f"trend5={'UP' if trend > 0 else 'DOWN'}",
            f"trend15={'UP' if trend15 > 0 else 'DOWN' if trend15 < 0 else 'FLAT'}",
            f"atr_pct={atr_pct:.3f}",
            f"risk_pct={risk / entry * 100:.3f}",
            f"tp1_r={first_r:.2f}",
            f"final_r={rr:.2f}",
            f"net_cost_est_pct={round_trip_cost * 100:.3f}",
        ]

        return Opportunity(
            m.symbol,
            side,
            "LEGACY_MICROSTRUCTURE",
            "PULSE_LEGACY",
            score_10 / 10.0,
            abs(tp[-1] - entry) / entry,
            entry,
            stop,
            tp[:max(1, min(5, int(tp_count)))],
            reasons,
            score_10=score_10,
            score_components=components,
            decision="ENTER",
        )
