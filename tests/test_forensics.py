import os
import tempfile
import unittest

from app.config import Settings
from app.engine import Engine
from app.models import Opportunity
from app.storage import Store


class ForensicsTests(unittest.TestCase):
    def make_engine(self):
        tmp = tempfile.TemporaryDirectory()
        store = Store(os.path.join(tmp.name, "db.sqlite"))
        engine = Engine(
            Settings(environment="paper", default_budget=100.0, paper_taker_fee_rate=0.00055),
            store,
        )
        engine.trading_enabled = True
        return tmp, store, engine

    def opportunity(self):
        return Opportunity(
            "BTCUSDT", "LONG", "TREND", "FORENSIC_TEST", 0.9, 0.03,
            100.0, 99.0, [101.4, 102.4, 103.5],
            ["trend5=UP", "book_imbalance=0.61"],
            score_10=8.4,
            score_components={
                "trend": 10.0, "volume": 8.0, "order_book": 9.0,
                "momentum": 8.0, "volatility": 7.0,
                "risk_reward": 9.0, "cost": 9.0,
            },
            decision="ENTER",
        )

    def test_entry_forensics_survive_position_persistence(self):
        tmp, store, engine = self.make_engine()
        try:
            p = engine.open_paper(self.opportunity())
            self.assertIsNotNone(p)
            self.assertEqual(p.setup, "FORENSIC_TEST")
            self.assertAlmostEqual(p.score_10, 8.4)
            row = store.db.execute(
                "SELECT forensic FROM positions WHERE id=?", (p.id,)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIn('"risk_pct"', row[0])
            self.assertIn('"tp1_r"', row[0])
            self.assertIn('"FORENSIC_TEST"', row[0])
        finally:
            tmp.cleanup()

    def test_closed_trade_contains_entry_and_exit_forensics(self):
        tmp, store, engine = self.make_engine()
        try:
            p = engine.open_paper(self.opportunity())
            engine._finish_position(p, 98.8, "STOP_LOSS")
            row = store.history(1)[0]
            self.assertEqual(row["reason"], "STOP_LOSS")
            forensic = __import__("json").loads(row["forensic"])
            self.assertEqual(forensic["setup"], "FORENSIC_TEST")
            self.assertAlmostEqual(forensic["score_10"], 8.4)
            self.assertEqual(forensic["exit"]["reason"], "STOP_LOSS")
            self.assertAlmostEqual(forensic["exit"]["price"], 98.8)
            self.assertIn("risk_pct", forensic["entry"])
            self.assertIn("tp1_r", forensic["entry"])
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
