import asyncio
import uuid
from datetime import datetime, timedelta

from app.exchange.bybit import BybitClient
from app.models import Position
from app.strategy import SmartStrategy
from app.risk import position_size

class Engine:
    def __init__(self, s, store):
        self.s = s
        self.store = store
        self.client = BybitClient(s.bybit_testnet)
        self.strategy = SmartStrategy()
        self.balance = float(s.default_budget)
        self.positions = {}
        self.last_scan = []
        self.latest_markets = []
        self.running = False
        self.last_error = None
        self.last_scan_at = None
        self.last_action = "Ожидание рынка"
        self.cooldowns = {}

        saved = self.store.get_settings()
        self.settings = {
            "budget": float(saved.get("budget", self.balance)),
            "leverage": int(saved.get("leverage", s.default_leverage)),
            "take_profits": int(saved.get("take_profits", 3)),
            "max_positions": int(saved.get("max_positions", s.max_simultaneous_positions)),
        }
        self.balance = self.settings["budget"]
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
        self.last_scan_at = datetime.utcnow()
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
        if not self.trading_enabled:
            return None
        if len(self.positions) >= int(self.settings["max_positions"]):
            return None
        if o.symbol in self._open_symbols():
            return None

        leverage = int(self.settings["leverage"])
        self.balance = float(self.settings["budget"])
        q = position_size(
            self.balance,
            self.s.risk_per_trade,
            o.entry,
            o.stop_loss,
            leverage,
        )
        if q <= 0:
            return None

        # Strategy already generated the requested number of dynamic TP levels.
        tps = o.take_profits[:int(self.settings["take_profits"])]
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
        )
        self.positions[p.id] = p
        if automatic:
            self.last_action = f"AUTO OPEN {p.symbol} {p.side}"
        return p

    def _unrealized(self, p, price, quantity=None):
        q = p.quantity if quantity is None else quantity
        return (price - p.entry) * q if p.side == "LONG" else (p.entry - price) * q

    async def manage_positions(self):
        if not self.positions or not self.latest_markets:
            return

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
        qty_to_close = p.initial_quantity / len(p.take_profits)
        qty_to_close = min(qty_to_close, p.quantity)
        realized = self._unrealized(p, price, qty_to_close)
        p.realized_pnl += realized
        p.quantity -= qty_to_close
        p.tp_index += 1
        p.pnl = self._unrealized(p, price)

        # Once TP1 is reached, protect the remaining position at breakeven.
        if p.tp_index == 1:
            p.stop_loss = p.entry
        elif p.tp_index > 1:
            prev_tp = p.take_profits[p.tp_index - 1]
            p.stop_loss = prev_tp

        self.last_action = (
            f"TP{p.tp_index} {p.symbol} | +{realized:.2f} USDT realized"
        )

    def _finish_position(self, p, price, reason):
        if p.id not in self.positions:
            return
        final_pnl = p.realized_pnl + self._unrealized(p, price, p.quantity)
        p.pnl = final_pnl
        p.last_price = price
        p.status = "CLOSED"
        self.store.add_trade(p, price, final_pnl, reason)
        del self.positions[p.id]
        self.last_action = f"CLOSE {p.symbol} | {reason} | {final_pnl:+.2f} USDT"

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
        if "budget" in data:
            self.settings["budget"] = max(1.0, float(data["budget"]))
        if "leverage" in data:
            self.settings["leverage"] = max(1, min(20, int(data["leverage"])))
        if "take_profits" in data:
            self.settings["take_profits"] = max(1, min(5, int(data["take_profits"])))
        if "max_positions" in data:
            self.settings["max_positions"] = max(1, min(5, int(data["max_positions"])))

        self.balance = self.settings["budget"]
        self.store.save_settings(self.settings)
        return self.settings

    def set_trading(self, enabled):
        self.trading_enabled = bool(enabled)
        self.store.save_settings({"trading_enabled": self.trading_enabled})
        self.last_action = "Автоторговля включена" if self.trading_enabled else "Автоторговля остановлена"
        return self.trading_enabled
