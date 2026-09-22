import asyncio
import httpx
from app.models import MarketSnapshot, Candle

class BybitClient:
    def __init__(self, testnet=False, category="linear"):
        self.base = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"
        self.category = category
        self.headers = {"User-Agent": "MyTradingBot/1.0"}

    async def get_tickers(self):
        last_error = None
        for attempt in range(3):
            try:
                timeout = httpx.Timeout(20.0, connect=8.0)
                async with httpx.AsyncClient(timeout=timeout, headers=self.headers) as c:
                    r = await c.get(
                        f"{self.base}/v5/market/tickers",
                        params={"category": self.category},
                    )
                    r.raise_for_status()
                    data = r.json()
                    if data.get("retCode", 0) != 0:
                        raise RuntimeError(f"Bybit retCode={data.get('retCode')}: {data.get('retMsg')}")
                    rows = data.get("result", {}).get("list", [])
                    if not rows:
                        raise RuntimeError("Bybit returned no linear market tickers")

                out = []
                for x in rows:
                    try:
                        last = float(x["lastPrice"])
                        bid = float(x["bid1Price"])
                        ask = float(x["ask1Price"])
                        if min(last, bid, ask) <= 0:
                            continue
                        out.append(MarketSnapshot(
                            x["symbol"], last, bid, ask,
                            float(x.get("turnover24h") or 0),
                            float(x.get("price24hPcnt") or 0) * 100,
                            float(x.get("volume24h") or 0),
                            float(x.get("fundingRate") or 0),
                            float(x.get("openInterest") or 0),
                            (ask - bid) / last * 10000,
                        ))
                    except (ValueError, TypeError, KeyError):
                        continue

                if not out:
                    raise RuntimeError("Bybit returned no valid ticker rows")
                return out
            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError, RuntimeError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                else:
                    raise ConnectionError(f"Bybit market request failed after 3 attempts: {exc}") from exc

        raise ConnectionError(f"Bybit market request failed: {last_error}")

    async def get_orderbook(self, symbol, limit=50):
        last_error = None
        for attempt in range(3):
            try:
                timeout = httpx.Timeout(10.0, connect=8.0)
                async with httpx.AsyncClient(timeout=timeout, headers=self.headers) as c:
                    r = await c.get(
                        f"{self.base}/v5/market/orderbook",
                        params={"category": self.category, "symbol": symbol, "limit": limit},
                    )
                    r.raise_for_status()
                    data = r.json()
                    if data.get("retCode", 0) != 0:
                        raise RuntimeError(
                            f"Bybit orderbook retCode={data.get('retCode')}: {data.get('retMsg')}"
                        )
                    result = data.get("result", {})
                    bids = result.get("b", [])
                    asks = result.get("a", [])
                    if not bids or not asks:
                        raise RuntimeError(f"Bybit returned empty orderbook for {symbol}")
                    return {"bids": bids, "asks": asks}
            except (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.NetworkError,
                RuntimeError,
                ValueError,
                TypeError,
            ) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.7 * (attempt + 1))
                else:
                    raise ConnectionError(
                        f"Bybit orderbook request failed after 3 attempts for {symbol}: {exc}"
                    ) from exc
        raise ConnectionError(f"Bybit orderbook request failed for {symbol}: {last_error}")

    async def get_klines(self, symbol, interval="5", limit=200):
        last_error = None
        for attempt in range(3):
            try:
                timeout = httpx.Timeout(15.0, connect=8.0)
                async with httpx.AsyncClient(timeout=timeout, headers=self.headers) as c:
                    r = await c.get(
                        f"{self.base}/v5/market/kline",
                        params={"category": self.category, "symbol": symbol, "interval": interval, "limit": limit},
                    )
                    r.raise_for_status()
                    data = r.json()
                    if data.get("retCode", 0) != 0:
                        raise RuntimeError(f"Bybit kline retCode={data.get('retCode')}: {data.get('retMsg')}")
                    rows = data.get("result", {}).get("list", [])
                    if not rows:
                        raise RuntimeError(f"Bybit returned no kline data for {symbol}")
                    rows = sorted(rows, key=lambda x: int(x[0]))
                    out = []
                    for x in rows:
                        if len(x) < 6:
                            continue
                        out.append(Candle(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])))
                    if len(out) < 60:
                        raise RuntimeError(f"Bybit returned insufficient kline data for {symbol}: {len(out)}")
                    return out
            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.0 * (attempt + 1))
                else:
                    raise ConnectionError(f"Bybit kline request failed after 3 attempts for {symbol}: {exc}") from exc
        raise ConnectionError(f"Bybit kline request failed for {symbol}: {last_error}")
