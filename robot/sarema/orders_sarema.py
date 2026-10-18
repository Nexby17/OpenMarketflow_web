"""F-061: Order manager для SAR-робота — SDK REST (finam_rest4) с comment=sarema-*.

Почему не DP-proxy (orders_dp): Finam gRPC PlaceOrder НЕ сохраняет comment ордера —
у арбитражных роботов (через DP) все сделки в /api/trades идут с пустым comment.
SDK REST Order.comment сохраняется (подтверждено сделками OF-роботов: of_entry/of_stop...).
Комментарий sarema-* — единственный надёжный маркер, отделяющий сделки SAR-робота
от сделок арбитражных роботов на том же счёте (замечание Дмитрия 07.10).

Количество: для акций SDK ждёт ШТУКИ (у DP gRPC был пересчёт лотов, здесь — нет).
«no response» ≠ не исполнен: после каждого ордера обязательна сверка (урок F-048).
"""
import logging
import time
from dataclasses import dataclass

log = logging.getLogger("sarema.orders")

BUY = 1
SELL = -1

_COMMENT_PREFIX = "sarema"


@dataclass
class PlacedOrder:
    order_id: str
    client_order_id: str = ""
    comment: str = ""


@dataclass
class FillInfo:
    order_id: str
    symbol: str
    side: str
    price: float
    quantity: int
    timestamp: float = 0.0
    comment: str = ""


