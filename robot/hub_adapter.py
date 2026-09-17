"""PAPER LAB: FinamHubAdapter — мост между finam_hub (WS) и обработчиками робота.

Замена gRPC-подписок finam_compat на единый WS-хаб (Этап 2 миграции, PC-003).

Совместимость: адаптер эмулирует event-объекты finam_compat (атрибуты .trades/.quote/
.order_book/.bars с .price.value и т.п.), поэтому _on_* обработчики main_of_mx_paper.py
не меняются вообще.

Плюс:paper-исследование — сюда пишем все отличия форматов (papercuts.md, PC-003).
"""
import logging
import threading

log = logging.getLogger("hub_adapter")


class _D:
    """Decimal-подобный объект: .value -> str (как в Finam gRPC)."""
    __slots__ = ("value",)

    def __init__(self, v):
        self.value = None if v is None else str(v)


class _Ts:
    """Timestamp-подобный: .seconds (epoch)."""
    __slots__ = ("seconds",)

    def __init__(self, iso_or_epoch, hub):
        self.seconds = hub._ts_to_epoch(iso_or_epoch)


class _Trade:
    __slots__ = ("price", "size", "side", "timestamp")

    def __init__(self, t, hub):
        # WS: {"tradeId","timestamp","price":{"value":"…"},"quantity":{"value":"…"},"side":"SIDE_BUY"/"SIDE_SELL"}
        self.price = _D(t.get("price", {}).get("value"))
        qty = t.get("quantity") or t.get("size") or {}
        self.size = _D(qty.get("value") if isinstance(qty, dict) else qty)
        side = t.get("side", "")
        self.side = 1 if "BUY" in str(side).upper() else 2  # 1=BUY, 2=SELL — как в Finam gRPC
        self.timestamp = _Ts(t.get("timestamp"), hub)


class _TradesEvent:
    __slots__ = ("trades", "symbol")

    def __init__(self, payload, hub):
        self.symbol = payload.get("symbol") if isinstance(payload, dict) else None
        trades = payload.get("trades", payload.get("trade", [])) if isinstance(payload, dict) else []
        self.trades = [_Trade(t, hub) for t in trades]


class _Quote:
    __slots__ = ("bid", "ask", "last", "symbol")

    def __init__(self, q):
        self.symbol = q.get("symbol")  # guard: робот игнорирует чужие котировки
        self.bid = _D(q.get("bid", {}).get("value"))
        self.ask = _D(q.get("ask", {}).get("value"))
        self.last = _D(q.get("last", {}).get("value"))


class _QuoteEvent:
    __slots__ = ("quote",)

    def __init__(self, payload):
        qs = payload.get("quote", []) if isinstance(payload, dict) else []
        self.quote = [_Quote(q) for q in qs]


class _OBRow:
    __slots__ = ("price", "buy_size", "sell_size", "action")

    def __init__(self, r):
        self.price = _D(r.get("price", {}).get("value"))
        self.buy_size = _D(r.get("buySize", {}).get("value"))
        self.sell_size = _D(r.get("sellSize", {}).get("value"))
        # WS action: "ACTION_UPDATE"/"ACTION_DELETE"/… → gRPC-подобные int (0=UPDATE,2=DELETE в compat)
        a = str(r.get("action", "ACTION_UPDATE")).upper()
        self.action = 0 if "UPDATE" in a else (2 if "DELETE" in a else 1)


class _OB:
    __slots__ = ("rows",)

    def __init__(self, ob):
        self.rows = [_OBRow(r) for r in ob.get("rows", [])]


class _OBEvent:
    __slots__ = ("order_book",)

    def __init__(self, payload):
        obs = payload.get("orderBook", []) if isinstance(payload, dict) else []
        self.order_book = [_OB(ob) for ob in obs]


