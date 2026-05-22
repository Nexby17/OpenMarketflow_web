"""Order management — place, cancel, track orders via Finam gRPC."""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable

from FinamPy import FinamPy
from FinamPy.grpc.orders_service_pb2 import (
    Order, CancelOrderRequest, OrdersRequest,
    ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT,
)
from FinamPy.grpc.side_pb2 import SIDE_BUY, SIDE_SELL
from google.type.decimal_pb2 import Decimal

import config

log = logging.getLogger("orders")

MSK = timezone(timedelta(hours=3))

# Side constants
BUY = 1
SELL = 2


@dataclass
class FillInfo:
    order_id: str
    client_order_id: str
    side: int        # 1=BUY, 2=SELL
    price: float
    quantity: float
    trade_id: str = ""
    timestamp: datetime = None


@dataclass
class PlacedOrder:
    order_id: str
    client_order_id: str
    side: int
    order_type: str     # MARKET, LIMIT
    price: float
    quantity: int
    status: str = "ACTIVE"  # ACTIVE, FILLED, CANCELLED, REJECTED


class OrderManager:
    """Places and tracks orders via gRPC. Receives fills via push."""

    def __init__(self, fp: FinamPy):
        self._fp = fp
        self._account_id = config.FINAM_ACCOUNT_ID
        self._symbol = config.SYMBOL

        # Callbacks
        self.on_fill: Callable[[FillInfo], None] = lambda f: None
        self.on_order_update: Callable[[PlacedOrder], None] = lambda o: None

        # Track known orders
        self._fills: list[FillInfo] = []
        self._lock_fill = False

    def _next_client_id(self) -> str:
        """Generate unique client_order_id (max 20 chars for Finam)."""
        return str(int(time.time() * 1000))[-13:]

    # === PLACE ORDERS ===

    def place_market(self, side: int, quantity: int, tag: str = "") -> Optional[PlacedOrder]:
        """Place market order. Returns PlacedOrder or None on error."""
        grpc_side = SIDE_BUY if side == BUY else SIDE_SELL
        client_id = self._next_client_id()

        try:
            result = self._fp.call_function(
                self._fp.orders_stub.PlaceOrder,
                Order(
                    account_id=self._account_id,
                    symbol=self._symbol,
                    quantity=Decimal(value=str(quantity)),
                    side=grpc_side,
                    type=ORDER_TYPE_MARKET,
                    client_order_id=client_id,
                ),
            )
            if result is None:
                log.error(f"Market order failed: {tag} side={side} qty={quantity}")
                return None

            order_id = result.order_id
            log.info(f"Market order placed: {tag} side={'BUY' if side==BUY else 'SELL'} "
                     f"qty={quantity} id={order_id}")

            po = PlacedOrder(
                order_id=order_id,
                client_order_id=client_id,
                side=side,
                order_type="MARKET",
                price=0,
                quantity=quantity,
                status="ACTIVE",
            )
            return po

        except Exception as e:
            log.error(f"Market order error: {e}")
            return None

    def place_limit(self, side: int, quantity: int, price: float, tag: str = "") -> Optional[PlacedOrder]:
        """Place limit order. Returns PlacedOrder or None on error."""
        grpc_side = SIDE_BUY if side == BUY else SIDE_SELL
        client_id = self._next_client_id()

        try:
            result = self._fp.call_function(
                self._fp.orders_stub.PlaceOrder,
                Order(
                    account_id=self._account_id,
                    symbol=self._symbol,
                    quantity=Decimal(value=str(quantity)),
                    side=grpc_side,
                    type=ORDER_TYPE_LIMIT,
                    limit_price=Decimal(value=str(int(price))),
                    client_order_id=client_id,
                ),
            )
            if result is None:
                log.error(f"Limit order failed: {tag} side={side} qty={quantity} price={price:.0f}")
                return None

            order_id = result.order_id
            log.info(f"Limit order placed: {tag} side={'BUY' if side==BUY else 'SELL'} "
                     f"qty={quantity} @ {price:.0f} id={order_id}")

            po = PlacedOrder(
                order_id=order_id,
                client_order_id=client_id,
                side=side,
                order_type="LIMIT",
                price=price,
                quantity=quantity,
                status="ACTIVE",
            )
            return po

        except Exception as e:
            log.error(f"Limit order error: {e}")
            return None

    def cancel(self, order_id: str) -> bool:
        """Cancel an order. Returns True if successful."""
        try:
            result = self._fp.call_function(
                self._fp.orders_stub.CancelOrder,
                CancelOrderRequest(
                    account_id=self._account_id,
                    order_id=order_id,
                ),
            )
            if result:
                log.info(f"Order cancelled: {order_id}")
                return True
            else:
                log.warning(f"Cancel returned None: {order_id}")
                return False
        except Exception as e:
            log.error(f"Cancel error: {e}")
            return False

    def cancel_all(self, orders: list[PlacedOrder]):
        """Cancel multiple orders."""
        for o in orders:
            if o.status == "ACTIVE":
                self.cancel(o.order_id)

    # === QUERY ===

    def get_active_orders(self) -> list[PlacedOrder]:
        """Get all active orders from broker."""
        try:
            resp = self._fp.call_function(
                self._fp.orders_stub.GetOrders,
                OrdersRequest(account_id=self._account_id),
            )
            if not resp:
                return []

            result = []
            for o in resp.orders:
                # Only include our orders (RBT- prefix)
                coid = o.client_order_id or ""
                if not coid or not coid.isdigit():
                    continue

                side = BUY if o.side == SIDE_BUY else SELL
                price = float(o.limit_price.value) if o.limit_price else 0
                status = "ACTIVE" if o.status == 1 else "FILLED" if o.status == 2 else "CANCELLED"

                result.append(PlacedOrder(
                    order_id=o.order_id,
                    client_order_id=coid,
                    side=side,
                    order_type="LIMIT" if o.type == ORDER_TYPE_LIMIT else "MARKET",
                    price=price,
                    quantity=int(o.quantity.value) if o.quantity else 0,
                    status=status,
                ))
            return result
        except Exception as e:
            log.error(f"GetOrders error: {e}")
            return []

    # === FILL TRACKING ===

    def get_recent_fills(self, since_sec: float = 30) -> list[FillInfo]:
        """Get fills received in the last N seconds."""
        cutoff = datetime.now(MSK) - timedelta(seconds=since_sec)
        return [f for f in self._fills if f.timestamp and f.timestamp > cutoff]

    def record_fill(self, fill: FillInfo):
        """Record a fill from gRPC push."""
        self._fills.append(fill)
        # Prune old fills (>60 sec)
        cutoff = datetime.now(MSK) - timedelta(seconds=60)
        self._fills = [f for f in self._fills if f.timestamp and f.timestamp > cutoff]
        log.info(f"Fill recorded: {'BUY' if fill.side==BUY else 'SELL'} "
                 f"{fill.quantity:.0f} @ {fill.price:.0f} order={fill.order_id}")
