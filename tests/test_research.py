import unittest

from app.research import build_research_report, wilson_interval


class ResearchTests(unittest.TestCase):
    def test_wilson_interval_matches_wolfram_reference(self):
        lo, hi = wilson_interval(58, 100)
        # Wolfram reference for p=.58, n=100, z=1.95996398454005.
        self.assertAlmostEqual(lo, 0.48206486703042983, places=12)
        self.assertAlmostEqual(hi, 0.6720161732564525, places=12)

    def test_small_sample_never_authorizes_optimization(self):
        rows = [{"pnl": 1, "setup": "A", "score_10": 8.5, "reason": "TP"} for _ in range(10)]
        report = build_research_report(rows, minimum_sample=30)
        self.assertEqual(report["status"], "COLLECT_MORE_DATA")
        self.assertFalse(report["by_setup"]["A"]["eligible_for_optimization"])
        self.assertTrue(report["guardrails"]["no_automatic_parameter_change"])

    def test_large_sample_is_ready_but_still_requires_oos(self):
        rows = []
        for i in range(40):
            rows.append({
                "pnl": 1.0 if i < 24 else -0.7,
                "setup": "TREND_EXPANSION",
                "score_10": 8.5,
                "reason": "TP1" if i < 24 else "STOP_LOSS",
            })
        report = build_research_report(rows, minimum_sample=30)
        self.assertEqual(report["status"], "READY_FOR_WALK_FORWARD")
        self.assertTrue(report["by_setup"]["TREND_EXPANSION"]["eligible_for_optimization"])
        self.assertTrue(report["guardrails"]["requires_out_of_sample_validation"])


if __name__ == "__main__":
    unittest.main()