class SaremaOrders:
    """Рыночные входы/выходы + брокер-стоп stop-market, все ордера с comment=sarema-*."""

    def __init__(self, account_id: str, symbol: str):
        self._account = account_id
        self._symbol = symbol
        self.last_error: str = ""
        self._rest4 = None

    def _reauth(self):
        """F-080: принудительная пере-авторизация (новый JWT) при Token is invalid."""
        try:
            if self._rest4 and hasattr(self._rest4, '_get_module'):
                mod = self._rest4._get_module()
                if hasattr(mod, 'tm') and hasattr(mod.tm, 'ensure_token'):
                    mod.tm.ensure_token(force=True)
                    log.info('F-080: JWT re-auth OK')
                else:
                    log.warning('F-080: TokenManager.ensure_token not available')
        except Exception as e:
            log.error(f'F-080: re-auth failed: {e}')

    def _is_token_error(self, err: str) -> bool:
        return 'Token is invalid' in err or 'Token expired' in err or 'Unauthorized' in err

    def _r4(self):
        """Ленивая инициализация finam_rest4 (нужен FINAM_API_KEY в env или .env робота)."""
        if self._rest4 is None:
            import os
            import sys
            robot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if robot_dir not in sys.path:
                sys.path.insert(0, robot_dir)
            import finam_rest4
            if not os.environ.get("FINAM_API_KEY"):
                # .env из bin/Debug/net10.0 (как у C# сервера) или рядом
                for cand in (os.path.join(robot_dir, "..", "src", "Server", "bin", "Debug", "net10.0", ".env"),
                             os.path.join(robot_dir, ".env")):
                    if os.path.exists(cand):
                        with open(cand, encoding="utf-8-sig") as f:
                            for line in f:
                                line = line.strip()
                                if line.startswith("FINAM_API_KEY="):
                                    os.environ["FINAM_API_KEY"] = line.split("=", 1)[1].strip()
                                    break
                    if os.environ.get("FINAM_API_KEY"):
                        break
            self._rest4 = finam_rest4
            self._rest4._get_module()   # инициализирует singleton (TokenManager + loop)
        return self._rest4

    def _tag(self, tag: str) -> str:
        """Гарантируем префикс sarema- (маркер для журнала/фильтров)."""
        return tag if tag.startswith(_COMMENT_PREFIX) else f"{_COMMENT_PREFIX}-{tag}"

    def place_market(self, side: int, quantity: int, tag: str = "") -> PlacedOrder | None:
        if quantity < 1:
            return None
        # SDK enum: 'SIDE_BUY' / 'SIDE_SELL' (не 'buy'/'sell' — иначе 'not a valid Side')
        side_str = "SIDE_BUY" if side == BUY else "SIDE_SELL"
        comment = self._tag(tag)
        try:
            r4 = self._r4()
            try:
                resp = r4.place_market(self._account, self._symbol, side_str, int(quantity), comment=comment)
            except Exception as e1:
                if self._is_token_error(str(e1)):
                    log.warning("F-080: Token invalid на market — пере-авторизация + retry")
                    self._reauth()
                    resp = r4.place_market(self._account, self._symbol, side_str, int(quantity), comment=comment)
                else:
                    raise
            oid = str(resp.get("order_id", resp.get("orderId", "")))
            if not oid:
                self.last_error = str(resp)[:200]
                log.error(f"market failed: {self.last_error}")
                return None
            log.info(f"market {side_str} qty={quantity} id={oid} comment={comment}")
            return PlacedOrder(order_id=oid, comment=comment)
        except Exception as e:
            self.last_error = str(e)[:200]
            log.error(f"market exception: {e}")
            return None

    def place_stop_market(self, side: int, quantity: int, stop_price: float, tag: str = "stop") -> PlacedOrder | None:
        """Stop-market. SDK REST 4.3.3 не даёт stop-ордера напрямую — fallback:
        сторожевой режим робота (программный стоп по тикам) + этот метод оставлен
        под gRPC-канал (этап 2 финализации). Пока логируем предупреждение.
        """
        log.warning(f"stop_market via REST not supported yet — watchdog-based stop only ({tag})")
        return None

    def cancel(self, order_id: str) -> bool:
        if not order_id:
            return False
        try:
            r4 = self._r4()
            r4.cancel_order(self._account, order_id)
            return True
        except Exception as e:
            log.error(f"cancel {order_id}: {e}")
            return False

    def last_fill_by_comment(self, tag: str, symbol: str | None = None) -> FillInfo | None:
        """F-074: последняя исполненная сделка с данным comment (sarema-entry/stop-hit/...)
        из ленты брокера (/api/trades — та же, что на вкладке «Торговля»). Реальная цена."""
        try:
            import urllib.request, json
            with urllib.request.urlopen("http://localhost:5050/api/trades", timeout=10) as r:
                data = json.loads(r.read().decode())
            rows = data if isinstance(data, list) else (data.get("trades") or [])
            want = self._tag(tag)
            best = None
            for t in rows:
                c = str(t.get("comment", ""))
                if c != want:
                    continue
                if symbol and str(t.get("symbol", "")) != symbol:
                    continue
                px = t.get("price"); q = t.get("size") or t.get("quantity")
                fi = FillInfo(
                    order_id=str(t.get("order_id", "")),
                    symbol=str(t.get("symbol", "")),
                    side=str(t.get("side", "")),
                    price=float(px.get("value") if isinstance(px, dict) else (px or 0)),
                    quantity=int(float(q.get("value") if isinstance(q, dict) else (q or 0))),
                    comment=c,
                    timestamp=0.0)
                if best is None or fi.order_id >= best.order_id:
                    best = fi
            return best
        except Exception as e:
            log.error(f"last_fill_by_comment exception: {e}")
            return None

    def recent_fills(self, since_sec: float = 60) -> list[FillInfo]:
        """Сделки счёта (через C# /api/trades proxy) — только с comment sarema-*."""
        try:
            import urllib.request, json
            with urllib.request.urlopen("http://localhost:5050/api/trades", timeout=10) as r:
                data = json.loads(r.read().decode())
            rows = data if isinstance(data, list) else (data.get("trades") or [])
            out = []
            for t in rows:
                c = str(t.get("comment", ""))
                if not c.lower().startswith(_COMMENT_PREFIX):
                    continue
                px = t.get("price"); q = t.get("size") or t.get("quantity")
                out.append(FillInfo(
                    order_id=str(t.get("order_id", "")),
                    symbol=str(t.get("symbol", "")),
                    side=str(t.get("side", "")),
                    price=float(px.get("value") if isinstance(px, dict) else (px or 0)),
                    quantity=int(float(q.get("value") if isinstance(q, dict) else (q or 0))),
                    comment=c))
            return out
        except Exception as e:
            log.error(f"fills exception: {e}")
            return []

    def get_position(self) -> int:
        """Позиция с брокера (штуки, +long/-short) — для reconciliation (DP /position: ticker без @MISX)."""
        try:
            import urllib.request, json
            ticker = self._symbol.split("@")[0]
            req = urllib.request.Request(
                f"http://localhost:5060/position?account={self._account}&ticker={ticker}")
            with urllib.request.urlopen(req, timeout=5) as r:
                d = json.loads(r.read().decode())
            return int(d.get("quantity", d.get("totalNet", 0)) or 0)
        except Exception as e:
            log.error(f"position exception: {e}")
            return 0
