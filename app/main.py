import asyncio
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from app.config import settings
from app.engine import Engine
from app.storage import Store
from app.telegram_bot import telegram_polling
from app.web import HTML

store = Store(settings.db_path)
engine = Engine(settings, store)

@asynccontextmanager
async def lifespan(app):
    try:
        engine.latest_markets = await engine.client.get_tickers()
        engine.last_error = None
    except Exception as exc:
        engine.last_error = f"{type(exc).__name__}: {exc}"

    tasks = [asyncio.create_task(engine.loop()), asyncio.create_task(telegram_polling())]
    try:
        yield
    finally:
        engine.running = False
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

app = FastAPI(title=settings.app_name, version="0.5.0", lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML

@app.get("/health")
async def health():
    return {
        "status":"ok", "mode":settings.environment, "running":engine.running,
        "trading_enabled":engine.trading_enabled, "opportunities":len(engine.last_scan),
        "markets":len(engine.latest_markets), "positions":len(engine.positions),
        "last_error":engine.last_error, "last_action":engine.last_action,
        **engine.account_snapshot(),
    }

@app.get("/api/markets")
async def markets():
    if not engine.latest_markets:
        try:
            engine.latest_markets = await engine.client.get_tickers()
            engine.last_error = None
        except Exception as exc:
            engine.last_error = f"{type(exc).__name__}: {exc}"
            return {"ok":False,"error":engine.last_error,"markets":[]}
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

@app.get("/api/stats")
async def stats():
    return store.statistics()

@app.get("/api/self-analysis")
async def self_analysis():
    return store.statistics().get("self_analysis", {})

@app.get("/api/forensics")
async def forensics():
    rows = store.history(100)
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["forensic"] = json.loads(item.get("forensic") or "{}")
        except Exception:
            item["forensic"] = {}
        out.append(item)
    return out

@app.get("/api/settings")
async def get_settings():
    return engine.settings

@app.post("/api/settings")
async def set_settings(data: dict):
    try:
        return engine.update_settings(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

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
    p=engine.open_paper(engine.last_scan[index], automatic=False)
    return p.__dict__ if p else {"error":"paper_mode_required_or_position_limit_or_risk_or_duplicate"}

@app.post("/api/positions/{position_id}/close")
async def close_position(position_id:str):
    await engine.refresh_positions()
    p=engine.close_paper(position_id)
    return {"ok":bool(p)}

@app.post("/api/reset")
async def reset():
    engine.reset_paper()
    return {"ok":True}
