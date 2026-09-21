import httpx
from app.models import MarketSnapshot, Candle

class BybitClient:
    def __init__(self, testnet=False):
        self.base = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"

    async def get_tickers(self):
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{self.base}/v5/market/tickers",
                             params={"category":"linear"})
            r.raise_for_status()
            rows = r.json()["result"]["list"]
        out=[]
        for x in rows:
            try:
                last=float(x["lastPrice"]); bid=float(x["bid1Price"]); ask=float(x["ask1Price"])
                if min(last,bid,ask)<=0: continue
                out.append(MarketSnapshot(
                    x["symbol"],last,bid,ask,float(x.get("turnover24h") or 0),
                    float(x.get("price24hPcnt") or 0)*100,float(x.get("volume24h") or 0),
                    float(x.get("fundingRate") or 0),float(x.get("openInterest") or 0),
                    (ask-bid)/last*10000))
            except (ValueError,TypeError,KeyError):
                pass
        return out

    async def get_klines(self,symbol,interval="5",limit=200):
        async with httpx.AsyncClient(timeout=10) as c:
            r=await c.get(f"{self.base}/v5/market/kline",
                params={"category":"linear","symbol":symbol,"interval":interval,"limit":limit})
            r.raise_for_status()
            rows=sorted(r.json()["result"]["list"],key=lambda x:int(x[0]))
        return [Candle(int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])) for x in rows]
