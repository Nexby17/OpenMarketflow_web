# -*- coding: utf-8 -*-
"""Tests: honest broker-position sync after 14.09 false-DESYNC incident.

Covers orders_rest4.get_broker_position / positions_in_sync / cancel and
main_of(_mx) consumption semantics (fast helper, startup guard, monitor).
No network, no SDK — rest4 is mocked.
"""
import io
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROBOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROBOT)

import orders_rest4 as or4  # noqa: E402
from orders_rest4 import get_broker_position, positions_in_sync  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


# ---------- mocks ----------

class MockRest4:
    def __init__(self, responses):
        # responses: list of either dict (account info) or Exception
        self._responses = list(responses)
        self.calls = 0

    def get_account_info(self, account_id):
        self.calls += 1
        r = self._responses.pop(0) if self._responses else self._responses[-1]
        if isinstance(r, Exception):
            raise r
        return r


def acc_info(*positions):
    return {"positions": list(positions)}


def pos_short_1():
    # как отдаёт Finam: шорт = quantity со знаком −1
    return {"symbol": "MXU6@RTSX", "quantity": {"value": -1},
            "average_price": {"value": 232200}}


def pos_long_2():
    return {"symbol": "MXU6@RTSX", "quantity": {"value": 2},
            "average_price": {"value": 230100}}


class TooMany(Exception):
    def __str__(self):
        return "code=8 | message=Too Many Requests | details=[]"


class BadToken(Exception):
    def __str__(self):
        return "code=16 | message=Api token could not be verified | details=[]"


# ускоряем ретраи в тесте
or4.time.sleep = lambda s: None

# ---------- T1: инцидент дня — шорт читается как lots=1 dir=-1 ----------
m = MockRest4([acc_info(pos_short_1())])
bd = get_broker_position(m, "1225953", "MXU6@RTSX")
check("T1 short-1 -> lots=1", bd is not None and bd["lots"] == 1, str(bd))
check("T1 short-1 -> dir=-1", bd is not None and bd["dir"] == -1, str(bd))
check("T1 short-1 -> avg=232200", bd is not None and bd["avg_price"] == 232200, str(bd))

# ---------- T2: лонг ----------
m = MockRest4([acc_info(pos_long_2())])
bd = get_broker_position(m, "1225953", "MXU6@RTSX")
check("T2 long-2 -> lots=2 dir=+1", bd == {"lots": 2, "dir": 1, "avg_price": 230100}, str(bd))

# ---------- T3: пустой счёт ----------
m = MockRest4([acc_info()])
bd = get_broker_position(m, "1225953", "MXU6@RTSX")
check("T3 flat -> lots=0 dir=0", bd == {"lots": 0, "dir": 0, "avg_price": 0.0}, str(bd))

# ---------- T4: 429 ретраится и успешен ----------
m = MockRest4([TooMany(), TooMany(), acc_info(pos_short_1())])
bd = get_broker_position(m, "1225953", "MXU6@RTSX")
check("T4 429x2 retry -> ok", bd is not None and bd["lots"] == 1 and m.calls == 3, f"calls={m.calls} bd={bd}")

# ---------- T5: 429 x3 -> None (НЕ lots=0) ----------
m = MockRest4([TooMany(), TooMany(), TooMany()])
bd = get_broker_position(m, "1225953", "MXU6@RTSX")
check("T5 429x3 -> None", bd is None, str(bd))

# ---------- T6: битый токен ретраится -> None ----------
m = MockRest4([BadToken(), BadToken(), BadToken()])
bd = get_broker_position(m, "1225953", "MXU6@RTSX")
check("T6 token x3 -> None", bd is None, str(bd))

# ---------- T7: другая ошибка (не transient) -> сразу None ----------
class HardErr(Exception):
    def __str__(self):
        return "code=3 | message=Internal"

m = MockRest4([HardErr()])
bd = get_broker_position(m, "1225953", "MXU6@RTSX")
check("T7 hard error -> None fast", bd is None and m.calls == 1, f"calls={m.calls}")

# ---------- T8: avg_price альтернативные ключи + number quantity ----------
m = MockRest4([{"positions": [{"symbol": "MXU6", "quantity": -3, "avg_price": 231000}]}])
bd = get_broker_position(m, "1225953", "MXU6@RTSX")
check("T8 alt keys qty-number", bd == {"lots": 3, "dir": -1, "avg_price": 231000}, str(bd))

m = MockRest4([{"positions": [{"symbol": "MXU6", "quantity": {"value": -2}, "weighted_average_price": {"value": 231050}}]}])
bd = get_broker_position(m, "1225953", "MXU6@RTSX")
check("T8b weighted_average_price", bd is not None and bd["avg_price"] == 231050 and bd["lots"] == 2, str(bd))

