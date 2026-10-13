# -*- coding: utf-8 -*-
"""F-064: unit tests for OF timing gate (use_timing/timing_start/timing_end) — no broker needed.

Semantics (Dmitry 12.10.2026):
  - use_timing ON + window set: entries only inside window;
  - after timing_end with open position: robot keeps managing it normally
    (averaging/pyramiding/exits) until closed — gate blocks NEW cycles only;
  - position closed after next day's timing_start → trading continues (stateless gate);
  - use_timing OFF or broken/empty times → gate inactive.
Run: cwd=robot, python test_of_timing.py
"""
import sys
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(HERE)
for p in (os.path.join(HERE, "py4"), HERE, _PROJ):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

from strategy_of import (  # noqa: E402
    OFParams, OrderFlowStrategy, LONG, SHORT, FLAT,
)

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

def make_strategy(**overrides):
    p = OFParams()
    p.lots = 1
    p.use_timing = True
    p.timing_start = "09:50"
    p.timing_end = "17:55"
    for k, v in overrides.items():
        setattr(p, k, v)
    return OrderFlowStrategy(p)

def msk_dt(h, m):
    from strategy_of import MSK
    return datetime(2026, 10, 13, h, m, tzinfo=MSK)

# ---------- _parse_hhmm ----------

def t_parse_hhmm():
    f = OrderFlowStrategy._parse_hhmm
    assert f("09:50") == 9 * 60 + 50, f("09:50")
    assert f("17:55") == 17 * 60 + 55
    assert f(" 10:30 ") == 10 * 60 + 30, "trim"
    assert f("") is None
    assert f(None) is None
    assert f("bad") is None
    assert f("9:50") == 9 * 60 + 50, "no leading zero ok"
    assert f("25:00") is None, "hour out of range"
    assert f("12:99") is None, "minute out of range"

# ---------- gate: window edges ----------

def t_gate_inside_window():
    s = make_strategy()
    assert s._timing_gate_closed(msk_dt(9, 50)) is False, "start minute = open"
    assert s._timing_gate_closed(msk_dt(12, 0)) is False
    assert s._timing_gate_closed(msk_dt(17, 55)) is False, "end minute = still open"

def t_gate_outside_window():
    s = make_strategy()
    assert s._timing_gate_closed(msk_dt(8, 0)) is True, "before start"
    assert s._timing_gate_closed(msk_dt(9, 49)) is True
    assert s._timing_gate_closed(msk_dt(17, 56)) is True, "after end"
    assert s._timing_gate_closed(msk_dt(23, 30)) is True

def t_gate_disabled():
    s = make_strategy(use_timing=False)
    for h, m in ((8, 0), (12, 0), (23, 59)):
        assert s._timing_gate_closed(msk_dt(h, m)) is False, f"use_timing OFF must be open @ {h}:{m}"

def t_gate_broken_times():
    # пустое/битое время → гейт неактивен (как legacy session)
    s = make_strategy(timing_start="", timing_end="")
    assert s._timing_gate_closed(msk_dt(12, 0)) is False
    s = make_strategy(timing_start="aa:bb")
    assert s._timing_gate_closed(msk_dt(12, 0)) is False
    s = make_strategy(timing_end=None)
    assert s._timing_gate_closed(msk_dt(12, 0)) is False

def t_gate_overnight_window():
    # окно через полночь (22:00 → 03:00)
    s = make_strategy(timing_start="22:00", timing_end="03:00")
    assert s._timing_gate_closed(msk_dt(23, 0)) is False, "late evening inside"
    assert s._timing_gate_closed(msk_dt(1, 0)) is False, "after midnight inside"
    assert s._timing_gate_closed(msk_dt(3, 0)) is False, "end minute inside"
    assert s._timing_gate_closed(msk_dt(12, 0)) is True, "midday outside"
    assert s._timing_gate_closed(msk_dt(21, 59)) is True

def t_gate_next_day_continuation():
    # Позиция закрылась в 10:05 следующего дня (старт 09:50) → окно уже открыто,
    # робот продолжает торговать; закрылась в 08:00 → ждёт старта.
    s = make_strategy()
    assert s._timing_gate_closed(msk_dt(10, 5)) is False
    assert s._timing_gate_closed(msk_dt(8, 0)) is True

