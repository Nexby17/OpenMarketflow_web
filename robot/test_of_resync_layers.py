# -*- coding: utf-8 -*-
"""F-224: full-resync не схлопывает слои при совпадающей позиции.

Кейс 23.11 13:49: было 8 слоёв по 1 лоту, после рестарта sync_from_broker(full=True)
сделал ОДИН слой price=82698 lots=8 — и трейл поставил один стоп на 8 лотов.

Run: cwd=robot, python test_of_resync_layers.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(HERE)
for p in (os.path.join(HERE, "py4"), HERE, _PROJ):
    if p.isdir() if hasattr(p, "isdir") else os.path.isdir(p):
        if p not in sys.path:
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


def make_strategy():
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
    p.use_cvd = False
    p.use_cvd_accel = False
    p.use_vwema = False
    p.phase_filter = False
    p.use_vah_val = False
    p.hl_filter = False
    p.use_timing = False
    p.enable_ob_filter = False
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


EIGHT = [82379.0, 82409.0, 82449.0, 82487.0, 82528.0, 82556.0, 82608.0, 82649.0]


def t_same_position_keeps_layers():
    s = make_strategy(); pos(s, SHORT, EIGHT)
    s.sync_from_broker({"lots": 8, "dir": -1, "avg_price": 82505.0, "current_price": 82500.0}, full=True)
    assert len(s._lot_queue) == 8, f"слои схлопнулись: {len(s._lot_queue)}"
    assert s._total_lots == 8
    assert [e.price for e in s._lot_queue] == EIGHT, [e.price for e in s._lot_queue]


def t_same_position_one_stop_per_layer():
    s = make_strategy(); pos(s, SHORT, EIGHT)
    s.sync_from_broker({"lots": 8, "dir": -1, "avg_price": 82505.0, "current_price": 82500.0}, full=True)
    st = Stops()
    # цена прошла +42 от последнего слоя (82649-42=82607) → встаёт один стоп на 1 лот
    s.sync_trail_stops(bid=82605.0, ask=82606.0, place_stop_fn=st.place,
                       cancel_fn=lambda o: None, last=82606.0)
    assert st.placed, "стоп не встал"
    assert all(p[1] == 1 for p in st.placed), f"на 8 лотов ставить нельзя: {st.placed}"
    assert st.placed[0][2] == 82618.0, st.placed[0]


def t_different_lots_collapse():
    """Позиция разошлась с брокером — тут схлопывание как раз нужно."""
    s = make_strategy(); pos(s, SHORT, EIGHT)
    s.sync_from_broker({"lots": 3, "dir": -1, "avg_price": 82500.0, "current_price": 82500.0}, full=True)
    assert len(s._lot_queue) == 1 and s._total_lots == 3, (len(s._lot_queue), s._total_lots)
    assert s._lot_queue[0].price == 82500.0


def t_flat_collapse():
    s = make_strategy(); pos(s, SHORT, EIGHT)
    s.sync_from_broker({"lots": 0, "dir": 0, "avg_price": 0.0, "current_price": 82500.0}, full=True)
    assert s._total_lots == 0 and not s._lot_queue


print("F-224 сохранение слоёв при full-resync")
check("позиция совпала — 8 слоёв сохранены", t_same_position_keeps_layers)
check("после resync стоп по 1 лоту, а не на 8", t_same_position_one_stop_per_layer)
check("позиция разошлась — схлопываем", t_different_lots_collapse)
check("FLAT — очередь пуста", t_flat_collapse)

passed = sum(RESULTS)
print(f"\n{passed}/{len(RESULTS)} PASS")
sys.exit(0 if passed == len(RESULTS) else 1)
