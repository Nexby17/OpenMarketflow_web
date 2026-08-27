"""PAPER LAB: orders_rest4 — drop-in замена OrderManager (orders_dp) на finam_rest4 + WS-хаб.

Этап 4 (PC-005): paper-робот полностью уходит от DataProvider (5060) и gRPC.

Совместимость API с orders_dp.OrderManager:
    place_market(side:int, qty:int, tag:str) -> PlacedOrder|None
    place_limit(side:int, qty:int, price:float, tag:str) -> PlacedOrder|None
    cancel(order_id) -> bool
    cancel_all(orders:list)
    get_active_orders(symbol=None) -> list
    get_recent_fills(since_sec=30) -> list
    record_fill(fill)

side: 1=BUY, 2=SELL (как в роботе: BUY/SELL из orders_dp).

PaperMode: PAPER=True → ордера НЕ отправляются брокеру; эмулируется мгновенное
исполнение по текущей цене хаба (realistic slippage = спред). Реальный режим
(prerequisite для PORT боевого робота) — через finam_rest4.place_*.
"""
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("orders_rest4")

BUY, SELL = 1, 2


@dataclass
class PlacedOrder:
    order_id: str
    side: int
    quantity: int
    price: float = 0.0
    tag: str = ""


@dataclass
class FillInfo:
    order_id: str
    symbol: str
    side: int
    quantity: int
    price: float
    timestamp: float = field(default_factory=time.time)


