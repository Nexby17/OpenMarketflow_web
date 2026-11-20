# -*- coding: utf-8 -*-
"""F-198..F-211: режим Trailing SL — unit-тесты (no broker, колбэки-заглушки).

Правила (итог согласования с Дмитрием):
  * вход по сигналу; partial TP по Spread и догоны тейков ОТКЛЮЧЕНЫ,
    tp_full по Min Profit/Lot РАБОТАЕТ;
  * как только ЛЮБОЙ лот дошёл до своего порога (Spread для входа/усреднения,
    Step Pyr для пирамидного лота) — ставится ОДИН стоп на ВЕСЬ объём
    на уровне «вход этого лота + порог» (для LONG выше = плотнее, для SHORT ниже);
  * стоп ставится только когда рынок уже за уровнем (иначе мгновенный триггер);
  * дальше трейлится шагом, только в сторону позиции, не ниже стартового уровня;
  * пирамида 3+ лота (pyramid_levels>=2) — стоп на весь объём сразу, по рынку (Bid/Ask -/+ SL);
  * срабатывание агрегата закрывает всю позицию.

Run: cwd=robot, python test_of_trail_sl.py
"""
import sys
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(HERE)
for p in (os.path.join(HERE, "py4"), HERE, _PROJ):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

from strategy_of import OFParams, OrderFlowStrategy, LONG, SHORT, FLAT  # noqa: E402

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
    p.step_average = 36
    p.step_pyramid = 30
    p.close_half_pct = 60
    p.trail_mode = True
    p.trail_sl_pts = 12
    p.trail_step_pts = 5
    p.trail_debounce_sec = 0.4
    p.partial_tp = True
    p.min_profit_per_lot = 125
    p.pyramid_full_exit = True
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
        self.placed = []
        self.cancelled = []
        self.seq = 0

    def place(self, side, qty, px, tag=""):
        self.seq += 1
        self.placed.append((side, qty, float(px), tag))
        return type("O", (), {"order_id": f"st{self.seq}"})()

    def cancel(self, oid):
        self.cancelled.append(oid)


def pos(s, direction, prices_lots, srcs=None):
    s._dir = direction
    s._lot_queue.clear()
    srcs = srcs or [""] * len(prices_lots)
    for (price, lots), src in zip(prices_lots, srcs):
        s._mk_lot(price, direction, lots)
        s._lot_queue[-1].src = src
    s._total_lots = sum(l for _, l in prices_lots)
    s._peak_lots = s._total_lots
    s._avg_price = sum(p * l for p, l in prices_lots) / max(1, s._total_lots)
    s._entry_price = prices_lots[0][0]


# ---------- 1. порог активации ----------

def t_no_stop_before_spread():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83430, ask=83431, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert not st.placed, f"до +42 стопа нет, got {st.placed}"


def t_exactly_42_no_stop():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83442, ask=83443, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert not st.placed, f"на ровно +42 стоп совпал бы с рынком, got {st.placed}"


def t_stop_on_whole_volume_at_43():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83443, ask=83444, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert len(st.placed) == 1, st.placed
    side, qty, px, tag = st.placed[0]
    assert side == "sell" and qty == 1 and px == 83442.0, st.placed[0]
    assert tag == "of_trail_sl_agg"


