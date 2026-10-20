# -*- coding: utf-8 -*-
"""F-091: unit tests for VAH/VAL RANGE v2 — зона-фильтр направления + block_inside."""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(HERE)
for p in (os.path.join(HERE, "py4"), HERE, _PROJ):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

from strategy_of import OFParams, OrderFlowStrategy, LONG, SHORT  # noqa: E402
from collections import deque  # noqa: E402
from orderflow_engine import BarMetrics, OFSignal  # noqa: E402
from datetime import datetime  # noqa: E402
from strategy_of import MSK  # noqa: E402

RESULTS = []

def check(name, fn):
    try:
        fn()
        RESULTS.append(True)
        print(f"  PASS {name}")
    except AssertionError as ex:
        RESULTS.append(False)
        print(f"  FAIL {name}: {ex}")
    except Exception as ex:
        RESULTS.append(False)
        print(f"  FAIL {name}: {type(ex).__name__}: {ex}")

def make():
    p = OFParams()
    p.lots = 1
    p.use_vah_val = True
    p.vah_val_mode = "range"
    p.vah_val_block_inside = False
    p.enable_ob_filter = False
    p.signal_confirm_count = 1
    p.use_cvd_accel = False
    p.use_cvd = False
    p.use_dm_wall = False
    p.hl_filter = False
    p.use_vwema = False
    p.phase_filter = False
    p.partial_tp = False
    p.close_half_pct = 0
    p.enable_daily_stop = False
    s = OrderFlowStrategy(p)

    class FakeVP:
        vah = 8500.0
        val = 8000.0
        ready = True
    s.vp = FakeVP()

    for _ in range(20):
        m = BarMetrics.__new__(BarMetrics)
        m.delta = 0
        m.buy = 0
        m.sell = 0
        s.signals.add_bar(m, 8400, 8390, 8395)

    def gen(direction):
        s.signals.generate_signals = lambda ob, price, **kw: [OFSignal("cvd_trend", direction, 0.9, price, 0)]
    s.gen = gen
    return s

def dt(h, m):
    return datetime(2026, 10, 20, h, m, tzinfo=MSK)

def t_long_at_vah_blocked():
    s = make()
    s.gen(LONG)
    a = s.process_tick(8520.0, dt(12, 0))
    assert not any(x["action"] == "entry" for x in a), f"LONG у VAH должен блокироваться: {a}"

def t_short_at_vah_allowed():
    s = make()
    s.gen(SHORT)
    a = s.process_tick(8520.0, dt(12, 1))
    assert a and a[0]["action"] == "entry", f"SHORT у VAH должен пройти: {a}"

def t_short_at_val_blocked():
    s = make()
    s.gen(SHORT)
    a = s.process_tick(7980.0, dt(12, 2))
    assert not any(x["action"] == "entry" for x in a), f"SHORT у VAL должен блокироваться: {a}"

def t_long_at_val_allowed():
    s = make()
    s.gen(LONG)
    a = s.process_tick(7980.0, dt(12, 3))
    assert a and a[0]["action"] == "entry", f"LONG у VAL должен пройти: {a}"

def t_inside_both_allowed_when_block_inside_false():
    s = make()
    s.gen(LONG)
    a = s.process_tick(8200.0, dt(12, 4))
    assert a and a[0]["action"] == "entry", f"inside VA, оба разрешены: {a}"

def t_inside_blocked_when_block_inside_true():
    s = make()
    s.p.vah_val_block_inside = True
    s.gen(LONG)
    a = s.process_tick(8200.0, dt(12, 5))
    assert not any(x["action"] == "entry" for x in a), f"inside VA + block_inside: {a}"

def t_averaging_not_gated():
    """Вариант А: усреднение существующей позиции не проходит через VAH/VAL гейт."""
    s = make()
    s.gen(LONG)
    a = s.process_tick(7980.0, dt(12, 6))  # LONG у VAL — вход
    assert a and a[0]["action"] == "entry", a
    # цена ниже на 100 при step_average=36 → усреднение (вне VA тоже, вариант А)
    a = s.process_tick(7900.0, dt(12, 7))
    assert any(x["action"] == "average" for x in a), f"усреднение должно работать (вариант А): {a}"


if __name__ == "__main__":
    print("=== F-091 VAH/VAL RANGE v2 tests ===")
    check("long_at_vah_blocked", t_long_at_vah_blocked)
    check("short_at_vah_allowed", t_short_at_vah_allowed)
    check("short_at_val_blocked", t_short_at_val_blocked)
    check("long_at_val_allowed", t_long_at_val_allowed)
    check("inside_both_allowed", t_inside_both_allowed_when_block_inside_false)
    check("inside_blocked_when_block_inside", t_inside_blocked_when_block_inside_true)
    check("averaging_not_gated", t_averaging_not_gated)
    passed = sum(1 for r in RESULTS if r)
    print(f"=== {passed}/{len(RESULTS)} PASS ===")
    sys.exit(0 if passed == len(RESULTS) else 1)
