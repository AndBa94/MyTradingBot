import asyncio
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from app.config import settings
from app.engine import Engine
from app.storage import Store
from app.telegram_bot import telegram_polling
from app.web import HTML

app=FastAPI(title=settings.app_name,version="0.3.0")
store=Store(settings.db_path)
engine=Engine(settings,store)

@app.on_event("startup")
async def startup():
    asyncio.create_task(engine.loop())
    asyncio.create_task(telegram_polling())

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML

@app.get("/health")
async def health():
    return {"status":"ok","mode":settings.environment,"running":engine.running,"trading_enabled":engine.trading_enabled,"opportunities":len(engine.last_scan)}

@app.get("/api/markets")
async def markets():
    if not engine.latest_markets:
        try:
            engine.latest_markets = await engine.client.get_tickers()
        except Exception:
            return []
    return [x.__dict__ for x in sorted(engine.latest_markets,key=lambda x:x.turnover_24h,reverse=True)[:100]]

@app.get("/api/opportunities")
async def opportunities():
    return [x.__dict__ for x in engine.last_scan]

@app.get("/api/positions")
async def positions():
    await engine.refresh_positions()
    return [x.__dict__ for x in engine.positions.values()]

@app.get("/api/history")
async def history():
    return store.history()

@app.get("/api/settings")
async def get_settings():
    return engine.settings

@app.post("/api/settings")
async def set_settings(data: dict):
    return engine.update_settings(data)

@app.post("/api/trading")
async def set_trading(data: dict):
    return {"trading_enabled": engine.set_trading(data.get("enabled", False))}

@app.post("/api/scan")
async def scan():
    return [x.__dict__ for x in await engine.scan_once()]

@app.post("/api/paper/open/{index}")
async def paper_open(index:int):
    if index<0 or index>=len(engine.last_scan):
        return {"error":"opportunity_not_found"}
    p=engine.open_paper(engine.last_scan[index])
    return p.__dict__ if p else {"error":"position_limit_or_risk"}

@app.post("/api/positions/{position_id}/close")
async def close_position(position_id:str):
    p=engine.close_paper(position_id)
    return {"ok":bool(p)}

@app.post("/api/reset")
async def reset():
    engine.positions.clear()
    engine.last_scan=[]
    store.reset()
    return {"ok":True}