class OrderManagerRest4:
    """Ордера: paper-эмуляция или REST 4.3.3; активные ордера/филлы — REST + WS."""

    def __init__(self, account: str = "", symbol: str = "", paper: bool = True,
                 rest4=None, quote_provider=None):
        self._account = account
        self._symbol = symbol
        self._paper = paper
        self._rest4 = rest4            # модуль finam_rest4 (lazy)
        self._quote = quote_provider   # callable() -> float (текущая цена, из хаба)
        self._fills_lock = threading.Lock()
        self._recent_fills: list[FillInfo] = []
        self._order_seq = int(time.time() * 1000) % 1_000_000_000

    # --- внутреннее ---

    def _next_id(self, tag: str) -> str:
        self._order_seq += 1
        return f"paper-{tag or 'ord'}-{self._order_seq}"

    def _current_price(self) -> float:
        if self._quote:
            try:
                p = self._quote()
                if p and p > 0:
                    return p
            except Exception:
                pass
        return 0.0

    def _record(self, order_id: str, side: int, qty: int, price: float):
        with self._fills_lock:
            self._recent_fills.append(FillInfo(
                order_id=order_id, symbol=self._symbol, side=side,
                quantity=qty, price=price))
            # держим окно 10 минут
            cutoff = time.time() - 600
            self._recent_fills = [f for f in self._recent_fills if f.timestamp >= cutoff]

    # --- API (совместимо с orders_dp) ---

    def place_market(self, side: int, quantity: int, tag: str = "") -> Optional[PlacedOrder]:
        price = self._current_price()
        if self._paper:
            oid = self._next_id(tag or "mkt")
            log.info("PAPER MARKET %s %d %s @ %.0f (%s)",
                     "BUY" if side == BUY else "SELL", quantity, self._symbol, price, tag)
            self._record(oid, side, quantity, price)
            return PlacedOrder(order_id=oid, side=side, quantity=quantity, price=price, tag=tag)
        # real: REST 4.3.3
        try:
            resp = self._rest4.place_market(
                self._account, self._symbol,
                "SIDE_BUY" if side == BUY else "SIDE_SELL", quantity, comment=tag)
            oid = str(resp.get("order_id", resp.get("orderId", "")))
            log.info("REST MARKET %s %d %s -> %s", side, quantity, self._symbol, oid)
            return PlacedOrder(order_id=oid, side=side, quantity=quantity, tag=tag)
        except Exception as e:
            log.error("place_market REST failed: %s", str(e)[:120])
            return None

    def place_limit(self, side: int, quantity: int, price: float, tag: str = "") -> Optional[PlacedOrder]:
        if self._paper:
            oid = self._next_id(tag or "lmt")
            log.info("PAPER LIMIT %s %d %s @ %.0f (%s)",
                     "BUY" if side == BUY else "SELL", quantity, self._symbol, price, tag)
            # лимит считаем исполненным когда цена дошла (упрощение paper) — фиксируем сразу
            self._record(oid, side, quantity, price)
            return PlacedOrder(order_id=oid, side=side, quantity=quantity, price=price, tag=tag)
        try:
            resp = self._rest4.place_limit(
                self._account, self._symbol,
                "SIDE_BUY" if side == BUY else "SIDE_SELL", quantity, price, comment=tag)
            oid = str(resp.get("order_id", resp.get("orderId", "")))
            return PlacedOrder(order_id=oid, side=side, quantity=quantity, price=price, tag=tag)
        except Exception as e:
            log.error("place_limit REST failed: %s", str(e)[:120])
            return None

    def cancel(self, order_id: str) -> bool:
        if self._paper:
            log.info("PAPER CANCEL %s", order_id)
            return True
        try:
            self._rest4.cancel_order(self._account, order_id)
            return True
        except Exception as e:
            log.error("cancel REST failed: %s", str(e)[:120])
            return False

    def cancel_all(self, orders: list):
        for o in (orders or []):
            oid = o.get("order_id") if isinstance(o, dict) else getattr(o, "order_id", "")
            if oid:
                self.cancel(oid)

    def get_active_orders(self, symbol: str = None) -> list:
        if self._paper:
            return []  # paper: лимиты исполняются мгновенно — активных нет
        try:
            # SDK get_orders требует OrdersRequest; возвращаем сырой список
            r4 = self._rest4._get_module()
            from finam_trade_api.order.model import OrdersRequest
            req = OrdersRequest(account_id=self._account)
            resp = r4.call(r4.client.orders.get_orders(req))
            d = resp.model_dump(mode="json") if hasattr(resp, "model_dump") else dict(resp)
            return d.get("orders", [])
        except Exception as e:
            log.error("get_active_orders failed: %s", str(e)[:120])
            return []

    def get_recent_fills(self, since_sec: float = 30) -> list:
        cutoff = time.time() - since_sec
        with self._fills_lock:
            return [f for f in self._recent_fills if f.timestamp >= cutoff]

    def record_fill(self, fill):
        with self._fills_lock:
            self._recent_fills.append(fill)

    def wait_fill(self, order_id: str, timeout: float = 3.0, poll: float = 0.3) -> Optional[float]:
        """PORT-B2: wait for order fill. Returns fill price or None.

        Paper: look up simulated fill in _recent_fills (no REST).
        Real: price source priority (FIX PnL-2026-08-27):
          1) average_price из get_order (если брокер вернул);
          2) фактические сделки брокера (get_today_trades) по order_id —
             средневзвешенная цена исполнения;
          3) None — НЕ используем limit_price IOC-ордера: это цена ЗАЯВКИ,
             а не исполнения (фантомный PnL: 92343/78113 вместо 86731/86746).
        """
        if not order_id:
            return None
        if self._paper:
            with self._fills_lock:
                for f in reversed(self._recent_fills):
                    if f.order_id == order_id:
                        return f.price
            return None
        # real: REST polling
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                o = self._rest4.get_order(self._account, order_id)
                status = str(o.get("status", "")).upper()
                avg = o.get("average_price") or o.get("avg_price") or {}
                price = float(avg.get("value", 0)) if isinstance(avg, dict) else float(avg or 0)
                filled_explicit = ("FILLED" in status or "EXECUTED" in status) and "PARTIALLY" not in status
                filled_heuristic = (price > 0 and status not in (
                    "", "ORDER_STATUS_UNSPECIFIED", "ORDER_STATUS_NEW", "ORDER_STATUS_PENDING_NEW",
                    "ORDER_STATUS_FORWARDING", "ORDER_STATUS_WAIT", "ORDER_STATUS_LINK_WAIT",
                    "ORDER_STATUS_WATCHING", "ORDER_STATUS_SUSPENDED"))
                if filled_explicit or filled_heuristic:
                    if price > 0:
                        return price
                    # фактические сделки по этому ордеру (средневзвешенная цена)
                    try:
                        trades = [t for t in self._rest4.get_today_trades(self._account)
                                  if t.get("order_id") == order_id and t.get("price", 0) > 0]
                        if trades:
                            total_qty = sum(t["size"] for t in trades) or 1.0
                            vwap = sum(t["price"] * t["size"] for t in trades) / total_qty
                            log.info("wait_fill %s: fill из сделок брокера vwap=%.1f (%d сделок)",
                                     order_id, vwap, len(trades))
                            return vwap
                    except Exception as e:
                        log.warning("wait_fill %s: trades lookup failed: %s", order_id, str(e)[:80])
                    # fill подтверждён, но цены нет — честный None (вызывающий код запишет по strategy price с WARNING)
                    log.warning("wait_fill %s: fill %s без цены исполнения — None (limit_price НЕ используем)",
                                order_id, status)
                    return None
            except Exception as e:
                log.debug("wait_fill %s: %s", order_id, str(e)[:80])
            time.sleep(poll)
        return None


# --- позиция для startup-reconciliation (замена DP /position) ---

def get_broker_position(rest4, account_id: str, symbol: str) -> dict:
    """{lots, avg_price} по символу из REST account info (совместимо с DP-ответом)."""
    try:
        acc = rest4.get_account_info(account_id)
        sym = symbol.split("@")[0]
        for p in acc.get("positions", []):
            psym = str(p.get("symbol", "")).split("@")[0]
            if psym == sym:
                q = p.get("quantity", {})
                qv = float(q.get("value", 0)) if isinstance(q, dict) else float(q or 0)
                ap = p.get("average_price") or {}
                apv = float(ap.get("value", 0)) if isinstance(ap, dict) else float(ap or 0)
                return {"lots": int(qv), "avg_price": apv}
        return {"lots": 0, "avg_price": 0.0}
    except Exception as e:
        log.error("get_broker_position failed: %s", str(e)[:120])
        return {"lots": 0, "avg_price": 0.0}
