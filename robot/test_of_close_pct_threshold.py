# -*- coding: utf-8 -*-
"""F-221: порог Close % All на пике 6 лотов (кейс 23.11 12:55).

Дмитрий: «было 6 лотов — 2 закрылись по SL трейл, осталось 4 — следующим
должен выставиться SL трейл по правилу Close % All 60».
Значит порог остатка = 4 (60% от 6 = 3.6 → округление вверх/к ближайшему),
а НЕ 3 (усечение int()).

Run: cwd=robot, python test_of_close_pct_threshold.py
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
    p.partial_tp = True
    p.min_profit_per_lot = 125
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


def pos(s, direction, prices):
    s._dir = direction
    s._lot_queue.clear()
    for price in prices:
        s._mk_lot(price, direction, 1)
    s._total_lots = len(prices)
    s._peak_lots = len(prices)
    s._avg_price = sum(prices) / len(prices)
    s._entry_price = prices[0]


def sync(s, bid, ask, st):
    s.sync_trail_stops(bid=bid, ask=ask, place_stop_fn=st.place, cancel_fn=st.cancel)


SIX = [82379.0, 82409.0, 82449.0, 82487.0, 82528.0, 82556.0]


def close_two_lots(s, st):
    """Реальный путь: армируем стоп лота, затем филим его как брокер."""
    sync(s, 82512.0, 82512.5, st)          # лот 82556 ушёл на +42
    rec = s._trail_stops.get(s._lot_queue[-1].uid)
    assert rec and rec.get("order_id"), "стоп по лоту 82556 не встал"
    s.apply_trail_stop_fill(rec["order_id"], 82513.0)

    sync(s, 82484.0, 82484.5, st)          # следующий лот 82528
    uid = [e.uid for e in s._lot_queue if e.price == 82528.0][0]
    rec = s._trail_stops.get(uid)
    assert rec and rec.get("order_id"), "стоп по лоту 82528 не встал"
    s.apply_trail_stop_fill(rec["order_id"], 82495.5)

    assert s._total_lots == 4, f"должно остаться 4 лота, got {s._total_lots}"
    assert s._trail_closed_lots == 2, s._trail_closed_lots


def t_agg_on_four_of_six():
    """Остаток 4 из 6 → ОДИН стоп на весь объём по уровню первого дошедшего лота."""
    s = make_strategy(); st = Stops()
    pos(s, SHORT, SIX)
    close_two_lots(s, st)
    st.placed.clear()
    sync(s, 82443.0, 82444.0, st)
    assert s._agg_trail is not None, (
        f"агрегата нет: closedLots={s._trail_closed_lots} peak={s._peak_lots} "
        f"lots={s._total_lots} — порог Close % All считается неверно")
    assert st.placed[-1][1] == 4, f"на весь остаток (4), got {st.placed[-1]}"
    assert st.placed[-1][2] == 82445.0, f"уровень = вход 82487 − 42, got {st.placed[-1]}"
    assert st.placed[-1][3] == "of_trail_sl_agg", st.placed[-1]


def t_no_agg_on_five_of_six():
    """Остаток 5 > порога (4) → пока по-лотово, агрегата нет."""
    s = make_strategy(); st = Stops()
    pos(s, SHORT, SIX)
    sync(s, 82512.0, 82512.5, st)
    rec = s._trail_stops.get(s._lot_queue[-1].uid)
    s.apply_trail_stop_fill(rec["order_id"], 82513.0)
    assert s._total_lots == 5
    st.placed.clear()
    sync(s, 82443.0, 82444.0, st)
    assert s._agg_trail is None, "при 5 лотах агрегат ставиться не должен"
    assert st.placed and all(p[1] == 1 for p in st.placed), st.placed


def t_agg_on_two_of_four():
    """Регресс: пик 4, остаток 2 (как было в 11:25 сегодня) — агрегат обязателен."""
    s = make_strategy(); st = Stops()
    pos(s, SHORT, [82487.0, 82528.0])
    s._peak_lots = 4
    s._trail_closed_lots = 2
    sync(s, 82443.0, 82444.0, st)
    assert s._agg_trail is not None, "при пике 4 и остатке 2 агрегат должен встать"
    assert st.placed[-1][1] == 2, f"на весь остаток, got {st.placed[-1]}"


def t_threshold_math():
    """Порог = округление половины вверх от peak*pct/100, минимум 2."""
    for peak, pct, want in [(6, 60, 4), (4, 60, 2), (5, 60, 3), (7, 60, 4),
                            (3, 60, 2), (2, 60, 2), (9, 60, 5), (11, 60, 7),
                            (6, 50, 3), (14, 70, 10)]:
        got = OrderFlowStrategy._close_pct_threshold(peak, pct)
        assert got == want, f"peak={peak} pct={pct}: want {want}, got {got}"


print("F-221 Close % All — порог остатка")
check("остаток 4 из 6 → один стоп на объём", t_agg_on_four_of_six)
check("остаток 5 из 6 → ещё по-лотово", t_no_agg_on_five_of_six)
check("регресс: пик 4 / остаток 2 → агрегат", t_agg_on_two_of_four)
check("арифметика порога", t_threshold_math)

passed = sum(RESULTS)
print(f"\n{passed}/{len(RESULTS)} PASS")
sys.exit(0 if passed == len(RESULTS) else 1)
