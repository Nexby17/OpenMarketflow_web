# -*- coding: utf-8 -*-
"""F-193: стопы на уровни пирамидинга — regression tests (no broker).

Инцидент 11.11.2026 (Si 5080, 15:25-15:29):
  PYRAMID sell 1 → позиция 2 лота (1 вход + 1 пирамида, lvl 1/5), но брокерский
  стоп не ставился: sync_pyramid_broker_stop требовал _total_lots >= 3.
  Стоп появился только на 3-м лоте (15:29:33), т.е. 4 минуты позиция была без защиты.

Дополнительно найдено и исправлено:
  - _last_pyr_entry_price не сохранялся в state → после рестарта позиция с
    пирамидами оставалась без стопа (уровень неизвестен);
  - sync_from_broker(full=True) (F-095) стирал _pyramid_levels/_last_pyr_entry_price
    при ЛЮБОМ расхождении → после ресинка стоп пропадал.

Run: cwd=robot, python test_of_pyramid_stop_levels.py
"""
import sys
import os

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
    p.step_pyramid = 25
    p.step_atr = False
    p.pyramid_full_exit = True
    p.max_pyramid_levels = 5
    for k, v in overrides.items():
        setattr(p, k, v)
    return OrderFlowStrategy(p)


class Stopper:
    """Заглушка брокерских вызовов: запоминает place/cancel."""

    def __init__(self):
        self.placed = []
        self.cancelled = []

    def place(self, side, qty, px, tag=""):
        self.placed.append((side, qty, round(px, 2), tag))
        return type("O", (), {"order_id": f"stop-{len(self.placed)}"})()

    def cancel(self, oid):
        self.cancelled.append(oid)


def set_pyr_position(s, direction, lots, levels, last_pyr, avg=None):
    s._dir = direction
    s._total_lots = lots
    s._pyramid_levels = levels
    s._last_pyr_entry_price = last_pyr
    s._avg_price = avg if avg is not None else last_pyr


# ---------- 1. два лота (вход + 1 пирамида) должны быть защищены ----------

