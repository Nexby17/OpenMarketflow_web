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
            msg = str(e)[:160]
            if "cannot be canceled" in msg.lower():
                # ордер уже в терминальном статусе (исполнен/отменён) — не ошибка
                log.info("cancel: order %s already terminal (filled/canceled)", order_id)
                return True
            log.error("cancel REST failed: %s", msg)
            return False

    def cancel_all(self, orders: list):
        for o in (orders or []):
            oid = o.get("order_id") if isinstance(o, dict) else getattr(o, "order_id", "")
            if oid:
                self.cancel(oid)

    _ACTIVE_STATUSES = ("NEW", "PENDING_NEW", "FORWARDING", "WAIT", "LINK_WAIT",
                        "WATCHING", "SUSPENDED", "PARTIALLY")

    def get_active_orders(self, symbol: str = None) -> list:
        """F-019: только АКТИВНЫЕ ордера НАШЕГО символа (было: все ордера счёта —
        cancel-цепочка уносила чужие/ручные лимитки и жгла rate-limit)."""
        if self._paper:
            return []  # paper: лимиты исполняются мгновенно — активных нет
        try:
            # SDK get_orders требует OrdersRequest; возвращаем сырой список
            r4 = self._rest4._get_module()
            from finam_trade_api.order.model import OrdersRequest
            req = OrdersRequest(account_id=self._account)
            resp = r4.call(r4.client.orders.get_orders(req))
            d = resp.model_dump(mode="json") if hasattr(resp, "model_dump") else dict(resp)
            raw = d.get("orders", []) or []
            out = []
            for o in raw:
                if not isinstance(o, dict):
                    continue
                st = str(o.get("status", "")).upper()
                if not any(a in st for a in self._ACTIVE_STATUSES):
                    continue
                if symbol:
                    osym = str(o.get("symbol", ""))
                    if osym and osym != symbol:
                        continue
                out.append(o)
            return out
        except Exception as e:
            log.error("get_active_orders failed: %s", str(e)[:120])
            return []

    def cancel_many(self, order_ids: list, max_per_min: int = 180) -> tuple:
        """F-019: пакетная отмена БЕЗ sleep(0.5) после каждой. Соблюдает rate-limit
        (Finam 200 rpm): держит не более max_per_min запросов в минуту.
        Возвращает (ok_count, failed_ids)."""
        ok = 0
        failed = []
        window_start = time.time()
        sent_in_window = 0
        for oid in order_ids:
            if sent_in_window >= max_per_min:
                elapsed = time.time() - window_start
                if elapsed < 60.0:
                    time.sleep(60.0 - elapsed)
                window_start = time.time()
                sent_in_window = 0
            try:
                res = self.cancel(oid)
                if res:
                    ok += 1
                else:
                    failed.append(oid)
            except Exception as e:
                log.warning("cancel_many %s: %s", oid, str(e)[:80])
                failed.append(oid)
            sent_in_window += 1
        return ok, failed

    def get_recent_fills(self, since_sec: float = 30) -> list:
        cutoff = time.time() - since_sec
        with self._fills_lock:
            return [f for f in self._recent_fills if f.timestamp >= cutoff]

    def record_fill(self, fill):
        with self._fills_lock:
            self._recent_fills.append(fill)

    def wait_fill(self, order_id: str, timeout: float = 3.0, poll: float = 0.06) -> Optional[float]:
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

_POSITION_AVG_KEYS = ("average_price", "avg_price", "weighted_average_price")


def _parse_money(v) -> float:
    """MoneyAmount {"value": ...} | число | None -> float."""
    if isinstance(v, dict):
        try:
            return float(v.get("value", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def get_broker_position(rest4, account_id: str, symbol: str):
    """Позиция брокера -> {"lots": |lots|, "dir": +1/-1/0, "avg_price": float} | None.

    Finam отдаёт quantity СО ЗНАКОМ (шорт = -1). Раньше знак попадал в lots:
    DESYNC-монитор видел "robot=1 broker=-1" на исполненном шорте (ложная
    раскорреляция 14.09), а close_all на шорте всегда выглядел исполненным
    (-1 < 1). Теперь lots — модуль, dir — знак.
    Transient-ошибки API (429 code=8 / токен code=16) ретраятся до 3 раз;
    при неуспехе -> None: вызывающий обязан считать позицию НЕИЗВЕСТНОЙ,
    а не нулевой.
    """
    last_err = ""
    for attempt in range(3):
        try:
            acc = rest4.get_account_info(account_id)
            sym = symbol.split("@")[0]
            for p in acc.get("positions", []):
                psym = str(p.get("symbol", "")).split("@")[0]
                if psym != sym:
                    continue
                signed = int(_parse_money(p.get("quantity", {})))
                avg = 0.0
                for key in _POSITION_AVG_KEYS:
                    if p.get(key):
                        avg = _parse_money(p.get(key))
                        if avg > 0:
                            break
                if signed != 0 and avg == 0.0:
                    log.debug("get_broker_position: open pos without avg_price, raw=%s",
                              json.dumps(p, default=str)[:300])
                return {"lots": abs(signed),
                        "dir": 1 if signed > 0 else (-1 if signed < 0 else 0),
                        "avg_price": avg}
            return {"lots": 0, "dir": 0, "avg_price": 0.0}
        except Exception as e:
            last_err = str(e)[:160]
            transient = ("Too Many Requests" in last_err or "code=8" in last_err
                         or "token could not be verified" in last_err or "code=16" in last_err)
            if transient and attempt < 2:
                time.sleep(1.0 + attempt * 1.2)
                continue
            break
    log.error("get_broker_position failed: %s", last_err)
    return None


def positions_in_sync(robot_lots: int, robot_dir: int, broker_data):
    """Честное сравнение позиций робота и брокера.

    robot_lots — модуль лотов (>=0), robot_dir из {+1, -1, 0};
    broker_data — dict из get_broker_position или None (брокер нечитабелен —
    это НЕ desync, ложных тревог быть не должно).
    Возвращает (synced, msg), msg = "robot=<signed> broker=<signed>".
    """
    if broker_data is None:
        return True, ""
    bl = int(broker_data.get("lots", 0) or 0)
    bdir = int(broker_data.get("dir", 0) or 0)
    rdir = 0 if robot_lots <= 0 else (1 if robot_dir > 0 else -1)
    synced = (robot_lots == bl and rdir == bdir)
    return synced, f"robot={rdir * robot_lots} broker={bdir * bl}"
