# -*- coding: utf-8 -*-
"""F-198: Trailing SL mode — unit tests (no broker, callbacks are stubs).

Правила (согласованы с Дмитрием 18.11.2026):
  * усреднение, каждый слой: при +Spread(42) по этому лоту ставится СВОЙ стоп на 1 лот
    (LONG Bid−12 / SHORT Ask+12), трейлится шагом 5 только в сторону позиции;
    срабатывание закрывает ТОЛЬКО этот лот;
  * 3+ лота, набранные усреднением → остаются по-лотовые;
  * Close % All (остаток <= max(2, peak*pct/100) после первых закрытий стопами) →
    все по-лотовые снимаются, один стоп на весь остаток;
  * пирамида с 3 лотов → сразу один стоп на весь объём;
  * в режиме отключены partial TP по spread и tp_full по min_profit_per_lot.

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
    p.partial_tp = True
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
    """Заглушка брокера: помнит place/cancel."""

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
    """prices_lots = [(price, lots), ...] → очередь слоёв; srcs = ['entry','avg','pyr',...]."""
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


# ---------- 1. активация по лоту ----------

def t_no_stop_before_spread():
    s = make_strategy()
    st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83430, ask=83431, place_stop_fn=st.place, cancel_fn=st.cancel)  # +30
    assert not st.placed, f"до +spread стопа быть не должно, got {st.placed}"


def t_stop_armed_at_spread_long():
    s = make_strategy()
    st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83442, ask=83443, place_stop_fn=st.place, cancel_fn=st.cancel)  # +42
    assert len(st.placed) == 1, f"ожидался 1 стоп, got {st.placed}"
    side, qty, px, tag = st.placed[0]
    assert side == "sell" and qty == 1 and px == 83430.0, f"LONG стоп неверный: {st.placed[0]}"
    assert tag == "of_trail_sl"


def t_stop_armed_at_spread_short():
    s = make_strategy()
    st = Stops()
    pos(s, SHORT, [(83400, 1)])
    s.sync_trail_stops(bid=83357, ask=83358, place_stop_fn=st.place, cancel_fn=st.cancel)  # +42 вниз
    assert len(st.placed) == 1, f"ожидался 1 стоп, got {st.placed}"
    side, qty, px, _ = st.placed[0]
    assert side == "buy" and px == 83370.0, f"SHORT стоп неверный: {st.placed[0]}"


# ---------- 2. трейлинг ----------

def t_trail_moves_by_step():
    s = make_strategy()
    st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83450, ask=83451, place_stop_fn=st.place, cancel_fn=st.cancel)
    first = st.placed[-1][2]
    assert first == 83438.0, first
    # шаг меньше 5 пт — перестановки нет (сдвигаем ts, чтобы пройти дебаунс)
    s._trail_stops[list(s._trail_stops)[0]]["ts"] = time.monotonic() - 10
    s.sync_trail_stops(bid=83453, ask=83454, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert len(st.placed) == 1, f"шаг 3 пт не должен двигать стоп, got {st.placed}"
    # шаг >= 5 пт — двигаем
    s._trail_stops[list(s._trail_stops)[0]]["ts"] = time.monotonic() - 10
    s.sync_trail_stops(bid=83456, ask=83457, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert len(st.placed) == 2, f"шаг 6 пт должен двигать стоп, got {st.placed}"
    assert st.placed[-1][2] == 83444.0, st.placed[-1]
    assert st.cancelled, "старая заявка должна быть снята"


def t_trail_never_moves_back():
    s = make_strategy()
    st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83460, ask=83461, place_stop_fn=st.place, cancel_fn=st.cancel)
    n = len(st.placed)
    uid = list(s._trail_stops)[0]
    s._trail_stops[uid]["ts"] = time.monotonic() - 10
    s.sync_trail_stops(bid=83440, ask=83441, place_stop_fn=st.place, cancel_fn=st.cancel)  # назад
    assert len(st.placed) == n, "стоп не должен двигаться назад"


# ---------- 3. по-лотовые стопы при усреднении ----------

def t_two_lots_each_own_stop():
    s = make_strategy()
    st = Stops()
    pos(s, LONG, [(83400, 1), (83364, 1)])   # вход + усреднение
    s.sync_trail_stops(bid=83445, ask=83446, place_stop_fn=st.place, cancel_fn=st.cancel)
    # первый слой +45 → стоп; второй +81 → тоже стоп; итого 2 заявки по 1 лоту
    assert len(st.placed) == 2, f"ожидались 2 по-лотовых стопа, got {st.placed}"
    assert all(p[1] == 1 for p in st.placed), f"каждый стоп на 1 лот, got {st.placed}"


def t_three_lots_by_averaging_stay_per_lot():
    s = make_strategy()
    st = Stops()
    pos(s, LONG, [(83400, 1), (83364, 1), (83328, 1)])   # 3 лота усреднением, пирамид нет
    s.sync_trail_stops(bid=83445, ask=83446, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert len(st.placed) == 3, f"при усреднении остаются по-лотовые, got {st.placed}"
    assert not s._agg_trail, "агрегата быть не должно"


# ---------- 4. Close % All ----------

def t_close_pct_aggregates():
    s = make_strategy(close_half_pct=60)
    st = Stops()
    pos(s, LONG, [(83300, 1), (83330, 1), (83360, 1)])   # peak=3, threshold=max(2,1)=2
    s._trail_closed_lots = 1                              # один лот уже закрыт стопом
    s._total_lots = 2
    s._lot_queue.pop()                                    # осталось 2 слоя
    s.sync_trail_stops(bid=83420, ask=83421, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert s._agg_trail is not None, "должен быть агрегированный стоп"
    assert st.placed[-1][1] == 2, f"агрегат на весь остаток (2), got {st.placed}"
    assert st.placed[-1][3] == "of_trail_sl_agg"


def t_close_pct_not_before_closures():
    s = make_strategy(close_half_pct=60)
    st = Stops()
    pos(s, LONG, [(83300, 1), (83330, 1), (83360, 1)])
    s.sync_trail_stops(bid=83420, ask=83421, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert s._agg_trail is None, "без закрытий стопами агрегата быть не должно"


# ---------- 5. пирамида ----------

def t_pyramid_three_lots_aggregate_immediately():
    s = make_strategy()
    st = Stops()
    pos(s, LONG, [(83400, 1), (83430, 1), (83460, 1)])
    s._pyramid_levels = 2
    s.sync_trail_stops(bid=83465, ask=83466, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert s._agg_trail is not None, "пирамида 3+ → агрегат сразу"
    assert st.placed[-1][1] == 3, f"агрегат на 3 лота, got {st.placed}"
    assert st.placed[-1][3] == "of_trail_sl_agg"


def t_pyramid_two_lots_per_lot():
    s = make_strategy()
    st = Stops()
    pos(s, LONG, [(83400, 1), (83364, 1)])   # оба слоя уже в плюсе > spread
    s._pyramid_levels = 1
    s.sync_trail_stops(bid=83445, ask=83446, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert s._agg_trail is None, "2 лота пирамиды — по-лотово"
    assert len(st.placed) == 2, f"2 по-лотовых стопа, got {st.placed}"


# ---------- 6. срабатывание ----------

def t_lot_fill_closes_only_that_lot():
    s = make_strategy()
    st = Stops()
    pos(s, LONG, [(83400, 1), (83364, 1)])
    s.sync_trail_stops(bid=83445, ask=83446, place_stop_fn=st.place, cancel_fn=st.cancel)
    oid = list(s._trail_stops.values())[0]["order_id"]
    act = s.apply_trail_stop_fill(oid, 83420.0)
    assert act and act["action"] == "partial_tp" and act["qty"] == 1, act
    assert act["reason"] == "trail_sl"
    assert s._total_lots == 1, f"должен остаться 1 лот, got {s._total_lots}"
    assert len(s._lot_queue) == 1, s._lot_queue
    assert s._dir == LONG, "позиция не закрыта полностью"


def t_agg_fill_closes_all():
    s = make_strategy()
    st = Stops()
    pos(s, LONG, [(83400, 1), (83430, 1), (83460, 1)])
    s._pyramid_levels = 2
    s.sync_trail_stops(bid=83465, ask=83466, place_stop_fn=st.place, cancel_fn=st.cancel)
    oid = s._agg_trail["order_id"]
    act = s.apply_trail_stop_fill(oid, 83450.0)
    assert act and act["action"] == "close_all", act
    assert act["qty"] == 3 and act["reason"] == "trail_sl_agg"
    assert s._dir == FLAT and s._total_lots == 0


# ---------- 7. отключение старых выходов ----------

def t_partial_tp_disabled():
    s = make_strategy()
    pos(s, LONG, [(83400, 1)])
    s.update_price(83500)
    assert s._check_partial_tp(83500) == [], "partial TP должен быть отключён"


def t_tp_full_disabled():
    s = make_strategy()
    pos(s, LONG, [(83400, 1)])
    from datetime import datetime
    from strategy_of import MSK
    acts = s._check_exits(83600.0, datetime(2026, 11, 18, 15, 0, tzinfo=MSK))
    assert acts is None, f"tp_full должен быть отключён в режиме, got {acts}"


def t_legacy_mode_unchanged():
    s = make_strategy(trail_mode=False)
    st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83450, ask=83451, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert not st.placed, "при выключенной галке стопы не ставим"
    s.update_price(83450)
    assert s._check_partial_tp(83450), "в старом режиме partial TP работает"


# ---------- 8. режим выключен / позиции нет ----------

def t_mode_off_cancels_stops():
    s = make_strategy()
    st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83450, ask=83451, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert s._trail_stops
    s.p.trail_mode = False
    s.sync_trail_stops(bid=83450, ask=83451, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert not s._trail_stops and not s._agg_trail, "при выключении всё снимается"
    assert st.cancelled, st.cancelled


def t_no_position_no_stops():
    s = make_strategy()
    st = Stops()
    s._dir = FLAT
    s.sync_trail_stops(bid=83450, ask=83451, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert not st.placed


# ---------- 9. state ----------

def t_state_roundtrip():
    s = make_strategy()
    st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83450, ask=83451, place_stop_fn=st.place, cancel_fn=st.cancel)
    stt = s.get_state()
    assert stt["trail"]["stops"], "реестр должен попасть в state"
    s2 = make_strategy()
    s2.load_state(stt)
    assert s2._trail_stops, "реестр должен восстановиться"
    uid = list(s2._trail_stops)[0]
    assert s2._trail_stops[uid]["order_id"] == list(s._trail_stops.values())[0]["order_id"]


# ---------- 10. F-200: ресинк не должен переворачивать направление ----------

def t_resync_short_stays_short():
    s = make_strategy()
    pos(s, SHORT, [(83061, 2)])
    s.sync_from_broker({"lots": 2, "dir": -1, "avg_price": 83061.0, "current_price": 83041.0}, full=True)
    assert s._dir == SHORT, f"после ресинка шорт должен остаться шортом, got dir={s._dir}"
    assert s._total_lots == 2, s._total_lots


def t_resync_long_stays_long():
    s = make_strategy()
    pos(s, LONG, [(83061, 3)])
    s.sync_from_broker({"lots": 3, "dir": 1, "avg_price": 83061.0, "current_price": 83070.0}, full=True)
    assert s._dir == LONG and s._total_lots == 3, (s._dir, s._total_lots)


def t_resync_flat():
    s = make_strategy()
    pos(s, SHORT, [(83061, 2)])
    s.sync_from_broker({"lots": 0, "dir": 0, "avg_price": 0.0, "current_price": 83041.0}, full=True)
    assert s._dir == FLAT and s._total_lots == 0


# ---------- 11. F-201: порог активации по типу позиции ----------

def t_pyramid_lot_arms_at_step_pyr():
    """Пирамидный лот взводится на +Step Pyr (30), не дожидаясь +Spread (42)."""
    s = make_strategy()          # spread=42, step_pyramid=30
    st = Stops()
    pos(s, SHORT, [(83077, 1)], srcs=["pyr"])
    s.sync_trail_stops(bid=83046, ask=83045, place_stop_fn=st.place, cancel_fn=st.cancel)  # +32
    assert len(st.placed) == 1, f"пирамидный лот должен взвестись на +30, got {st.placed}"


def t_pyramid_lot_waits_below_step_pyr():
    s = make_strategy()
    st = Stops()
    pos(s, SHORT, [(83077, 1)], srcs=["pyr"])
    s.sync_trail_stops(bid=83049, ask=83048, place_stop_fn=st.place, cancel_fn=st.cancel)  # +29
    assert not st.placed, f"до +30 стопа быть не должно, got {st.placed}"


def t_averaging_lot_not_armed_at_30():
    s = make_strategy()
    st = Stops()
    pos(s, SHORT, [(83077, 1)], srcs=["avg"])
    s.sync_trail_stops(bid=83046, ask=83045, place_stop_fn=st.place, cancel_fn=st.cancel)  # +32
    assert not st.placed, f"усреднение ждёт +42, got {st.placed}"


def t_averaging_lot_armed_at_spread():
    s = make_strategy()
    st = Stops()
    pos(s, SHORT, [(83077, 1)], srcs=["avg"])
    s.sync_trail_stops(bid=83036, ask=83035, place_stop_fn=st.place, cancel_fn=st.cancel)  # +42
    assert len(st.placed) == 1, f"усреднение взводится на +42, got {st.placed}"


def t_entry_lot_uses_spread():
    s = make_strategy()
    st = Stops()
    pos(s, LONG, [(83400, 1)], srcs=["entry"])
    s.sync_trail_stops(bid=83432, ask=83433, place_stop_fn=st.place, cancel_fn=st.cancel)  # +32
    assert not st.placed, f"входной лот ждёт +42, got {st.placed}"


# ---------- 12. F-202: дебаунс параметром + не откатывать уровень ----------

def t_debounce_param_zero():
    s = make_strategy(trail_debounce_sec=0)
    st = Stops()
    pos(s, LONG, [(83400, 1)])
    s.sync_trail_stops(bid=83450, ask=83451, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert len(st.placed) == 1
    s.sync_trail_stops(bid=83456, ask=83457, place_stop_fn=st.place, cancel_fn=st.cancel)  # +6
    assert len(st.placed) == 2, f"при debounce=0 перестановка сразу, got {st.placed}"


def t_agg_qty_change_tightens():
    s = make_strategy()
    st = Stops()
    pos(s, LONG, [(83400, 1), (83420, 1), (83440, 1)])
    s._pyramid_levels = 2
    s.sync_trail_stops(bid=83460, ask=83461, place_stop_fn=st.place, cancel_fn=st.cancel)
    first = st.placed[-1][2]
    assert first == 83448.0, first
    # долив → qty 4, цена выше: уровень должен стать выше (плотнее), не ниже
    s._mk_lot(83470, LONG, 1); s._lot_queue[-1].src = "pyr"
    s._total_lots = 4
    s.sync_trail_stops(bid=83480, ask=83481, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert st.placed[-1][1] == 4, st.placed[-1]
    assert st.placed[-1][2] == 83468.0, f"новый уровень 83480-12, got {st.placed[-1]}"


def t_agg_qty_change_no_backtrack():
    s = make_strategy()
    st = Stops()
    pos(s, LONG, [(83400, 1), (83420, 1), (83440, 1)])
    s._pyramid_levels = 2
    s.sync_trail_stops(bid=83460, ask=83461, place_stop_fn=st.place, cancel_fn=st.cancel)
    # цена ушла вниз, добавляем лот: новый уровень 83408 ниже старого 83448 → не откатываем
    s._mk_lot(83410, LONG, 1); s._lot_queue[-1].src = "pyr"
    s._total_lots = 4
    n = len(st.placed)
    s.sync_trail_stops(bid=83420, ask=83421, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert len(st.placed) == n, f"уровень не должен откатываться назад, got {st.placed[n:]}"


print("F-198 Trailing SL")
check("до +spread стопа нет", t_no_stop_before_spread)
check("LONG: стоп на +spread (Bid-12)", t_stop_armed_at_spread_long)
check("SHORT: стоп на +spread (Ask+12)", t_stop_armed_at_spread_short)
check("трейл шагом 5", t_trail_moves_by_step)
check("трейл не двигается назад", t_trail_never_moves_back)
check("2 лота усреднения — 2 по-лотовых стопа", t_two_lots_each_own_stop)
check("3 лота усреднением — остаются по-лотовые", t_three_lots_by_averaging_stay_per_lot)
check("Close % All → агрегат на остаток", t_close_pct_aggregates)
check("без закрытий стопами агрегата нет", t_close_pct_not_before_closures)
check("пирамида 3+ → агрегат сразу", t_pyramid_three_lots_aggregate_immediately)
check("пирамида 2 лота — по-лотово", t_pyramid_two_lots_per_lot)
check("срабатывание 1 стопа закрывает 1 лот", t_lot_fill_closes_only_that_lot)
check("срабатывание агрегата закрывает всё", t_agg_fill_closes_all)
check("partial TP отключён в режиме", t_partial_tp_disabled)
check("tp_full отключён в режиме", t_tp_full_disabled)
check("старый режим не тронут", t_legacy_mode_unchanged)
check("выключение галки снимает стопы", t_mode_off_cancels_stops)
check("без позиции стопов нет", t_no_position_no_stops)
check("реестр переживает рестарт", t_state_roundtrip)
check("F-200 ресинк шорта остаётся шортом", t_resync_short_stays_short)
check("F-200 ресинк лонга остаётся лонгом", t_resync_long_stays_long)
check("F-200 ресинк во FLAT", t_resync_flat)
check("F-201 пирамида: стоп на +30", t_pyramid_lot_arms_at_step_pyr)
check("F-201 пирамида: до +30 стопа нет", t_pyramid_lot_waits_below_step_pyr)
check("F-201 усреднение: на +32 стопа нет", t_averaging_lot_not_armed_at_30)
check("F-201 усреднение: стоп на +42", t_averaging_lot_armed_at_spread)
check("F-201 вход: порог Spread", t_entry_lot_uses_spread)
check("F-202 дебаунс параметром", t_debounce_param_zero)
check("F-202 долив: уровень плотнее", t_agg_qty_change_tightens)
check("F-202 долив: без отката назад", t_agg_qty_change_no_backtrack)

passed = sum(RESULTS)
print(f"\n{passed}/{len(RESULTS)} PASS")
sys.exit(0 if passed == len(RESULTS) else 1)
