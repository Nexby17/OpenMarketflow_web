# -*- coding: utf-8 -*-
"""F-073: unit tests for Pyramid Full Exit (Dmitry 16.10.2026) — no broker needed.

Rule (Si robots):
  - position >= 3 lots (1 entry + >=2 pyramids): exits are ONLY
    (1) pullback of 1 pyr-step from the LAST pyramid entry price -> close_all;
    (2) PnL/lot >= min_profit_per_lot -> close_all (tp_full).
    Partial LIFO TPs are disabled while rule is active.
  - position <= 2 lots: normal trading (averaging, LIFO partial TPs).
  - pyramid_full_exit=False (default): old behavior everywhere.
Run: cwd=robot, python test_of_pyramid_full_exit.py
"""
import sys
import os
from datetime import datetime
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(HERE)
for p in (os.path.join(HERE, "py4"), HERE, _PROJ):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

from strategy_of import (  # noqa: E402
    OFParams, OrderFlowStrategy, LONG, SHORT, FLAT, LotEntry,
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
    p.pyramid_full_exit = True
    p.step_pyramid = 50
    p.step_average = 50
    p.min_profit_per_lot = 30
    p.spread = 15
    p.max_pyramid_levels = 5
    p.max_average_levels = 10
    p.enable_max_levels = True
    for k, v in overrides.items():
        setattr(p, k, v)
    return OrderFlowStrategy(p)

def msk_dt(h, m):
    from strategy_of import MSK
    return datetime(2026, 10, 16, h, m, tzinfo=MSK)

def seed_position(s, entries):
    """entries: list of (price, kind) kind in {'entry','pyr','avg'}; all same dir=LONG."""
    import time as _t
    s._dir = LONG
    s._total_lots = 0
    for price, kind in entries:
        s._lot_queue.append(LotEntry(price=price, side=LONG, lots=1, added_ts=_t.monotonic() - 60))
        s._total_lots += 1
    s._entry_price = entries[0][0]
    s._avg_price = sum(e[0] for e in entries) / len(entries)
    s._entry_time = msk_dt(10, 0)
    s._peak_lots = len(entries)
    s._pyramid_levels = sum(1 for _, k in entries if k == 'pyr')
    s._average_levels = sum(1 for _, k in entries if k == 'avg')
    last_pyr = [pr for pr, k in entries if k == 'pyr']
    s._last_pyramid_price = last_pyr[-1] if last_pyr else entries[0][0]
    s.update_price(entries[-1][0])
    import time as _t2
    s._last_price_update = _t2.time()
    s._force_unlock()

# ---------- unit: last pyramid entry price ----------

def t_last_pyr_entry():
    s = make_strategy()
    seed_position(s, [(100.0, 'entry'), (105.0, 'pyr'), (110.0, 'pyr')])
    assert s._last_pyramid_entry_price() == 110.0, s._last_pyramid_entry_price()

def t_last_pyr_entry_single_lot():
    s = make_strategy()
    seed_position(s, [(100.0, 'entry')])
    assert s._last_pyramid_entry_price() is None, s._last_pyramid_entry_price()

# ---------- unit: pullback condition ----------

def t_pullback_hit():
    s = make_strategy()
    seed_position(s, [(100.0, 'entry'), (105.0, 'pyr'), (110.0, 'pyr')])
    # last pyr entry 110, step 50 -> trigger at 60
    assert s._pyramid_pullback_hit(60.0) is True, "exact trigger"
    assert s._pyramid_pullback_hit(60.5) is False, "above trigger"
    assert s._pyramid_pullback_hit(59.0) is True, "below trigger"

def t_pullback_short_side():
    s = make_strategy()
    seed_position(s, [(110.0, 'entry'), (105.0, 'pyr'), (100.0, 'pyr')])
    s._dir = SHORT
    # last pyr entry 100 (SHORT), step 50 -> trigger at 150 (rise against short)
    assert s._pyramid_pullback_hit(150.0) is True
    assert s._pyramid_pullback_hit(149.0) is False

def t_pullback_requires_3_lots():
    s = make_strategy()
    seed_position(s, [(100.0, 'entry'), (105.0, 'pyr')])
    # 2 lots: rule off even deep pullback
    assert s._pyramid_pullback_hit(50.0) is False, "2 lots must not trigger"

def t_pullback_disabled_flag():
    s = make_strategy(pyramid_full_exit=False)
    seed_position(s, [(100.0, 'entry'), (105.0, 'pyr'), (110.0, 'pyr')])
    assert s._pyramid_pullback_hit(10.0) is False, "flag off -> no trigger"

# ---------- integration: process_tick ----------

def t_process_tick_closes_on_pullback():
    s = make_strategy()
    seed_position(s, [(100.0, 'entry'), (105.0, 'pyr'), (110.0, 'pyr')])
    actions = s.process_tick(60.0, msk_dt(12, 0))
    assert actions and actions[0]['action'] == 'close_all', actions
    assert actions[0]['reason'] == 'pyramid_full_exit', actions
    assert actions[0]['side'] == 'sell', actions
    assert actions[0]['qty'] == 3, actions

def t_process_tick_2lots_no_pullback_exit():
    s = make_strategy()
    seed_position(s, [(100.0, 'entry'), (105.0, 'pyr')])
    # 2 lots: deep pullback must NOT close; averaging should trigger instead
    actions = s.process_tick(50.0, msk_dt(12, 0))
    assert not any(a['action'] == 'close_all' for a in actions), actions
    assert any(a['action'] == 'average' for a in actions), actions

def t_process_tick_tp_full_still_works():
    s = make_strategy()
    seed_position(s, [(100.0, 'entry'), (105.0, 'pyr'), (110.0, 'pyr')])
    # avg 105, min_profit 30 -> tp at 135; move to 140 (no pullback hit: 110-50=60 < 140)
    actions = s.process_tick(140.0, msk_dt(12, 0))
    assert actions and actions[0]['action'] == 'close_all', actions
    assert actions[0]['reason'] == 'tp_full', actions

def t_process_tick_partial_tp_blocked_at_3plus():
    s = make_strategy()
    seed_position(s, [(100.0, 'entry'), (105.0, 'pyr'), (110.0, 'pyr')])
    # price 120: last lot (110) has pnl 10 < spread 15 -> would be nothing anyway;
    # price 128: last lot pnl 18 >= spread 15 -> LIFO partial TP WOULD fire in old logic.
    # With F-073 active it must not fire (no partial_tp in actions, no close_all).
    actions = s.process_tick(128.0, msk_dt(12, 0))
    assert not any(a['action'] == 'partial_tp' for a in actions), f"partial_tp must be blocked: {actions}"
    assert not any(a['action'] == 'close_all' for a in actions), f"not yet: {actions}"

def t_process_tick_pyramiding_continues():
    # 2 лота + pyramid_full_exit: правило не активно (нужно ≥3), торгуем как раньше:
    # пирамиду проверяем на цене, где нет ни tp_full (pnl/лот < 30), ни partial (pnl лота < spread)
    s = make_strategy()
    seed_position(s, [(100.0, 'entry'), (140.0, 'pyr')])
    # avg=120; tp_full при 150+; лот 140 закрывается при 155+; pyr-шаг от 140 = 190.
    # 190: pnl/лот = 70 >= 30 → tp_full сработает раньше. Значит для чистоты пирамиды
    # поднимаем min_profit_per_lot так, чтобы tp_full не мешал (штатно у Дмитрия 30+):
    s.p.min_profit_per_lot = 75
    # 190: avg pnl 70 < 75 → нет tp_full; лот 140: 50 >= 15 → частичный... тоже мешает.
    # Правильный сценарий Дмитрия: пирамида продолжается в profits — но partial TP в старой
    # логике всегда приоритетен. При pyramid_full_exit и <3 лотах сохраняем старое поведение;
    # поэтому проверяем пирамиду при ОТКЛЮЧЁННОМ partial (настройка Дмитрия):
    s.p.partial_tp = False
    actions = s.process_tick(190.0, msk_dt(12, 0))
    assert any(a['action'] == 'pyramid' for a in actions), actions

def t_2lots_after_rule_no_partial_block():
    # 2 lots: partial TP still works (rule only for >=3)
    s = make_strategy()
    seed_position(s, [(100.0, 'entry'), (115.0, 'pyr')])
    # last lot 115, price 100 -> pnl -15 no; price 132 -> last lot pnl 17 >= spread 15
    actions = s.process_tick(132.0, msk_dt(12, 0))
    assert any(a['action'] == 'partial_tp' for a in actions), actions


if __name__ == "__main__":
    print("=== F-073 pyramid full exit tests ===")
    check("last_pyr_entry", t_last_pyr_entry)
    check("last_pyr_entry_single_lot", t_last_pyr_entry_single_lot)
    check("pullback_hit", t_pullback_hit)
    check("pullback_short_side", t_pullback_short_side)
    check("pullback_requires_3_lots", t_pullback_requires_3_lots)
    check("pullback_disabled_flag", t_pullback_disabled_flag)
    check("process_tick_closes_on_pullback", t_process_tick_closes_on_pullback)
    check("process_tick_2lots_no_pullback_exit", t_process_tick_2lots_no_pullback_exit)
    check("process_tick_tp_full_still_works", t_process_tick_tp_full_still_works)
    check("process_tick_partial_tp_blocked_at_3plus", t_process_tick_partial_tp_blocked_at_3plus)
    check("process_tick_pyramiding_continues", t_process_tick_pyramiding_continues)
    check("t_2lots_after_rule_no_partial_block", t_2lots_after_rule_no_partial_block)
    passed = sum(1 for r in RESULTS if r)
    print(f"=== {passed}/{len(RESULTS)} PASS ===")
    sys.exit(0 if passed == len(RESULTS) else 1)