# ---------- gate blocks NEW entries (flat) ----------

def t_entry_blocked_when_gate_closed():
    import time as _t
    s = make_strategy()
    s.update_price(100.0)
    s._last_price_update = _t.time()
    s._force_unlock()
    captured = {}

    def fake_entry(price, now):
        captured["called"] = True
        return {"action": "entry", "side": "buy", "qty": 1, "price": price}

    s._check_entry = fake_entry
    # 18:30 — вне окна, флэт: вход заблокирован
    actions = s.process_tick(100.0, msk_dt(18, 30))
    assert actions == [], f"gate closed must block entry, got {actions}"
    assert "called" not in captured, "_check_entry must not be called when gate closed"
    # 12:00 — внутри окна: вход проходит
    actions = s.process_tick(100.0, msk_dt(12, 0))
    assert captured.get("called") is True, "_check_entry must be called inside window"
    assert actions and actions[0]["action"] == "entry", actions

# ---------- position management NOT blocked by gate ----------

def t_exits_run_when_gate_closed():
    import time as _t
    s = make_strategy()
    s.update_price(100.0)
    s._last_price_update = _t.time()
    # открыта позиция LONG 1 лот
    s._dir = LONG
    s._total_lots = 1
    s._avg_price = 99.0
    s._entry_price = 99.0
    s._entry_time = msk_dt(10, 0)
    s._peak_lots = 1
    exit_action = {"action": "close_all", "side": "sell", "qty": 1, "price": 100.0, "reason": "tp_full"}
    s._check_exits = lambda price, now: exit_action
    # 18:30 — вне окна, но позиция должна управляться штатно
    actions = s.process_tick(100.0, msk_dt(18, 30))
    assert actions and actions[0]["action"] == "close_all", f"exit must run after end, got {actions}"

def t_averaging_runs_when_gate_closed():
    from collections import deque
    from strategy_of import LotEntry
    import time as _t
    s = make_strategy()
    s.update_price(90.0)
    s._last_price_update = _t.time()
    s._dir = LONG
    s._total_lots = 1
    s._avg_price = 100.0
    s._entry_price = 100.0
    s._entry_time = msk_dt(10, 0)
    s._peak_lots = 1
    s._lot_queue = deque([LotEntry(price=100.0, side=LONG, lots=1, added_ts=_t.monotonic())])
    s.p.step_average = 5
    s.p.max_average_levels = 10
    # 18:30 — вне окна: усреднение разрешено (торгуем в рамках открытой позиции)
    actions = s.process_tick(90.0, msk_dt(18, 30))
    assert actions, f"averaging must run after end, got {actions}"
    assert any(a["action"] == "average" for a in actions), actions

# ---------- is_timing_gate_closed (UI status) ----------

def t_is_timing_gate_closed_now():
    s = make_strategy(use_timing=False)
    assert s.is_timing_gate_closed() is False, "OFF → gate open"
    s2 = make_strategy(timing_start="", timing_end="")
    assert s2.is_timing_gate_closed() is False, "no times → gate open"


if __name__ == "__main__":
    print("=== F-064 timing gate tests ===")
    check("parse_hhmm", t_parse_hhmm)
    check("gate_inside_window", t_gate_inside_window)
    check("gate_outside_window", t_gate_outside_window)
    check("gate_disabled", t_gate_disabled)
    check("gate_broken_times", t_gate_broken_times)
    check("gate_overnight_window", t_gate_overnight_window)
    check("gate_next_day_continuation", t_gate_next_day_continuation)
    check("entry_blocked_when_gate_closed", t_entry_blocked_when_gate_closed)
    check("exits_run_when_gate_closed", t_exits_run_when_gate_closed)
    check("averaging_runs_when_gate_closed", t_averaging_runs_when_gate_closed)
    check("is_timing_gate_closed_now", t_is_timing_gate_closed_now)
    passed = sum(1 for r in RESULTS if r)
    print(f"=== {passed}/{len(RESULTS)} PASS ===")
    sys.exit(0 if passed == len(RESULTS) else 1)
