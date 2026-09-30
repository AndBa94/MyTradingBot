import unittest

from app.research import equity_max_drawdown, monte_carlo


class MonteCarloTests(unittest.TestCase):
    def test_max_drawdown(self):
        self.assertAlmostEqual(equity_max_drawdown([2, -1, -3, 4]), 4.0)

    def test_monte_carlo_is_deterministic(self):
        pnls = [1.0, -0.5, 2.0, -1.0, 0.8]
        a = monte_carlo(pnls, simulations=250, seed=7)
        b = monte_carlo(pnls, simulations=250, seed=7)
        self.assertEqual(a, b)
        self.assertEqual(a["trades"], 5)
        self.assertEqual(a["simulations"], 250)
        self.assertLessEqual(a["p05_final_pnl"], a["median_final_pnl"])
        self.assertLessEqual(a["median_final_pnl"], a["p95_final_pnl"])
        self.assertGreaterEqual(a["p95_max_drawdown"], a["median_max_drawdown"])

    def test_empty_sample(self):
        result = monte_carlo([])
        self.assertEqual(result["simulations"], 0)
        self.assertEqual(result["trades"], 0)


if __name__ == "__main__":
    unittest.main()
