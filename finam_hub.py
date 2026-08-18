"""Finam WebSocket Hub — единое WS-соединение для всех роботов.

Этап 1 миграции на новое API (см. paper_lab/papercuts.md, исследование 2026-08-18).

Архитектура:
- Один WS-коннект к wss://api.finam.ru:443/ws (JWT в Authorization header)
- Автопродление JWT за минуту до истечения (рестим через REST /v1/sessions)
- Автоматический реконнект с backoff
- Fan-out: подписчики (роботы/DataProvider) получают данные через callbacks
- Подписки QUOTES (мульти-символьно), ORDER_BOOK, INSTRUMENT_TRADES, BARS, ORDERS

Usage:
    from finam_hub import get_hub
    hub = get_hub()                      # singleton
    hub.start()                          # фоновый поток
    hub.subscribe_quotes(["MXU6@RTSX"], on_quote)   # callback(quote_dict)
    hub.subscribe_order_book("MXU6@RTSX", on_ob)    # callback(ob_dict)
    hub.subscribe_trades("MXU6@RTSX", on_trade)     # callback(trade_dict)
    hub.unsubscribe_all(callback)
"""
import json
import logging
import os
import threading
import time
import urllib.request
from typing import Callable, Optional

log = logging.getLogger("finam_hub")

WS_URL = "wss://api.finam.ru:443/ws"
AUTH_URL = "https://api.finam.ru/v1/sessions"
JWT_TTL = 15 * 60          # Finam JWT живёт 15 минут
JWT_REFRESH_MARGIN = 60    # обновляем за минуту до истечения
RECONNECT_BASE = 1.0       # базовая задержка реконнекта, сек
RECONNECT_MAX = 30.0       # максимальная

SUB_QUOTES = "QUOTES"
SUB_ORDER_BOOK = "ORDER_BOOK"
SUB_TRADES = "INSTRUMENT_TRADES"
SUB_BARS = "BARS"
SUB_ORDERS = "ORDERS"


def _get_api_key() -> str:
    key = os.environ.get("FINAM_API_KEY", "")
    if key:
        return key
    # fallback: поиск .env — cwd, папка самого finam_hub.py (корень проекта) и src/
    here = os.path.dirname(os.path.abspath(__file__))
    bases = [os.getcwd(), here, os.path.join(here, "src")]
    for base in bases:
        for sub in ("", ".."):
            p = os.path.join(base, sub, ".env")
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        for line in f:
                            if line.startswith("FINAM_API_KEY="):
                                return line.split("=", 1)[1].strip()
                except OSError:
                    continue
    return ""


def _fetch_jwt(api_key: str) -> str:
    req = urllib.request.Request(
        AUTH_URL,
        data=json.dumps({"secret": api_key}).encode(),
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())["token"]


