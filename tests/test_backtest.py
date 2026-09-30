import unittest
from types import SimpleNamespace

from app.backtest import evaluate_opportunity, walk_forward_slices
from app.models import Candle, Opportunity


class BacktestTests(unittest.TestCase):
    def _op(self, side="LONG"):
        return Opportunity(
            "BTCUSDT", side, "TEST", "TEST", 0.8, 0.02,
            100.0, 99.0, [101.0, 102.0], []
        )

    def test_long_tp_after_signal(self):
        future = [
            Candle(1, 100, 100.4, 99.8, 100.2, 10),
            Candle(2, 100.2, 101.2, 100.1, 101.1, 10),
        ]
        result = evaluate_opportunity(self._op(), future, fee_rate=0.00055, max_bars=8)
        self.assertEqual(result.result, "WIN")
        self.assertEqual(result.reason, "TP1")
        self.assertEqual(result.bars_held, 2)
        self.assertLess(result.net_return, result.gross_return)

    def test_same_candle_stop_and_target_is_conservatively_a_loss(self):
        future = [Candle(1, 100, 101.5, 98.5, 100.5, 10)]
        result = evaluate_opportunity(self._op(), future)
        self.assertEqual(result.result, "LOSS")
        self.assertEqual(result.reason, "STOP_LOSS")

    def test_short_stop(self):
        future = [Candle(1, 100, 101.2, 98.8, 100.4, 10)]
        result = evaluate_opportunity(self._op("SHORT"), future)
        self.assertEqual(result.result, "LOSS")

    def test_walk_forward_is_chronological(self):
        parts = walk_forward_slices(100)
        self.assertEqual(parts["train"], (0, 60))
        self.assertEqual(parts["validation"], (60, 80))
        self.assertEqual(parts["test"], (80, 100))


if __name__ == "__main__":
    unittest.main()
