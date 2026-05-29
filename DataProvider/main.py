"""DataProvider — FastAPI app serving cached Finam data via REST."""
import logging
import os
import sys
import time

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query

import cache as cache_mod
import config as cfg_mod
import provider as prov_mod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("main")

# Globals
the_cache: cache_mod.DataCache | None = None
the_provider: prov_mod.FinamProvider | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global the_cache, the_provider
    cfg = cfg_mod.load_config()
    logger.info("Config: %s", cfg)

    the_cache = cache_mod.DataCache(candle_max=cfg["candle_cache_size"])
    the_provider = prov_mod.FinamProvider(the_cache, cfg["account_id"])

    try:
        the_provider.connect()
        the_provider.start_subscriptions(cfg["symbols"], cfg["timeframes"])
    except Exception as e:
        logger.error("Failed to start: %s", e)
        sys.exit(1)

    yield

    the_provider.shutdown()


app = FastAPI(title="Finam DataProvider", lifespan=lifespan)


@app.get("/status")
def get_status():
    connected = the_provider is not None and the_provider.fp is not None
    return {
        "status": "ok",
        "connected": connected,
        "cache": the_cache.get_status() if the_cache else {},
    }


@app.get("/health")
def get_health():
    """Health check for heartbeat monitoring. Returns data freshness info."""
    health = the_cache.get_health() if the_cache else {"healthy": False, "bars": {}, "quotes": {}}
    connected = the_provider is not None and the_provider.fp is not None
    health["connected"] = connected
    health["overall"] = health["healthy"] and connected
    return health


@app.get("/quote/{symbol}")
def get_quote(symbol: str):
    q = the_cache.get_quote(symbol) if the_cache else None
    if q is None:
        return {"error": "no data", "symbol": symbol}
    return q


@app.post("/invalidate")
def invalidate_cache(account: str = ""):
    """Invalidate all caches for an account — force fresh fetch on next request."""
    if the_provider:
        with the_provider._pos_lock:
            the_provider._pos_cache.clear()
        with the_provider._orders_lock:
            the_provider._orders_cache.clear()
        with the_provider._account_lock:
            the_provider._account_cache.clear()
        # Also clear stale recent fills to prevent false detection
        if the_cache:
            with the_cache._lock:
                the_cache.recent_fills.clear()
    return {"invalidated": True, "account": account}


@app.get("/candles/{symbol}")
def get_candles(symbol: str, tf: str = "M5", limit: int = Query(default=100, le=500)):
    bars = the_cache.get_candles(symbol, tf, limit) if the_cache else []
    return {"symbol": symbol, "tf": tf, "count": len(bars), "bars": bars}


@app.get("/orders")
def get_orders(account: str = ""):
    if the_provider and account:
        return the_provider.get_all_orders(account)
    return the_cache.get_orders(account) if the_cache else []


@app.get("/position")
def get_position(account: str, ticker: str):
    pos = the_provider.get_positions(account, ticker) if the_provider else None
    if pos is None:
        return {"ticker": ticker, "account": account, "dir": 0, "lots": 0,
                "avg_price": 0.0, "current_price": 0.0}
    return pos


@app.get("/recent-fills")
def get_recent_fills(account: str = "", symbol: str = ""):
    """Get recent fills from gRPC streaming (last 10 sec). For fill detection."""
    if not the_cache:
        return []
    now = time.time()
    # Prune expired fills on every read
    with the_cache._lock:
        the_cache.recent_fills = {k: v for k, v in the_cache.recent_fills.items() 
                                   if now - v.get("fill_ts", 0) < 10}
    fills = []
    with the_cache._lock:
        for fid, f in the_cache.recent_fills.items():
            if account and f.get("account_id") != account:
                continue
            if symbol and f.get("symbol") != symbol:
                continue
            fills.append(f)
    return fills


@app.post("/subscribe/{symbol}")
def subscribe(symbol: str, tf: str = "M5"):
    if not the_provider:
        return {"error": "not ready"}
    added = the_provider.start_bar_sub(symbol, tf)
    return {"symbol": symbol, "tf": tf, "subscribed": added}


@app.post("/unsubscribe/{symbol}")
def unsubscribe(symbol: str, tf: str = "M5"):
    if not the_provider:
        return {"error": "not ready"}
    the_provider.stop_bar_sub(symbol, tf)
    return {"symbol": symbol, "tf": tf, "unsubscribed": True}


if __name__ == "__main__":
    import uvicorn
    cfg = cfg_mod.load_config()
    uvicorn.run(app, host="0.0.0.0", port=cfg["port"])
