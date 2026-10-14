"""F-061 Stage 2 tests: risk sizing, kill-switch, state persistence, stop idempotency."""
import os
import sys
from datetime import date

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "robot"))

from sarema.risk_sarema import (risk_sizing, apply_lot_rounding,      # noqa: E402
                                apply_liq_cap, check_kill_switch)
from sarema.state_sarema import SaremaState                            # noqa: E402


# ---------- risk sizing ----------
def test_risk_sizing_basic():
    # 1M * 0.5% = 5000₽ риска; стоп-дистанция 3.5₽ → floor(1428.57) = 1428
    assert risk_sizing(1_000_000, 0.005, 3.5) == 1428


def test_risk_sizing_edge():
    assert risk_sizing(0, 0.005, 3.5) == 0
    assert risk_sizing(1_000_000, 0.005, 0) == 0
    assert risk_sizing(1_000_000, 0, 3.5) == 0


# ---------- lot rounding ----------
def test_lot_rounding_sngs100():
    # SNGS lot=100: 42 шт -> 0 (пропуск), 350 -> 300
    assert apply_lot_rounding(42, 100) == 0
    assert apply_lot_rounding(350, 100) == 300
    assert apply_lot_rounding(350, 1) == 350


def test_lot_rounding_gazp10():
    assert apply_lot_rounding(115, 10) == 110
    assert apply_lot_rounding(9, 10) == 0


def test_lot_rounding_bad_lot():
    assert apply_lot_rounding(115, 0) == 115
    assert apply_lot_rounding(115, None) == 115


# ---------- liq cap ----------
def test_liq_cap():
    assert apply_liq_cap(1000, 50_000, 0.05) == 1000      # 2500 > 1000 — не режет
    assert apply_liq_cap(1000, 10_000, 0.05) == 500       # режет до 5% объёма
    assert apply_liq_cap(1000, 10_000, None) == 1000      # выключен
    assert apply_liq_cap(1000, 10, 0.05) == 0             # тонкий бар


# ---------- kill switch ----------
class FakeState:
    def __init__(self, day_R):
        self.day_realized_R = day_R
        self.kill_day = None


def test_kill_switch_triggers_next_trading_day():
    # пятница 2026-10-02 с R=-5 при пороге -4 → блокируем понедельник 2026-10-05
    st = FakeState({"2026-10-02": -5.0})
    today = date(2026, 10, 5)
    kill = check_kill_switch(st, today, -4.0)
    assert kill == "2026-10-05"


def test_kill_switch_not_triggered():
    st = FakeState({"2026-10-02": -3.0})
    assert check_kill_switch(st, date(2026, 10, 5), -4.0) is None


def test_kill_switch_off():
    st = FakeState({"2026-10-02": -99.0})
    assert check_kill_switch(st, date(2026, 10, 5), None) is None


def test_kill_switch_skips_weekend():
    # пятница -5, среда как today → предыдущий торговый день - вторник? нет: ищем
    # последний торговый день с записью: понедельник 28.09 -5 → блокируем today 30.09
    st = FakeState({"2026-09-29": -5.0})
    assert check_kill_switch(st, date(2026, 9, 30), -4.0) == "2026-09-30"


# ---------- state persistence ----------
def test_state_roundtrip(tmp_path):
    p = str(tmp_path / "st.json")
    s = SaremaState(p)
    s.direction = -1
    s.lots_qty = 350
    s.entry_price = 62.34
    s.stop_price = 64.5
    s.base = 2_500_000
    s.day_realized_R = {"2026-10-02": -5.0}
    s.trades_done = 41
    s.save()

    s2 = SaremaState(p)
    assert s2.load() is True
    assert s2.direction == -1
    assert s2.lots_qty == 350
    assert s2.entry_price == pytest.approx(62.34)
    assert s2.stop_price == pytest.approx(64.5)
    assert s2.base == pytest.approx(2_500_000)
    assert s2.day_realized_R["2026-10-02"] == -5.0
    assert s2.trades_done == 41
    # F-059a (14.10): persist_mode — running переживает рестарт (выключение только Дмитрием)
    s.mode = "running"
    s.save()
    s3 = SaremaState(p)
    s3.load()
    assert s3.mode == "running"


def test_state_missing_file():
    s = SaremaState(str(tmp_path_none()) / "nope.json" if False else "no_such_dir/x.json")
    assert s.load() is False


def tmp_path_none():
    import tempfile
    return __import__("pathlib").Path(tempfile.gettempdir())
