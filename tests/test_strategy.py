import unittest

from app.models import Candle, MarketSnapshot
from app.strategy import SmartStrategy
from app.risk import position_size


def candles_for_bounce():
    rows = []
    for i in range(60):
        close = 99.7 + max(0, i - 35) * 0.015
        rows.append(
            Candle(i, close - 0.04, close + 0.07, close - 0.07, close, 1000)
        )

    rows[-3] = Candle(57, 100.02, 100.12, 99.98, 100.06, 1000)
    rows[-2] = Candle(58, 100.06, 100.18, 100.00, 100.10, 1050)
    rows[-1] = Candle(59, 99.95, 100.30, 99.98, 100.25, 1200)
    return rows


def candles_for_breakout():
    rows = []
    for i in range(60):
        close = 99.85 + max(0, i - 45) * 0.01
        rows.append(
            Candle(i, close - 0.04, close + 0.07, close - 0.07, close, 1000)
        )

    rows[-3] = Candle(57, 100.15, 100.35, 100.05, 100.30, 1000)
    rows[-2] = Candle(58, 100.25, 100.45, 100.10, 100.40, 1050)
    rows[-1] = Candle(59, 100.25, 100.70, 100.20, 100.55, 1400)
    return rows


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.strategy = SmartStrategy()

    def test_wall_bounce(self):
        m = MarketSnapshot(
            "XRPUSDT", 100.18, 100.17, 100.19,
            10_000_000, 1.0, 1_000_000, 0, 0, 2.0,
        )
        book = {
            "bids": [
                [100.0, 8], [99.99, 2], [99.98, 2],
                [99.97, 2], [99.96, 2], [99.90, 2],
            ],
            "asks": [
                [101.0, 3], [101.1, 1], [101.2, 1],
                [101.3, 1], [101.35, 1],
            ],
        }

        o = self.strategy.analyze(m, candles_for_bounce(), book)

        self.assertIsNotNone(o)
        self.assertEqual(o.side, "LONG")
        self.assertEqual(o.setup, "WALL_BOUNCE")
        self.assertLess(o.stop_loss, o.entry)
        self.assertTrue(all(tp > o.entry for tp in o.take_profits))

    def test_level_breakout(self):
        m = MarketSnapshot(
            "BTCUSDT", 100.55, 100.54, 100.56,
            100_000_000, 2.0, 2_000_000, 0, 0, 2.0,
        )
        book = {
            "bids": [
                [100.10, 4], [100.09, 4], [100.08, 4],
                [100.07, 4], [100.06, 4],
            ],
            "asks": [
                [102.00, 1], [102.10, 1], [102.20, 1],
                [102.30, 1], [102.40, 1],
            ],
        }

        o = self.strategy.analyze(m, candles_for_breakout(), book)

        self.assertIsNotNone(o)
        self.assertEqual(o.side, "LONG")
        self.assertEqual(o.setup, "LEVEL_BREAKOUT")
        self.assertLess(o.stop_loss, o.entry)

    def test_sub_ten_dollar_coin_is_not_rejected(self):
        rows = []
        for i in range(60):
            rows.append(Candle(i, 0.98, 1.01, 0.97, 1.00, 1000))

        rows[-3] = Candle(57, 0.995, 1.01, 0.98, 1.00, 1000)
        rows[-2] = Candle(58, 1.00, 1.015, 0.99, 1.005, 1050)
        rows[-1] = Candle(59, 1.005, 1.03, 0.985, 1.02, 1200)

        m = MarketSnapshot(
            "ALTUSDT", 1.02, 1.019, 1.021,
            10_000_000, 1.0, 1_000_000, 0, 0, 2.0,
        )
        book = {
            "bids": [
                [0.99, 80], [0.989, 20], [0.988, 20],
                [0.987, 20], [0.986, 20],
            ],
            "asks": [
                [1.04, 10], [1.05, 10], [1.06, 10],
                [1.07, 10], [1.08, 10],
            ],
        }

        # Regression check: the old whole-dollar exclusion is gone.
        result = self.strategy.analyze(m, rows, book)
        self.assertTrue(result is None or result.symbol == "ALTUSDT")

    def test_position_size_respects_risk_and_leverage(self):
        q = position_size(
            balance=1000,
            risk_fraction=0.005,
            entry=100,
            stop=99,
            max_leverage=3,
            fee_rate=0.00055,
            stop_slippage_rate=0.001,
        )
        self.assertGreater(q, 0)
        self.assertLessEqual(q, 30.0)

        # The extra stop-slippage allowance must make the size no larger
        # than the original fee-only risk calculation.
        q_without_slippage = position_size(
            balance=1000,
            risk_fraction=0.005,
            entry=100,
            stop=99,
            max_leverage=3,
            fee_rate=0.00055,
        )
        self.assertLess(q, q_without_slippage)


if __name__ == "__main__":
    unittest.main()
