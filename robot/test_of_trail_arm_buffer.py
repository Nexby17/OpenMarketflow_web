# -*- coding: utf-8 -*-
"""F-222: страховка от стопа «за рынком» (биржа триггерит по LAST).

Основное правило дистанции живёт в test_of_trail_distance.py (F-223).
Здесь — только проверка зазора: если стоп оказался ближе trail_arm_buffer_pts
к лучшей из цен (bid/ask и last), он не выставляется.

Run: cwd=robot, python test_of_trail_arm_buffer.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(HERE)
for p in (os.path.join(HERE, "py4"), HERE, _PROJ):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

from strategy_of import OFParams, OrderFlowStrategy, LONG, SHORT  # noqa: E402

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
    p.spread = 42
    p.step_average = 38
    p.step_pyramid = 35
    p.close_half_pct = 60
    p.trail_mode = True
    p.trail_sl_pts = 12
    p.trail_step_pts = 7
    p.trail_debounce_sec = 0
    p.trail_arm_buffer_pts = 3.0
    p.partial_tp = True
    p.use_cvd = False
    p.use_cvd_accel = False
    p.use_vwema = False
    p.phase_filter = False
    p.use_vah_val = False
    p.hl_filter = False
    p.use_timing = False
    p.enable_ob_filter = False
    for k, v in overrides.items():
        setattr(p, k, v)
    return OrderFlowStrategy(p)


class Stops:
    def __init__(self):
        self.placed, self.seq = [], 0

    def place(self, side, qty, px, tag=""):
        self.seq += 1
        self.placed.append((side, qty, float(px), tag))
        return type("O", (), {"order_id": f"st{self.seq}"})()

    def cancel(self, oid):
        pass


def pos(s, direction, price):
    s._dir = direction
    s._lot_queue.clear()
    s._mk_lot(price, direction, 1)
    s._total_lots = 1
    s._peak_lots = 1
    s._avg_price = price
    s._entry_price = price


def t_last_spike_does_not_delay_short():
    """F-225: вынос last выше ask не должен откладывать постановку."""
    s = make_strategy(); st = Stops()
    pos(s, SHORT, 82500.0)
    s.sync_trail_stops(bid=82443.0, ask=82444.0, place_stop_fn=st.place,
                       cancel_fn=lambda o: None, last=82460.0)
    assert st.placed, "стоп должен встать сразу"
    assert st.placed[0][2] == 82456.0, st.placed


def t_normal_short_places():
    s = make_strategy(); st = Stops()
    pos(s, SHORT, 82410.0)
    s.sync_trail_stops(bid=82363.0, ask=82364.0, place_stop_fn=st.place,
                       cancel_fn=lambda o: None, last=82364.0)
    assert st.placed and st.placed[0][2] == 82376.0, st.placed


def t_last_spike_does_not_delay_long():
    """F-225: провал last ниже bid не откладывает постановку."""
    s = make_strategy(); st = Stops()
    pos(s, LONG, 83300.0)
    s.sync_trail_stops(bid=83404.0, ask=83405.0, place_stop_fn=st.place,
                       cancel_fn=lambda o: None, last=83390.0)
    assert st.placed, "стоп должен встать сразу"
    assert st.placed[0][2] == 83392.0, st.placed


def t_normal_long_places():
    s = make_strategy(); st = Stops()
    pos(s, LONG, 83360.0)
    s.sync_trail_stops(bid=83409.0, ask=83410.0, place_stop_fn=st.place,
                       cancel_fn=lambda o: None, last=83409.0)
    assert st.placed and st.placed[0][2] == 83397.0, st.placed


def t_tiny_sl_blocked_by_buffer():
    """Если trail_sl_pts меньше зазора — стоп не ставим (защита от 1-2 пт)."""
    s = make_strategy(trail_sl_pts=2); st = Stops()
    pos(s, SHORT, 82410.0)
    s.sync_trail_stops(bid=82363.0, ask=82364.0, place_stop_fn=st.place,
                       cancel_fn=lambda o: None, last=82364.0)
    assert not st.placed, f"стоп в 2 пт недопустим: {st.placed}"


print("F-222 зазор стопа от рынка")
check("SHORT: last выше ask — ставим сразу", t_last_spike_does_not_delay_short)
check("SHORT: нормальная дистанция — ставим", t_normal_short_places)
check("LONG: last ниже bid — ставим сразу", t_last_spike_does_not_delay_long)
check("LONG: нормальная дистанция — ставим", t_normal_long_places)
check("trail_sl_pts < зазора — не ставим", t_tiny_sl_blocked_by_buffer)

passed = sum(RESULTS)
print(f"\n{passed}/{len(RESULTS)} PASS")
sys.exit(0 if passed == len(RESULTS) else 1)
