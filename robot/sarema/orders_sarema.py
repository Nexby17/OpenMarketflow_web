"""F-061: Order manager для SAR-робота через DataProvider REST proxy (:5060).

Общий gRPC-канал DP — как у остальных роботов. «No response» ≠ не исполнен:
после каждого ордера обязательна сверка факта (reconciliation, урок F-048).
"""
import logging
import time
from dataclasses import dataclass

import requests

log = logging.getLogger("sarema.orders")

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
    timestamp: float = 0.0


class SaremaOrders:
    """Рыночные входы/выходы + брокер-стоп stop-market + сверка исполнения."""

    def __init__(self, dp_url: str, account: str, symbol: str):
        self._dp_url = dp_url.rstrip("/")
        self._account = account
        self._symbol = symbol
        self._timeout = 5.0
        self.last_error: str = ""

    def _post(self, path: str, params: dict, timeout: float | None = None) -> dict | None:
        for attempt in range(3):
            try:
                r = requests.post(f"{self._dp_url}{path}", params=params,
                                  timeout=timeout or self._timeout)
                data = r.json()
                if "error" in data:
                    self.last_error = data["error"]
                    log.error(f"{path} error: {data['error']}")
                    if attempt < 2:
                        time.sleep(2)
                        continue
                    return None
                return data
            except Exception as e:
                self.last_error = str(e)
                log.error(f"{path} exception (attempt {attempt+1}/3): {e}")
                if attempt < 2:
                    time.sleep(2)
        return None

    def place_market(self, side: int, quantity: int, tag: str = "") -> PlacedOrder | None:
        if quantity < 1:
            return None
        side_str = "buy" if side == BUY else "sell"
        data = self._post("/order/place", {
            "account": self._account, "symbol": self._symbol, "side": side_str,
            "quantity": int(quantity), "order_type": "market", "tag": tag})
        if not data:
            return None
        po = PlacedOrder(order_id=data.get("order_id", ""), client_order_id=data.get("client_order_id", ""))
        log.info(f"market {side_str} qty={quantity} id={po.order_id} tag={tag}")
        return po

    def place_stop_market(self, side: int, quantity: int, stop_price: float, tag: str = "stop") -> PlacedOrder | None:
        if quantity < 1:
            return None
        side_str = "buy" if side == BUY else "sell"
        data = self._post("/order/place", {
            "account": self._account, "symbol": self._symbol, "side": side_str,
            "quantity": int(quantity), "stop_price": round(stop_price, 2),
            "order_type": "stop_market", "tag": tag}, timeout=10)
        if not data:
            return None
        po = PlacedOrder(order_id=data.get("order_id", ""), client_order_id=data.get("client_order_id", ""))
        log.info(f"stop-market {side_str} qty={quantity} @ {stop_price:.2f} id={po.order_id}")
        return po

    def cancel(self, order_id: str) -> bool:
        if not order_id:
            return False
        data = self._post("/order/cancel", {"account": self._account, "order_id": order_id})
        return data is not None

    def recent_fills(self, since_sec: float = 60) -> list[FillInfo]:
        try:
            r = requests.get(f"{self._dp_url}/order/fills",
                             params={"account": self._account, "symbol": self._symbol,
                                     "since_sec": since_sec}, timeout=self._timeout)
            data = r.json()
            fills = []
            for f in data.get("fills", []):
                fills.append(FillInfo(order_id=f.get("order_id", ""), symbol=f.get("symbol", ""),
                                      side=f.get("side", ""), price=float(f.get("price", 0)),
                                      quantity=int(f.get("quantity", 0)),
                                      timestamp=float(f.get("timestamp", 0))))
            return fills
        except Exception as e:
            log.error(f"fills exception: {e}")
            return []

    def get_position(self) -> int:
        """Позиция с брокера (штуки, +long/-short) — для reconciliation."""
        try:
            r = requests.get(f"{self._dp_url}/position",
                             params={"account": self._account, "symbol": self._symbol},
                             timeout=self._timeout)
            data = r.json()
            return int(data.get("quantity", 0))
        except Exception as e:
            log.error(f"position exception: {e}")
            return 0
