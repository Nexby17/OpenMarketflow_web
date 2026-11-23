"""Smoke-тест сигнальной логики: прогон StrategyEngine по реальной истории MTLR H1 (ISS).
Offline: события исполняются симулированно по close бара, тики = h/l внутри бара для SL/TP.
Проверяем: сигналы генерируются, позиции открываются/закрываются, журнал валиден.
"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from candles_source import CandleSource
from config_mtlr import MtlrParams, TF_TO_ISS
from strategy_mtlr import StrategyEngine, LONG, SHORT

p = MtlrParams()
p.paper = True
p.long_qty = 100
p.short_qty = 100
p.max_capital = 100000
p.tp_enabled = True; p.tp_unit = "pct"; p.tp_value = 2.0
p.sl_enabled = True; p.sl_unit = "pct"; p.sl_value = 1.5

src = CandleSource("MTLR", 500)
candles = src.fetch_history(TF_TO_ISS["H1"], 500)
print(f"loaded {len(candles)} H1 bars, {candles[0]['dt']} .. {candles[-1]['dt']}")

eng = StrategyEngine(p)

# Симуляция: обрабатываем бары поштучно, внутри каждого бара тики = [o, h, l, c]
# (порядок OHLC приблизительный; SL-приоритет при конфликтном баре)
import itertools
signals_open = signals_close = 0
for cut in range(30, len(candles) + 1):
    sub = candles[:cut]
    eng.recompute(sub)
    evs = eng.scan()
    for ev in evs:
        if ev["action"] == "open":
            price = sub[-1]["c"]
            eng.on_open_fill(ev["side"], ev["qty"], price, time.time(), ev["reason"])
            signals_open += 1
        elif ev["action"] == "close":
            # close по close бара (flip); SL/TP внутри бара обработает тик-фаза ниже
            price = sub[-1]["c"]
            eng.on_close_fill(price, time.time(), ev["reason"])
            signals_close += 1
    # тик-фаза: intrabar h/l
    bar = sub[-1]
    if eng.position:
        for tick in (bar["h"], bar["l"], bar["c"]):
            ev = eng.on_tick(tick)
            if ev and ev["action"] == "close":
                eng.on_close_fill(tick, time.time(), ev["reason"])
                signals_close += 1
                break

st = eng.stats()
print("\n=== РЕЗУЛЬТАТ ПРОГОНА (cross ИЛИ pb, TP2%/SL1.5%) ===")
for k, v in st.items():
    print(f"  {k}: {v}")
print("last_signal:", eng.last_signal)
assert st["trades"] > 0, "нет сделок!"
assert st["trades"] < 200, "слишком много сделок (ловит деградацию логики)"
# валидность журнала
for t in eng.journal:
    assert t["pnl"] is not None and t["entry_price"] > 0 and t["exit_price"] > 0
    assert t["reason_in"] in ("cross", "pb")
    assert t["reason_out"] in ("tp", "sl", "trail", "flip", "manual")
print("\n=== SMOKE-ТЕСТ СТРАТЕГИИ ПРОЙДЕН ===")
