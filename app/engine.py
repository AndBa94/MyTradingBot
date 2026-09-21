import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from app.exchange.bybit import BybitClient
from app.models import Position
from app.strategy import SmartStrategy, _tp_multipliers
from app.risk import position_size

class Engine:
    def __init__(self, s, store):
        self.s = s
        self.store = store
        self.client = BybitClient(s.bybit_testnet, s.bybit_category)
        self.strategy = SmartStrategy()
        self.balance = float(s.default_budget)
        self.positions = self.store.load_positions()
        self.last_scan = []
        self.latest_markets = []
        self.running = False
        self.last_error = None
        self.last_scan_at = None
        self.last_action = "Ожидание рынка"
        self.cooldowns = {}
        self.paper_fee_rate = float(getattr(s, "paper_taker_fee_rate", 0.00055))

        saved = self.store.get_settings()
        self.settings = {
            "budget": float(saved.get("budget", self.balance)),
            "leverage": int(saved.get("leverage", s.default_leverage)),
            "take_profits": int(saved.get("take_profits", 3)),
            "max_positions": int(saved.get("max_positions", s.max_simultaneous_positions)),
        }
        self.balance = float(saved.get("balance", self.settings["budget"]))
        self.capital_base = float(saved.get("capital_base", self.settings["budget"]))
        self.trading_enabled = bool(saved.get("trading_enabled", False))

    async def scan_once(self):
        try:
            markets = await self.client.get_tickers()
            self.latest_markets = markets
            self.last_error = None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise

        markets = [
            m for m in markets
            if m.turnover_24h >= self.s.min_24h_turnover_usdt
            and m.spread_bps <= self.s.max_spread_bps
            and m.bid > 0 and m.ask > 0
        ]
        markets = sorted(markets, key=lambda x: x.turnover_24h, reverse=True)[:30]
        sem = asyncio.Semaphore(10)

        async def analyze_market(m):
            async with sem:
                try:
                    candles = await self.client.get_klines(m.symbol)
                    return self.strategy.analyze(
                        m, candles, int(self.settings["take_profits"])
                    )
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    return None

        results = await asyncio.gather(*(analyze_market(m) for m in markets))
        self.last_scan = sorted(
            [x for x in results if x],
            key=lambda x: x.confidence,
            reverse=True,
        )
        self.last_scan_at = datetime.now(timezone.utc).replace(tzinfo=None)
        return self.last_scan

    async def loop(self):
        self.running = True
        while self.running:
            try:
                await self.scan_once()
                await self.manage_positions()
                if self.trading_enabled:
                    self.auto_enter()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(max(5, self.s.scan_interval_seconds))

    def _open_symbols(self):
        return {p.symbol for p in self.positions.values() if p.status == "OPEN"}

    def auto_enter(self):
        if not self.trading_enabled:
            return
        max_positions = int(self.settings["max_positions"])
        if len(self.positions) >= max_positions:
            return

        now = datetime.utcnow()
        for o in self.last_scan:
            if len(self.positions) >= max_positions:
                break
            if o.confidence < float(self.s.auto_min_confidence):
                continue
            if o.symbol in self._open_symbols():
                continue

            last_entry = self.cooldowns.get(o.symbol)
            if last_entry and (now - last_entry).total_seconds() < self.s.auto_cooldown_seconds:
                continue

            p = self.open_paper(o, automatic=True)
            if p:
                self.cooldowns[o.symbol] = now
                self.last_action = (
                    f"AUTO {p.side} {p.symbol} | conf {o.confidence*100:.0f}%"
                )

    def open_paper(self, o, automatic=False):
        if str(self.s.environment).lower() != "paper":
            return None
        if not self.trading_enabled:
            return None
        if len(self.positions) >= int(self.settings["max_positions"]):
            return None
        if o.symbol in self._open_symbols():
            return None

        leverage = int(self.settings["leverage"])
        # Do not reset the live PAPER balance to the initial budget on every entry.
        q = position_size(
            self.balance,
            self.s.risk_per_trade,
            o.entry,
            o.stop_loss,
            leverage,
            fee_rate=self.paper_fee_rate,
        )
        if q <= 0:
            return None

        # Strategy already generated the requested number of dynamic TP levels.
        tps = o.take_profits[:int(self.settings["take_profits"])]
        entry_fee = abs(o.entry * q) * self.paper_fee_rate
        if entry_fee >= self.balance:
            return None
        p = Position(
            str(uuid.uuid4()),
            o.symbol,
            o.side,
            o.entry,
            q,
            o.stop_loss,
            tps,
            datetime.utcnow(),
            leverage,
            initial_quantity=q,
            last_price=o.entry,
            initial_stop_loss=o.stop_loss,
            entry_fee=entry_fee,
            fees=entry_fee,
            realized_pnl=-entry_fee,
        )
        self.balance -= entry_fee
        self.store.save_settings({"balance": self.balance})
        self.positions[p.id] = p
        self.store.save_position(p)
        if automatic:
            self.last_action = f"AUTO OPEN {p.symbol} {p.side}"
        return p

    def _sync_position_tps(self, p):
        """Rebuild TP levels for an open position using the current TP setting.
        Entry and the original risk/SL are preserved; already completed TP stages
        are never moved backwards.
        """
        count = max(1, min(5, int(self.settings["take_profits"])))
        original_sl = p.initial_stop_loss or p.stop_loss
        risk = abs(p.entry - original_sl)
        if risk <= 0:
            return

        multipliers = _tp_multipliers(count)
        if p.side == "LONG":
            levels = [round(p.entry + risk * r, 10) for r in multipliers]
        else:
            levels = [round(p.entry - risk * r, 10) for r in multipliers]

        if p.tp_index >= count:
            return
        p.take_profits = levels
        p.tp_index = min(p.tp_index, len(levels) - 1)
        self.store.save_position(p)

    def _sync_all_open_tps(self):
        for p in self.positions.values():
            if p.status == "OPEN":
                self._sync_position_tps(p)

    def _unrealized(self, p, price, quantity=None):
        q = p.quantity if quantity is None else quantity
        return (price - p.entry) * q if p.side == "LONG" else (p.entry - price) * q

    async def manage_positions(self):
        if not self.positions or not self.latest_markets:
            return

        # Keep open positions aligned with the current TP setting.
        self._sync_all_open_tps()

        tickers = {m.symbol: m for m in self.latest_markets}
        for p in list(self.positions.values()):
            m = tickers.get(p.symbol)
            if not m:
                continue

            price = m.bid if p.side == "LONG" else m.ask
            p.last_price = price
            p.pnl = self._unrealized(p, price)

            hit_sl = price <= p.stop_loss if p.side == "LONG" else price >= p.stop_loss
            if hit_sl:
                self._finish_position(p, price, "STOP_LOSS")
                continue

            if p.tp_index < len(p.take_profits):
                target = p.take_profits[p.tp_index]
                hit_tp = price >= target if p.side == "LONG" else price <= target
                if hit_tp:
                    self._take_profit(p, target)
                    if p.id not in self.positions:
                        continue

            age = datetime.utcnow() - p.opened_at
            if age >= timedelta(minutes=self.s.max_hold_minutes):
                self._finish_position(p, price, "TIME_EXIT")

    def _take_profit(self, p, price):
        remaining_tps = len(p.take_profits) - p.tp_index
        if remaining_tps <= 1:
            self._finish_position(p, price, "TAKE_PROFIT")
            return

        # Equal tranches. The final tranche closes the remainder.
        qty_to_close = p.quantity / remaining_tps
        qty_to_close = min(qty_to_close, p.quantity)
        gross_realized = self._unrealized(p, price, qty_to_close)
        exit_fee = abs(price * qty_to_close) * self.paper_fee_rate
        realized = gross_realized - exit_fee
        p.realized_pnl += realized
        p.fees += exit_fee
        p.quantity -= qty_to_close
        self.balance += realized
        self.store.save_settings({"balance": self.balance})
        p.tp_index += 1
        p.pnl = self._unrealized(p, price)

        # Once TP1 is reached, protect the remaining position at breakeven.
        if p.tp_index == 1:
            p.stop_loss = p.entry
        elif p.tp_index > 1:
            prev_tp = p.take_profits[p.tp_index - 1]
            p.stop_loss = prev_tp

        self.last_action = (
            f"TP{p.tp_index} {p.symbol} | {realized:+.2f} USDT net | fee {exit_fee:.4f}"
        )

    def _finish_position(self, p, price, reason):
        if p.id not in self.positions:
            return
        gross_remaining = self._unrealized(p, price, p.quantity)
        exit_fee = abs(price * p.quantity) * self.paper_fee_rate
        net_remaining = gross_remaining - exit_fee
        final_pnl = p.realized_pnl + net_remaining
        p.fees += exit_fee
        p.pnl = final_pnl
        p.last_price = price
        p.status = "CLOSED"
        self.store.add_trade(p, price, final_pnl, reason, p.fees)
        # Prior TP tranches were already credited when they closed.
        self.balance += net_remaining
        self.store.save_settings({"balance": self.balance})
        del self.positions[p.id]
        self.store.delete_position(p.id)
        self.last_action = (
            f"CLOSE {p.symbol} | {reason} | {final_pnl:+.2f} USDT | fee {p.fees:.4f}"
        )

    async def refresh_positions(self):
        if not self.positions:
            return
        try:
            tickers = {m.symbol: m for m in await self.client.get_tickers()}
            for p in self.positions.values():
                m = tickers.get(p.symbol)
                if not m:
                    continue
                price = m.bid if p.side == "LONG" else m.ask
                p.last_price = price
                p.pnl = self._unrealized(p, price)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"

    def close_paper(self, position_id, reason="MANUAL"):
        p = self.positions.get(position_id)
        if not p:
            return None
        price = p.last_price or p.entry
        self._finish_position(p, price, reason)
        return p

    def update_settings(self, data):
        old_budget = self.settings["budget"]
        if "budget" in data:
            self.settings["budget"] = max(1.0, float(data["budget"]))
        if "leverage" in data:
            self.settings["leverage"] = max(1, min(20, int(data["leverage"])))
        if "take_profits" in data:
            self.settings["take_profits"] = max(1, min(5, int(data["take_profits"])))
        if "max_positions" in data:
            self.settings["max_positions"] = max(1, min(5, int(data["max_positions"])))

        # Change the account baseline only when the budget itself changed.
        # Saving TP/leverage/max-position settings must not erase accumulated PnL.
        if self.settings["budget"] != old_budget:
            delta = self.settings["budget"] - old_budget
            self.balance += delta
            self.capital_base += delta
        self._sync_all_open_tps()
        self.store.save_settings({**self.settings, "balance": self.balance, "capital_base": self.capital_base})
        return self.settings

    def account_snapshot(self):
        today = datetime.now(timezone.utc).replace(tzinfo=None).date()
        history = self.store.all_history()
        today_pnl = sum(float(x["pnl"]) for x in history if str(x["closed_at"])[:10] == today.isoformat())
        closed_pnl = sum(float(x["pnl"]) for x in history)
        unrealized = sum(self._unrealized(p, p.last_price or p.entry) for p in self.positions.values())
        return {
            "balance": self.balance,
            "initial_budget": self.capital_base,
            "today_pnl": today_pnl,
            "closed_pnl": closed_pnl,
            "unrealized_pnl": unrealized,
            "equity": self.balance + unrealized,
        }

    def reset_paper(self):
        self.positions.clear()
        self.store.clear_positions()
        self.last_scan = []
        self.cooldowns.clear()
        self.balance = self.settings["budget"]
        self.capital_base = self.settings["budget"]
        self.trading_enabled = False
        self.last_action = "PAPER сброшен"
        self.store.reset()
        self.store.save_settings({**self.settings, "balance": self.balance, "capital_base": self.capital_base, "trading_enabled": False})

    def set_trading(self, enabled):
        self.trading_enabled = bool(enabled)
        self.store.save_settings({"trading_enabled": self.trading_enabled})
        self.last_action = "Автоторговля включена" if self.trading_enabled else "Автоторговля остановлена"
        return self.trading_enabled
