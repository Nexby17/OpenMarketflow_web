# -*- coding: utf-8 -*-
"""Smoke test: features ported from server-main (утверждено Дмитрием 28.09.2026).

1. get_hl_window() — HL-окно для фильтра входа
2. _hl_state() — вердикты short_only / long_only / blocked / both
3. HL-гейт входа: блок из середины диапазона, пропуск у экстремума
4. Пауза усреднения вне VA (AVG VA BLOCK) + catch-up при возврате в VA
5. Дефолт hl_filter=False — поведение как раньше

Запуск (py4 содержит numpy и прочие зависимости робота):
    python tests/test_of_port_features.py
"""
import sys, os

ROBOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROBOT)
sys.path.insert(0, os.path.join(ROBOT, "py4"))
os.chdir(ROBOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime
from strategy_of import OrderFlowStrategy, OFParams, LONG, SHORT
from orderflow_engine import BarMetrics


def main():
    P = OFParams(use_vah_val=True, vah_val_mode="range", hl_filter=True,
                 hl_window=12, hl_delta=6.0, step_average=5)
    s = OrderFlowStrategy(P)

    # 1) get_hl_window
    assert s.signals.get_hl_window(12) is None, "window must be not-ready with no bars"
    for i in range(12):
        m = BarMetrics(buy_volume=10, sell_volume=10, delta=0, delta_ratio=0.0,
                       total_volume=20, cvd=0)
        s.signals.add_bar(m, bar_high=100 + i, bar_low=95 + i, bar_close=98 + i)
    hl = s.signals.get_hl_window(12)
    assert hl is not None and hl[0] == 111 and hl[1] == 95, f"get_hl_window wrong: {hl}"
    print("1. get_hl_window OK:", hl)

    # 2) verdicts
    s._current_price = 111.5
    assert s._hl_state()["verdict"] == "short_only", s._hl_state()
    s._current_price = 95.5
    assert s._hl_state()["verdict"] == "long_only"
    s._current_price = 103.0
    assert s._hl_state()["verdict"] == "blocked"
    s._current_price = 100.0
    assert s._hl_state()["verdict"] == "long_only"  # 100 ближе к low: 5<=6, до high 11>6
    s.p.hl_window = 3  # узкое окно (highs 109-111, lows 104-106, ширина 7 <= 2*delta)
    s._current_price = 107.5
    assert s._hl_state()["verdict"] == "both", s._hl_state()
    print("2. _hl_state verdicts OK")

    # 3) HL gate on entry (VP готов, OB застаблен — тестируется именно HL)
    s.vp._profile = {100: 500}; s.vp._total_volume = 500
    s.vp._vah = 105.0; s.vp._val = 95.0

    class Sig:
        def __init__(self, d):
            self.direction = d
            self.signal_type = "stub"
    s.p.hl_window = 12
    s.signals.generate_signals = lambda *a, **k: [Sig(LONG)]
    s._lock_entry = lambda *a: None
    s._ob_filter_passes = lambda ob: True
    s._current_price = 103.0
    assert s._check_entry(103.0, datetime.now()) is None, "middle-range LONG must be blocked by HL"
    s._current_price = 96.0
    res = s._check_entry(96.0, datetime.now())
    assert res is not None and res.get("action") == "entry", "near-low LONG must pass HL: " + repr(res)
    print("3. HL gate OK: middle blocked, near-low entered", res)

    # 4) VA averaging pause + catch-up
    s._reset_position()
    s._dir = LONG; s._total_lots = 1; s._average_levels = 0
    s._lot_queue.clear()
    s._lot_queue.append(type("L", (), {"price": 100.0, "side": 1, "lots": 1, "added_ts": 0.0})())
    s.vp._profile = {100: 500}; s.vp._total_volume = 500
    s.vp._vah = 105.0; s.vp._val = 95.0
    assert s._check_averaging(90.0) == [], "outside VA averaging must pause"
    assert s._avg_va_block_logged is True
    assert s._check_averaging(90.0) == [], "still paused"
    acts = s._check_averaging(95.0)  # вернулись в VA: 100-95=5 >= step 5
    assert acts != [], "inside VA catch-up must fire: " + repr(acts)
    assert s._avg_va_block_logged is False
    print("4. VA averaging pause + catch-up OK:", acts)

    # 5) default OFF
    P2 = OFParams(use_vah_val=False, hl_filter=False)
    s2 = OrderFlowStrategy(P2)
    assert s2._hl_state() == {"enabled": False}
    print("5. hl_filter OFF default OK")
    print("SMOKE PASS")


if __name__ == "__main__":
    main()
