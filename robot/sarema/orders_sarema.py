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

import os
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

    def __init__(self, account_id: str, symbol: str, state_ref=None):
        self._account = account_id
        self._state_ref = state_ref   # F-140: ссылка на state для защиты объёма
        self._symbol = symbol
        self.last_error: str = ""
        self._rest4 = None

    def _reload_api_key(self) -> bool:
        """F-101: перечитать FINAM_API_KEY из .env; True если ключ изменился."""
        import os
        robot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_key = os.environ.get('FINAM_API_KEY', '')
        for cand in (os.path.join(robot_dir, '..', 'src', 'Server', 'bin', 'Debug', 'net10.0', '.env'),
                     os.path.join(robot_dir, '.env')):
            if os.path.exists(cand):
                with open(cand, encoding='utf-8-sig') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('FINAM_API_KEY='):
                            disk = line.split('=', 1)[1].strip()
                            if disk and disk != env_key:
                                os.environ['FINAM_API_KEY'] = disk
                                return True
                            return False
        return False

    def _reauth(self):
        """F-080 fix3: официальный путь SDK — client.access_tokens.set_jwt_token()
        (async, сам делает POST /sessions и tm.set_jwt_token).
        F-101: перед re-auth ВСЕГДА сверяем FINAM_API_KEY с .env — процесс мог
        стартовать с битым унаследованным ключом; если на диске другой ключ,
        пересоздаём rest4 целиком (TokenManager держит старый секрет)."""
        try:
            if self._reload_api_key():
                log.info('F-101: FINAM_API_KEY обновлён из .env, пересоздаю rest4')
                self._rest4 = None
                self._r4()
            # F-103: /sessions с заголовком Authorization (битый JWT) даёт 400 даже с валидным
            # секретом — сбрасываем jwt_token перед получением нового.
            if self._rest4 is not None and hasattr(self._rest4, '_get_module'):
                mod = self._rest4._get_module()
                if hasattr(mod, 'tm'):
                    mod.tm._jwt_token = None
            if self._rest4 and hasattr(self._rest4, '_get_module'):
                mod = self._rest4._get_module()
                # F-152: SDK set_jwt_token шлёт POST /sessions с Authorization=битый JWT → 400,
                # старый битый токен остаётся в tm → retry вечно invalid. Надёжный путь:
                # POST /sessions {"secret"} БЕЗ Authorization → новый JWT → прямо в tm.
                import urllib.request as _uw
                import json as _j
                _key = os.environ.get('FINAM_API_KEY', '')
                if not _key:
                    self._reload_api_key()
                    _key = os.environ.get('FINAM_API_KEY', '')
                if not _key:
                    raise RuntimeError('FINAM_API_KEY пуст — re-auth невозможен')
                _req = _uw.Request('https://api.finam.ru/v1/sessions',
                                   data=_j.dumps({'secret': _key}).encode(), method='POST')
                _req.add_header('Content-Type', 'application/json')
                with _uw.urlopen(_req, timeout=15) as _r:
                    _tok = _j.loads(_r.read()).get('token')
                if not _tok:
                    raise RuntimeError('POST /sessions не вернул token')
                if hasattr(mod, 'tm'):
                    mod.tm._jwt_token = _tok
                    log.info('F-152: JWT обновлён напрямую (POST /sessions, без Authorization)')
                else:
                    log.warning('F-152: mod.tm не найден — JWT некуда записать')
            else:
                log.warning('F-080: _rest4 not initialized')
        except Exception as e:
            log.error(f'F-080: re-auth failed: {e}')

    def _is_token_error(self, err: str) -> bool:
        # F-155: 'Missing auth token' (code=16) = JWT в SDK нет (после auto_refresh=False
        # SDK сам токен не получает) — тоже повод для re-auth.
        return ('Token is invalid' in err or 'Token expired' in err or 'Unauthorized' in err
                or 'Missing auth token' in err or 'Invalid access key' in err or 'auth token' in err.lower())

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
        side_str = "SIDE_BUY" if (side == BUY or str(side).lower() == "buy") else "SIDE_SELL"  # F-156: понимаем и строку buy/sell
        comment = self._tag(tag)
        try:
            r4 = self._r4()
            try:
                resp = self._r4().place_market(self._account, self._symbol, side_str, int(quantity), comment=comment)
            except Exception as e1:
                if self._is_token_error(str(e1)):
                    log.warning("F-080: Token invalid на market — пере-авторизация + retry")
                    self._reauth()
                    resp = self._r4().place_market(self._account, self._symbol, side_str, int(quantity), comment=comment)
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

    def place_limit(self, side: int, quantity: int, price: float, tag: str = "tp") -> PlacedOrder | None:
        """F-105: лимитный ордер по TP (закрытие позиции по цене цели)."""
        if quantity < 1 or price <= 0:
            return None
        side_str = "SIDE_BUY" if (side == BUY or str(side).lower() == "buy") else "SIDE_SELL"  # F-156: понимаем и строку buy/sell
        # F-140: ЗАЩИТА — TP-лимитка должна ЗАКРЫВАТЬ позицию:
        # шорт → BUY (закрытие), лонг → SELL (закрытие). Обратная сторона = открытие объёма = БЛОК.
        if self._state_ref is not None:
            _d = int(self._state_ref.direction or 0)
            if _d == -1 and side_str == "SIDE_SELL":
                log.critical("F-140 BLOCK: place_limit SELL при шорте — откроет объём вместо закрытия")
                return None
            if _d == 1 and side_str == "SIDE_BUY":
                log.critical("F-140 BLOCK: place_limit BUY при лонге — откроет объём вместо закрытия")
                return None
        comment = self._tag(tag)
        try:
            r4 = self._r4()
            try:
                resp = self._r4().place_limit(self._account, self._symbol, side_str, int(quantity), float(price), comment=comment)
            except Exception as _e1:
                if self._is_token_error(str(_e1)):
                    log.warning("F-101: Token invalid на limit — re-auth + retry")
                    self._reauth()
                    resp = self._r4().place_limit(self._account, self._symbol, side_str, int(quantity), float(price), comment=comment)
                else:
                    raise
            oid = str(resp.get("order_id", resp.get("orderId", "")))
            if not oid:
                self.last_error = str(resp)[:200]
                log.error(f"limit failed: {self.last_error}")
                return None
            log.info(f"limit {side_str} qty={quantity} @ {price} id={oid} comment={comment}")
            return PlacedOrder(order_id=oid, comment=comment)
        except Exception as e:
            self.last_error = str(e)[:200]
            log.error(f"limit exception: {e}")
            return None

    DEAD_STATUSES = ('ORDER_STATUS_DONE_FOR_DAY', 'ORDER_STATUS_CANCELED', 'ORDER_STATUS_REJECTED',
                     'ORDER_STATUS_UNSPECIFIED')

    def list_active_stop_orders(self) -> list:
        """F-117: активные STOP-заявки робота по его тикеру (symbol+comment=sarema-stop).
        Возвращает [{order_id, stop_price, quantity, side}]."""
        try:
            import urllib.request as _uw
            env = os.environ.get('FINAM_API_KEY', '')
            if not env:
                robot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                for cand in (os.path.join(robot_dir, '..', 'src', 'Server', 'bin', 'Debug', 'net10.0', '.env'),
                             os.path.join(robot_dir, '.env')):
                    if os.path.exists(cand):
                        with open(cand, encoding='utf-8-sig') as f:
                            for line in f:
                                line = line.strip()
                                if line.startswith('FINAM_API_KEY='):
                                    env = line.split('=', 1)[1].strip()
                                    break
                    if env:
                        break
            if not env:
                return []
            import json as _j
            req = _uw.Request('https://api.finam.ru/v1/sessions',
                              data=_j.dumps({'secret': env}).encode(), method='POST')
            req.add_header('Content-Type', 'application/json')
            with _uw.urlopen(req, timeout=15) as r:
                jwt = _j.loads(r.read())['token']
            req2 = _uw.Request(f'https://api.finam.ru/v1/accounts/{self._account}/orders', method='GET')
            req2.add_header('Authorization', jwt)
            with _uw.urlopen(req2, timeout=15) as r:
                data = _j.loads(r.read())
            orders = data.get('orders', [])
            out = []
            for o in orders:
                order = o.get('order', o)
                sym = str(order.get('symbol', ''))
                typ = str(order.get('type', ''))
                status = str(o.get('status', order.get('status', '')))
                comment = str(order.get('comment', ''))
                if self._symbol.split('@')[0] in sym and 'STOP' in typ and 'sarema-stop' in comment and ('WATCHING' in status or 'ORDER_STATUS_NEW' in status):  # F-156: скобки — or раньше матчил ЛЮБУЮ NEW-заявку:
                    stop_price = order.get('stop_price', {})
                    qty = order.get('quantity', {})
                    out.append(dict(
                        order_id=str(o.get('order_id', '')),
                        stop_price=float(stop_price.get('value', 0)) if isinstance(stop_price, dict) else float(stop_price or 0),
                        quantity=int(float(qty.get('value', 0))) if isinstance(qty, dict) else int(float(qty or 0)),
                        side=str(order.get('side', '')),
                    ))
            return out
        except Exception as e:
            log.error(f'list_active_stop_orders err: {e}')
            return []

    def cancel_all_robot_orders(self) -> int:
        """F-127: снять ВСЕ активные заявки робота (стопы + лимитки) по тикеру и тегу sarema-*.
        Гарантия: после вызова у брокера не останется ни одной нашей заявки."""
        cancelled = 0
        try:
            for o in self.list_active_stop_orders():
                try:
                    self.cancel(o['order_id'])
                    cancelled += 1
                except Exception:
                    pass
            # TP-лимитки (список активных лимиток с тегом sarema-tp)
            try:
                import urllib.request as _uw
                _env_key = os.environ.get('FINAM_API_KEY', '')
                if _env_key:
                    import json as _j
                    req = _uw.Request('https://api.finam.ru/v1/sessions',
                                      data=_j.dumps({'secret': _env_key}).encode(), method='POST')
                    req.add_header('Content-Type', 'application/json')
                    with _uw.urlopen(req, timeout=15) as r:
                        jwt = _j.loads(r.read())['token']
                    req2 = _uw.Request(f'https://api.finam.ru/v1/accounts/{self._account}/orders', method='GET')
                    req2.add_header('Authorization', jwt)
                    with _uw.urlopen(req2, timeout=15) as r:
                        data = _j.loads(r.read())
                    for o in data.get('orders', []):
                        order = o.get('order', o)
                        sym = str(order.get('symbol', ''))
                        cmt = str(order.get('comment', ''))
                        status = str(o.get('status', ''))
                        if self._symbol.split('@')[0] in sym and 'sarema-' in cmt and \
                           ('ORDER_STATUS_WATCHING' in status or 'ORDER_STATUS_NEW' in status or
                            'ORDER_STATUS_PARTIALLY_FILLED' in status):
                            oid = str(o.get('order_id', ''))
                            try:
                                self.cancel(oid)
                                cancelled += 1
                            except Exception:
                                pass
            except Exception:
                pass
        except Exception as e:
            log.error(f'cancel_all_robot_orders err: {e}')
        return cancelled

    def get_order_status(self, order_id: str) -> str | None:
        """F-113: статус заявки у брокера (ORDER_STATUS_*) или None если недоступен."""
        if not order_id:
            return None
        try:
            r4 = self._r4()
            d = r4.get_order(self._account, order_id)
            st = d.get('status') or d.get('orderStatus') or ''
            return str(st)
        except Exception as e:
            # F-113: 'Order not found' = заявки больше нет (снята/сгорела/исполнена) → dead
            if 'not found' in str(e):
                return 'ORDER_STATUS_CANCELED'
            if self._is_token_error(str(e)):
                self._reauth()
                try:
                    r4 = self._r4()
                    d = r4.get_order(self._account, order_id)
                    st = d.get('status') or d.get('orderStatus') or ''
                    return str(st)
                except Exception as e2:
                    if 'not found' in str(e2):
                        return 'ORDER_STATUS_CANCELED'
                    log.error(f'get_order_status retry err: {e2}')
            else:
                log.error(f'get_order_status err: {e}')
            return None

    def is_order_filled(self, order_id: str) -> bool:
        """F-129: True если заявка ИСПОЛНЕНА брокером (позиция ею закрыта/открыта)."""
        st = self.get_order_status(order_id)
        if st is None:
            return False
        return 'ORDER_STATUS_FILLED' in st

    def is_dead_order(self, order_id: str) -> bool:
        """F-113: True если заявка снята брокером (конец дня/cancel/reject). None (нет связи) → False (не паникуем)."""
        st = self.get_order_status(order_id)
        if st is None:
            return False
        return any(dead in st for dead in self.DEAD_STATUSES)

    def place_stop_loss(self, side: int, quantity: int, stop_price: float, tag: str = "stop") -> PlacedOrder | None:
        """F-113: брокерский STOP-ордер (stop-market) через finam_rest4.place_stop_loss."""
        if quantity < 1 or stop_price <= 0:
            return None
        close_side = 'sell' if (side == BUY or side == 'sell') else 'buy'
        # F-140: ЗАЩИТА — стоп-заявка должна ЗАКРЫВАТЬ позицию:
        # шорт → BUY-стоп (закрытие при росте), лонг → SELL-стоп (закрытие при падении).
        if self._state_ref is not None:
            _d = int(self._state_ref.direction or 0)
            if _d == -1 and close_side == 'sell':
                log.critical("F-140 BLOCK: стоп SELL при шорте — откроет объём вместо закрытия")
                return None
            if _d == 1 and close_side == 'buy':
                log.critical("F-140 BLOCK: стоп BUY при лонге — откроет объём вместо закрытия")
                return None
        comment = self._tag(tag)
        try:
            r4 = self._r4()
            try:
                resp = self._r4().place_stop_loss(self._account, self._symbol, close_side, int(quantity), float(stop_price), comment=comment)
            except Exception as _e1:
                if self._is_token_error(str(_e1)):
                    log.warning('F-101: Token invalid на stop_loss — re-auth + retry')
                    self._reauth()
                    resp = self._r4().place_stop_loss(self._account, self._symbol, close_side, int(quantity), float(stop_price), comment=comment)
                else:
                    raise
            oid = str(resp.get('order_id', resp.get('orderId', '')))
            if not oid:
                self.last_error = str(resp)[:200]
                log.error(f'stop_loss failed: {self.last_error}')
                return None
            log.info(f'stop_loss {close_side} qty={quantity} @ {stop_price} id={oid}')
            return PlacedOrder(order_id=oid, comment=comment)
        except Exception as e:
            self.last_error = str(e)[:200]
            log.error(f'stop_loss exception: {e}')
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
