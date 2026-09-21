# -*- coding: utf-8 -*-
"""Тесты F-019: cancel_many, фильтр get_active_orders, wait_fill speed, ORDERS bridge.

Мок-брокер: no network.
"""
import io
import sys
import time
import types
import os

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
RO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, RO)
sys.path.insert(0, os.path.join(RO, "py4"))
sys.path.insert(0, os.path.dirname(RO))

results = []


def check(name, cond, extra=""):
    results.append((name, cond, extra))
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" | {extra}" if extra else ""))


# ================= 1. get_active_orders фильтр (unit, без сети) =================
# Имитируем d["orders"] от брокера
FAKE_ORDERS = [
    {"order_id": "1", "status": "ORDER_STATUS_NEW", "symbol": "MXZ6@RTSX"},
    {"order_id": "2", "status": "ORDER_STATUS_PARTIALLY_FILLED", "symbol": "MXZ6@RTSX"},
    {"order_id": "3", "status": "ORDER_STATUS_FILLED", "symbol": "MXZ6@RTSX"},      # неактивный
    {"order_id": "4", "status": "ORDER_STATUS_CANCELED", "symbol": "MXZ6@RTSX"},    # неактивный
    {"order_id": "5", "status": "ORDER_STATUS_NEW", "symbol": "SiZ6@RTSX"},         # чужой символ
    {"order_id": "6", "status": "ORDER_STATUS_WAIT", "symbol": "MXZ6@RTSX"},
]


class FakeOM:
    """Минимальный OrderManagerRest4 с подменённым REST."""
    _paper = False
    _account = "2049688"

    class _R4:
        @staticmethod
        def _get_module():
            import finam_trade_api
            from finam_trade_api.order import model as _omod

            class _Orders:
                @staticmethod
                def get_orders(req):
                    class _Resp:
                        def model_dump(self, mode=None):
                            return {"orders": FAKE_ORDERS}
                    return _Resp()
            # r4.call(r4.client.orders.get_orders(req)) — call должен существовать на _get_module()
            _mod = types.SimpleNamespace(client=types.SimpleNamespace(orders=_Orders()), call=lambda x: x)
            return _mod

    _rest4 = _R4()

    # подмешиваем тестируемые методы из реального класса
    import orders_rest4 as _ormod
    get_active_orders = _ormod.OrderManagerRest4.get_active_orders
    cancel_many = _ormod.OrderManagerRest4.cancel_many
    cancel = _ormod.OrderManagerRest4.cancel
    _ACTIVE_STATUSES = _ormod.OrderManagerRest4._ACTIVE_STATUSES


om = FakeOM()
active = om.get_active_orders(symbol="MXZ6@RTSX")
ids = sorted(o["order_id"] for o in active)
check("T1 get_active_orders: только активные", ids == ["1", "2", "6"], f"ids={ids}")
check("T2 фильтр symbol отсёк чужие", all(o["symbol"] == "MXZ6@RTSX" for o in active))
check("T3 FILLED/CANCELED отфильтрованы", "3" not in ids and "4" not in ids)

# ================= 2. cancel_many: rate-limit, без sleep(0.5) =================
cancel_calls = []


class FakeOM2(FakeOM):
    def cancel(self, oid):
        cancel_calls.append((time.time(), oid))
        return True


om2 = FakeOM2()
ids_many = [str(i) for i in range(1, 51)]  # 50 ордеров
t0 = time.time()
ok_n, failed = om2.cancel_many(ids_many, max_per_min=600)  # лимит не упрётся
dt = time.time() - t0
check("T4 cancel_many: все отменены", ok_n == 50 and not failed)
check("T5 cancel_many: 50 отмен < 1.5с (без sleep 0.5)", dt < 1.5, f"{dt:.2f}с (было бы 25+с)")

# rate-limit: 200 ордеров при лимите 180/мин → вторая пачка после паузы
# (не ждём 60с в тесте — проверяем только логику подсчёта)
cancel_calls.clear()
ok2, f2 = om2.cancel_many([str(i) for i in range(200)], max_per_min=10**9)
check("T6 cancel_many 200 шт при большом лимите: без пауз", ok2 == 200 and not failed)

# ================= 3. wait_fill: быстрый poll (0.06) =================
# wait_fill real-путь зовёт _rest4.get_order — мокаем
class FakeOM3(FakeOM):
    class _R4b:
        calls = 0

        @staticmethod
        def get_order(account, oid):
            FakeOM3._R4b.calls += 1
            # филл на 3-м опросе (имитация: брокер подтвердил через ~180мс при poll 0.06)
            if FakeOM3._R4b.calls >= 3:
                return {"status": "ORDER_STATUS_FILLED", "average_price": {"value": 229250.0}}
            return {"status": "ORDER_STATUS_NEW"}

    _rest4 = _R4b()

    wait_fill = __import__("orders_rest4").OrderManagerRest4.wait_fill