class FinamHub:
    """Единый WS-хаб Finam. Singleton через get_hub()."""

    def __init__(self):
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._ws = None
        self._loop = None  # asyncio-loop hub-потока (для потокобезопасной отправки)
        self._ready = threading.Event()  # WS подключен и handshake прошёл
        self._outbox = []           # исходящие, если loop ещё не запущен
        self._outbox_lock = threading.Lock()

        # JWT
        self._api_key = ""
        self._jwt = ""
        self._jwt_exp = 0.0

        # Подписки: (type, key) -> set(callback)
        # QUOTES ключ = "QUOTES" (мульти-символьная подписка), остальные = символ/счёт
        self._subs = {}          # dict[(str,str), set[Callable]]
        self._sub_keys = set()   # активные ключи подписок на сервере

        # Статистика
        self.stats = {
            "connected": False,
            "last_connect_ts": 0,
            "reconnects": 0,
            "messages": 0,
            "errors": 0,
            "by_type": {},
            "last_msg_ts": 0,
        }

    # === JWT ===

    def _ensure_jwt(self) -> str:
        now = time.time()
        if not self._jwt or now >= self._jwt_exp - JWT_REFRESH_MARGIN:
            if not self._api_key:
                self._api_key = _get_api_key()
            if not self._api_key:
                raise RuntimeError("FINAM_API_KEY not set")
            self._jwt = _fetch_jwt(self._api_key)
            self._jwt_exp = now + JWT_TTL
            log.info("JWT refreshed, expires in %d min", JWT_TTL // 60)
        return self._jwt

    # === Lifecycle ===

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._running = True
            self._thread = threading.Thread(target=self._run, name="finam-hub", daemon=True)
            self._thread.start()

    def stop(self):
        self._running = False
        # закрываем ws через потокобезопасный bridge (coroutine в hub-loop)
        import asyncio
        ws, loop = self._ws, self._loop
        if ws and loop and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop).result(timeout=3)
            except Exception:
                pass

    def _run(self):
        """Главный цикл: connect → receive loop → reconnect с backoff."""
        import websockets  # лениво: зависимость только у хаба

        delay = RECONNECT_BASE
        while self._running:
            try:
                jwt = self._ensure_jwt()
                ws = websockets.connect(
                    WS_URL,
                    additional_headers={"Authorization": jwt},
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=10,
                )
                self._ws = ws
                # handshake
                import asyncio

                async def _connect_and_serve():
                    self._ws = await ws
                    hello = await asyncio.wait_for(self._ws.recv(), timeout=8)
                    d = json.loads(hello)
                    if d.get("type") == "EVENT" and d.get("event_info", {}).get("event") == "HANDSHAKE_SUCCESS":
                        return True
                    log.warning("unexpected hello: %s", hello[:120])
                    return True  # всё равно продолжаем

                #websockets.connect возвращает awaitable-объект; запускаем через свежий loop в этом потоке
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                ok = loop.run_until_complete(_connect_and_serve())
                delay = RECONNECT_BASE
                self.stats["connected"] = True
                self.stats["last_connect_ts"] = time.time()
                self._ready.set()
                log.info("WS connected (handshake ok=%s)", ok)

                # receive loop: resubscribe (после реконнекта) + flush outbox
                async def _serve():
                    self._resubscribe_all()
                    await self._flush_outbox()
                    await self._recv_loop()
                loop.run_until_complete(_serve())
            except Exception as e:
                self.stats["connected"] = False
                self._ready.clear()
                if not self._running:
                    log.info("hub stopped, exit reconnect loop")
                    break
                self.stats["errors"] += 1
                log.warning("WS disconnected: %s — reconnect in %.1fs", str(e)[:120], delay)
                time.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX)
            finally:
                self.stats["connected"] = False
                self._ready.clear()

    async def _recv_loop(self):
        import asyncio
        while self._running:
            try:
                msg = await asyncio.wait_for(self._ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                continue
            self._dispatch(msg)

    # === Subscribe API ===

    def subscribe_quotes(self, symbols: list, callback: Callable):
        key = json.dumps(sorted(symbols))
        self._add_sub(SUB_QUOTES, key, callback)
        self._send_sub(SUB_QUOTES, {"symbols": sorted(symbols)})

    def subscribe_order_book(self, symbol: str, callback: Callable):
        self._add_sub(SUB_ORDER_BOOK, symbol, callback)
        self._send_sub(SUB_ORDER_BOOK, {"symbol": symbol})

    def subscribe_trades(self, symbol: str, callback: Callable):
        self._add_sub(SUB_TRADES, symbol, callback)
        self._send_sub(SUB_TRADES, {"symbol": symbol})

    def subscribe_bars(self, symbol: str, timeframe: str, callback: Callable):
        key = f"{symbol}|{timeframe}"
        self._add_sub(SUB_BARS, key, callback)
        self._send_sub(SUB_BARS, {"symbol": symbol, "timeframe": timeframe})

    def subscribe_orders(self, account_id: str, callback: Callable):
        self._add_sub(SUB_ORDERS, account_id, callback)
        self._send_sub(SUB_ORDERS, {"account_id": account_id})

    def unsubscribe_all(self, callback: Callable):
        with self._lock:
            dead = [k for k, cbs in self._subs.items() if callback in cbs]
            for k in dead:
                self._subs[k].discard(callback)
                if not self._subs[k]:
                    del self._subs[k]

    def _add_sub(self, sub_type: str, key: str, callback: Callable):
        with self._lock:
            self._subs.setdefault((sub_type, key), set()).add(callback)

    def _send_sub(self, sub_type: str, data: dict):
        if not self._ready.wait(timeout=15):
            log.warning("hub not ready in 15s, sub %s dropped (will resend on connect)", sub_type)
            return
        payload = {
            "action": "SUBSCRIBE",
            "type": sub_type,
            "data": data,
            "token": self._ensure_jwt(),
        }
        self._send_raw(payload)

    def _send_raw(self, payload: dict):
        """Потокобезопасная отправка: планируем coroutine в loop hub-потока.
        Если loop ещё не активен — буферизуем; flush при старте recv-цикла."""
        import asyncio
        with self._outbox_lock:
            loop = self._loop
            if loop is None or not loop.is_running() or self._ws is None:
                self._outbox.append(payload)
                if len(self._outbox) > 100:
                    self._outbox.pop(0)
                return
        asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(payload)), loop)

    async def _flush_outbox(self):
        with self._outbox_lock:
            pending, self._outbox = self._outbox, []
        for payload in pending:
            try:
                await self._ws.send(json.dumps(payload))
            except Exception as e:
                log.warning("flush send failed: %s", str(e)[:80])

    def _resubscribe_all(self):
        with self._lock:
            pending = dict(self._subs)
        for (sub_type, key), _cbs in pending.items():
            data = self._key_to_data(sub_type, key)
            if data:
                self._send_raw({
                    "action": "SUBSCRIBE",
                    "type": sub_type,
                    "data": data,
                    "token": self._jwt,
                })

    @staticmethod
    def _key_to_data(sub_type: str, key: str) -> Optional[dict]:
        if sub_type == SUB_QUOTES:
            # ключ QUOTES — мульти-символьная: собираем все символы из подписок? Упрощение: ключ хранит json-список
            try:
                return {"symbols": json.loads(key)}
            except Exception:
                return None
        if sub_type == SUB_BARS:
            sym, tf = key.split("|", 1)
            return {"symbol": sym, "timeframe": tf}
        if sub_type == SUB_ORDERS:
            return {"account_id": key}
        return {"symbol": key}

    # === Dispatch ===

    def _dispatch(self, raw: str):
        try:
            d = json.loads(raw)
        except Exception:
            return
        t = d.get("type")
        self.stats["messages"] += 1
        self.stats["last_msg_ts"] = time.time()

        if t == "DATA":
            st = d.get("subscription_type", "")
            payload_raw = d.get("payload")
            try:
                payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
            except Exception:
                payload = payload_raw
            self.stats["by_type"][st] = self.stats["by_type"].get(st, 0) + 1
            self._fan_out(st, d.get("subscription_key", ""), payload)
        elif t == "ERROR":
            self.stats["errors"] += 1
            log.warning("WS ERROR: %s", str(d)[:200])
        elif t == "EVENT":
            ev = d.get("event_info", {}).get("event", "")
            log.info("WS EVENT: %s", ev)

    def _fan_out(self, sub_type: str, key: str, payload):
        with self._lock:
            # точные подписчики по ключу
            cbs = set()
            if sub_type == SUB_QUOTES:
                # все QUOTES-подписчики (мульти-символьные ключи)
                for (st, k), callbacks in self._subs.items():
                    if st == SUB_QUOTES:
                        cbs |= callbacks
            else:
                cbs = self._subs.get((sub_type, key), set())
        for cb in cbs:
            try:
                cb(payload)
            except Exception as e:
                log.error("callback error (%s): %s", sub_type, str(e)[:100])


_hub: Optional[FinamHub] = None
_hub_lock = threading.Lock()


def get_hub() -> FinamHub:
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = FinamHub()
        return _hub
