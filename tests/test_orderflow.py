import unittest

from app.orderflow import analyze_orderflow, directional_pressure


class OrderFlowTests(unittest.TestCase):
    def test_imbalance_and_pressure(self):
        current = {
            "bids": [["100", "10"], ["99", "10"]],
            "asks": [["101", "5"], ["102", "5"]],
        }
        previous = {
            "bids": [["100", "5"], ["99", "5"]],
            "asks": [["101", "10"], ["102", "10"]],
        }
        f = analyze_orderflow(current, previous, depth=2)
        self.assertAlmostEqual(f["imbalance"], 2 / 3, places=12)
        self.assertGreater(f["flow_delta"], 0)
        self.assertGreater(f["pressure"], 0)
        self.assertGreater(directional_pressure(f, "LONG"), 0.5)
        self.assertLess(directional_pressure(f, "SHORT"), 0.5)

    def test_empty_book_is_safe(self):
        f = analyze_orderflow({}, {})
        self.assertEqual(f["imbalance"], 0.5)
        self.assertEqual(f["flow_delta"], 0.0)
        self.assertFalse(f["healthy"])
        self.assertAlmostEqual(directional_pressure(f, "LONG"), 0.5)

    def test_spread_is_computed_from_top_levels(self):
        book = {
            "bids": [["99", "1"]],
            "asks": [["101", "1"]],
        }
        f = analyze_orderflow(book)
        self.assertAlmostEqual(f["spread_bps"], 20000 / 200, places=12)


if __name__ == "__main__":
    unittest.main()
