"""DataProvider — FastAPI app serving cached Finam data via REST."""
import logging
import os
import sys

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
    return {
        "status": "ok",
        "cache": the_cache.get_status() if the_cache else {},
    }


@app.get("/quote/{symbol}")
def get_quote(symbol: str):
    q = the_cache.get_quote(symbol) if the_cache else None
    if q is None:
        return {"error": "no data", "symbol": symbol}
    return q


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
        return {"error": "not found", "account": account, "ticker": ticker}
    return pos


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
