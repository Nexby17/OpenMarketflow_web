"""FinamPy wrapper — manages gRPC subscriptions and feeds the cache."""
import logging
import os
import threading
import time
from typing import Optional

from FinamPy import FinamPy
from FinamPy.grpc import marketdata_service_pb2 as md

from cache import DataCache

logger = logging.getLogger("provider")

# Timeframe string -> Finam TimeFrame enum
_TF_MAP = {
    "M1": md.TimeFrame.TIME_FRAME_M1,
    "M5": md.TimeFrame.TIME_FRAME_M5,
    "M15": md.TimeFrame.TIME_FRAME_M15,
    "M30": md.TimeFrame.TIME_FRAME_M30,
    "M60": md.TimeFrame.TIME_FRAME_H1,
    "D1": md.TimeFrame.TIME_FRAME_D,
}


class FinamProvider:
    def __init__(self, cache: DataCache, account_id: str):
        self.cache = cache
        self.account_id = account_id
        self.fp: Optional[FinamPy] = None
        self._threads: list[threading.Thread] = []
        self._bar_threads: dict[tuple[str, str], threading.Thread] = {}
        self._stopped = threading.Event()

    def connect(self) -> None:
        """Initialize FinamPy connection."""
        token = os.environ.get("FINAM_TOKEN")
        if not token:
            raise RuntimeError("FINAM_TOKEN env var not set")

        logger.info("Connecting to Finam gRPC...")
        self.fp = FinamPy(token)
        logger.info("Connected. Accounts: %s", self.fp.account_ids)

        # Wire up event handlers
        self.fp.on_new_bar.subscribe(self._on_bar)
        self.fp.on_quote.subscribe(self._on_quote)
        self.fp.on_order.subscribe(self._on_order)

    def start_subscriptions(self, symbols: list[str], timeframes: list[str]) -> None:
        """Start all subscriptions."""
        if not self.fp:
            raise RuntimeError("Not connected")

        # Orders + trades
        self._start_thread("orders", self._sub_orders)
        self._start_thread("trades", self._sub_trades)

        # Quotes for all symbols
        self._start_thread("quotes", self._sub_quotes, args=(tuple(symbols),))

        # Bars
        for symbol in symbols:
            for tf in timeframes:
                key = (symbol, tf)
                with self.cache._lock:
                    self.cache.bar_subs.add(key)
                self._start_bar_sub(symbol, tf)

        logger.info("All subscriptions started")

    def _start_thread(self, name: str, target, args=()) -> None:
        t = threading.Thread(target=self._wrap_thread(name, target), args=args, daemon=True)
        self._threads.append(t)
        t.start()

    def _wrap_thread(self, name, target):
        def wrapper(*args, **kwargs):
            while not self._stopped.is_set():
                try:
                    target(*args, **kwargs)
                except Exception as e:
                    logger.error("Thread %s crashed: %s", name, e)
                    if self._stopped.wait(5):
                        return
        return wrapper

    def _sub_orders(self) -> None:
        self.fp.subscribe_orders_thread(account_id=self.account_id)

    def _sub_trades(self) -> None:
        self.fp.subscribe_trades_thread(account_id=self.account_id)

    def _sub_quotes(self, symbols: tuple) -> None:
        self.fp.subscribe_quote_thread(symbols)

    def start_bar_sub(self, symbol: str, tf: str) -> bool:
        """Subscribe to bars for symbol/tf. Returns True if new sub."""
        key = (symbol, tf)
        if key in self._bar_threads and self._bar_threads[key].is_alive():
            return False
        self._start_bar_sub(symbol, tf)
        with self.cache._lock:
            self.cache.bar_subs.add(key)
        return True

    def _start_bar_sub(self, symbol: str, tf: str) -> None:
        if tf not in _TF_MAP:
            logger.warning("Unknown timeframe: %s", tf)
            return
        finam_tf = _TF_MAP[tf]
        name = f"bars-{symbol}-{tf}"
        t = threading.Thread(
            target=self._wrap_thread(name, self.fp.subscribe_bars_thread),
            args=(symbol, finam_tf),
            daemon=True,
        )
        self._bar_threads[(symbol, tf)] = t
        t.start()
        logger.info("Subscribed bars: %s %s", symbol, tf)

    def stop_bar_sub(self, symbol: str, tf: str) -> bool:
        """Mark bar sub as stopped (thread will die on its own, no graceful unsub in FinamPy)."""
        key = (symbol, tf)
        with self.cache._lock:
            self.cache.bar_subs.discard(key)
        return True

    # --- Event handlers ---

    def _on_bar(self, event, finam_tf) -> None:
        """Callback from fp.on_new_bar. event is SubscribeBarsResponse."""
        symbol = event.symbol
        # Convert finam_tf back to string
        tf_str = "UNKNOWN"
        for k, v in _TF_MAP.items():
            if v == finam_tf:
                tf_str = k
                break
        for bar in event.bars:
            self.cache.add_bar(symbol, tf_str, bar)

    def _on_quote(self, event) -> None:
        """Callback from fp.on_quote. event is SubscribeQuoteResponse."""
        self.cache.update_quote(event)

    def _on_order(self, order_state) -> None:
        """Callback from fp.on_order."""
        self.cache.update_order(order_state)

    # --- Cached position/orders (TTL-based to avoid rate limits) ---
    _pos_cache: dict = {}  # key -> {data, ts}
    _orders_cache: dict = {}  # key -> {data, ts}
    _POS_TTL = 2.0  # seconds
    _ORDERS_TTL = 2.0
    _pos_lock = threading.Lock()
    _orders_lock = threading.Lock()

    def get_positions(self, account_id: str, ticker: str) -> dict | None:
        """Get position via gRPC GetAccount, with TTL cache to avoid rate limits."""
        if not self.fp:
            return None

        cache_key = f"{account_id}:{ticker}"
        now = time.time()

        # Check cache
        with self._pos_lock:
            cached = self._pos_cache.get(cache_key)
            if cached and (now - cached["ts"]) < self._POS_TTL:
                return cached["data"]

        # Fetch from gRPC
        data = self._fetch_position(account_id, ticker)

        # Update cache (even if None — "no position" is valid)
        with self._pos_lock:
            self._pos_cache[cache_key] = {"data": data, "ts": now}

        return data

    def _fetch_position(self, account_id: str, ticker: str) -> dict | None:
        """Raw fetch — gRPC first, REST fallback."""
        # Variant 1: gRPC GetAccount
        try:
            from FinamPy.grpc import accounts_service_pb2 as accts
            resp = self.fp.call_function(
                self.fp.accounts_stub.GetAccount,
                accts.GetAccountRequest(account_id=account_id),
            )
            if resp:
                for pos in resp.positions:
                    sym = pos.symbol.split('@')[0] if '@' in pos.symbol else pos.symbol
                    if sym == ticker or pos.symbol == ticker:
                        qty_raw = pos.quantity
                        qty = 0
                        try:
                            qty = int(qty_raw)
                        except (TypeError, ValueError):
                            try:
                                qty = int(float(str(qty_raw)))
                            except:
                                qty = 0
                        qty = abs(qty)
                        return {
                            "ticker": ticker,
                            "account": account_id,
                            "dir": 1 if qty > 0 else (-1 if qty < 0 else 0),
                            "lots": abs(qty),
                            "avg_price": (lambda v: float(str(v)) if v and str(v) else 0.0)(pos.average_price),
                            "current_price": (lambda v: float(str(v)) if v and str(v) else 0.0)(pos.current_price),
                        }
                return None
        except Exception as e:
            logger.warning("gRPC GetAccount failed: %s, falling back to REST", e)

        # Variant 2: REST fallback
        try:
            import requests
            jwt = self.fp.jwt_token
            if not jwt:
                return None
            r = requests.get(
                f"https://api.finam.ru/v1/accounts/{account_id}",
                headers={"Authorization": f"Bearer {jwt}"},
                timeout=3,
            )
            if r.status_code == 200:
                data = r.json()
                for p in data.get("positions", []):
                    sym = (p.get("symbol", "")).split('@')[0]
                    if sym == ticker:
                        qty = int(p.get("quantity", {}).get("value", 0))
                        return {
                            "ticker": ticker,
                            "account": account_id,
                            "dir": 1 if qty > 0 else (-1 if qty < 0 else 0),
                            "lots": abs(qty),
                            "avg_price": float(p.get("average_price", {}).get("value", 0)),
                            "current_price": float(p.get("current_price", {}).get("value", 0)),
                        }
        except Exception as e:
            logger.error("REST fallback failed: %s", e)
        return None

    def get_all_orders(self, account_id: str) -> list[dict]:
        """Get all active orders via gRPC, with TTL cache."""
        if not self.fp:
            return []

        now = time.time()
        cache_key = account_id

        with self._orders_lock:
            cached = self._orders_cache.get(cache_key)
            if cached and (now - cached["ts"]) < self._ORDERS_TTL:
                return cached["data"]

        data = self._fetch_orders(account_id)

        with self._orders_lock:
            self._orders_cache[cache_key] = {"data": data, "ts": now}

        return data

    def _fetch_orders(self, account_id: str) -> list[dict]:
        """Raw fetch orders via REST (gRPC GetAccount doesn't return orders)."""
        try:
            import requests
            jwt = self.fp.jwt_token
            if not jwt:
                return []
            r = requests.get(
                f"https://api.finam.ru/v1/accounts/{account_id}",
                headers={"Authorization": f"Bearer {jwt}"},
                timeout=3,
            )
            if r.status_code == 200:
                data = r.json()
                orders = []
                for o in data.get("orders", []):
                    orders.append({
                        "order_id": str(o.get("id", "")),
                        "symbol": o.get("symbol", ""),
                        "side": o.get("side", ""),
                        "quantity": int(float(str(o.get("quantity", {}).get("value", "0")))),
                        "limit_price": float(str(o.get("limit_price", {}).get("value", "0"))) if o.get("limit_price") else 0,
                        "status": o.get("status", ""),
                        "comment": o.get("comment", ""),
                    })
                return orders
        except Exception as e:
            logger.error("REST orders failed: %s", e)
        return []

    def shutdown(self) -> None:
        logger.info("Shutting down provider...")
        self._stopped.set()
        if self.fp:
            try:
                self.fp.close_channel()
            except Exception:
                pass