def t_first_lot_covers_all():
    """2 лота: первый дошёл до +42 → стоп на ВЕСЬ объём по его уровню."""
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1), (83440, 1)])
    s.sync_trail_stops(bid=83445, ask=83446, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert len(st.placed) == 1, st.placed
    side, qty, px, _ = st.placed[0]
    assert qty == 2, f"стоп на весь объём (2), got {st.placed[0]}"
    assert px == 83442.0, f"уровень = вход лота + 42, got {px}"


def t_three_lots_covered_by_first():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1), (83430, 1), (83460, 1)], srcs=["entry", "avg", "avg"])
    s.sync_trail_stops(bid=83445, ask=83446, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert st.placed and st.placed[-1][1] == 3, st.placed
    assert st.placed[-1][2] == 83442.0, st.placed[-1]


def t_short_side_level():
    s = make_strategy(); st = Stops()
    pos(s, SHORT, [(83400, 1)])
    s.sync_trail_stops(bid=83357, ask=83356, place_stop_fn=st.place, cancel_fn=st.cancel)  # +44
    assert st.placed, "должен встать стоп"
    side, qty, px, _ = st.placed[0]
    assert side == "buy" and px == 83358.0, f"SHORT: уровень = вход-42 = 83358, got {st.placed[0]}"


def t_pyramid_lot_uses_step_pyr():
    s = make_strategy(); st = Stops()
    pos(s, SHORT, [(83077, 1)], srcs=["pyr"])
    s.sync_trail_stops(bid=83047, ask=83046, place_stop_fn=st.place, cancel_fn=st.cancel)  # +31
    assert st.placed, "пирамидный лот взводится на +30"
    assert st.placed[0][2] == 83047.0, f"уровень = вход-30 = 83047, got {st.placed[0]}"


# ---------- 2. пирамида 3+ ----------

def t_pyramid_three_lots_market_level():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1), (83430, 1), (83460, 1)], srcs=["entry", "pyr", "pyr"])
    s._pyramid_levels = 2
    s.sync_trail_stops(bid=83465, ask=83466, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert s._agg_trail is not None, "пирамида 3+ → агрегат сразу"
    assert st.placed[-1][1] == 3 and st.placed[-1][2] == 83453.0, st.placed[-1]


def t_pyramid_two_lots_threshold():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1), (83430, 1)], srcs=["entry", "pyr"])
    s._pyramid_levels = 1
    s.sync_trail_stops(bid=83435, ask=83436, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert s._agg_trail is None, "до +42 (вход) агрегата нет"
    assert not st.placed, st.placed


# ---------- 3. трейлинг ----------

def t_trail_moves_by_step():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83450, ask=83451, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert st.placed[-1][2] == 83442.0, st.placed[-1]
    s._agg_trail["ts"] = time.monotonic() - 10
    s.sync_trail_stops(bid=83453, ask=83454, place_stop_fn=st.place, cancel_fn=st.cancel)  # +3
    assert len(st.placed) == 1, f"шаг 3 не двигает, got {st.placed}"
    s._agg_trail["ts"] = time.monotonic() - 10
    s.sync_trail_stops(bid=83456, ask=83457, place_stop_fn=st.place, cancel_fn=st.cancel)  # +6
    assert len(st.placed) == 2 and st.placed[-1][2] == 83444.0, st.placed
    assert st.cancelled, "старая заявка снята"


def t_trail_never_back():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83460, ask=83461, place_stop_fn=st.place, cancel_fn=st.cancel)
    n = len(st.placed)
    s._agg_trail["ts"] = time.monotonic() - 10
    s.sync_trail_stops(bid=83440, ask=83441, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert len(st.placed) == n, "назад стоп не идёт"


def t_debounce_param():
    s = make_strategy(trail_debounce_sec=0); st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83450, ask=83451, place_stop_fn=st.place, cancel_fn=st.cancel)
    s.sync_trail_stops(bid=83456, ask=83457, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert len(st.placed) == 2, st.placed


def t_qty_change_keeps_tighter():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1), (83430, 1)])
    s.sync_trail_stops(bid=83460, ask=83461, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert st.placed[-1][2] == 83442.0, st.placed[-1]
    s._mk_lot(83490, LONG, 1); s._lot_queue[-1].src = "pyr"; s._total_lots = 3
    s._agg_trail["ts"] = time.monotonic() - 10
    s.sync_trail_stops(bid=83495, ask=83496, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert st.placed[-1][1] == 3, st.placed[-1]
    assert st.placed[-1][2] == 83483.0, f"уровень = рынок-12 = 83483 (плотнее старого), got {st.placed[-1]}"


# ---------- 4. срабатывание ----------

def t_agg_fill_closes_all():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1), (83430, 1), (83460, 1)], srcs=["entry", "pyr", "pyr"])
    s._pyramid_levels = 2
    s.sync_trail_stops(bid=83465, ask=83466, place_stop_fn=st.place, cancel_fn=st.cancel)
    oid = s._agg_trail["order_id"]
    act = s.apply_trail_stop_fill(oid, 83450.0)
    assert act and act["action"] == "close_all" and act["qty"] == 3, act
    assert act["reason"] == "trail_sl_agg" and act.get("entrySide") == LONG
    assert s._dir == FLAT and s._total_lots == 0