# ---------- T9: positions_in_sync — инцидент 11:46 ----------
# робот: sell 1 (шорт), брокер честно -1 => SYNC (раньше было ложное DESYNC)
ok, msg = positions_in_sync(robot_lots=1, robot_dir=-1, broker_data={"lots": 1, "dir": -1, "avg_price": 232200})
check("T9 short==short synced", ok is True and msg == "robot=-1 broker=-1", msg)
# робот шорт 1, брокер флэт => DESYNC
ok, msg = positions_in_sync(1, -1, {"lots": 0, "dir": 0, "avg_price": 0.0})
check("T9b short vs flat -> desync", ok is False and msg == "robot=-1 broker=0", msg)
# робот лонг 1, брокер шорт 1 => DESYNC (раньше знаки путались!)
ok, msg = positions_in_sync(1, 1, {"lots": 1, "dir": -1, "avg_price": 0.0})
check("T9c long vs short -> desync", ok is False, msg)
# broker None => не тревога
ok, msg = positions_in_sync(1, -1, None)
check("T9d None -> silent ok", ok is True and msg == "", msg)
# несогласованный вход (lots>0, dir=0) не должен падать
ok, msg = positions_in_sync(2, 0, {"lots": 2, "dir": 1})
check("T9e robot dir=0 edge", isinstance(ok, bool), msg)

# ---------- T10: _get_broker_position_fast в main-файлах ----------
import importlib.util


def load_main(fname, modname):
    # main_of/main_of_mx тянут config и создают OrderManager — но не стартуют цикл.
    spec = importlib.util.spec_from_file_location(modname, os.path.join(ROBOT, fname))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass  # argparse на непонятных argv — ожидаемо вне CLI
    return mod


# конфиг-зависимые импорты могут потребовать config_of/config_of_mx — они в robot/
try:
    mx = load_main("main_of_mx.py", "main_of_mx_testmod")
    check("T10 mx import ok", True)
    src = io.open(os.path.join(ROBOT, 'main_of_mx.py'), encoding='utf-8').read()
    check("T10 mx fast None-guard",
          "broker_lots is None" in src and "returns 0 on error" not in src)
    check("T10 mx startup None-guard",
          "skipping reconciliation" in src and "broker unreadable" not in src.split("STARTUP:")[1][:120])
    check("T10 mx monitor uses positions_in_sync", "positions_in_sync(strategy._total_lots, strategy._dir, bd)" in src)
    check("T10 mx import has positions_in_sync",
          "from orders_rest4 import OrderManagerRest4, get_broker_position, positions_in_sync" in src)
    src_si = io.open(os.path.join(ROBOT, 'main_of.py'), encoding='utf-8').read()
    check("T10 si fast None-guard", "broker_lots is None" in src_si)
    check("T10 si monitor uses positions_in_sync", "positions_in_sync(strategy._total_lots, strategy._dir, bd)" in src_si)
    check("T10 si import has positions_in_sync",
          "from orders_rest4 import OrderManagerRest4, get_broker_position, positions_in_sync" in src_si)
except Exception as e:
    check("T10 mx import ok", False, repr(e))

# ---------- T11: cancel — уже исполненный ордер не ошибка ----------
mgr = or4.OrderManagerRest4(account="1225953", symbol="MXU6@RTSX", paper=False, rest4=None)


class FakeRest4Mgr:
    def cancel_order(self, account, order_id):
        raise Exception('code=9 | message={"code":"validation_error","message":"Order 1984994379379006531 cannot be canceled"} | details=[]')


mgr._rest4 = FakeRest4Mgr()
check("T11 cancel terminal -> True (no error spam)", mgr.cancel("1984994379379006531") is True)


class FakeRest4Hard:
    def cancel_order(self, account, order_id):
        raise Exception("code=5 | message=Server error")


mgr._rest4 = FakeRest4Hard()
check("T11b cancel hard error -> False", mgr.cancel("123") is False)

# ---------- T12: py_compile всех трёх файлов ----------
import py_compile
for f in ('orders_rest4.py', 'main_of.py', 'main_of_mx.py'):
    try:
        py_compile.compile(os.path.join(ROBOT, f), doraise=True)
        check(f"T12 compile {f}", True)
    except py_compile.PyCompileError as e:
        check(f"T12 compile {f}", False, str(e)[:200])

# ---------- T13: симуляция полной цепочки монитора (как в main) ----------
# робот стоит в шорте 1, брокер ответил -1 — монитор молчит; потом брокер 0 — тревога
class FakeStrategy:
    _total_lots = 1
    _dir = -1
    _avg_price = 232200.0


st = FakeStrategy()
bd_ok = {"lots": 1, "dir": -1, "avg_price": 232200}
ok, msg = positions_in_sync(st._total_lots, st._dir, bd_ok)
check("T13 monitor silent on signed short", ok, msg)
bd_flat = {"lots": 0, "dir": 0, "avg_price": 0.0}
ok, msg = positions_in_sync(st._total_lots, st._dir, bd_flat)
check("T13b monitor fires on real desync", not ok and msg == "robot=-1 broker=0", msg)
# после reset (FLAT) брокер -1 (робот не знает о позиции) — тревога
st._total_lots, st._dir = 0, 0
ok, msg = positions_in_sync(st._total_lots, st._dir, bd_ok)
check("T13c robot flat vs broker short -> desync", not ok and msg == "robot=0 broker=-1", msg)

print()
print(f"RESULT: {len(PASS)} pass, {len(FAIL)} fail")
if FAIL:
    print("FAILED:", ", ".join(FAIL))
    sys.exit(1)
print("ALL TESTS PASS")
