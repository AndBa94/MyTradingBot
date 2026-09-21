import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace

from app.config import Settings
from app.engine import Engine
from app.models import Candle, MarketSnapshot, Opportunity
from app.storage import Store
from app.strategy import SmartStrategy, _tp_multipliers


class TradingMathTests(unittest.TestCase):
    def make_engine(self):
        tmp = tempfile.TemporaryDirectory()
        store = Store(os.path.join(tmp.name, "db.sqlite"))
        engine = Engine(
            Settings(environment="paper", default_budget=100.0, paper_taker_fee_rate=0.00055),
            store,
        )
        engine.trading_enabled = True
        return tmp, store, engine

    def opportunity(self, side="LONG"):
        return Opportunity(
            "BTCUSDT", side, "TREND", "TEST", 0.9, 0.03, 100.0,
            99.0 if side == "LONG" else 101.0,
            [101.0, 102.0, 103.0] if side == "LONG" else [99.0, 98.0, 97.0],
            [],
        )

    def test_tp_targets_are_exact_r_multiples(self):
        candles = []
        for i in range(60):
            close = 100 + 0.01 * i if i < 50 else 100.5 + 0.01 * (i - 50)
            candles.append(Candle(i, close, close + 0.2, close - 0.2, close, 100))
        candles[-1] = Candle(59, 101, 102, 100.8, 101.5, 300)
        market = MarketSnapshot("TEST", 101.5, 101.4, 101.6, 10_000_000, 1, 1_000_000, 0, 0, 2)
        opportunity = SmartStrategy().analyze(market, candles, 3)
        self.assertIsNotNone(opportunity)
        risk = opportunity.entry - opportunity.stop_loss
        for target, multiplier in zip(opportunity.take_profits, _tp_multipliers(3)):
            self.assertAlmostEqual((target - opportunity.entry) / risk, multiplier, places=8)

    def test_balance_is_initial_plus_net_closed_pnl(self):
        tmp, store, engine = self.make_engine()
        try:
            position = engine.open_paper(self.opportunity())
            engine._take_profit(position, 101.0)
            engine._take_profit(position, 102.0)
            engine._finish_position(position, 103.0, "TAKE_PROFIT")
            trade = store.history()[0]
            self.assertAlmostEqual(engine.balance, 100.0 + trade["pnl"], places=8)
            self.assertGreater(trade["fees"], 0)
        finally:
            tmp.cleanup()

    def test_multiple_positions_respect_total_margin(self):
        tmp, store, engine = self.make_engine()
        try:
            engine.settings["leverage"] = 3
            first = engine.open_paper(self.opportunity())
            second = engine.open_paper(
                Opportunity("ETHUSDT", "LONG", "TREND", "TEST", 0.9, 0.03, 100.0, 99.0, [101.0, 102.0, 103.0], [])
            )
            used_margin = sum(
                abs(p.entry * p.quantity) / p.leverage
                for p in engine.positions.values()
            )
            self.assertLessEqual(used_margin, engine.balance + first.entry_fee + second.entry_fee + 1e-9)
        finally:
            tmp.cleanup()

    def test_open_pnl_is_net(self):
        tmp, store, engine = self.make_engine()
        try:
            position = engine.open_paper(self.opportunity())
            engine.latest_markets = [
                MarketSnapshot("BTCUSDT", 101, 101, 101, 10_000_000, 0, 1_000_000)
            ]
            import asyncio
            asyncio.run(engine.manage_positions())
            self.assertAlmostEqual(
                position.pnl,
                position.realized_pnl + engine._net_unrealized(position, 101),
                places=10,
            )
        finally:
            tmp.cleanup()

    def test_settings_do_not_erase_pnl_and_bad_withdrawal_is_rejected(self):
        tmp, store, engine = self.make_engine()
        try:
            position = engine.open_paper(self.opportunity())
            engine._finish_position(position, 101, "MANUAL")
            balance = engine.balance
            engine.update_settings({"take_profits": 5, "leverage": 5})
            self.assertAlmostEqual(engine.balance, balance, places=10)
            engine.balance = 0.5
            with self.assertRaises(ValueError):
                engine.update_settings({"budget": 1})
        finally:
            tmp.cleanup()

    def test_short_direction(self):
        tmp, store, engine = self.make_engine()
        try:
            position = engine.open_paper(self.opportunity("SHORT"))
            engine._finish_position(position, 99, "MANUAL")
            self.assertGreater(store.history()[0]["pnl"], 0)
        finally:
            tmp.cleanup()

    def test_partial_tp_is_persisted(self):
        tmp, store, engine = self.make_engine()
        try:
            position = engine.open_paper(self.opportunity())
            engine._take_profit(position, 101)
            restored = Engine(engine.s, Store(os.path.join(tmp.name, "db.sqlite"))).positions[position.id]
            self.assertEqual(restored.tp_index, 1)
            self.assertLess(restored.quantity, restored.initial_quantity)
            self.assertGreater(restored.fees, restored.entry_fee)
        finally:
            tmp.cleanup()

    def test_restart_preserves_open_position_and_balance(self):
        tmp, store, engine = self.make_engine()
        try:
            position = engine.open_paper(self.opportunity())
            restarted = Engine(engine.s, Store(os.path.join(tmp.name, "db.sqlite")))
            self.assertIn(position.id, restarted.positions)
            self.assertAlmostEqual(restarted.balance, engine.balance, places=10)
        finally:
            tmp.cleanup()

    def test_reducing_tp_count_never_rewinds_to_old_target(self):
        tmp, store, engine = self.make_engine()
        try:
            position = engine.open_paper(self.opportunity())
            position.tp_index = 2
            position.last_price = 102
            engine.update_settings({"take_profits": 2})
            self.assertEqual(position.take_profits, [102.0])
            self.assertEqual(position.tp_index, 0)
        finally:
            tmp.cleanup()

    def test_statistics_use_all_trades(self):
        tmp, store, engine = self.make_engine()
        try:
            for pnl in (2.0, -1.0, 3.0, -2.0):
                store.add_trade(
                    SimpleNamespace(
                        id=os.urandom(8).hex(), symbol="BTCUSDT", side="LONG",
                        entry=100.0, opened_at=datetime.utcnow()
                    ),
                    100.0, pnl, "TEST", 0.01,
                )
            stats = store.statistics()
            self.assertEqual(stats["trades"], 4)
            self.assertEqual(stats["win_rate"], 50.0)
            self.assertAlmostEqual(stats["profit_factor"], 5.0 / 3.0)
            self.assertAlmostEqual(stats["max_drawdown"], 2.0)
        finally:
            tmp.cleanup()

    def test_paper_only(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            with self.assertRaises(RuntimeError):
                Engine(Settings(environment="live"), Store(os.path.join(tmp.name, "db.sqlite")))
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
