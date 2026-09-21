import asyncio
import uuid
from datetime import datetime
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
        self.balance = 1000.0
        self.positions = {}
        self.last_scan = []
        self.running = False
        self.trading_enabled = False
        self.last_scan_at = None
        self.settings = {
            "budget": self.balance,
            "leverage": s.default_leverage,
            "take_profits": 3,
            "max_positions": s.max_simultaneous_positions,
        }

    async def scan_once(self):
        markets = await self.client.get_tickers()
        markets = [
            m for m in markets
            if m.turnover_24h >= self.s.min_24h_turnover_usdt
            and m.spread_bps <= self.s.max_spread_bps
            and m.bid > 0 and m.ask > 0
        ]
        markets = sorted(markets, key=lambda x: x.turnover_24h, reverse=True)[:60]
        sem = asyncio.Semaphore(10)

        async def analyze_market(m):
            async with sem:
                try:
                    candles = await self.client.get_klines(m.symbol)
                    return self.strategy.analyze(m, candles)
                except Exception:
                    return None

        results = await asyncio.gather(*(analyze_market(m) for m in markets))
        self.last_scan = sorted([x for x in results if x], key=lambda x: x.confidence, reverse=True)
        self.last_scan_at = datetime.utcnow()
        return self.last_scan

    async def loop(self):
        self.running = True
        while self.running:
            try:
                await self.scan_once()
            except Exception:
                pass
            await asyncio.sleep(self.s.scan_interval_seconds)

    def open_paper(self, o):
        if not self.trading_enabled:
            return None
        if len(self.positions) >= int(self.settings["max_positions"]):
            return None
        leverage = int(self.settings["leverage"])
        self.balance = float(self.settings["budget"])
        q = position_size(self.balance, self.s.risk_per_trade, o.entry, o.stop_loss, leverage)
        if q <= 0:
            return None
        tps = o.take_profits[:int(self.settings["take_profits"])]
        p = Position(
            str(uuid.uuid4()), o.symbol, o.side, o.entry, q, o.stop_loss,
            tps, datetime.utcnow(), leverage
        )
        self.positions[p.id] = p
        return p

    async def refresh_positions(self):
        if not self.positions:
            return
        try:
            tickers = {m.symbol: m for m in await self.client.get_tickers()}
            for p in self.positions.values():
                m = tickers.get(p.symbol)
                if not m:
                    continue
                p.pnl = (m.last - p.entry) * p.quantity if p.side == "LONG" else (p.entry - m.last) * p.quantity
        except Exception:
            pass

    def close_paper(self, position_id, reason="MANUAL"):
        p = self.positions.pop(position_id, None)
        if not p:
            return None
        exit_price = p.entry + (p.pnl / max(p.quantity, 1e-12)) if p.side == "LONG" else p.entry - (p.pnl / max(p.quantity, 1e-12))
        self.store.add_trade(p, exit_price, p.pnl, reason)
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
        return self.settings

    def set_trading(self, enabled):
        self.trading_enabled = bool(enabled)
        return self.trading_enabled
