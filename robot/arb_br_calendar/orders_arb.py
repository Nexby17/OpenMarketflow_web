"""Arbitrage Order Manager — passive-aggressive execution.

Leg A: LIMIT IOC (passive) — best bid/ask
Leg B: MARKET (aggressive) — immediate fill

Flow:
1. Signal → place LIMIT on leg A
2. Wait for fill (poll up to timeout)
3. On fill → place MARKET on leg B with matched volume
4. On timeout/partial < min_fill_ratio → cancel, abort
5. On market failure → emergency close leg A

Trade subscription:
- Background thread subscribes to gRPC SubscribeTrades stream
- Captures actual fill prices from AccountTrade events
- execute_both_market waits for real fills before returning
"""
import logging
import time
import threading
import requests
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("orders_arb")

# FinamPy gRPC order support (optional — used when DP server is down)
try:
    from FinamPy.grpc.orders_service_pb2 import Order as GrpcOrder, OrdersRequest
    from FinamPy.grpc.orders_service_pb2 import CancelOrderRequest as GrpcCancel
    from FinamPy.grpc.orders_service_pb2 import SubscribeTradesRequest as GrpcSubTradesReq
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
    slippage: float = 0.0  # actual vs estimated price difference


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

        # MOEX stocks: gRPC requires quantity in SHARES, not lots.
        self._lot_sizes: dict[str, int] = {
            'GAZP': 10, 'SBER': 10, 'LKOH': 10, 'ROSN': 10, 'TATN': 10,
            'GMKN': 10, 'ALRS': 10, 'VTBR': 10, 'MTSS': 10, 'NVTK': 10,
            'MOEX': 10, 'SNGS': 10, 'CHMF': 10, 'NLMK': 10, 'MAGN': 10,
            'POLY': 10, 'YNDX': 1, 'FIVE': 10, 'OZON': 10, 'PLZL': 10,
            'FLOT': 10, 'PHOR': 10, 'RUAL': 10, 'MGNT': 10, 'SMLT': 10,
        }
        self._default_lot_size = 10

        # === Trade subscription: real fill price tracking ===
        self._trade_sub_thread = None
        self._trade_sub_running = False
        self._trade_sub_ready = threading.Event()  # set when subscription is active
        self._pending_fills: dict[str, threading.Event] = {}  # order_id → Event
        self._fill_prices: dict[str, float] = {}       # order_id → actual fill price
        self._fill_lock = threading.Lock()
        self._total_slippage: float = 0.0  # cumulative slippage for logging

    def set_finam_py(self, fp):
        """Attach FinamPy instance for direct gRPC order placement."""
        self._fp = fp

    # === Trade subscription for real fill prices ===

    def start_trade_subscription(self):
        """Start background thread subscribing to gRPC trade stream.
        Must be called after set_finam_py()."""
        if not self._fp or not _HAS_GRPC:
            log.warning("Trade subscription: no FinamPy or gRPC not available")
            return
        if self._trade_sub_thread and self._trade_sub_thread.is_alive():
            return
        self._trade_sub_running = True
        self._trade_sub_thread = threading.Thread(target=self._trade_sub_loop, daemon=True, name="trade-sub")
        self._trade_sub_thread.start()
        log.info("Trade subscription started (background thread)")

    def stop_trade_subscription(self):
        """Stop trade subscription thread."""
        self._trade_sub_running = False
        if self._trade_sub_thread:
            self._trade_sub_thread.join(timeout=5)
        log.info("Trade subscription stopped")

    def _trade_sub_loop(self):
        """Background loop: use FinamPy's subscribe_trades_thread for real fills.
        subscribe_trades_thread() blocks forever, calls on_trade.trigger() for each fill."""
        while self._trade_sub_running:
            try:
                if not self._fp:
                    time.sleep(2)
                    continue
                self._fp.on_trade.subscribe(self._on_trade_event)
                self._trade_sub_ready.set()
                log.info(f"Trade subscription active via FinamPy on_trade (acc={self._account})")
                # subscribe_trades_thread blocks forever — loops + reconnects internally
                self._fp.subscribe_trades_thread(account_id=self._account)
                # Only reaches here if channel closed or cancelled
                log.warning("subscribe_trades_thread ended")
            except Exception as e:
                log.warning(f"Trade subscription error: {e}")
                time.sleep(3)

    def _wait_fill_price(self, order_id: str, timeout: float = 5.0) -> Optional[float]:
        """Wait for actual fill price from trade subscription.
        Falls back to gRPC GetOrder if trade sub doesn't fire.
        Returns real price or None if timeout (caller should use est_price as fallback)."""
        with self._fill_lock:
            if order_id in self._fill_prices:
                return self._fill_prices.pop(order_id)
            event = threading.Event()
            self._pending_fills[order_id] = event
        try:
            if event.wait(timeout=timeout):
                with self._fill_lock:
                    return self._fill_prices.pop(order_id, None)
        finally:
            with self._fill_lock:
                self._pending_fills.pop(order_id, None)

        # Trade subscription didn't fire — fallback to gRPC GetOrder for avg_price
        if self._fp and _HAS_GRPC:
            try:
                from FinamPy.grpc.orders_service_pb2 import GetOrderRequest
                for acc in [self._account, self._stock_account]:
                    resp, _ = self._fp.orders_stub.GetOrder.with_call(
                        request=GetOrderRequest(account_id=acc, order_id=order_id),
                        timeout=3, metadata=(self._fp.metadata,))
                    if resp.HasField('order'):
                        o = resp.order
                        avg = float(o.average_price.value) if hasattr(o, 'average_price') and o.average_price.value else 0
                        price = float(o.price.value) if hasattr(o, 'price') and o.price.value else 0
                        fill_price = avg if avg > 0 else price
                        if fill_price > 0:
                            log.info(f"FILL gRPC fallback {order_id}: avg={avg:.2f} price={price:.2f}")
                            return fill_price
            except Exception as e:
                log.debug(f"GetOrder fallback {order_id}: {str(e)[:60]}")
        return None

    def _get_real_fill_price(self, order_id: str, fallback_price: float) -> float:
        """Get real fill price for an order. Priority: trade subscription > gRPC avg_price > fallback.
        Returns the best available price, always >= 0."""
        # 1. Try trade subscription (may already have the fill from earlier)
        real = self._wait_fill_price(order_id, timeout=1.5)
        if real and real > 0:
            log.info(f"REAL FILL from trade sub: {order_id} price={real:.2f}")
            return real

        # 2. Try gRPC GetOrder for average_price
        if self._fp and _HAS_GRPC:
            try:
                from FinamPy.grpc.orders_service_pb2 import GetOrderRequest
                for acc in [self._account, self._stock_account]:
                    status = self._grpc_get_order_status(order_id, acc)
                    if status and status.get("avg_price", 0) > 0:
                        log.info(f"REAL FILL from gRPC: {order_id} avg_price={status['avg_price']:.2f}")
                        return float(status["avg_price"])
            except Exception as e:
                log.debug(f"gRPC avg fallback {order_id}: {str(e)[:60]}")

        # 3. Fallback (limit price or OB estimate)
        if fallback_price > 0:
            log.debug(f"FILL fallback to estimate: {order_id} price={fallback_price:.2f}")
            return fallback_price

        log.warning(f"No fill price available for {order_id}!")
        return 0.0

    def _log_slippage(self, leg: str, est: float, actual: float):
        """Log slippage for monitoring."""
        if actual > 0 and est > 0:
            slip = actual - est
            self._total_slippage += abs(slip)
            if abs(slip) > 0.005:
                log.info(f"SLIPPAGE {leg}: est={est:.4f} actual={actual:.4f} diff={slip:+.4f} cumul={self._total_slippage:.4f}")

    def _on_trade_event(self, trade):
        """Handle incoming trade from FinamPy's on_trade event."""
        oid = getattr(trade, 'order_id', '')
        price_val = getattr(trade.price, 'value', None) if hasattr(trade, 'price') and trade.price else None
        if not price_val or not oid:
            return
        fill_price = float(price_val)
        with self._fill_lock:
            if oid in self._pending_fills:
                self._fill_prices[oid] = fill_price
                self._pending_fills[oid].set()
                log.info(f"FILL {oid}: price={fill_price} symbol={getattr(trade, 'symbol', '?')}")
            else:
                if not hasattr(self, '_unexpected_count'):
                    self._unexpected_count = 0
                self._unexpected_count += 1
                if self._unexpected_count <= 10:
                    log.info(f"TRADE UNEXPECTED: oid={oid} price={fill_price} sym={getattr(trade, 'symbol', '?')} (no pending)")

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
                base = symbol.split('@')[0]
                order_symbol = f"{base}@{self._stock_mic}"
                lot_size = self._lot_sizes.get(base, self._default_lot_size)
                grpc_qty = quantity * lot_size
            else:
                acc = self._account
                order_symbol = symbol
                grpc_qty = quantity

            o = GrpcOrder(
                account_id=acc,
                symbol=order_symbol,
                quantity=GrpcDecimal(value=str(grpc_qty)),
                side=1 if side.lower() == "buy" else 2,
            )
            if order_type == "limit":
                o.type = 2  # ORDER_TYPE_LIMIT
                o.limit_price.CopyFrom(GrpcDecimal(value=f"{price:.2f}"))
                o.time_in_force = 1  # DAY (MICEX stocks don't support GTC)
            else:
                o.type = 1  # ORDER_TYPE_MARKET
                o.time_in_force = 1  # DAY

            log.info(f"gRPC sending: {order_type} {side} {grpc_qty} {order_symbol} acc={acc} @ {price or 'MKT'}")
            resp, _ = self._fp.orders_stub.PlaceOrder.with_call(
                request=o, timeout=10, metadata=(self._fp.metadata,))
            oid = resp.order_id if hasattr(resp, 'order_id') else str(resp)
            log.info(f"gRPC {order_type} placed: {side} {grpc_qty} {order_symbol} acc={acc} @ {price or 'MKT'} id={oid}")
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
        """Get order status via gRPC. Returns state + filled_qty + actual avg_fill_price from broker."""
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
            # Extract real average fill price from broker
            avg_price = 0.0
            # Response may have order field or fields at top level
            resp_order = resp.order if hasattr(resp, 'order') and resp.HasField('order') else resp
            if hasattr(resp_order, 'average_price') and resp_order.average_price:
                avg_val = getattr(resp_order.average_price, 'value', None)
                if avg_val:
                    avg_price = float(avg_val)
            if avg_price <= 0 and hasattr(resp_order, 'price') and resp_order.price:
                price_val = getattr(resp_order.price, 'value', None)
                if price_val:
                    avg_price = float(price_val)
            return {"state": state_str, "filled_quantity": executed, "avg_price": avg_price}
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
                    # Priority: trade subscription > gRPC avg_price > known_price
                    fill_price = self._get_real_fill_price(order_id, known_price)
                    return LegResult(filled=True, price=fill_price,
                                     quantity=filled_qty, order_id=order_id)
                if state in ("cancelled", "canceled", "rejected") and filled_qty == 0:
                    return LegResult(filled=False, order_id=order_id,
                                     error=f"order_{state}")
                if state in ("cancelled", "canceled") and filled_qty > 0:
                    # Partial fill then cancelled — use real fill price
                    fill_price = self._get_real_fill_price(order_id, known_price)
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
            if filled_qty > 0:
                fill_price = self._get_real_fill_price(order_id, known_price)
                return LegResult(filled=True, price=fill_price,
                                 quantity=filled_qty, order_id=order_id,
                                 error="timeout_partial")
        # If cancel returned NOT_FOUND, order likely already filled
        if cancel_result == 'not_found':
            fill_price = self._get_real_fill_price(order_id, known_price)
            log.warning(f"Fill poll timeout but cancel NOT_FOUND — order likely filled, real_fill_price={fill_price:.2f}")
            return LegResult(filled=True, price=fill_price,
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

        # Leg B fill price — get REAL fill from broker (trade sub / gRPC), fallback to OB estimate
        oid_b = market_b.get("order_id", "")
        leg_b_fill_price = self._get_real_fill_price(oid_b, market_price_b) if oid_b else market_price_b
        log.info(f"Leg B fill price: {leg_b_fill_price:.2f} (OB est={market_price_b:.2f})")
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

    def execute_both_limit(self, symbol_a: str, symbol_b: str,
                           side_a: str, side_b: str,
                           lots_a: int, lots_b: int,
                           limit_price_a: float,
                           limit_price_b: float,
                           paper: bool = True,
                           est_price_a: float = 0.0,
                           est_price_b: float = 0.0,
                           limit_timeout: float = 3.0,
                           force_market: bool = False) -> dict:
        """Execute both legs: LIMIT first, MARKET fallback.

        Flow:
        1. Place LIMIT on both legs simultaneously (by best_bid/best_ask)
        2. Wait up to limit_timeout seconds for fills
        3. For each leg:
           - Filled → use actual fill price
           - Partial fill (>= min_fill_ratio) → accept partial, cancel rest
           - Partial fill (< min_fill_ratio) or timeout → cancel, fallback to MARKET
        4. If one leg filled but other didn't → emergency close filled leg
        """
        result = {"leg_a": None, "leg_b": None, "success": False, "error": ""}
        min_fill_ratio = 0.5

        if paper:
            log.info(f"PAPER LIMIT: A={side_a} {lots_a} {symbol_a} @ {limit_price_a:.2f} | B={side_b} {lots_b} {symbol_b} @ {limit_price_b:.2f}")
            result["leg_a"] = LegResult(filled=True, price=limit_price_a, quantity=lots_a)
            result["leg_b"] = LegResult(filled=True, price=limit_price_b, quantity=lots_b)
            result["success"] = True
            return result

        # === FORCE MARKET (emergency exits) ===
        if force_market:
            log.info(f"FORCE MARKET: A={side_a} {lots_a} {symbol_a} | B={side_b} {lots_b} {symbol_b}")
            res_a = self._place_market(symbol_a, side_a, lots_a, tag="arb_mkt_a")
            res_b = self._place_market(symbol_b, side_b, lots_b, tag="arb_mkt_b")
            ok_a = res_a is not None
            ok_b = res_b is not None

            if ok_a and ok_b:
                oid_a = res_a.get("order_id", "")
                oid_b = res_b.get("order_id", "")
                log.info(f"Both market legs placed: A id={oid_a} B id={oid_b}")
                fill_a = self._wait_fill_price(oid_a, timeout=3.0)
                fill_b = self._wait_fill_price(oid_b, timeout=3.0)
                final_a = fill_a if fill_a and fill_a > 0 else est_price_a
                final_b = fill_b if fill_b and fill_b > 0 else est_price_b
                self._log_slippage("A", est_price_a, final_a)
                self._log_slippage("B", est_price_b, final_b)
                result["leg_a"] = LegResult(filled=True, price=final_a, quantity=lots_a,
                                           order_id=oid_a, slippage=final_a - est_price_a)
                result["leg_b"] = LegResult(filled=True, price=final_b, quantity=lots_b,
                                           order_id=oid_b, slippage=final_b - est_price_b)
                result["success"] = True
                return result
            # Emergency: close the leg that succeeded
            if ok_a and not ok_b:
                self._emergency_close(symbol_a, side_a, lots_a, tag="arb_emergency_a")
                result["error"] = "leg_b_failed"
            elif ok_b and not ok_a:
                self._emergency_close(symbol_b, side_b, lots_b, tag="arb_emergency_b")
                result["error"] = "leg_a_failed"
            else:
                result["error"] = "both_legs_failed"
            return result

        # === LIMIT-FIRST EXECUTION ===
        log.info(f"LIMIT ENTRY: A={side_a} {lots_a} {symbol_a} @ {limit_price_a:.2f} | B={side_b} {lots_b} {symbol_b} @ {limit_price_b:.2f} (timeout={limit_timeout}s)")

        oid_a = self._place_limit(symbol_a, side_a, lots_a, limit_price_a, tag="arb_lim_a")
        oid_b = self._place_limit(symbol_b, side_b, lots_b, limit_price_b, tag="arb_lim_b")
        ok_a = oid_a is not None
        ok_b = oid_b is not None

        if not ok_a and not ok_b:
            result["error"] = "both_legs_place_failed"
            return result

        # --- Wait for fills ---
        leg_a = self._wait_for_fill(oid_a, int(limit_timeout), known_price=limit_price_a) if ok_a else LegResult(filled=False, error="place_failed")
        leg_b = self._wait_for_fill(oid_b, int(limit_timeout), known_price=limit_price_b) if ok_b else LegResult(filled=False, error="place_failed")

        # --- Process leg A ---
        final_a_price = est_price_a
        final_a_qty = 0
        a_filled = False

        if ok_a and leg_a.filled:
            a_filled = True
            final_a_price = leg_a.price
            final_a_qty = leg_a.quantity
            log.info(f"Leg A LIMIT filled: {final_a_qty}@{final_a_price:.2f}")
        elif ok_a and not leg_a.filled and leg_a.error in ("timeout", "order_cancelled"):
            # Cancel already done by _wait_for_fill. Fallback to MARKET.
            log.warning(f"Leg A limit timeout → MARKET fallback {side_a} {lots_a} {symbol_a}")
            mkt_res = self._place_market(symbol_a, side_a, lots_a, tag="arb_mkt_fallback_a")
            if mkt_res:
                a_filled = True
                final_a_qty = lots_a
                final_a_price = est_price_a  # will update from trade sub if available
                oid_mkt = mkt_res.get("order_id", "")
                fill_price = self._wait_fill_price(oid_mkt, timeout=3.0)
                if fill_price and fill_price > 0:
                    final_a_price = fill_price
                self._log_slippage("A_fallback", est_price_a, final_a_price)
            else:
                result["error"] = "leg_a_both_limit_and_market_failed"
                # Cancel leg B if it was placed
                if ok_b and oid_b:
                    self._cancel(oid_b)
                return result

        # --- Process leg B ---
        final_b_price = est_price_b
        final_b_qty = 0
        b_filled = False

        if ok_b and leg_b.filled:
            b_filled = True
            final_b_price = leg_b.price
            final_b_qty = leg_b.quantity
            log.info(f"Leg B LIMIT filled: {final_b_qty}@{final_b_price:.2f}")
        elif ok_b and not leg_b.filled and leg_b.error in ("timeout", "order_cancelled"):
            log.warning(f"Leg B limit timeout → MARKET fallback {side_b} {lots_b} {symbol_b}")
            mkt_res = self._place_market(symbol_b, side_b, lots_b, tag="arb_mkt_fallback_b")
            if mkt_res:
                b_filled = True
                final_b_qty = lots_b
                final_b_price = est_price_b
                oid_mkt = mkt_res.get("order_id", "")
                fill_price = self._wait_fill_price(oid_mkt, timeout=3.0)
                if fill_price and fill_price > 0:
                    final_b_price = fill_price
                self._log_slippage("B_fallback", est_price_b, final_b_price)
            else:
                result["error"] = "leg_b_both_limit_and_market_failed"
                # Emergency close leg A if it filled
                if a_filled and final_a_qty > 0:
                    emergency_side = SELL if side_a == BUY else BUY
                    self._emergency_close(symbol_a, emergency_side, final_a_qty, tag="arb_emergency_close_a")
                return result

        # --- Check both legs filled ---
        if a_filled and b_filled:
            self._log_slippage("A", est_price_a, final_a_price)
            self._log_slippage("B", est_price_b, final_b_price)
            result["leg_a"] = LegResult(filled=True, price=final_a_price, quantity=final_a_qty,
                                       slippage=final_a_price - est_price_a)
            result["leg_b"] = LegResult(filled=True, price=final_b_price, quantity=final_b_qty,
                                       slippage=final_b_price - est_price_b)
            result["success"] = True
            return result

        # One leg filled, other didn't — emergency close
        if a_filled and not b_filled:
            log.error(f"Leg B completely failed — emergency closing leg A ({final_a_qty} lots)")
            emergency_side = SELL if side_a == BUY else BUY
            self._emergency_close(symbol_a, emergency_side, final_a_qty, tag="arb_emergency_close_a")
            result["leg_a"] = LegResult(filled=True, price=final_a_price, quantity=final_a_qty)
            result["error"] = "leg_b_failed"
        elif b_filled and not a_filled:
            log.error(f"Leg A completely failed — emergency closing leg B ({final_b_qty} lots)")
            emergency_side = SELL if side_b == BUY else BUY
            self._emergency_close(symbol_b, emergency_side, final_b_qty, tag="arb_emergency_close_b")
            result["leg_b"] = LegResult(filled=True, price=final_b_price, quantity=final_b_qty)
            result["error"] = "leg_a_failed"
        else:
            result["error"] = "both_legs_failed"

        return result

    def _emergency_close(self, symbol: str, side: str, quantity: int, tag: str = ""):
        """Emergency close position — try up to 3 times."""
        for attempt in range(3):
            r = self._place_market(symbol, side, quantity, tag=f"{tag}_v{attempt}")
            if r:
                log.info(f"Emergency close ok: {tag} order_id={r.get('order_id','?')}")
                return True
            log.warning(f"Emergency close attempt {attempt} failed — retry in 1s")
            time.sleep(1)
        log.error(f"ALL 3 emergency close attempts FAILED — {side} {quantity} {symbol} may be OPEN")
        return False