def t_fill_preserves_src():
    s = make_strategy()
    pos(s, LONG, [(83400, 1)], srcs=["pyr"])
    uid = s._lot_queue[0].uid
    s.update_fill_price(83405.0, "entry", 83400.0)
    assert s._lot_queue[0].src == "pyr" and s._lot_queue[0].uid == uid, s._lot_queue[0]


# ---------- 5. прочие выходы ----------

def t_tp_full_works():
    from datetime import datetime
    from strategy_of import MSK
    s = make_strategy(min_profit_per_lot=50)
    pos(s, LONG, [(83400, 2)])
    s.update_price(83500)
    act = s._check_exits(83500.0, datetime(2026, 11, 20, 12, 0, tzinfo=MSK))
    assert act and act.get("reason") == "tp_full", act


def t_partial_tp_off():
    s = make_strategy()
    pos(s, LONG, [(83400, 1)])
    s.update_price(83500)
    assert s._check_partial_tp(83500) == [], "partial TP выключен"


def t_catchup_tp_off():
    s = make_strategy()
    pos(s, LONG, [(83400, 1)])
    s.update_price(83600)
    s._blind_since = time.time() - 100
    assert s._catchup_missed_tp(83600) == []
    assert s._scan_missed_tp(83600) == []


# ---------- 6. режим/состояние ----------

def t_mode_off_cancels():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83450, ask=83451, place_stop_fn=st.place, cancel_fn=st.cancel)
    s.p.trail_mode = False
    s.sync_trail_stops(bid=83450, ask=83451, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert not s._trail_stops and not s._agg_trail and st.cancelled, (s._trail_stops, s._agg_trail)


def t_no_position_no_stops():
    s = make_strategy(); st = Stops()
    s._dir = FLAT
    s.sync_trail_stops(bid=83450, ask=83451, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert not st.placed


def t_state_roundtrip():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83450, ask=83451, place_stop_fn=st.place, cancel_fn=st.cancel)
    stt = s.get_state()
    assert stt["trail"]["agg"], "агрегат должен попасть в state"
    s2 = make_strategy()
    s2.load_state(stt)
    assert s2._agg_trail and s2._agg_trail["order_id"] == s._agg_trail["order_id"]


def t_resync_direction():
    s = make_strategy(); pos(s, SHORT, [(83061, 2)])
    s.sync_from_broker({"lots": 2, "dir": -1, "avg_price": 83061.0, "current_price": 83041.0}, full=True)
    assert s._dir == SHORT and s._total_lots == 2, (s._dir, s._total_lots)


print("F-198..F-211 Trailing SL")
check("до +42 стопа нет", t_no_stop_before_spread)
check("ровно +42 — стоп не ставим", t_exactly_42_no_stop)
check("на +43 стоп на весь объём по вход+42", t_stop_on_whole_volume_at_43)
check("первый лот накрывает весь объём", t_first_lot_covers_all)
check("3 лота накрываются первым", t_three_lots_covered_by_first)
check("SHORT: уровень = вход-42", t_short_side_level)
check("пирамидный лот — порог Step Pyr", t_pyramid_lot_uses_step_pyr)
check("пирамида 3+ — агрегат по рынку", t_pyramid_three_lots_market_level)
check("пирамида 2 лота — без агрегата до +42", t_pyramid_two_lots_threshold)
check("трейл шагом 5", t_trail_moves_by_step)
check("трейл не идёт назад", t_trail_never_back)
check("дебаунс параметром", t_debounce_param)
check("долив: уровень плотнее", t_qty_change_keeps_tighter)
check("срабатывание закрывает всё", t_agg_fill_closes_all)
check("филл сохраняет src/uid", t_fill_preserves_src)
check("tp_full работает", t_tp_full_works)
check("partial TP выключен", t_partial_tp_off)
check("догоны тейков выключены", t_catchup_tp_off)
check("выключение галки снимает стопы", t_mode_off_cancels)
check("без позиции стопов нет", t_no_position_no_stops)
check("state переживает рестарт", t_state_roundtrip)
check("ресинк не переворачивает шорт", t_resync_direction)

passed = sum(RESULTS)
print(f"\n{passed}/{len(RESULTS)} PASS")
sys.exit(0 if passed == len(RESULTS) else 1)
