import unittest

from app.models import Opportunity
from app.scoring import evaluate_opportunity


class ScoringTests(unittest.TestCase):
    def make(self, side="LONG"):
        reasons = [
            "trend5=UP" if side == "LONG" else "trend5=DOWN",
            "trend15=UP" if side == "LONG" else "trend15=DOWN",
            "volume_x=1.60",
            "book_imbalance=0.62" if side == "LONG" else "book_imbalance=0.38",
            "flow_delta=+0.025" if side == "LONG" else "flow_delta=-0.025",
            "atr_pct=0.45",
            "body=0.75",
            "close_loc=0.85" if side == "LONG" else "close_loc=0.15",
            "tp1_r=2.40",
            "tp1_move_pct=0.80",
            "net_cost_est_pct=0.20",
        ]
        return Opportunity(
            "TESTUSDT", side, "TREND_UP", "MOMENTUM_CONTINUATION",
            0.8, 0.01, 100, 99, [102, 103], reasons
        )

    def test_score_has_all_components(self):
        score, components = evaluate_opportunity(self.make())
        self.assertGreater(score, 0)
        self.assertLessEqual(score, 10)
        self.assertEqual(
            set(components),
            {"trend", "volume", "order_book", "momentum", "volatility", "risk_reward", "cost"},
        )

    def test_opposite_book_reduces_score(self):
        good, _ = evaluate_opportunity(self.make("LONG"))
        bad = self.make("LONG")
        bad.reasons = [r.replace("book_imbalance=0.62", "book_imbalance=0.42") for r in bad.reasons]
        bad_score, _ = evaluate_opportunity(bad)
        self.assertLess(bad_score, good)


if __name__ == "__main__":
    unittest.main()
