import asyncio,uuid
from datetime import datetime
from app.exchange.bybit import BybitClient
from app.models import Position
from app.strategy import SmartStrategy
from app.risk import position_size

class Engine:
    def __init__(self,s,store):
        self.s=s; self.store=store; self.client=BybitClient(s.bybit_testnet)
        self.strategy=SmartStrategy(); self.balance=1000.0
        self.positions={}; self.last_scan=[]; self.running=False
    async def scan_once(self):
        markets=await self.client.get_tickers()
        markets=[m for m in markets if m.turnover_24h>=self.s.min_24h_turnover_usdt and m.spread_bps<=self.s.max_spread_bps]
        results=[]
        for m in sorted(markets,key=lambda x:x.turnover_24h,reverse=True)[:80]:
            try:
                o=self.strategy.analyze(m,await self.client.get_klines(m.symbol))
                if o: results.append(o)
            except Exception: pass
        self.last_scan=sorted(results,key=lambda x:x.confidence,reverse=True)
        return self.last_scan
    async def loop(self):
        self.running=True
        while self.running:
            try: await self.scan_once()
            except Exception: pass
            await asyncio.sleep(self.s.scan_interval_seconds)
    def open_paper(self,o):
        if len(self.positions)>=self.s.max_simultaneous_positions:return None
        q=position_size(self.balance,self.s.risk_per_trade,o.entry,o.stop_loss,self.s.default_leverage)
        if q<=0:return None
        p=Position(str(uuid.uuid4()),o.symbol,o.side,o.entry,q,o.stop_loss,o.take_profits,datetime.utcnow(),self.s.default_leverage)
        self.positions[p.id]=p; return p
