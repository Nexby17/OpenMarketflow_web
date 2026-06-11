"""OrderManager via DataProvider REST proxy — single gRPC connection for all robots."""
import logging
import time
import requests
from typing import Optional
from dataclasses import dataclass

log = logging.getLogger("orders")

BUY = 1
SELL = -1


@dataclass
class PlacedOrder:
    order_id: str
    client_order_id: str = ""


@dataclass
class FillInfo:
    order_id: str
    symbol: str
    side: str
    price: float
    quantity: int
    timestamp: float


class OrderManager:
    """Order manager using DataProvider REST API (shared gRPC)."""

    def __init__(self, dp_url: str = "http://localhost:5060", account: str = "", symbol: str = ""):
        self._dp_url = dp_url.rstrip("/")
        self._account = account
        self._symbol = symbol
        self._timeout = 3.0

    def place_market(self, side: int, quantity: int, tag: str = "") -> Optional[PlacedOrder]:
        side_str = "buy" if side == BUY else "sell"
        try:
            r = requests.post(
                f"{self._dp_url}/order/place",
                params={"account": self._account, "symbol": self._symbol,
                        "side": side_str, "quantity": quantity, "order_type": "market", "tag": tag},
                timeout=self._timeout,
            )
            data = r.json()
            if "error" in data:
                log.error(f"Market order error: {data['error']}")
                return None
            po = PlacedOrder(order_id=data.get("order_id", ""), client_order_id=data.get("client_order_id", ""))
            log.info(f"Market order placed: {tag} side={side_str} qty={quantity} id={po.order_id}")
            return po
        except Exception as e:
            log.error(f"Market order exception: {e}")
            return None

    def place_limit(self, side: int, quantity: int, price: float, tag: str = "") -> Optional[PlacedOrder]:
        side_str = "buy" if side == BUY else "sell"
        for attempt in range(3):
            try:
                r = requests.post(
                    f"{self._dp_url}/order/place",
                    params={"account": self._account, "symbol": self._symbol,
                            "side": side_str, "quantity": quantity, "price": int(price),
                            "order_type": "limit", "tag": tag},
                    timeout=self._timeout,
                )
                data = r.json()
                if "error" in data:
                    log.error(f"Limit order error (attempt {attempt+1}): {data['error']}")
                    if attempt < 2:
                        import time; time.sleep(1)
                        continue
                    return None
                po = PlacedOrder(order_id=data.get("order_id", ""), client_order_id=data.get("client_order_id", ""))
                log.info(f"Limit order placed: {tag} side={side_str} qty={quantity} @ {price:.0f} id={po.order_id}")
                return po
            except Exception as e:
                log.error(f"Limit order exception (attempt {attempt+1}): {e}")
                if attempt < 2:
                    import time; time.sleep(1)
                    continue
                return None
        return None

    def cancel(self, order_id: str) -> bool:
        if not order_id:
            return False
        try:
            r = requests.post(
                f"{self._dp_url}/order/cancel",
                params={"account": self._account, "order_id": order_id},
                timeout=self._timeout,
            )
            data = r.json()
            if "error" in data:
                log.warning(f"Cancel error for {order_id}: {data.get('error')}")
                return False
            log.info(f"Order cancelled: {order_id}")
            return True
        except Exception as e:
            log.error(f"Cancel exception: {e}")
            return False

    def cancel_all(self, orders: list):
        for o in orders:
            self.cancel(o.order_id if hasattr(o, 'order_id') else str(o))

    def get_active_orders(self, symbol: str = None) -> list:
        try:
            r = requests.get(
                f"{self._dp_url}/active-orders",
                params={"account": self._account},
                timeout=self._timeout,
            )
            orders = r.json()
            # Filter by symbol if specified
            if symbol and orders:
                orders = [o for o in orders if getattr(o, 'symbol', None) == symbol or (isinstance(o, dict) and o.get('symbol') == symbol)]
            return orders
        except Exception as e:
            log.error(f"Get orders error: {e}")
            return []

    def get_recent_fills(self, since_sec: float = 30) -> list:
        try:
            r = requests.get(
                f"{self._dp_url}/recent-fills",
                params={"account": self._account, "symbol": self._symbol},
                timeout=self._timeout,
            )
            return r.json()
        except Exception as e:
            log.error(f"Get fills error: {e}")
            return []

    def record_fill(self, fill: FillInfo):
        pass  # DP tracks fills via gRPC streaming
