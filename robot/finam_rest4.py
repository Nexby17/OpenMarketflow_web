"""PAPER LAB: finam_rest4 — тонкий синхронный мост на Finam Trade API SDK 4.x (REST).

Этап 3 миграции (PC-004): аккаунты/ордера/бары/квоты через официальный SDK
finam-trade-api 4.3.3 (TokenManager + автопродление JWT).

Все вызовы синхронны (роботы однопоточно-тредовые): async-методы SDK
выполняются в выделенном event-loop этого модуля.

Зависимости (venv paper_lab): finam-trade-api==4.3.3
"""
import json
import logging
import os
import threading

log = logging.getLogger("finam_rest4")

_REST4 = None  # singleton


def _get_module():
    """Импорт SDK (в venv lab) с гарантией singleton loop."""
    global _REST4
    if _REST4 is not None:
        return _REST4

    from finam_trade_api import Client
    from finam_trade_api.base_client.token_manager import TokenManager

    api_key = os.environ.get("FINAM_API_KEY", "")
    if not api_key:
        # .env поиск: lab dir, robot/, проект/src
        here = os.path.dirname(os.path.abspath(__file__))
        for base in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here)),
                     os.path.join(os.path.dirname(os.path.dirname(here)), "src")):
            p = os.path.join(base, ".env")
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        for line in f:
                            if line.startswith("FINAM_API_KEY="):
                                api_key = line.split("=", 1)[1].strip()
                                break
                except OSError:
                    pass
            if api_key:
                break
    if not api_key:
        raise RuntimeError("FINAM_API_KEY not set")

    import asyncio

    class _Rest4:
        def __init__(self):
            self.tm = TokenManager(api_key)
            self.client = Client(self.tm, auto_refresh_tokens=True)
            self.loop = asyncio.new_event_loop()
            self._lock = threading.Lock()
            threading.Thread(target=self._loop_thread, name="finam-rest4", daemon=True).start()

        def _loop_thread(self):
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()

        def call(self, coro, timeout=15):
            import asyncio
            with self._lock:
                fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
                return fut.result(timeout=timeout)

    _REST4 = _Rest4()
    return _REST4


# ================= Public API =================

def get_accounts() -> list:
    """Список счетов (IDs) из деталей токена: access_tokens/…? SDK: quotas/…?
    В SDK 4.3.3 прямого списка счетов нет — используем get_account_info для проверки.
    Возвращаем сохранённый из env FINAM_ACCOUNT_ID если задан."""
    acc = os.environ.get("FINAM_ACCOUNT_ID", "")
    return [acc] if acc else []


def get_account_info(account_id: str) -> dict:
    """Позиции/маржа по счёту → dict (упрощённо, для старта робота)."""
    r4 = _get_module()
    resp = r4.call(r4.client.account.get_account_info(account_id))
    return resp.model_dump(mode="json") if hasattr(resp, "model_dump") else dict(resp)


def get_last_quote(symbol: str) -> dict:
    r4 = _get_module()
    resp = r4.call(r4.client.instruments.get_last_quote(symbol))
    return resp.model_dump(mode="json") if hasattr(resp, "model_dump") else dict(resp)


def get_bars(symbol: str, timeframe: str, start: str, end: str) -> dict:
    """Бары истории. timeframe: TIME_FRAME_M1/M5/M15/H1/D; start/end: ISO8601."""
    r4 = _get_module()
    from finam_trade_api.instruments.model import BarsRequest
    req = BarsRequest(symbol=symbol, timeframe=timeframe, start_time=start, end_time=end)
    resp = r4.call(r4.client.instruments.get_bars(req))
    return resp.model_dump(mode="json") if hasattr(resp, "model_dump") else dict(resp)


def get_quotas() -> dict:
    """Лимиты API (200 rpm на метод) — мониторинг расхода."""
    r4 = _get_module()
    resp = r4.call(r4.client.quotas.get_quotas())
    return resp.model_dump(mode="json") if hasattr(resp, "model_dump") else dict(resp)


def place_market(account_id: str, symbol: str, side: str, quantity: int, comment: str = "") -> dict:
    """MARKET-ордер. side: 'buy'/'sell'. Возвращает OrderState dict."""
    r4 = _get_module()
    from finam_trade_api.order.model import Order, OrderType
    from finam_trade_api.base_client.models import Side, FinamDecimal
    o = Order(
        account_id=account_id,
        symbol=symbol,
        quantity=FinamDecimal(value=str(quantity)),
        side=Side(side),
        type=OrderType("ORDER_TYPE_MARKET"),
    )
    if comment:
        o.comment = comment
    resp = r4.call(r4.client.orders.place_order(o), timeout=10)
    return resp.model_dump(mode="json") if hasattr(resp, "model_dump") else dict(resp)


def place_limit(account_id: str, symbol: str, side: str, quantity: int, price: float, comment: str = "") -> dict:
    r4 = _get_module()
    from finam_trade_api.order.model import Order, OrderType
    from finam_trade_api.base_client.models import Side, FinamDecimal
    o = Order(
        account_id=account_id,
        symbol=symbol,
        quantity=FinamDecimal(value=str(quantity)),
        side=Side(side),
        type=OrderType("ORDER_TYPE_LIMIT"),
        limit_price=FinamDecimal(value=f"{price:.2f}"),
    )
    if comment:
        o.comment = comment
    resp = r4.call(r4.client.orders.place_order(o), timeout=10)
    return resp.model_dump(mode="json") if hasattr(resp, "model_dump") else dict(resp)


def cancel_order(account_id: str, order_id: str) -> dict:
    r4 = _get_module()
    from finam_trade_api.order.model import CancelOrderRequest
    req = CancelOrderRequest(account_id=account_id, order_id=order_id)
    resp = r4.call(r4.client.orders.cancel_order(req))
    return resp.model_dump(mode="json") if hasattr(resp, "model_dump") else dict(resp)


def get_order(account_id: str, order_id: str) -> dict:
    r4 = _get_module()
    from finam_trade_api.order.model import GetOrderRequest
    req = GetOrderRequest(account_id=account_id, order_id=order_id)
    resp = r4.call(r4.client.orders.get_order(req))
    return resp.model_dump(mode="json") if hasattr(resp, "model_dump") else dict(resp)


def get_today_trades(account_id: str, limit: int = 200) -> list:
    """Исполненные сделки счёта за сегодня (UTC-день). [{order_id, price, size, side, timestamp}]"""
    r4 = _get_module()
    from finam_trade_api.account.model import GetTradesRequest
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    req = GetTradesRequest(
        account_id=account_id,
        start_time=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_time=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        limit=limit)
    resp = r4.call(r4.client.account.get_trades(req))
    d = resp.model_dump(mode="json") if hasattr(resp, "model_dump") else dict(resp)
    out = []
    for t in d.get("trades", []):
        price = t.get("price") or {}
        size = t.get("size") or {}
        out.append({
            "order_id": str(t.get("order_id", "")),
            "price": float(price.get("value", 0)) if isinstance(price, dict) else float(price or 0),
            "size": float(size.get("value", 0)) if isinstance(size, dict) else float(size or 0),
            "side": t.get("side", ""),
            "timestamp": t.get("timestamp", ""),
        })
    return out
