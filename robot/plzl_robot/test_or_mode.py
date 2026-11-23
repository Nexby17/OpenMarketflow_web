#!/usr/bin/env python3
"""Верификация новой семантики «кросс ИЛИ пуллбэк» в StrategyEngine (16.11.2026).

Offline на MTLR_2Y_M5.csv → H1. Три режима:
1) both:   use_cross+use_pullback — входы двух типов (cross и pb), перекрытий нет
2) cross:  только use_cross — входы только cross
3) pb:     только use_pullback — входы только pb
Проверяем: reasons в журнале соответствуют включённым сигналам;
в both — cross-входы + pb-входы сосуществуют (сумма сделок > каждого режима в одиночку
не обязана, но оба типа должны присутствовать).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/root/.openclaw/workspace/HedgeFund/backtest/src")
from config_plzl import MtlrParams
from strategy_plzl import StrategyEngine
from tick_full_opt import load_csv, aggregate, DATA

TF = "H1"
bars = aggregate(load_csv(f"{DATA}/MTLR_2Y_M5.csv"), 60)
# candle-формат движка: t,o,h,l,c,v (совпадает)
print(f"H1 bars: {len(bars)}  {bars[0]['t']}..{bars[-1]['t']}")


def run(use_cross, use_pullback):
    p = MtlrParams()
    p.timeframe = TF
    p.use_cross = use_cross
    p.use_pullback = use_pullback
    p.paper = True
    p.long_qty = 100
    p.short_qty = 100
    p.max_capital = 100000.0
    p.entry_by_limit = False
    p.tp_enabled = True; p.tp_unit = "pct"; p.tp_value = 6.0
    p.sl_enabled = True; p.sl_unit = "pct"; p.sl_value = 2.0
    eng = StrategyEngine(p)
    t = 0.0
    for i in range(150, len(bars) + 1):
        sub = bars[:i]
        eng.recompute(sub)
        evs = eng.scan()
        for ev in evs:
            if ev["action"] == "open":
                eng.on_open_fill(ev["side"], ev["qty"], sub[-1]["c"], t, ev["reason"])
            elif ev["action"] == "close":
                eng.on_close_fill(sub[-1]["c"], t, ev["reason"])
        bar = sub[-1]
        if eng.position:
            for tick in (bar["h"], bar["l"], bar["c"]):
                eng.on_tick(tick)
                if not eng.position:
                    break
        t += 1
    reasons = {}
    for tr in eng.journal:
        reasons[tr["reason_in"]] = reasons.get(tr["reason_in"], 0) + 1
    outs = {}
    for tr in eng.journal:
        outs[tr["reason_out"]] = outs.get(tr["reason_out"], 0) + 1
    return reasons, outs, eng


r_both, o_both, e_both = run(True, True)
r_cross, o_cross, e_cross = run(True, False)
r_pb, o_pb, e_pb = run(False, True)

print(f"\nboth (cross+pb): {r_both} | exits {o_both} | PnL {e_both.realized_pnl:+.0f}")
print(f"cross only:      {r_cross} | exits {o_cross} | PnL {e_cross.realized_pnl:+.0f}")
print(f"pb only:         {r_pb} | exits {o_pb} | PnL {e_pb.realized_pnl:+.0f}")

# 1) оба режима дают валидные reason_in
assert set(r_both) <= {"cross", "pb"}, f"both: лишние reason_in: {r_both}"
assert set(r_cross) <= {"cross"}, f"cross-only: {r_cross}"
assert set(r_pb) <= {"pb"}, f"pb-only: {r_pb}"
# 2) в both присутствуют ОБА типа входов
assert r_both.get("cross", 0) > 0, "both: нет cross-входов"
assert r_both.get("pb", 0) > 0, "both: нет pb-входов"
# 3) cross-only входит на каждом флипе не реже, чем в both (в both часть флипов
#    перекрыта занятой позицией) — sanity на порядок
assert r_cross.get("cross", 0) >= r_both.get("cross", 0), "cross-only входов меньше, чем в both?!"
print("\n=== ВЕРИФИКАЦИЯ «CROSS ИЛИ PB» ПРОЙДЕНА ===")
