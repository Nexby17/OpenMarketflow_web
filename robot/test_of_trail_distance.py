# -*- coding: utf-8 -*-
"""F-223: стоп всегда в trail_sl_pts (12) от цены, шаг трейла 7.

Правило Дмитрия (23.11): стоп ставится в 12 пт от цены ПОСЛЕ того, как цена
прошла +42 (усреднение/вход) или +35 (пирамидинг), и переставляется шагом 7 пт,
оставаясь в 12 пт от цены.

Run: cwd=robot, python test_of_trail_distance.py
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
    p.trail_arm_buffer_pts = 0
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
        self.placed, self.cancelled, self.seq = [], [], 0

    def place(self, side, qty, px, tag=""):
        self.seq += 1
        self.placed.append((side, qty, float(px), tag))
        return type("O", (), {"order_id": f"st{self.seq}"})()

    def cancel(self, oid):
        self.cancelled.append(oid)


def pos(s, direction, prices, srcs=None):
    s._dir = direction
    s._lot_queue.clear()
    srcs = srcs or [""] * len(prices)
    for price, src in zip(prices, srcs):
        s._mk_lot(price, direction, 1)
        s._lot_queue[-1].src = src
    s._total_lots = len(prices)
    s._peak_lots = len(prices)
    s._avg_price = sum(prices) / len(prices)
    s._entry_price = prices[0]


def sync(s, bid, ask, st):
    s.sync_trail_stops(bid=bid, ask=ask, place_stop_fn=st.place,
                       cancel_fn=st.cancel, last=(bid + ask) / 2)


def t_avg_arm_at_42_dist_12():
    """Усреднение: триггер +42, уровень ровно bid−12."""
    s = make_strategy(); st = Stops()
    pos(s, LONG, [83400.0], srcs=["avg"])
    sync(s, 83441, 83442, st)
    assert not st.placed, f"до +42 стопа нет: {st.placed}"
    sync(s, 83442, 83443, st)
    assert st.placed and st.placed[0][2] == 83430.0, st.placed


def t_pyr_arm_at_35_dist_12():
    """Пирамидинг: триггер +35 (Step Pyr), уровень bid−12."""
    s = make_strategy(); st = Stops()
    pos(s, LONG, [83400.0], srcs=["pyr"])
    sync(s, 83434, 83435, st)
    assert not st.placed, f"до +35 стопа нет: {st.placed}"
    sync(s, 83435, 83436, st)
    assert st.placed and st.placed[0][2] == 83423.0, st.placed


def t_short_dist_12():
    s = make_strategy(); st = Stops()
    pos(s, SHORT, [83400.0], srcs=["avg"])
    sync(s, 83359, 83358, st)
    assert st.placed and st.placed[0][2] == 83370.0, st.placed


def t_trail_keeps_12():
    """При движении цены стоп идёт за ней, сохраняя 12 пт, шаг срабатывания 7."""
    s = make_strategy(); st = Stops()
    pos(s, LONG, [83400.0], srcs=["avg"])
    sync(s, 83450, 83451, st)
    assert s._trail_stops and list(s._trail_stops.values())[0]["stop_px"] == 83438.0
    sync(s, 83456, 83457, st)          # +6 < шага 7 → не двигаем
    assert list(s._trail_stops.values())[0]["stop_px"] == 83438.0
    sync(s, 83458, 83459, st)          # +8 ≥ 7 → двигаем
    assert list(s._trail_stops.values())[0]["stop_px"] == 83446.0
    assert st.placed[-1][2] == 83446.0, st.placed


def t_trail_never_backward():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [83400.0], srcs=["avg"])
    sync(s, 83460, 83461, st)
    prev = list(s._trail_stops.values())[0]["stop_px"]
    sync(s, 83445, 83446, st)
    assert list(s._trail_stops.values())[0]["stop_px"] == prev


def t_agg_first_stop_dist_12():
    """Агрегат: первый стоп тоже в 12 пт от цены."""
    s = make_strategy(); st = Stops()
    pos(s, SHORT, [82409.0, 82449.0])
    s._trail_closed_lots = 1
    s._peak_lots = 3
    sync(s, 82363.0, 82364.0, st)      # лот 82409 в плюсе на +45 → триггер
    assert s._agg_trail is not None, "агрегат должен встать"
    assert s._agg_trail["stop_px"] == 82376.0, s._agg_trail
    assert s._agg_trail["qty"] == 2


print("F-223 дистанция стопа 12 пт / шаг 7")
check("усреднение: триггер +42, дистанция 12", t_avg_arm_at_42_dist_12)
check("пирамида: триггер +35, дистанция 12", t_pyr_arm_at_35_dist_12)
check("SHORT: дистанция 12", t_short_dist_12)
check("трейл держит 12 пт, шаг 7", t_trail_keeps_12)
check("стоп не идёт назад", t_trail_never_backward)
check("агрегат: первый стоп в 12 пт", t_agg_first_stop_dist_12)

passed = sum(RESULTS)
print(f"\n{passed}/{len(RESULTS)} PASS")
sys.exit(0 if passed == len(RESULTS) else 1)
