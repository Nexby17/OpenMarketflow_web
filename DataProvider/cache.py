"""Thread-safe in-memory cache for candles, quotes, orders, positions."""
import threading
import time
from collections import deque
from datetime import datetime, timezone


def _decimal_val(d) -> float:
    """Extract float from google.type.Decimal protobuf."""
    if hasattr(d, "value"):
        return float(d.value)
    return float(d) if d else 0.0


def _ts(dt_or_ts) -> str:
    """Convert protobuf Timestamp to ISO string."""
    if dt_or_ts is None:
        return ""
    if hasattr(dt_or_ts, "seconds"):
        return datetime.fromtimestamp(dt_or_ts.seconds, tz=timezone.utc).isoformat()
    return str(dt_or_ts)


class DataCache:
    def __init__(self, candle_max: int = 500):
        self._lock = threading.Lock()
        self._candle_max = candle_max

        # key: (symbol, tf_str) -> deque of dicts
        self.candles: dict[tuple[str, str], deque] = {}
        # key: symbol -> dict with bid/ask/last/timestamp
        self.quotes: dict[str, dict] = {}
        # key: order_id -> dict
        self.orders: dict[str, dict] = {}
        # key: (account_id, symbol) -> dict with position info
        self.positions: dict[tuple[str, str], dict] = {}

        # Recent fills (terminal orders) — kept for 10 sec for fill detection
        self.recent_fills: dict[str, dict] = {}  # order_id -> {data, ts}

        # Active bar subscriptions: set of (symbol, tf_str)
        self.bar_subs: set[tuple[str, str]] = set()

        # Last update timestamps for health monitoring
        self._last_bar_ts: dict[tuple[str, str], float] = {}  # key -> time.time()
        self._last_quote_ts: dict[str, float] = {}  # symbol -> time.time()

    # --- Candles ---

    def add_bar(self, symbol: str, tf_str: str, bar) -> None:
        """Add a Bar protobuf from on_new_bar callback."""
        d = {
            "timestamp": _ts(bar.timestamp),
            "open": _decimal_val(bar.open),
            "high": _decimal_val(bar.high),
            "low": _decimal_val(bar.low),
            "close": _decimal_val(bar.close),
            "volume": _decimal_val(bar.volume),
        }
        key = (symbol, tf_str)
        with self._lock:
            if key not in self.candles:
                self.candles[key] = deque(maxlen=self._candle_max)
            # Avoid duplicates (same timestamp)
            q = self.candles[key]
            if not q or q[-1]["timestamp"] != d["timestamp"]:
                q.append(d)
            else:
                q[-1] = d  # update last bar
            self._last_bar_ts[key] = time.time()

    def get_candles(self, symbol: str, tf_str: str, limit: int = 100) -> list[dict]:
        with self._lock:
            q = self.candles.get((symbol, tf_str), deque())
            return list(q)[-limit:]

    # --- Quotes ---

    def update_quote(self, quote) -> None:
        """Update from SubscribeQuoteResponse (has quote list)."""
        for q in quote.quote:
            symbol = q.symbol
            d = {
                "symbol": symbol,
                "bid": _decimal_val(q.bid),
                "ask": _decimal_val(q.ask),
                "last": _decimal_val(q.last),
                "bid_size": _decimal_val(q.bid_size),
                "ask_size": _decimal_val(q.ask_size),
                "last_size": _decimal_val(q.last_size),
                "volume": _decimal_val(q.volume),
                "timestamp": _ts(q.timestamp),
            }
            with self._lock:
                self.quotes[symbol] = d
                self._last_quote_ts[symbol] = time.time()

    def get_quote(self, symbol: str) -> dict | None:
        with self._lock:
            return self.quotes.get(symbol)

    # --- Orders ---

    def update_order(self, order_state) -> None:
        """Update from on_order callback (OrderState)."""
        oid = order_state.order_id
        o = order_state.order
        d = {
            "order_id": oid,
            "account_id": o.account_id,
            "symbol": o.symbol,
            "quantity": _decimal_val(o.quantity),
            "side": str(o.side),
            "type": str(o.type),
            "limit_price": _decimal_val(o.limit_price) if o.HasField("limit_price") else None,
            "stop_price": _decimal_val(o.stop_price) if o.HasField("stop_price") else None,
            "status": str(order_state.status),
            "executed_quantity": _decimal_val(order_state.executed_quantity),
            "remaining_quantity": _decimal_val(order_state.remaining_quantity),
            "transact_at": _ts(order_state.transact_at),
            "client_order_id": o.client_order_id,
        }
        with self._lock:
            # If fully executed/cancelled, remove from active
            status_val = order_state.status  # int enum
            # Terminal statuses — remove from active
            terminal = {3, 5, 9, 13, 16, 19, 20, 22, 23, 28, 31}  # filled, cancelled, rejected, expired, failed, etc.
            if status_val in terminal:
                self.orders.pop(oid, None)
                # Keep fills for 10 sec so callers can detect them
                if status_val == 3:  # filled
                    # Only add to recent_fills if actually executed something
                    exec_qty = _decimal_val(order_state.executed_quantity)
                    if exec_qty > 0:
                        d["fill_ts"] = time.time()
                        self.recent_fills[oid] = d
            else:
                self.orders[oid] = d

        # Prune old fills (> 10 sec)
        now = time.time()
        with self._lock:
            self.recent_fills = {k: v for k, v in self.recent_fills.items() if now - v.get("fill_ts", 0) < 10}

    def get_orders(self, account_id: str) -> list[dict]:
        with self._lock:
            return [o for o in self.orders.values() if not account_id or o.get("account_id") == account_id]

    # --- Positions (derived from fills) ---

    def update_trade(self, trade) -> None:
        """Update position from on_trade callback."""
        # Build position from fills
        pass  # Positions will be queried on demand from REST endpoint

    def get_status(self) -> dict:
        with self._lock:
            return {
                "candle_subscriptions": list(self.bar_subs),
                "cached_symbols": list(self.quotes.keys()),
                "active_orders": len(self.orders),
                "candle_keys": [f"{s}/{t}" for s, t in self.candles.keys()],
            }

    def get_health(self) -> dict:
        """Health check: how fresh is the data?"""
        now = time.time()
        with self._lock:
            bar_health = {}
            for key, ts in self._last_bar_ts.items():
                sym, tf = key
                bar_health[f"{sym}/{tf}"] = {
                    "age_s": round(now - ts, 1),
                    "stale": (now - ts) > 120,  # 2 min = stale
                }
            quote_health = {}
            for sym, ts in self._last_quote_ts.items():
                quote_health[sym] = {
                    "age_s": round(now - ts, 1),
                    "stale": (now - ts) > 30,  # 30 sec = stale
                }
        return {
            "bars": bar_health,
            "quotes": quote_health,
            "healthy": not any(b["stale"] for b in bar_health.values()) and not any(q["stale"] for q in quote_health.values()),
        }
