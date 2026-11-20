# -*- coding: utf-8 -*-
"""F-198..F-212: режим Trailing SL — unit-тесты (no broker, колбэки-заглушки).

ИТОГОВЫЕ ПРАВИЛА (согласованы с Дмитрием):
  * partial TP по Spread и догоны тейков выключены; tp_full (Min Profit/Lot) работает;
  * КАЖДЫЙ лот при достижении своего порога (Spread для входа/усреднения,
    Step Pyr для пирамидного лота) получает СВОЙ стоп на 1 лот по уровню
    «вход этого лота + порог» (LONG: вход+42, SHORT: вход−42);
  * стоп ставится только когда рынок уже за уровнем, дальше трейлится шагом,
    только в сторону позиции;
  * как только часть лотов закрылась стопами (closedLots > 0) и остаток снова
    доходит до +42 — остаток накрывается ОДНИМ стопом на весь объём по правилу
    Close % All (уровень = вход квалифицированного лота + порог);
  * пирамида 3+ лота (pyramid_levels>=2) — стоп на весь объём сразу, по рынку.

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


def sync(s, bid, ask, st):
    s.sync_trail_stops(bid=bid, ask=ask, place_stop_fn=st.place, cancel_fn=st.cancel)


# ---------- 1. по-лотовые стопы ----------

def t_no_stop_before_spread():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1)])
    sync(s, 83430, 83431, st)
    assert not st.placed, st.placed


def t_exactly_42_no_stop():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1)])
    sync(s, 83442, 83443, st)
    assert not st.placed, f"на ровно +42 стоп совпал бы с рынком: {st.placed}"


def t_one_lot_stop_at_43():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1)])
    sync(s, 83443, 83444, st)
    assert len(st.placed) == 1, st.placed
    side, qty, px, tag = st.placed[0]
    assert side == "sell" and qty == 1 and px == 83442.0, st.placed[0]
    assert tag == "of_trail_sl", f"это по-лотовый стоп, not agg: {st.placed[0]}"


def t_two_lots_own_stops():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1), (83364, 1)])
    sync(s, 83445, 83446, st)
    assert len(st.placed) == 2, st.placed
    assert all(p[1] == 1 for p in st.placed), st.placed
    assert s._agg_trail is None, "до закрытий агрегата быть не должно"


def t_three_lots_still_per_lot():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1), (83364, 1), (83328, 1)], srcs=["entry", "avg", "avg"])
    sync(s, 83445, 83446, st)
    assert len(st.placed) == 3 and all(p[1] == 1 for p in st.placed), st.placed
    assert s._agg_trail is None


def t_short_per_lot_level():
    s = make_strategy(); st = Stops()
    pos(s, SHORT, [(83400, 1)])
    sync(s, 83357, 83356, st)
    assert st.placed and st.placed[0][0] == "buy" and st.placed[0][2] == 83358.0, st.placed


def t_pyramid_lot_threshold():
    s = make_strategy(); st = Stops()
    pos(s, SHORT, [(83077, 1)], srcs=["pyr"])
    sync(s, 83047, 83046, st)
    assert st.placed and st.placed[0][2] == 83047.0, st.placed


# ---------- 2. агрегат после закрытий ----------

def t_agg_after_closure():
    """После первого закрытия остаток дошёл до +42 → один стоп на весь остаток."""
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83300, 1), (83360, 1)])
    s._trail_closed_lots = 1
    sync(s, 83410, 83411, st)
    assert s._agg_trail is not None, "агрегат должен встать"
    assert st.placed[-1][1] == 2, f"на весь остаток (2), got {st.placed[-1]}"
    assert st.placed[-1][2] == 83402.0, f"уровень = вход 83360 + 42, got {st.placed[-1]}"
    assert st.placed[-1][3] == "of_trail_sl_agg"


def t_agg_not_without_closure():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83300, 1), (83360, 1)])
    s._trail_closed_lots = 0
    sync(s, 83410, 83411, st)
    assert s._agg_trail is None, "без закрытий — только по-лотово"
    assert len(st.placed) == 2 and all(p[1] == 1 for p in st.placed), st.placed


def t_agg_not_before_remainder_42():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1), (83430, 1)])
    s._trail_closed_lots = 1
    sync(s, 83410, 83411, st)
    assert s._agg_trail is None, "остаток не дошёл до +42 — агрегата нет"
    assert not st.placed, st.placed


def t_pyramid_three_market():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1), (83430, 1), (83460, 1)], srcs=["entry", "pyr", "pyr"])
    s._pyramid_levels = 2
    sync(s, 83465, 83466, st)
    assert s._agg_trail is not None and st.placed[-1][1] == 3, st.placed
    assert st.placed[-1][2] == 83453.0, st.placed[-1]


# ---------- 3. трейлинг ----------

def t_trail_moves_by_step():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1)])
    sync(s, 83450, 83451, st)
    uid = list(s._trail_stops)[0]
    assert s._trail_stops[uid]["stop_px"] == 83442.0, s._trail_stops[uid]
    s._trail_stops[uid]["ts"] = time.monotonic() - 10
    sync(s, 83453, 83454, st)
    assert len(st.placed) == 1, f"шаг 3 не двигает: {st.placed}"
    s._trail_stops[uid]["ts"] = time.monotonic() - 10
    sync(s, 83456, 83457, st)
    assert len(st.placed) == 2 and st.placed[-1][2] == 83444.0, st.placed
    assert st.cancelled


def t_trail_never_back():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1)])
    sync(s, 83460, 83461, st)
    n = len(st.placed)
    uid = list(s._trail_stops)[0]
    s._trail_stops[uid]["ts"] = time.monotonic() - 10
    sync(s, 83440, 83441, st)
    assert len(st.placed) == n, "назад стоп не идёт"


def t_debounce_param():
    s = make_strategy(trail_debounce_sec=0); st = Stops()
    pos(s, LONG, [(83400, 1)])
    sync(s, 83450, 83451, st)
    sync(s, 83456, 83457, st)
    assert len(st.placed) == 2, st.placed


# ---------- 4. срабатывания ----------

def t_lot_fill_closes_one():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1), (83364, 1)])
    sync(s, 83445, 83446, st)
    oid = list(s._trail_stops.values())[0]["order_id"]
    act = s.apply_trail_stop_fill(oid, 83420.0)
    assert act and act["action"] == "partial_tp" and act["qty"] == 1, act
    assert act["reason"] == "trail_sl"
    assert s._total_lots == 1 and s._dir == LONG, (s._total_lots, s._dir)


def t_agg_fill_closes_all():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1), (83430, 1), (83460, 1)], srcs=["entry", "pyr", "pyr"])
    s._pyramid_levels = 2
    sync(s, 83465, 83466, st)
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
    assert s._lot_queue[0].src == "pyr" and s._lot_queue[0].uid == uid


# ---------- 5. прочие выходы ----------

def t_tp_full_works():
    from datetime import datetime
    from strategy_of import MSK
    s = make_strategy(min_profit_per_lot=50)
    pos(s, LONG, [(83400, 2)])
    s.update_price(83500)
    act = s._check_exits(83500.0, datetime(2026, 11, 20, 12, 0, tzinfo=MSK))
    assert act and act.get("reason") == "tp_full", act


def t_partial_and_catchup_off():
    s = make_strategy()
    pos(s, LONG, [(83400, 1)])
    s.update_price(83600)
    assert s._check_partial_tp(83600) == []
    s._blind_since = time.time() - 100
    assert s._catchup_missed_tp(83600) == []
    assert s._scan_missed_tp(83600) == []


# ---------- 6. режим/состояние ----------

def t_mode_off_cancels():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1)])
    sync(s, 83450, 83451, st)
    s.p.trail_mode = False
    sync(s, 83450, 83451, st)
    assert not s._trail_stops and not s._agg_trail and st.cancelled


def t_no_position_no_stops():
    s = make_strategy(); st = Stops()
    s._dir = FLAT
    sync(s, 83450, 83451, st)
    assert not st.placed


def t_state_roundtrip():
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1)])
    sync(s, 83450, 83451, st)
    stt = s.get_state()
    assert stt["trail"]["stops"], "реестр по-лотовых должен попасть в state"
    s2 = make_strategy()
    s2.load_state(stt)
    assert s2._trail_stops, "реестр должен восстановиться"
    uid = list(s2._trail_stops)[0]
    assert s2._trail_stops[uid]["order_id"] == list(s._trail_stops.values())[0]["order_id"]


def t_resync_direction():
    s = make_strategy(); pos(s, SHORT, [(83061, 2)])
    s.sync_from_broker({"lots": 2, "dir": -1, "avg_price": 83061.0, "current_price": 83041.0}, full=True)
    assert s._dir == SHORT and s._total_lots == 2


def t_pyramid_two_lots_one_stop():
    """F-213: пирамида на 2 лотах — стоп только на 1 лот (первый)."""
    s = make_strategy(); st = Stops()
    pos(s, LONG, [(83400, 1), (83436, 1)], srcs=["entry", "pyr"])
    s._pyramid_levels = 1
    sync(s, 83440, 83441, st)
    assert len(st.placed) == 1, f"только один стоп, got {st.placed}"
    side, qty, px, tag = st.placed[0]
    assert qty == 1, f"на 1 лот, got {qty}"
    assert px == 83430.0, f"уровень = вход+Step Pyr = 83430, got {px}"
    assert s._agg_trail is None, "агрегата на 2 лотах быть не должно"


print("F-198..F-212 Trailing SL")
check("до +42 стопа нет", t_no_stop_before_spread)
check("ровно +42 — стоп не ставим", t_exactly_42_no_stop)
check("на +43 стоп на 1 лот по вход+42", t_one_lot_stop_at_43)
check("2 лота — у каждого свой стоп", t_two_lots_own_stops)
check("3 лота — пока по-лотово", t_three_lots_still_per_lot)
check("SHORT: уровень вход−42", t_short_per_lot_level)
check("пирамидный лот — порог Step Pyr", t_pyramid_lot_threshold)
check("F-213 пирамида 2 лота — стоп на 1 лот", t_pyramid_two_lots_one_stop)
check("F-212 агрегат после закрытия", t_agg_after_closure)
check("без закрытий агрегата нет", t_agg_not_without_closure)
check("остаток не дошёл до +42 — нет агрегата", t_agg_not_before_remainder_42)
check("пирамида 3+ — агрегат по рынку", t_pyramid_three_market)
check("трейл шагом 5", t_trail_moves_by_step)
check("трейл не идёт назад", t_trail_never_back)
check("дебаунс параметром", t_debounce_param)
check("филл закрывает 1 лот", t_lot_fill_closes_one)
check("агрегат закрывает всё", t_agg_fill_closes_all)
check("филл сохраняет src/uid", t_fill_preserves_src)
check("tp_full работает", t_tp_full_works)
check("partial/catchup выключены", t_partial_and_catchup_off)
check("выключение галки снимает стопы", t_mode_off_cancels)
check("без позиции стопов нет", t_no_position_no_stops)
check("state переживает рестарт", t_state_roundtrip)
check("ресинк не переворачивает шорт", t_resync_direction)

passed = sum(RESULTS)
print(f"\n{passed}/{len(RESULTS)} PASS")
sys.exit(0 if passed == len(RESULTS) else 1)