def t_two_lots_short_gets_stop():
    s = make_strategy()
    st = Stopper()
    set_pyr_position(s, SHORT, 2, 1, 83337.5)
    s.sync_pyramid_broker_stop(83337.5, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert len(st.placed) == 1, f"stop must be placed at 2 lots (1 pyramid), got {st.placed}"
    side, qty, px, tag = st.placed[0]
    assert side == "buy", f"SHORT closes with buy, got {side}"
    assert qty == 2, f"qty must be total lots (2), got {qty}"
    assert px == 83362.5, f"stop must be last_pyr + step (83362.5), got {px}"
    assert tag == "pyr_full_exit_stop"


def t_two_lots_long_gets_stop():
    s = make_strategy()
    st = Stopper()
    set_pyr_position(s, LONG, 2, 1, 83337.5)
    s.sync_pyramid_broker_stop(83337.5, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert len(st.placed) == 1, f"stop must be placed, got {st.placed}"
    side, qty, px, _ = st.placed[0]
    assert side == "sell" and qty == 2 and px == 83312.5, f"LONG stop wrong: {st.placed[0]}"


def t_three_lots_still_works():
    s = make_strategy()
    st = Stopper()
    set_pyr_position(s, SHORT, 3, 2, 83337.5)
    s.sync_pyramid_broker_stop(83337.5, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert st.placed and st.placed[0][1] == 3, f"regression: 3 lots must still be placed, got {st.placed}"


# ---------- 2. без пирамид стопа нет ----------

def t_no_pyramid_no_stop():
    s = make_strategy()
    st = Stopper()
    set_pyr_position(s, SHORT, 2, 0, 0.0)
    s.sync_pyramid_broker_stop(83337.5, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert not st.placed, f"no pyramid → no stop, got {st.placed}"


def t_rule_off_no_stop():
    s = make_strategy(pyramid_full_exit=False)
    st = Stopper()
    set_pyr_position(s, SHORT, 3, 2, 83337.5)
    s.sync_pyramid_broker_stop(83337.5, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert not st.placed, f"rule off → no stop, got {st.placed}"


def t_existing_stop_cancelled_when_no_pyramid():
    s = make_strategy()
    st = Stopper()
    set_pyr_position(s, SHORT, 2, 0, 0.0)
    s._pyr_stop_order_id = "old-1"
    s._pyr_stop_price = 83362.5
    s._pyr_stop_qty = 2
    s.sync_pyramid_broker_stop(83337.5, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert st.cancelled == ["old-1"], f"stale stop must be cancelled, got {st.cancelled}"


def t_stop_not_replaced_when_same():
    s = make_strategy()
    st = Stopper()
    set_pyr_position(s, SHORT, 2, 1, 83337.5)
    s._pyr_stop_order_id = "keep-1"
    s._pyr_stop_price = 83362.5
    s._pyr_stop_qty = 2
    s.sync_pyramid_broker_stop(83337.5, place_stop_fn=st.place, cancel_fn=st.cancel)
    assert not st.placed and not st.cancelled, f"same level/qty → no churn, got {st.placed}/{st.cancelled}"


# ---------- 3. ресинк с брокером не должен терять пирамиды ----------

def t_resync_same_dir_keeps_pyramid():
    s = make_strategy()
    set_pyr_position(s, LONG, 3, 2, 83400.0)
    s.sync_from_broker({"lots": 3, "dir": 1, "avg_price": 83410.0, "current_price": 83410.0}, full=True)
    assert s._pyramid_levels == 2, f"pyramid levels must survive resync, got {s._pyramid_levels}"
    assert s._last_pyr_entry_price == 83400.0, f"last pyr price must survive, got {s._last_pyr_entry_price}"


def t_resync_to_flat_clears_pyramid():
    s = make_strategy()
    set_pyr_position(s, LONG, 3, 2, 83400.0)
    s.sync_from_broker({"lots": 0, "dir": 0, "avg_price": 0.0, "current_price": 83410.0}, full=True)
    assert s._pyramid_levels == 0 and s._last_pyr_entry_price == 0.0, "FLAT resync must clear pyramid state"


def t_resync_dir_flip_clears_pyramid():
    s = make_strategy()
    set_pyr_position(s, LONG, 3, 2, 83400.0)
    s.sync_from_broker({"lots": -3, "dir": -1, "avg_price": 83410.0, "current_price": 83410.0}, full=True)
    assert s._pyramid_levels == 0, f"direction flip must clear pyramid levels, got {s._pyramid_levels}"


# ---------- 4. state переживает рестарт ----------

def t_state_roundtrip_keeps_last_pyr():
    s = make_strategy()
    set_pyr_position(s, SHORT, 3, 2, 83337.5)
    st = s.get_state()
    assert st.get("lastPyrEntryPrice") == 83337.5, f"state must persist last pyr price, got {st.get('lastPyrEntryPrice')}"
    s2 = make_strategy()
    s2.load_state(st)
    assert s2._pyramid_levels == 2 and s2._last_pyr_entry_price == 83337.5, "restart must keep pyramid state"


def t_state_migration_old_state():
    s = make_strategy()
    old = {"dir": -1, "totalLots": 3, "pyramidLevels": 1,
           "lastPyramidPrice": 83337.5, "avgPrice": 83344.0}
    s.load_state(old)
    assert s._last_pyr_entry_price == 83337.5, f"old state fallback must use lastPyramidPrice, got {s._last_pyr_entry_price}"


print("F-193 pyramid stop levels")
check("2 lots (entry+1 pyramid) SHORT gets stop", t_two_lots_short_gets_stop)
check("2 lots LONG gets stop", t_two_lots_long_gets_stop)
check("3 lots still protected", t_three_lots_still_works)
check("no pyramid -> no stop", t_no_pyramid_no_stop)
check("rule off -> no stop", t_rule_off_no_stop)
check("stale stop cancelled when pyramid gone", t_existing_stop_cancelled_when_no_pyramid)
check("same level/qty -> no churn", t_stop_not_replaced_when_same)
check("resync same dir keeps pyramid state", t_resync_same_dir_keeps_pyramid)
check("resync FLAT clears pyramid state", t_resync_to_flat_clears_pyramid)
check("resync direction flip clears pyramid", t_resync_dir_flip_clears_pyramid)
check("state round-trip keeps last pyr price", t_state_roundtrip_keeps_last_pyr)
check("old state migration fallback", t_state_migration_old_state)

passed = sum(RESULTS)
print(f"\n{passed}/{len(RESULTS)} PASS")
sys.exit(0 if passed == len(RESULTS) else 1)
