import asyncio
from fastapi import FastAPI
from app.config import settings
from app.engine import Engine
from app.storage import Store

app=FastAPI(title=settings.app_name,version="0.1.0")
store=Store(settings.db_path); engine=Engine(settings,store)

@app.on_event("startup")
async def startup(): asyncio.create_task(engine.loop())

@app.get("/health")
async def health(): return {"status":"ok","mode":settings.environment,"running":engine.running}

@app.get("/api/markets")
async def markets(): return [x.__dict__ for x in sorted(await engine.client.get_tickers(),key=lambda x:x.turnover_24h,reverse=True)[:100]]

@app.get("/api/opportunities")
async def opportunities(): return [x.__dict__ for x in engine.last_scan]

@app.get("/api/positions")
async def positions(): return [x.__dict__ for x in engine.positions.values()]

@app.get("/api/history")
async def history(): return store.history()

@app.post("/api/scan")
async def scan(): return [x.__dict__ for x in await engine.scan_once()]

@app.post("/api/paper/open/{index}")
async def paper_open(index:int):
    if index<0 or index>=len(engine.last_scan): return {"error":"opportunity_not_found"}
    p=engine.open_paper(engine.last_scan[index])
    return p.__dict__ if p else {"error":"position_limit_or_risk"}

@app.post("/api/reset")
async def reset():
    engine.positions.clear(); engine.last_scan=[]
    return {"ok":True}