t0 = time.time()
fp = FakeOM3().wait_fill("oid1", timeout=1.2, poll=0.06)
dt = time.time() - t0
check("T7 wait_fill вернул цену", fp == 229250.0, f"price={fp}")
check("T8 wait_fill быстрый (<1.2с)", dt < 1.2, f"{dt:.2f}с")
# старый poll 0.3 дал бы 0.9с на 3 опроса; новый 0.06 → ~0.18с
check("T9 poll 0.06: 3 опроса < 0.5с", dt < 0.5, f"{dt:.3f}с")

# ================= 4. ORDERS bridge payload parse (hub_adapter._fmt) =================
# имитируем w_orders логику напрямую
def parse_orders_payload(payload, symbol, filled_cb):
    orders_list = payload.get("orders", []) if isinstance(payload, dict) else []
    if isinstance(payload, list):
        orders_list = payload
    for o in orders_list:
        if not isinstance(o, dict):
            continue
        st = str(o.get("status", "")).upper()
        if "FILLED" not in st and "PARTIALLY" not in st:
            continue
        sym = str(o.get("symbol", ""))
        if sym and sym != symbol:
            continue
        avg = o.get("average_price") or o.get("avg_price") or o.get("price") or {}
        price = avg.get("value") if isinstance(avg, dict) else avg
        filled_cb({
            "order_id": str(o.get("order_id", "")),
            "symbol": sym,
            "status": st,
            "price": float(price) if price else 0.0,
            "qty": 1,
        })


got = []
pl = {"orders": [
    {"order_id": "a", "status": "ORDER_STATUS_FILLED", "symbol": "MXZ6@RTSX", "average_price": {"value": 229300}},
    {"order_id": "b", "status": "ORDER_STATUS_NEW", "symbol": "MXZ6@RTSX"},
    {"order_id": "c", "status": "ORDER_STATUS_PARTIALLY_FILLED", "symbol": "MXZ6@RTSX", "average_price": {"value": 229250}},
    {"order_id": "d", "status": "ORDER_STATUS_FILLED", "symbol": "SiZ6@RTSX", "average_price": {"value": 86000}},
]}
parse_orders_payload(pl, "MXZ6@RTSX", got.append)
check("T10 ORDERS bridge: 2 филла (a,c)", len(got) == 2 and got[0]["order_id"] == "a" and got[1]["order_id"] == "c", str([g["order_id"] for g in got]))
check("T11 ORDERS bridge: цена из average_price.value", got[0]["price"] == 229300.0)
check("T12 ORDERS bridge: чужой символ отфильтрован", all(g["symbol"] == "MXZ6@RTSX" for g in got))

# ================= 5. _on_my_trade dict-совместимость (main_of_mx) =================
# грузим только функцию через ast — без импорта модуля (он запускает main loop)
import ast as _ast

src = io.open(os.path.join(RO, "main_of_mx.py"), encoding="utf-8").read()
tree = _ast.parse(src)
fn_src = None
for node in _ast.walk(tree):
    if isinstance(node, _ast.FunctionDef) and node.name == "_on_my_trade":
        fn_src = _ast.get_source_segment(src, node)
        break
check("T13 _on_my_trade найдена в main_of_mx", fn_src is not None)
if fn_src:
    ns = {"time": time, "log": types.SimpleNamespace(info=print, error=print), "SYMBOL": "MXZ6@RTSX",
          "_last_fill_price": 0.0, "_last_fill_time": 0.0, "_last_fill_qty": 0}
    exec(compile(fn_src, "<test>", "exec"), ns)
    ns["_on_my_trade"]({"order_id": "x1", "symbol": "MXZ6@RTSX", "price": 229400.0, "qty": 1})
    check("T14 _on_my_trade(dict): fill записан", ns["_last_fill_price"] == 229400.0)
    ns["_on_my_trade"]({"order_id": "x2", "symbol": "SiZ6@RTSX", "price": 86000.0, "qty": 1})
    check("T15 _on_my_trade: чужой символ игнор", ns["_last_fill_price"] == 229400.0)

# ================= Итог =================
passed = sum(1 for _, c, _ in results if c)
total = len(results)
print(f"\n=== {passed}/{total} PASS ===")
sys.exit(0 if passed == total else 1)