class _Bar:
    __slots__ = ("open", "high", "low", "close", "volume", "timestamp")

    def __init__(self, b, hub):
        self.open = _D(b.get("open", {}).get("value"))
        self.high = _D(b.get("high", {}).get("value"))
        self.low = _D(b.get("low", {}).get("value"))
        self.close = _D(b.get("close", {}).get("value"))
        self.volume = _D(b.get("volume", {}).get("value"))
        self.timestamp = _Ts(b.get("timestamp"), hub)


class _BarsEvent:
    __slots__ = ("bars",)

    def __init__(self, payload, hub):
        bars = payload.get("bars", payload.get("bar", [])) if isinstance(payload, dict) else []
        self.bars = [_Bar(b, hub) for b in bars]


class FinamHubAdapter:
    """Подключает обработчики робота к finam_hub, конвертируя WS-payload → compat-events."""

    def __init__(self, symbol: str):
        import sys, os
        here = os.path.dirname(os.path.abspath(__file__))
        proj = os.path.dirname(here)  # robot/ → проект
        if proj not in sys.path:
            sys.path.insert(0, proj)
        from finam_hub import get_hub
        self.hub = get_hub()
        self.symbol = symbol
        self._callbacks = []          # (handler, wrapper)
        self._bar_tf = None           # timeframe str для BARS
        self._ts_cache = {}            # iso → epoch

    # --- утилиты ---

    def _ts_to_epoch(self, iso) -> float:
        if not isinstance(iso, str):
            try:
                return float(iso)
            except Exception:
                return 0.0
        if iso in self._ts_cache:
            return self._ts_cache[iso]
        from datetime import datetime, timezone
        try:
            t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            epoch = t.timestamp()
        except Exception:
            epoch = 0.0
        if len(self._ts_cache) > 4096:
            self._ts_cache.clear()
        self._ts_cache[iso] = epoch
        return epoch

    # --- подписки ---

    def start(self, on_trades, on_order_book, on_quote, on_bar=None, timeframe: str = None):
        """Подключить все каналы данных робота к хабу."""
        self._bar_tf = timeframe

        def w_trades(payload):
            try:
                on_trades(_TradesEvent(payload, self))
            except Exception as e:
                log.error("trades bridge: %s", str(e)[:120])

        def w_ob(payload):
            try:
                on_order_book(_OBEvent(payload))
            except Exception as e:
                log.error("ob bridge: %s", str(e)[:120])

        def w_quote(payload):
            try:
                on_quote(_QuoteEvent(payload))
            except Exception as e:
                log.error("quote bridge: %s", str(e)[:120])

        self.hub.start()
        self.hub.subscribe_trades(self.symbol, w_trades)
        self.hub.subscribe_order_book(self.symbol, w_ob)
        self.hub.subscribe_quotes([self.symbol], w_quote)

        if on_bar and timeframe:
            tf_map = {"M1": "TIME_FRAME_M1", "M5": "TIME_FRAME_M5",
                      "M15": "TIME_FRAME_M15", "M30": "TIME_FRAME_M30"}
            ws_tf = tf_map.get(timeframe, "TIME_FRAME_M5")

            def w_bar(payload):
                try:
                    on_bar(_BarsEvent(payload, self))
                except Exception as e:
                    log.error("bar bridge: %s", str(e)[:120])

            self.hub.subscribe_bars(self.symbol, ws_tf, w_bar)
            self._callbacks.append((on_bar, w_bar))

        self._callbacks.extend([(on_trades, w_trades), (on_order_book, w_ob), (on_quote, w_quote)])
        log.info("HubAdapter started: %s (trades+ob+quotes%s)",
                 self.symbol, "+bars" if on_bar and timeframe else "")

    def stop(self):
        for handler, wrapper in self._callbacks:
            try:
                self.hub.unsubscribe_all(wrapper)
            except Exception:
                pass
        self._callbacks.clear()
        log.info("HubAdapter stopped")

    def stats(self):
        return dict(self.hub.stats)
