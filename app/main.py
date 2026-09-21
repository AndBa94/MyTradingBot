import asyncio
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from app.config import settings
from app.engine import Engine
from app.storage import Store
from app.telegram_bot import telegram_polling

app=FastAPI(title=settings.app_name,version="0.2.0")
store=Store(settings.db_path); engine=Engine(settings,store)

@app.on_event("startup")
async def startup():
    asyncio.create_task(engine.loop())
    asyncio.create_task(telegram_polling())

@app.get("/", response_class=HTMLResponse)
async def home():
    return """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MyTradingBot</title><style>
body{margin:0;background:#0b0f14;color:#eaf0f6;font-family:-apple-system,BlinkMacSystemFont,sans-serif}
main{padding:20px;max-width:600px;margin:auto}.card{background:#141a22;border:1px solid #27303b;border-radius:18px;padding:18px;margin:12px 0}
h1{font-size:28px;margin:8px 0}.muted{color:#8e9aa8}.value{font-size:30px;font-weight:700}
button{width:100%;padding:14px;border:0;border-radius:12px;background:#2f81f7;color:white;font-size:16px}
</style></head><body><main><h1>MyTradingBot</h1><div class="card"><div class="muted">Режим</div><div class="value">PAPER</div></div>
<div class="card"><div class="muted">Статус</div><div id="status" class="value">Подключение…</div></div>
<div class="card"><div class="muted">Возможности</div><div id="opp">Загрузка…</div></div>
<button onclick="load()">Обновить</button></main>
<script>
async function load(){try{let h=await fetch('/health').then(r=>r.json());document.getElementById('status').textContent=h.running?'🟢 Работает':'🔴 Остановлен';
let o=await fetch('/api/opportunities').then(r=>r.json());document.getElementById('opp').textContent=o.length+' найдено';}catch(e){document.getElementById('status').textContent='Ошибка';}}
load();setInterval(load,5000);
</script></body></html>"""

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
