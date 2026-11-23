# -*- coding: utf-8 -*-
"""F-222: зазор стопа от рынка (иначе стоп-маркет брокера срабатывает мгновенно).

Кейс 23.11 13:04: AGG-стоп buy 4 @ 82445 при ask 82443 был принят в 10:04:10Z
и уже в 10:04:11Z превратился в рыночную заявку (triggered_order_id), филл 82441.5.
Причина: уровень считался от Ask, а биржа триггерит стоп по LAST — стоп стоял
вплотную к рынку.

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
    p.trail_debounce_sec = 0.4
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


def pos(s, direction, prices):
    s._dir = direction
    s._lot_queue.clear()
    for price in prices:
        s._mk_lot(price, direction, 1)
    s._total_lots = len(prices)
    s._peak_lots = len(prices)
    s._avg_price = sum(prices) / len(prices)
    s._entry_price = prices[0]


def t_short_no_arm_inside_buffer():
    """SHORT: уровень 82445, ask 82443, last 82450 → стоп не ставим (мгновенный триггер)."""
    s = make_strategy(); st = Stops()
    pos(s, SHORT, [82487.0])
    s.sync_trail_stops(bid=82442.0, ask=82443.0, place_stop_fn=st.place,
                       cancel_fn=lambda o: None, last=82450.0)
    assert not st.placed, f"стоп вплотную к рынку недопустим: {st.placed}"
    assert s._trail_stops == {}, s._trail_stops


def t_short_arms_with_gap():
    """SHORT: last 82441 → зазор до уровня 82445 = 4 > 3 → стоп ставим."""
    s = make_strategy(); st = Stops()
    pos(s, SHORT, [82487.0])
    s.sync_trail_stops(bid=82440.0, ask=82441.0, place_stop_fn=st.place,
                       cancel_fn=lambda o: None, last=82441.0)
    assert st.placed, "стоп должен встать"
    assert st.placed[0][2] == 82445.0, st.placed[0]


def t_short_last_beats_ask():
    """last выше ask: именно last определяет зазор (биржа триггерит по last)."""
    s = make_strategy(); st = Stops()
    pos(s, SHORT, [82487.0])
    s.sync_trail_stops(bid=82444.0, ask=82445.0, place_stop_fn=st.place,
                       cancel_fn=lambda o: None, last=82446.0)
    assert not st.placed, f"last уже за уровнем — ставить нельзя: {st.placed}"


def t_long_mirror():
    """LONG: уровень 83402, bid 83410, last 83409 → зазор 7 > 3 → ставим; last 83404 → нет."""
    s = make_strategy(); st = Stops()
    pos(s, LONG, [83360.0])
    s.sync_trail_stops(bid=83404.0, ask=83405.0, place_stop_fn=st.place,
                       cancel_fn=lambda o: None, last=83404.0)
    assert not st.placed, f"для LONG стоп слишком близко: {st.placed}"

    s2 = make_strategy(); st2 = Stops()
    pos(s2, LONG, [83360.0])
    s2.sync_trail_stops(bid=83409.0, ask=83410.0, place_stop_fn=st2.place,
                        cancel_fn=lambda o: None, last=83409.0)
    assert st2.placed and st2.placed[0][2] == 83402.0, st2.placed


def t_buffer_zero_keeps_legacy():
    """При buffer=0 поведение прежнее: стоп армится сразу за уровнем."""
    s = make_strategy(trail_arm_buffer_pts=0.0); st = Stops()
    pos(s, SHORT, [82487.0])
    s.sync_trail_stops(bid=82443.0, ask=82444.0, place_stop_fn=st.place,
                       cancel_fn=lambda o: None, last=82444.0)
    assert st.placed and st.placed[0][2] == 82445.0, st.placed


print("F-222 зазор стопа от рынка")
check("SHORT: уровень вплотную к рынку — не ставим", t_short_no_arm_inside_buffer)
check("SHORT: с зазором — ставим по уровню", t_short_arms_with_gap)
check("SHORT: last важнее ask", t_short_last_beats_ask)
check("LONG: зеркально", t_long_mirror)
check("buffer=0 — прежнее поведение", t_buffer_zero_keeps_legacy)

passed = sum(RESULTS)
print(f"\n{passed}/{len(RESULTS)} PASS")
sys.exit(0 if passed == len(RESULTS) else 1)
