import unittest

from app.models import Candle, MarketSnapshot
from app.strategy import SmartStrategy
from app.risk import position_size


def strong_trend_candles():
    rows = []
    # Controlled trend with small pullbacks keeps RSI in a usable range.
    pattern = [0.0010, 0.0008, -0.0003, 0.0011, 0.0007, -0.0002]
    price = 100.0
    for i in range(80):
        d = pattern[i % len(pattern)]
        new_price = price * (1.0 + d)
        rows.append(Candle(
            i,
            price,
            max(price, new_price) * 1.001,
            min(price, new_price) * 0.999,
            new_price,
            1000,
        ))
        price = new_price

    # Final candle: strong participation and bullish close.
    rows[-1] = Candle(
        79, price * 0.9990, price * 1.0040, price * 0.9985,
        price * 1.0030, 1800
    )
    return rows


def book_long():
    return {
        "bids": [[100.0, 30], [99.9, 10], [99.8, 10], [99.7, 10], [99.6, 10]],
        "asks": [[100.5, 5], [100.6, 5], [100.7, 5], [100.8, 5], [100.9, 5]],
    }


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.strategy = SmartStrategy()

    def test_opposite_book_rejects_long(self):
        rows = strong_trend_candles()
        m = MarketSnapshot(
            "BTCUSDT", rows[-1].close, rows[-1].close - 0.01,
            rows[-1].close + 0.01, 100_000_000, 2.0, 2_000_000,
            0, 0, 2.0,
        )
        book = {
            "bids": [[100.0, 5], [99.9, 5], [99.8, 5], [99.7, 5], [99.6, 5]],
            "asks": [[100.5, 30], [100.6, 10], [100.7, 10], [100.8, 10], [100.9, 10]],
        }
        self.assertIsNone(self.strategy.analyze(m, rows, book))

    def test_weak_volume_is_rejected(self):
        rows = strong_trend_candles()
        for i in range(len(rows) - 21, len(rows)):
            c = rows[i]
            rows[i] = Candle(c.timestamp, c.open, c.high, c.low, c.close, 1000)
        m = MarketSnapshot(
            "BTCUSDT", rows[-1].close, rows[-1].close - 0.01,
            rows[-1].close + 0.01, 100_000_000, 2.0, 2_000_000,
            0, 0, 2.0,
        )
        self.assertIsNone(self.strategy.analyze(m, rows, book_long()))

    def test_risk_sizing_respects_risk_and_leverage(self):
        q = position_size(
            balance=1000, risk_fraction=0.005, entry=100, stop=99,
            max_leverage=3, fee_rate=0.00055, stop_slippage_rate=0.001,
        )
        self.assertGreater(q, 0)
        self.assertLessEqual(q, 30.0)

        q_without_slippage = position_size(
            balance=1000, risk_fraction=0.005, entry=100, stop=99,
            max_leverage=3, fee_rate=0.00055,
        )
        self.assertLess(q, q_without_slippage)


if __name__ == "__main__":
    unittest.main()
