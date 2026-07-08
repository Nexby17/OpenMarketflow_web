"""Arbitrage Order Manager — passive-aggressive execution.

Leg A: LIMIT IOC (passive) — best bid/ask
Leg B: MARKET (aggressive) — immediate fill

Flow:
1. Signal → place LIMIT on leg A
2. Wait for fill (poll up to timeout)
3. On fill → place MARKET on leg B with matched volume
4. On timeout/partial < min_fill_ratio → cancel, abort
5. On market failure → emergency close leg A
"""
import logging
import time
import threading
import requests
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("orders_arb")

# FinamPy gRPC order support (optional — used when DP server is down)
try:
    from FinamPy.grpc.orders_service_pb2 import Order as GrpcOrder, OrdersRequest
    from FinamPy.grpc.orders_service_pb2 import CancelOrderRequest as GrpcCancel
    from google.type.decimal_pb2 import Decimal as GrpcDecimal
    _HAS_GRPC = True
except ImportError:
    _HAS_GRPC = False

BUY = "buy"
SELL = "sell"


@dataclass
class LegResult:
    """Result of a single leg execution."""
    filled: bool
    price: float = 0.0
    quantity: int = 0
    order_id: str = ""
    error: str = ""


class ArbOrderManager:
    """Manages orders for both legs via DataProvider REST."""

    def __init__(self, dp_url: str = "http://localhost:5060", account: str = ""):
        self._dp_url = dp_url.rstrip("/")
        self._account = account  # FORTS account for futures
        self._stock_account = "1225950"  # MICEX account for stocks
        self._timeout = 5.0
        self._fp = None  # FinamPy instance (set via set_finam_py)
        self._stock_mic = "MISX"  # MIC for stock orders
        self._fut_mic = "RTSX"   # MIC for futures orders

    def set_finam_py(self, fp):
        """Attach FinamPy instance for direct gRPC order placement."""
        self._fp = fp

    def _grpc_place_order(self, symbol: str, side: str, quantity: int,
                          price: float = None, order_type: str = "market") -> Optional[dict]:
        """Place order via FinamPy gRPC. Returns dict with order_id or None."""
        if not self._fp:
            log.warning("gRPC: no FinamPy instance")
            return None
        if not _HAS_GRPC:
            log.warning("gRPC: _HAS_GRPC=False")
            return None
        try:
            from FinamPy.grpc.orders_service_pb2 import Order as GrpcOrder
            # Determine account and MIC based on instrument type
            # Futures (GZ*, SR*, Si*, etc) → FORTS account + RTSX mic
            # Stocks (GAZP, SBER, etc) → MICEX account + MISX mic
            is_stock = not any(symbol.startswith(p) for p in ['GZ', 'SR', 'Si', 'RI', 'MX', 'GD', 'BR', 'LK', 'TT', 'RN', 'Eu', 'ED'])
            if is_stock:
                acc = self._stock_account
                # Convert SYMBOL@RTSX to SYMBOL@MISX for orders
                base = symbol.split('@')[0]
                order_symbol = f"{base}@{self._stock_mic}"
            else:
                acc = self._account
                order_symbol = symbol  # Already SYMBOL@RTSX

            o = GrpcOrder(
                account_id=acc,
                symbol=order_symbol,
                quantity=GrpcDecimal(value=str(quantity)),
                side=1 if side.lower() == "buy" else 2,
            )
            if order_type == "limit":
                o.type = 2  # ORDER_TYPE_LIMIT
                o.limit_price.CopyFrom(GrpcDecimal(value=f"{price:.2f}"))
                o.time_in_force = 1  # DAY (MICEX stocks don't support GTC)
            else:
                o.type = 1  # ORDER_TYPE_MARKET
                o.time_in_force = 1  # DAY

            log.info(f"gRPC sending: {order_type} {side} {quantity} {order_symbol} acc={acc} @ {price or 'MKT'}")
            resp, _ = self._fp.orders_stub.PlaceOrder.with_call(
                request=o, timeout=10, metadata=(self._fp.metadata,))
            oid = resp.order_id if hasattr(resp, 'order_id') else str(resp)
            log.info(f"gRPC {order_type} placed: {side} {quantity} {order_symbol} acc={acc} @ {price or 'MKT'} id={oid}")
            return {"order_id": oid, "raw": resp}
        except Exception as e:
            err_details = e.details() if hasattr(e, 'details') else str(e)
            log.error(f"gRPC order error ({symbol} {side} {quantity}@{price}): code={e.code() if hasattr(e,'code') else '?'} details={err_details[:120]}")
            return None

    def _grpc_cancel(self, order_id: str) -> tuple[bool, bool]:
        """Cancel via gRPC. Returns (sent_cancel, order_not_found).
        sent_cancel=True means cancel was acknowledged.
        order_not_found=True means order doesn't exist (already filled/cancelled)."""
        if not self._fp or not _HAS_GRPC or not order_id:
            return (False, False)
        # Try both accounts (stock orders on 1225950, futures on _account)
        not_found_count = 0
        for acc in [self._stock_account, self._account]:
            try:
                req = GrpcCancel(account_id=acc, order_id=order_id)
                self._fp.orders_stub.CancelOrder(request=req, timeout=5, metadata=(self._fp.metadata,))
                return (True, False)  # cancel sent
            except Exception as e:
                err_str = str(e)
                if 'NOT_FOUND' in err_str or 'not found' in err_str.lower():
                    log.debug(f"gRPC cancel NOT_FOUND {order_id} acc={acc} — order already done")
                    not_found_count += 1
                    continue
                log.warning(f"gRPC cancel error {order_id} acc={acc}: {err_str[:80]}")
        if not_found_count > 0:
            return (False, True)  # order not found = already done
        return (False, False)  # real error

    def _is_stock(self, symbol: str) -> bool:
        """Check if symbol is a stock (not a future)."""
        return not any(symbol.startswith(p) for p in ['GZ', 'SR', 'Si', 'RI', 'MX', 'GD', 'BR', 'LK', 'TT', 'RN', 'Eu', 'ED'])

    def _order_account(self, symbol: str) -> str:
        """Return correct account for the symbol."""
        return self._stock_account if self._is_stock(symbol) else self._account

    def _place_limit(self, symbol: str, side: str, quantity: int,
                     price: float, tag: str = "") -> Optional[str]:
        """Place limit order. Returns order_id or None."""
        # Try gRPC first if FinamPy is connected
        if self._fp and _HAS_GRPC:
            result = self._grpc_place_order(symbol, side, quantity, price, "limit")
            if result:
                return result.get("order_id")
            log.warning(f"gRPC limit failed, falling back to DP server")
        # DP server fallback
        try:
            # Stock prices in kopecks (×100), futures in points
            price_int = int(round(price * 100)) if self._is_stock(symbol) else int(round(price))
            r = requests.post(
                f"{self._dp_url}/order/place",
                params={
                    "account": self._order_account(symbol), "symbol": symbol,
                    "side": side, "quantity": quantity,
                    "price": price_int,
                    "order_type": "limit", "tag": tag,
                },
                timeout=self._timeout,
            )
            data = r.json()
            if "error" in data:
                log.error(f"Limit order error ({symbol} {side} {quantity}@{price}): {data['error']}")
                return None
            oid = data.get("order_id", "")
            log.info(f"Limit placed (DP): {tag} {side} {quantity} {symbol} @ {price} id={oid}")
            return oid
        except Exception as e:
            log.error(f"Limit order exception: {e}")
            return None

    def _place_market(self, symbol: str, side: str, quantity: int,
                      tag: str = "") -> Optional[dict]:
        """Place market order. Returns {order_id} — caller knows estimated price."""
        # Try gRPC first if FinamPy is connected
        if self._fp and _HAS_GRPC:
            result = self._grpc_place_order(symbol, side, quantity, None, "market")
            if result:
                return {"order_id": result.get("order_id", "")}
            log.warning(f"gRPC market failed, falling back to DP server")
        # DP server fallback
        try:
            r = requests.post(
                f"{self._dp_url}/order/place",
                params={
                    "account": self._order_account(symbol), "symbol": symbol,
                    "side": side, "quantity": quantity,
                    "order_type": "market", "tag": tag,
                },
                timeout=self._timeout,
            )
            data = r.json()
            if "error" in data:
                log.error(f"Market order error ({symbol} {side} {quantity}): {data['error']}")
                return None
            oid = data.get("order_id", "")
            log.info(f"Market placed: {tag} {side} {quantity} {symbol} id={oid}")
            return data
        except Exception as e:
            log.error(f"Market order exception: {e}")
            return None

    def _cancel(self, order_id: str) -> str:
        """Cancel order. Returns 'cancelled', 'not_found' (already done), or 'error'."""
        if not order_id:
            return 'error'
        # Try gRPC first (tries both accounts internally)
        if self._fp and _HAS_GRPC:
            sent, not_found = self._grpc_cancel(order_id)
            if sent:
                return 'cancelled'
            if not_found:
                return 'not_found'
        # DP server fallback — cancel via gRPC connector
        try:
            r = requests.post(
                f"{self._dp_url}/api/orders/cancel-all",
                timeout=self._timeout,
            )
            data = r.json()
            if data.get("status") == "ok":
                return 'cancelled'
        except Exception as e:
            log.warning(f"DP cancel-all fallback failed: {e}")
        return 'error'
        # Both accounts returned NOT_FOUND — order is done
        return 'not_found'

    def _grpc_get_order_status(self, order_id: str, account: str = None) -> Optional[dict]:
        """Get order status via gRPC. Returns state + filled_qty (no avg_price — caller knows price)."""
        if not self._fp or not _HAS_GRPC or not order_id:
            return None
        acc = account or self._account
        try:
            from FinamPy.grpc.orders_service_pb2 import GetOrderRequest
            resp, _ = self._fp.orders_stub.GetOrder.with_call(
                request=GetOrderRequest(account_id=acc, order_id=order_id),
                timeout=5, metadata=(self._fp.metadata,))
            state = resp.status if hasattr(resp, 'status') else 0
            executed = 0
            if hasattr(resp, 'executed_quantity') and resp.executed_quantity.value:
                executed = int(float(resp.executed_quantity.value))
            state_str = {1: 'active', 2: 'partial', 3: 'filled', 4: 'rejected', 5: 'cancelled'}.get(state, 'unknown')
            return {"state": state_str, "filled_quantity": executed, "avg_price": 0}
        except Exception as e:
            log.debug(f"gRPC GetOrder {order_id}: {str(e)[:60]}")
            return None

    def _get_order_status(self, order_id: str) -> Optional[dict]:
        """Get order fill status. Try gRPC first, then DP server."""
        # Try gRPC for stock account first (GAZP orders are on 1225950)
        if self._fp and _HAS_GRPC:
            for acc in [self._stock_account, self._account]:
                status = self._grpc_get_order_status(order_id, acc)
                if status:
                    return status
        # DP server fallback
        try:
            r = requests.get(
                f"{self._dp_url}/order/status",
                params={"account": self._account, "order_id": order_id},
                timeout=3.0,
            )
            return r.json()
        except Exception as e:
            log.error(f"Order status error: {e}")
            return None

    def _wait_for_fill(self, order_id: str, timeout: int,
                       known_price: float = 0.0, poll_interval: float = 0.2) -> LegResult:
        """Wait for limit order to fill. Returns LegResult with known_price as fill price."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self._get_order_status(order_id)
            if status:
                state = status.get("state", "").lower()
                filled_qty = int(status.get("filled_quantity", 0))
                fill_price = float(status.get("avg_price", 0))
                log.debug(f"Fill poll {order_id}: state={state} qty={filled_qty} price={fill_price}")

                if state in ("filled", "matched"):
                    fill_price = known_price if known_price > 0 else float(status.get("avg_price", 0))
                    return LegResult(filled=True, price=fill_price,
                                     quantity=filled_qty, order_id=order_id)
                if state in ("cancelled", "canceled", "rejected") and filled_qty == 0:
                    return LegResult(filled=False, order_id=order_id,
                                     error=f"order_{state}")
                if state in ("cancelled", "canceled") and filled_qty > 0:
                    # Partial fill then cancelled — use known_price as fallback
                    fill_price = known_price if known_price > 0 else fill_price
                    return LegResult(filled=True, price=fill_price,
                                     quantity=filled_qty, order_id=order_id,
                                     error="partial_fill")

            time.sleep(poll_interval)

        # Timeout — cancel
        cancel_result = self._cancel(order_id)
        # Check final fill
        status = self._get_order_status(order_id)
        if status:
            filled_qty = int(status.get("filled_quantity", 0))
            fill_price = known_price if known_price > 0 else float(status.get("avg_price", 0))
            if filled_qty > 0:
                return LegResult(filled=True, price=fill_price,
                                 quantity=filled_qty, order_id=order_id,
                                 error="timeout_partial")
        # If cancel returned NOT_FOUND, order likely already filled
        if cancel_result == 'not_found':
            log.warning(f"Fill poll timeout but cancel NOT_FOUND — order likely filled at known_price={known_price}")
            return LegResult(filled=True, price=known_price,
                             quantity=0, order_id=order_id,
                             error="likely_filled_no_price")
        return LegResult(filled=False, order_id=order_id, error="timeout")

    # === Public: Execute both legs ===

    def execute_entry(self, symbol_a: str, symbol_b: str,
                      side_a: str, side_b: str,
                      lots_a: int, lots_b: int,
                      limit_price_a: float,
                      timeout: int = 5,
                      min_fill_ratio: float = 0.5,
                      paper: bool = True,
                      market_price_b: float = 0.0) -> dict:
        """
        Execute arbitrage entry: LIMIT on A, MARKET on B.
        Returns dict with results for both legs.
        """
        result = {
            "leg_a": None, "leg_b": None,
            "success": False, "error": "",
        }

        # --- PAPER MODE ---
        if paper:
            price_b = market_price_b if market_price_b > 0 else limit_price_a * 10
            log.info(f"📄 PAPER ENTRY: {side_a} {lots_a} {symbol_a} @ {limit_price_a:.2f} (LIMIT)")
            log.info(f"📄 PAPER ENTRY: {side_b} {lots_b} {symbol_b} @ {price_b:.2f} (MARKET)")
            result["leg_a"] = LegResult(filled=True, price=limit_price_a, quantity=lots_a)
            result["leg_b"] = LegResult(filled=True, price=price_b, quantity=lots_b)
            result["success"] = True
            return result

        # --- REAL MODE ---
        # Leg A: LIMIT
        oid_a = self._place_limit(symbol_a, side_a, lots_a, limit_price_a, tag="arb_entry_a")
        if not oid_a:
            result["error"] = "leg_a_place_failed"
            return result

        leg_a = self._wait_for_fill(oid_a, timeout, known_price=limit_price_a)
        result["leg_a"] = leg_a

        if not leg_a.filled:
            result["error"] = f"leg_a_{leg_a.error}"
            return result

        # Check min fill ratio
        if lots_a > 0 and leg_a.quantity / lots_a < min_fill_ratio:
            log.warning(f"Leg A fill ratio {leg_a.quantity}/{lots_a} < {min_fill_ratio} — emergency closing partial {leg_a.quantity}")
            emergency_side = SELL if side_a == BUY else BUY
            self._place_market(symbol_a, emergency_side, leg_a.quantity, tag="arb_partial_fill_close")
            result["error"] = "leg_a_insufficient_fill"
            return result

        # Adjust leg B volume to match actual fill
        actual_lots_b = lots_b  # could scale: int(leg_a.quantity * ratio)
        log.info(f"Leg A filled {leg_a.quantity}@{leg_a.price:.2f} → executing leg B market {actual_lots_b}")

        # Leg B: MARKET
        market_b = self._place_market(symbol_b, side_b, actual_lots_b, tag="arb_entry_b")
        if not market_b:
            # EMERGENCY: close leg A (try up to 3 times)
            log.error("Leg B market failed — emergency closing leg A")
            emergency_side = SELL if side_a == BUY else BUY
            closed = False
            for _attempt in range(3):
                res = self._place_market(symbol_a, emergency_side, leg_a.quantity, tag=f"arb_emergency_close_a_v{_attempt}")
                if res and res.get("order_id"):
                    closed = True
                    log.info(f"Emergency close sent: order_id={res['order_id']}")
                    break
                log.warning(f"Emergency close attempt {_attempt} failed — retrying in 1s")
                time.sleep(1)
            if not closed:
                log.error(f"ALL 3 emergency close attempts FAILED — {side_a} {leg_a.quantity} {symbol_a} may be OPEN")
            result["error"] = "leg_b_failed_emergency_closed_a"
            return result

        # Leg B fill price = market_price_b (estimated from OB before order)
        leg_b_fill_price = market_price_b if market_price_b > 0 else 0.0
        log.info(f"Leg B fill price: {leg_b_fill_price:.2f}")
        result["leg_b"] = LegResult(filled=True, price=leg_b_fill_price,
                                    quantity=actual_lots_b)
        result["success"] = True
        return result

    def execute_exit(self, symbol_a: str, symbol_b: str,
                     side_a: str, side_b: str,
                     lots_a: int, lots_b: int,
                     limit_price_a: float,
                     timeout: int = 5,
                     min_fill_ratio: float = 0.5,
                     paper: bool = True,
                     market_price_b: float = 0.0) -> dict:
        """Execute arbitrage exit — same mechanics as entry but opposite directions."""
        return self.execute_entry(
            symbol_a, symbol_b, side_a, side_b,
            lots_a, lots_b, limit_price_a,
            timeout, min_fill_ratio, paper, market_price_b,
        )

    def execute_both_market(self, symbol_a: str, symbol_b: str,
                            side_a: str, side_b: str,
                            lots_a: int, lots_b: int,
                            paper: bool = True,
                            est_price_a: float = 0.0,
                            est_price_b: float = 0.0) -> dict:
        """Execute both legs as MARKET simultaneously — no waiting, no polling.
        Returns dict with estimated fill prices from orderbook snapshot."""
        result = {"leg_a": None, "leg_b": None, "success": False, "error": ""}

        if paper:
            log.info(f"PAPER BOTH MKT: A={side_a} {lots_a} {symbol_a} @ {est_price_a:.2f} | B={side_b} {lots_b} {symbol_b} @ {est_price_b:.2f}")
            result["leg_a"] = LegResult(filled=True, price=est_price_a, quantity=lots_a)
            result["leg_b"] = LegResult(filled=True, price=est_price_b, quantity=lots_b)
            result["success"] = True
            return result

        # Place both market orders simultaneously
        log.info(f"MARKET ENTRY: A={side_a} {lots_a} {symbol_a} | B={side_b} {lots_b} {symbol_b}")
        res_a = self._place_market(symbol_a, side_a, lots_a, tag="arb_mkt_a")
        res_b = self._place_market(symbol_b, side_b, lots_b, tag="arb_mkt_b")

        ok_a = res_a is not None
        ok_b = res_b is not None

        if ok_a and ok_b:
            log.info(f"Both legs placed: A id={res_a.get('order_id','?')} B id={res_b.get('order_id','?')}")
            result["leg_a"] = LegResult(filled=True, price=est_price_a, quantity=lots_a)
            result["leg_b"] = LegResult(filled=True, price=est_price_b, quantity=lots_b)
            result["success"] = True
            return result

        # Emergency: close the leg that succeeded
        if ok_a and not ok_b:
            log.error("Leg B market failed — emergency closing leg A")
            emergency = SELL if side_a == BUY else BUY
            for attempt in range(3):
                r = self._place_market(symbol_a, emergency, lots_a, tag=f"arb_emergency_a_{attempt}")
                if r:
                    log.info(f"Emergency close A ok: {r.get('order_id','?')}")
                    break
                time.sleep(0.5)
            result["error"] = "leg_b_failed"
        elif ok_b and not ok_a:
            log.error("Leg A market failed — emergency closing leg B")
            emergency = SELL if side_b == BUY else BUY
            for attempt in range(3):
                r = self._place_market(symbol_b, emergency, lots_b, tag=f"arb_emergency_b_{attempt}")
                if r:
                    log.info(f"Emergency close B ok: {r.get('order_id','?')}")
                    break
                time.sleep(0.5)
            result["error"] = "leg_a_failed"
        else:
            result["error"] = "both_legs_failed"

        return result
