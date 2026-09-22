import tempfile
import unittest
from types import SimpleNamespace

from app.engine import Engine
from app.models import MarketSnapshot, Opportunity
from app.risk import position_size
from app.storage import Store
from app.strategy import SmartStrategy


def settings():
    return SimpleNamespace(
        environment="paper",
        bybit_testnet=True,
        bybit_category="linear",
        default_budget=100.0,
        default_leverage=3,
        max_simultaneous_positions=3,
        risk_per_trade=0.005,
        paper_taker_fee_rate=0.00055,
        min_24h_turnover_usdt=0,
        max_spread_bps=100,
        auto_min_confidence=0.62,
        auto_cooldown_seconds=90,
        max_hold_minutes=30,
        scan_interval_seconds=5,
    )


def opportunity(side="LONG", entry=100.0, sl=99.0, tps=None):
    return Opportunity(
        "BTCUSDT", side, "TREND", "BREAKOUT", 0.8, 0.02,
        entry, sl, tps or [101.0, 102.0, 103.0], []
    )


class TradingMathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.s = settings()
        self.engine = Engine(self.s, self.store)
        self.engine.set_trading(True)

    def tearDown(self):
        self.store.db.close()

    def test_position_size_includes_fee_in_risk_budget(self):
        q = position_size(100, 0.005, 100, 99, 3, fee_rate=0.00055)
        expected = 0.5 / (1.0 + (100 + 99) * 0.00055)
        self.assertAlmostEqual(q, expected, places=10)

    def test_entry_and_exit_balance_matches_realized_pnl_minus_fees(self):
        p = self.engine.open_paper(opportunity())
        entry_fee = p.entry_fee
        self.assertAlmostEqual(self.engine.balance, 100.0 - entry_fee, places=10)

        self.engine._finish_position(p, 101.0, "TEST_CLOSE")
        gross = (101.0 - 100.0) * p.initial_quantity
        exit_fee = 101.0 * p.initial_quantity * self.engine.paper_fee_rate
        expected_balance = 100.0 + gross - entry_fee - exit_fee
        self.assertAlmostEqual(self.engine.balance, expected_balance, places=10)

        row = self.store.all_history()[0]
        self.assertAlmostEqual(row["pnl"], gross - entry_fee - exit_fee, places=10)
        self.assertAlmostEqual(row["fees"], entry_fee + exit_fee, places=10)

    def test_partial_tp_uses_remaining_quantity_and_closes_exactly(self):
        p = self.engine.open_paper(opportunity())
        initial = p.initial_quantity
        self.engine._take_profit(p, 101.0)
        self.assertAlmostEqual(p.quantity, initial * 2 / 3, places=10)
        self.assertAlmostEqual(self.engine.balance, 100.0 + p.realized_pnl, places=10)

        self.engine._take_profit(p, 102.0)
        self.assertAlmostEqual(p.quantity, initial / 3, places=10)
        self.engine._take_profit(p, 103.0)
        self.assertNotIn(p.id, self.engine.positions)
        self.assertEqual(len(self.store.all_history()), 1)

    def test_settings_do_not_erase_pnl(self):
        p = self.engine.open_paper(opportunity())
        before = self.engine.balance
        self.engine.update_settings({"leverage": 5, "take_profits": 4})
        self.assertAlmostEqual(self.engine.balance, before, places=10)

        self.engine.update_settings({"budget": 150})
        self.assertAlmostEqual(self.engine.balance, 150.0, places=10)
        self.assertAlmostEqual(self.engine.capital_base, 150, places=10)
        self.assertIn(p.id, self.engine.positions)

    def test_open_position_persists_in_storage(self):
        p = self.engine.open_paper(opportunity())
        reloaded = Engine(self.s, self.store)
        self.assertIn(p.id, reloaded.positions)
        self.assertAlmostEqual(reloaded.positions[p.id].quantity, p.quantity, places=10)

    def test_reset_restores_budget_and_disables_trading(self):
        self.engine.open_paper(opportunity())
        self.engine._finish_position(next(iter(self.engine.positions.values())), 98, "TEST_CLOSE")
        self.engine.reset_paper()
        self.assertEqual(self.engine.positions, {})
        self.assertAlmostEqual(self.engine.balance, 100.0)
        self.assertFalse(self.engine.trading_enabled)
        self.assertEqual(self.store.all_history(), [])

    def test_statistics_use_all_trades_not_only_last_20(self):
        for i in range(25):
            p = self.engine.open_paper(
                opportunity(entry=100 + i, sl=99 + i, tps=[101 + i])
            )
            price = 101 + i if i % 2 == 0 else 99 + i
            self.engine._finish_position(p, price, "TEST")
        stats = self.store.statistics()
        self.assertEqual(stats["trades"], 25)
        self.assertGreaterEqual(stats["win_rate"], 0)
        self.assertGreaterEqual(stats["total_fees"], 0)


class StrategyTests(unittest.TestCase):
    def test_liquidity_strategy_finds_three_integer_targets(self):
        candles = []
        for i in range(60):
            candles.append(type("C", (), {
                "timestamp": i, "open": 100.0, "high": 100.2,
                "low": 99.8, "close": 100.0, "volume": 100.0
            })())
        candles[-2] = type("C", (), {
            "timestamp": 58, "open": 100.3, "high": 100.6,
            "low": 100.1, "close": 100.5, "volume": 100.0
        })()
        candles[-1] = type("C", (), {
            "timestamp": 59, "open": 100.5, "high": 100.8,
            "low": 100.3, "close": 100.7, "volume": 150.0
        })()
        m = MarketSnapshot(
            "BTCUSDT", 100.7, 100.4, 100.6, 10_000_000,
            0.1, 100_000, 0, 0, 2
        )
        book = {
            "bids": [["99.0", "1000"], ["98.0", "10"], ["97.0", "10"], ["96.0", "10"]],
            "asks": [["103.0", "1000"], ["106.0", "1000"], ["102.0", "10"], ["104.0", "10"], ["105.0", "10"]],
        }
        o = SmartStrategy().analyze(m, candles, book, 3)
        self.assertIsNotNone(o)
        self.assertEqual(o.take_profits, [102.0, 104.0, 105.0])
        self.assertEqual(o.entry % 1, 0)
        self.assertEqual(o.stop_loss % 1, 0)


if __name__ == "__main__":
    unittest.main()
