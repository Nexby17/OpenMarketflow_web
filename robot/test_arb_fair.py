# -*- coding: utf-8 -*-
"""Unit tests for fair-value deviation mode (Fix #1-#3, 2026-09-10).

Covers:
  #1  Z-score from deviation at slow cadence (not 50s of raw spread)
  #2  Fair value: cost of carry minus dividend calendar
  #3  spread_long_rub (realizable LONG spread) vs spread_rub (SHORT)
  Entry logic: dev_ann thresholds, direction guards, averaging on dev_ann
"""
import sys, os, time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'arb_common'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'py4'))

from arb_engine import BasisCalculator
from strategy_arb import ArbitrageStrategy, ArbParams, LONG_BASIS, SHORT_BASIS, FLAT

PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


print("=== Fix #2: fair_spread = carry - dividends ===")
bc = BasisCalculator(lookback=50, hedge_ratio=10.0, risk_free_rate=0.16,
                     expiration_date="2026-12-17", contract_size=100)
# days semantics: floor of (exp - now), same as _days_to_expiration
from datetime import datetime as _dt
days_expected = ( _dt.strptime("2026-12-17", "%Y-%m-%d") - _dt.now() ).days
bc.update_price_a(89.44)
bc.update_price_b(8971.0)
fair = bc.fair_spread
# days_to_exp is floor-based; recompute fair expectation with the same day count
days = bc._days_to_expiration()
carry_expected = 89.44 * 100 * 0.16 * days / 365.0
check("days_to_exp floor-consistent", days == days_expected, f"got {days} expected {days_expected}")
check("fair without div = carry", abs(fair - carry_expected) < 1e-6,
      f"fair={fair:.2f} expected={carry_expected:.2f}")
# Add dividend with ex-date before expiration: reduces fair
bc.set_dividends([{"date": "2026-10-15", "amount": 12.0}])
fair2 = bc.fair_spread
check("fair with div = carry - 1200",
      abs(fair2 - (carry_expected - 1200.0)) < 1e-6,
      f"fair2={fair2:.2f} expected={carry_expected-1200:.2f}")
# Dividend after expiration: ignored
bc.set_dividends([{"date": "2027-01-15", "amount": 12.0}])
check("div after exp ignored", abs(bc.fair_spread - carry_expected) < 1e-6)
# Both sides of boundary
bc.set_dividends([("2026-12-17", 5.0), ("2026-12-18", 9.0)])
check("ex-date == exp counted, exp+1 not",
      abs(bc.fair_spread - (carry_expected - 500.0)) < 1e-6)

print("=== Fix #1: deviation Z at slow cadence ===")
bc2 = BasisCalculator(lookback=50, hedge_ratio=10.0, risk_free_rate=0.16,
                      expiration_date="2026-12-17", contract_size=100)
bc2._dev_push_interval = 0.05  # speed up for test
bc2.dev_lookback = 100
# Simulate spot moving but deviation stationary (carry trend should NOT create Z drift)
n = 200
for i in range(n):
    t = i / n
    spot = 100.0 + 30.0 * t           # strong spot trend (carry grows)
    fut = spot * 10 + 16.0 * t * 100 / 365.0 * 0  # fair grows with T decay handled by fair_spread
    bc2.update_price_a(spot)
    bc2.update_price_b(spot * 10 + 100.0 * 0.16 * (98 - 0) / 365.0 * 100 * 0 + 100 * 0.16 * 98 / 365.0)  # const fair premium
    bc2._push_deviation(force=True)
# Deviation should hover near 0 (fut priced exactly at fair carry with T frozen in sim)
# Note: fair_spread uses live days_to_exp; sim keeps fut premium constant, so deviation
# drifts slightly as days decay — but std must be >> that drift per push, and Z bounded.
z = bc2.zscore_dev_no_push
check("dev Z computed", isinstance(z, float), f"z={z}")
check("dev history filled", len(bc2._deviation_history) == 200,
      f"len={len(bc2._deviation_history)}")
check("has_enough_data (dev mode, 200 pushes >= min(2500,50))", bc2.has_enough_data)
# Cadence: non-forced push should be skipped right after a forced one
before = len(bc2._deviation_history)
bc2._push_deviation()  # within interval -> skipped
check("cadence gate skips fast push", len(bc2._deviation_history) == before)

print("=== Fix #3: spread_long_rub vs spread_rub ===")
bc3 = BasisCalculator(lookback=50, hedge_ratio=10.0, risk_free_rate=0.16,
                      expiration_date="2026-12-17", contract_size=100)
bc3.update_price_a(89.44)
bc3.update_price_b(8971.0)
bc3.update_market(bid_a=89.40, ask_a=89.48, bid_b=8969.0, ask_b=8973.0)
s_short = bc3.spread_rub      # bid_B - ask_A*hedge = 8969 - 894.8
s_long = bc3.spread_long_rub  # ask_B - bid_A*hedge = 8973 - 894.0
check("short spread = bid_B - ask_A*h", abs(s_short - (8969.0 - 894.8)) < 1e-9, f"{s_short}")
check("long spread = ask_B - bid_A*h", abs(s_long - (8973.0 - 894.0)) < 1e-9, f"{s_long}")
check("long spread > short spread (crossing spread)", s_long > s_short)

print("=== Entry logic: dev_ann thresholds ===")
params = ArbParams(
    symbol_a="GAZP@MISX", ticker_a="GAZP",
    symbol_b="GZU6@RTSX", ticker_b="GZU6",
    lots_a=10, lots_b=1, hedge_ratio=10.0,
    mult_a=10.0, mult_b=1.0,
    entry_mode="zscore",
    dev_ann_high=2.5, dev_ann_low=-1.0,
    dev_lookback=100, dev_push_interval=0.05,
    risk_free_rate=0.16, expiration_date=(datetime.now() + timedelta(days=98)).strftime("%Y-%m-%d"),
    contract_size=100,
    capital=1_000_000,
    allow_long_basis=True,
)
st = ArbitrageStrategy(params)
bc = st.basis_calc
# fill deviation history: fair carry + mispricing +4% annualized => short signal
days = bc._days_to_expiration()
spot = 89.44
spot_val = spot * 100
fair_carry = spot_val * 0.16 * days / 365.0     # fair premium at r=16%
dev_target = spot_val * 0.04 * days / 365.0     # +4% annualized deviation in rub
for i in range(150):
    jitter = 0.02 * (((i * 37) % 11) - 5)  # deterministic pseudo-noise
    bc.update_price_a(spot)
    bc.update_price_b(spot * 10 + fair_carry + dev_target + jitter)
    bc._push_deviation(force=True)
    time.sleep(0.001)
# F-059: гейты требуют живой OB, согласованный с last (вариант B)
spot_mid = 89.44
st.ob_a.update([(spot_mid - 0.01, 10, 0, 1), (spot_mid + 0.01, 0, 10, 1)])
st.ob_b.update([(spot * 10 + fair_carry + dev_target - 1.0, 10, 0, 1), (spot * 10 + fair_carry + dev_target + 1.0, 0, 10, 1)])
sig = st.check_entry()
check("short signal fires at dev_ann ~ +4% > 2.5", sig is not None and sig["side"] == "short_basis",
      f"sig={sig} dev_ann={bc.dev_ann_pct:.2f}")
if sig:
    check("signal carries dev_ann", "dev_ann" in sig)

# Long signal: futures mispriced -3% annualized
st2 = ArbitrageStrategy(ArbParams(
    symbol_a="GAZP@MISX", ticker_a="GAZP",
    symbol_b="GZU6@RTSX", ticker_b="GZU6",
    lots_a=10, lots_b=1, hedge_ratio=10.0,
    entry_mode="zscore", dev_ann_high=2.5, dev_ann_low=-1.0,
    dev_lookback=100, dev_push_interval=0.05,
    risk_free_rate=0.16,
    expiration_date=(datetime.now() + timedelta(days=98)).strftime("%Y-%m-%d"),
    contract_size=100, capital=1_000_000, allow_long_basis=True,
))
bc = st2.basis_calc
days2 = bc._days_to_expiration()
fair2 = spot_val * 0.16 * days2 / 365.0
dev_neg = -(spot_val * 0.03 * days2 / 365.0)  # -3% annualized
for i in range(150):
    jitter = 0.02 * (((i * 29) % 7) - 3)
    bc.update_price_a(spot)
    bc.update_price_b(spot * 10 + fair2 + dev_neg + jitter)
    bc._push_deviation(force=True)
    time.sleep(0.001)
st2.ob_a.update([(spot_mid - 0.01, 10, 0, 1), (spot_mid + 0.01, 0, 10, 1)])
st2.ob_b.update([(spot * 10 + fair2 + dev_neg - 1.0, 10, 0, 1), (spot * 10 + fair2 + dev_neg + 1.0, 0, 10, 1)])
sig2 = st2.check_entry()
check("long signal fires at dev_ann ~ -3% < -1", sig2 is not None and sig2["side"] == "long_basis",
      f"sig={sig2} dev_ann={bc.dev_ann_pct:.2f}")

# allow_long_basis=False blocks long
params_nolong = ArbParams(
    symbol_a="GAZP@MISX", ticker_a="GAZP",
    symbol_b="GZU6@RTSX", ticker_b="GZU6",
    lots_a=10, lots_b=1, hedge_ratio=10.0,
    entry_mode="zscore", dev_ann_high=2.5, dev_ann_low=-1.0,
    dev_lookback=100, dev_push_interval=0.05,
    risk_free_rate=0.16,
    expiration_date=(datetime.now() + timedelta(days=98)).strftime("%Y-%m-%d"),
    contract_size=100, capital=1_000_000, allow_long_basis=False,
)
st3 = ArbitrageStrategy(params_nolong)
bc = st3.basis_calc
days3 = bc._days_to_expiration()
fair3 = spot_val * 0.16 * days3 / 365.0
for i in range(150):
    bc.update_price_a(spot)
    bc.update_price_b(spot * 10 + fair3 + dev_neg)
    bc._push_deviation(force=True)
    time.sleep(0.001)
sig3 = st3.check_entry()
check("long blocked when allow_long_basis=False", sig3 is None, f"sig={sig3}")

# Warmup gate: fresh strategy must NOT fire immediately
st4 = ArbitrageStrategy(ArbParams(
    symbol_a="GAZP@MISX", ticker_a="GAZP",
    symbol_b="GZU6@RTSX", ticker_b="GZU6",
    entry_mode="zscore", dev_lookback=100,
    risk_free_rate=0.16,
    expiration_date=(datetime.now() + timedelta(days=98)).strftime("%Y-%m-%d"),
    contract_size=100, capital=1_000_000,
))
sig4 = st4.check_entry()
check("warmup gate: no signal without dev history", sig4 is None)

print("=== Averaging on dev_ann ===")
# Open first short layer, then verify averaging blocked when dev NOT expanding
stA = ArbitrageStrategy(ArbParams(
    symbol_a="GAZP@MISX", ticker_a="GAZP",
    symbol_b="GZU6@RTSX", ticker_b="GZU6",
    entry_mode="zscore", dev_ann_high=2.5, dev_ann_low=-1.0,
    dev_lookback=100, dev_push_interval=0.05,
    risk_free_rate=0.16,
    expiration_date=(datetime.now() + timedelta(days=98)).strftime("%Y-%m-%d"),
    contract_size=100, capital=10_000_000, allow_long_basis=False,
))
bc = stA.basis_calc
daysA = bc._days_to_expiration()
fairA = spot_val * 0.16 * daysA / 365.0
dev_hi = spot_val * 0.04 * daysA / 365.0
for i in range(150):
    bc.update_price_a(spot)
    bc.update_price_b(spot * 10 + fairA + dev_hi)
    bc._push_deviation(force=True)
    time.sleep(0.001)
stA.open_layer(SHORT_BASIS, spot, spot * 10 + fairA + dev_hi, 10, 1, z=2.0, dev_ann=bc.dev_ann_pct)
# same dev now -> averaging blocked (dev not expanding)
sigA = stA.check_entry()
check("averaging blocked at flat dev", sigA is None, f"sig={sigA}")
# higher dev -> averaging allowed (bypass anti-duplicate lock: testing averaging logic)
dev_hi2 = spot_val * 0.05 * daysA / 365.0
for i in range(5):
    bc.update_price_a(spot)
    bc.update_price_b(spot * 10 + fairA + dev_hi2)
    bc._push_deviation(force=True)
    time.sleep(0.06)
stA.ob_a.update([(spot_mid - 0.01, 10, 0, 1), (spot_mid + 0.01, 0, 10, 1)])
stA.ob_b.update([(spot * 10 + fairA + dev_hi2 - 1.0, 10, 0, 1), (spot * 10 + fairA + dev_hi2 + 1.0, 0, 10, 1)])
stA.force_unlock()
sigA2 = stA.check_entry()
check("averaging allowed when dev expands", sigA2 is not None and sigA2["side"] == "short_basis",
      f"sig={sigA2} dev_now={bc.dev_ann_pct:.2f}")

print("=== Legacy state migration ===")
stB = ArbitrageStrategy(ArbParams(symbol_a="GAZP@MISX", ticker_a="GAZP",
                                  symbol_b="GZU6@RTSX", ticker_b="GZU6",
                                  risk_free_rate=0.16, contract_size=100))
stB.load_state({"layers": [{
    "side": SHORT_BASIS, "entry_price_a": 89.0, "entry_price_b": 8900.0,
    "entry_basis": 10.0, "entry_z": 1.9, "entry_time": time.time() - 60,
    "lots_a": 10, "lots_b": 1, "layer_id": 1,
}]})
check("legacy layer loaded (entry_dev_ann defaults 0)", stB.layers[0].entry_dev_ann == 0.0)
stB.p.dev_lookback = 100
# averaging fallback: current dev basis > entry basis -> allow
bc = stB.basis_calc
bc.update_price_a(spot)
bc.update_price_b(spot * 10 + dev_hi + 200)
sigB = stB.check_entry()
# may fire or block depending on warmup; just ensure no crash and no exception
check("legacy averaging path no crash", True)

print("=== get_state exposes new fields ===")
state = st.basis_calc.get_state()
for k in ("spread_long_rub", "dev_ann_pct", "zscore_dev", "dev_mean", "dev_std",
          "dev_points", "dev_lookback", "dividends", "div_sum"):
    check(f"state[{k}]", k in state)

print("=== F-032: realizable PnL exit (стакан вместо last) ===")
# F-032-1: реализуемый PnL считается по стакану, а не по last
cfg = dict(symbol_a="GAZP@MISX", ticker_a="GAZP", symbol_b="GZU6@RTSX", ticker_b="GZU6",
           lots_a=10, lots_b=1, hedge_ratio=10.0, mult_a=10.0, mult_b=1.0,
           entry_mode="zscore", dev_ann_high=2.5, dev_ann_low=-1.0,
           dev_lookback=100, dev_push_interval=0.05,
           risk_free_rate=0.16,
           expiration_date=(datetime.now() + timedelta(days=98)).strftime("%Y-%m-%d"),
           contract_size=100, capital=1_000_000, allow_long_basis=False,
           min_profit_type="rub", min_profit_value=25.0,
           slippage_bps=5.0, use_commission=False)

stX = ArbitrageStrategy(ArbParams(**cfg))
stX.ob_a.update([(99.80, 10, 0, 1), (99.82, 0, 10, 1)])   # bid_a=99.80 ask_a=99.82
stX.ob_b.update([(10319.0, 10, 0, 1), (10320.0, 0, 10, 1)])  # bid_b=10319 ask_b=10320
# SHORT слой: вошли buy A@99.75, sell B@10350.0
lay = stX.layers[0] if False else None
stX.open_layer(SHORT_BASIS, 99.75, 10350.0, 10, 1, z=2.0)
lay = stX.layers[0]
rl = stX._layer_realizable_pnl(lay)
# ожидаем: pnl_a = (99.80-99.75)*10*10 = 5.0; pnl_b = (10350 - (10320+5.16))*1 = 24.84; gross 29.84
exp = (99.80-99.75)*10*10 + (10350.0 - (10320.0 + 10320.0*5.0/10000.0))
check("F-032 realizable math (short)", rl is not None and abs(rl - exp) < 0.01, f"rl={rl} exp={exp}")
# last-путь даёт другое значение (проверяем что стакан реально приоритетен)
check("F-032 realizable differs from last-path",
      abs(rl - stX._layer_unrealized_pnl(lay)) > 0.01,
      f"rl={rl:.2f} last={stX._layer_unrealized_pnl(lay):.2f}")

# F-032-2: протухший стакан -> realizable None -> тейк НЕ квалифицируется
stX.ob_a._ts = time.time() - 10
stX.ob_b._ts = time.time() - 10
check("F-032 stale OB -> None", stX._layer_realizable_pnl(lay) is None)
check("F-032 stale OB -> no exit", stX.check_exit() is None)
stX.ob_a.update([(99.80, 10, 0, 1), (99.82, 0, 10, 1)])
stX.ob_b.update([(10319.0, 10, 0, 1), (10320.0, 0, 10, 1)])

# F-032-3: подтверждение N тиков — один тик не закрывает
stX2 = ArbitrageStrategy(ArbParams(**{**cfg, "min_profit_value": 25.0}))
stX2.ob_a.update([(99.80, 10, 0, 1), (99.82, 0, 10, 1)])
stX2.ob_b.update([(10450.0, 10, 0, 1), (10451.0, 0, 10, 1)])  # большой профит по B
stX2.open_layer(SHORT_BASIS, 99.75, 10500.0, 10, 1, z=2.0)
sig1 = stX2.check_exit()
check("F-032 tick 1 no exit", sig1 is None, f"sig={sig1}")
sig2 = stX2.check_exit()
check("F-032 tick 2 no exit", sig2 is None)
sig3 = stX2.check_exit()
check("F-032 tick 3 fires exit", sig3 is not None and sig3["reason"] == "profit_target" and sig3["layer_id"] == stX2.layers[0].layer_id,
      f"sig={sig3}")

# F-032-4: фантомный выброс (один тик стакана) не закрывает — реальный сценарий утечки
stX3 = ArbitrageStrategy(ArbParams(**cfg))
stX3.ob_a.update([(99.80, 10, 0, 1), (99.82, 0, 10, 1)])
stX3.ob_b.update([(10319.0, 10, 0, 1), (10320.0, 0, 10, 1)])  # стакан стоит на месте
stX3.open_layer(SHORT_BASIS, 99.75, 10345.0, 10, 1, z=2.0)
# last дёргается в пользу SHORT (как GZZ6 утром): одиночный принт A вверх, B вниз —
# last-математика рисует ~+60 ₽, но стакан стоит: реализуемый всего ~20,65 ₽ < 25
stX3.basis_calc.update_price_a(99.95)
stX3.basis_calc.update_price_b(10300.0)
un_ph = stX3._layer_unrealized_pnl(stX3.layers[0])
rl_ph = stX3._layer_realizable_pnl(stX3.layers[0])
check("F-032 phantom last-profit exists", un_ph >= 25.0,
      f"unreal={un_ph:.2f}")
check("F-032 phantom below threshold by OB", rl_ph is not None and rl_ph < 25.0,
      f"realizable={rl_ph:.2f}")
check("F-032 phantom not qualifying by OB", stX3.check_exit() is None,
      "стакан стоит — realizable < порога, тейк не должен сработать (старый код закрыл бы в минус)")

# F-032-5: FIFO — при двух qualifying слоях закрывается старейший
stX4 = ArbitrageStrategy(ArbParams(**cfg))
stX4.ob_a.update([(99.80, 10, 0, 1), (99.82, 0, 10, 1)])
stX4.ob_b.update([(10450.0, 10, 0, 1), (10451.0, 0, 10, 1)])
stX4.open_layer(SHORT_BASIS, 99.75, 10500.0, 10, 1, z=2.0)   # старейший
stX4.open_layer(SHORT_BASIS, 99.75, 10510.0, 10, 1, z=2.1)   # новее
for _ in range(3):
    sigF = stX4.check_exit()
check("F-032 FIFO oldest first", sigF is not None and sigF["layer_id"] == stX4.layers[0].layer_id,
      f"sig={sigF} layers={[l.layer_id for l in stX4.layers]}")

# F-032-6: LONG-слой — симметричная математика
stX5 = ArbitrageStrategy(ArbParams(**{**cfg, "allow_long_basis": True}))
stX5.ob_a.update([(99.80, 10, 0, 1), (99.82, 0, 10, 1)])
stX5.ob_b.update([(10319.0, 10, 0, 1), (10320.0, 0, 10, 1)])
stX5.open_layer(LONG_BASIS, 99.90, 10290.0, 10, 1, z=-2.0)  # вошли sell A@99.90, buy B@10290
rlL = stX5._layer_realizable_pnl(stX5.layers[0])
expL = (99.90-99.82)*10*10 + ((10319.0 - 10319.0*5.0/10000.0) - 10290.0)
check("F-032 realizable math (long)", rlL is not None and abs(rlL - expL) < 0.01, f"rl={rlL} exp={expL}")

print("=== F-039: quote routing + last guard + realizable card ===")
# F-039-1: маршрутизация по тикеру
stR = ArbitrageStrategy(ArbParams(symbol_a="BRV6@RTSX", ticker_a="BRV6",
                                  symbol_b="BRZ6@RTSX", ticker_b="BRZ6",
                                  risk_free_rate=0.16, contract_size=1))
check("F-039 route by ticker A", stR.route_quote("BRV6", 105.5) == "A")
check("F-039 route by ticker B", stR.route_quote("BRZ6", 95.7) == "B")
# F-039-2: неизвестный тикер — по близости к OB mid ±2%; близко к обеим? выбираем ближайшую
stR.ob_a.update([(105.60, 5, 0, 1), (105.62, 0, 5, 1)])   # mid 105.61
stR.ob_b.update([(95.70, 5, 0, 1), (95.74, 0, 5, 1)])     # mid 95.72
check("F-039 route unknown -> nearest", stR.route_quote("???", 95.73) == "B", f"got {stR.route_quote('???', 95.73)}")
check("F-039 route unknown -> nearest A", stR.route_quote("???", 105.60) == "A")
# F-039-3: last между ног (midway ~100.6, 4.7% от обеих) — отбрасывается
check("F-039 ambiguous quote dropped", stR.route_quote("???", 100.6) is None,
      f"got {stR.route_quote('???', 100.6)}")
# F-039-4: пустые стаканы — легаси-эвристика по порядку величины
stR2 = ArbitrageStrategy(ArbParams(symbol_a="BRV6@RTSX", ticker_a="BRV6",
                                   symbol_b="BRZ6@RTSX", ticker_b="BRZ6",
                                   risk_free_rate=0.16, contract_size=1))
check("F-039 empty OB legacy <1000 -> A", stR2.route_quote("", 95.0) == "A")
check("F-039 empty OB legacy >1000 -> B", stR2.route_quote("", 5000.0) == "B")

# F-039-5: _total_realizable — None при протухшем OB, сумма при живом
stR3 = ArbitrageStrategy(ArbParams(symbol_a="BRV6@RTSX", ticker_a="BRV6",
                                   symbol_b="BRZ6@RTSX", ticker_b="BRZ6",
                                   risk_free_rate=0.16, contract_size=1,
                                   min_profit_type="rub", min_profit_value=25.0,
                                   slippage_bps=5.0, use_commission=False))
stR3.ob_a.update([(105.60, 5, 0, 1), (105.62, 0, 5, 1)])
stR3.ob_b.update([(95.70, 5, 0, 1), (95.74, 0, 5, 1)])
stR3.open_layer(LONG_BASIS, 105.78, 96.33, 1, 1, z=2.0)
tot = stR3._total_realizable()
l1 = stR3._layer_realizable_pnl(stR3.layers[0])
check("F-039 total = sum of layers", tot is not None and abs(tot - l1) < 0.01, f"tot={tot} l1={l1}")
stR3.ob_a._ts = time.time() - 10
check("F-039 total None on stale OB", stR3._total_realizable() is None)

# F-039-6: карточка unrealizedPnl = реализуемый (свежий стакан)
stR3.ob_a.update([(105.60, 5, 0, 1), (105.62, 0, 5, 1)])
stt = stR3.get_status()
check("F-039 card unrealized = realizable", stt["unrealizedPnl"] == stt["realizableUnrealizedPnl"] and abs(stt["unrealizedPnl"] - l1) < 0.01,
      f"card={stt['unrealizedPnl']} realizable={stt['realizableUnrealizedPnl']}")
check("F-039 card keeps last-variant for diagnostics", "localUnrealizedPnl" in stt)

print("=== F-040: orderbook sanity guard (вход по мусорному OB закрыт) ===")
# Реальный кейс 28.09: OB GAZP ask=93.00 / bid=105.00 при last 99.55 → вход был по 93
stG = ArbitrageStrategy(ArbParams(symbol_a="GAZP@MISX", ticker_a="GAZP",
                                  symbol_b="GZZ6@RTSX", ticker_b="GZZ6",
                                  lots_a=10, lots_b=1, hedge_ratio=100.0, mult_a=10.0, mult_b=1.0,
                                  risk_free_rate=0.145, contract_size=100, capital=30000,
                                  min_profit_type="rub", min_profit_value=25.0,
                                  entry_mode="zscore", dev_ann_high=2.5, dev_ann_low=-1.0,
                                  dev_lookback=100, dev_push_interval=0.05,
                                  expiration_date=(datetime.now() + timedelta(days=80)).strftime("%Y-%m-%d")))
# мусорный OB
stG.ob_a.update([(105.00, 10, 0, 1), (93.00, 0, 10, 1)])
stG.ob_b.update([(10269.86, 5, 0, 1), (10270.5, 0, 5, 1)])
stG.basis_calc.update_price_a(99.55)
stG.basis_calc.update_price_b(10277.0)
check("F-040 crossed OB insane", stG._orderbook_sane() is False)
check("F-040 crossed OB blocks entry", stG.check_entry() is None)

# нормальный OB — проверки проходят
stG.ob_a.update([(98.70, 10, 0, 1), (98.72, 0, 10, 1)])
stG.basis_calc.update_price_a(98.71)
check("F-040 sane OB ok (price near mid)", stG._orderbook_sane() is True)

# last далеко от mid (1.5%) — блок
stG.basis_calc.update_price_a(97.30)
check("F-040 last dev 1.4% from mid blocks", stG._orderbook_sane() is False)

# протухший OB — блок
stG.basis_calc.update_price_a(98.71)
stG.ob_a._ts = time.time() - 10
check("F-040 stale OB blocks", stG._orderbook_sane() is False)

# широкий стакан (3%) — блок
stG.ob_a.update([(97.50, 10, 0, 1), (100.50, 0, 10, 1)])
stG.basis_calc.update_price_a(99.0)
check("F-040 wide spread blocks", stG._orderbook_sane() is False)

print("=== F-052: OB-vs-last sanity на realizable (инцидент GZZ6: фантомный ask) ===")
# F-052-1: стакан B расходится с last на 0.7% (eps=0.5% фьючерс) -> realizable None
stY = ArbitrageStrategy(ArbParams(**cfg))
stY.basis_calc.update_price_a(99.75)
stY.basis_calc.update_price_b(10350.0)
stY.ob_a.update([(99.74, 10, 0, 1), (99.76, 0, 10, 1)])
stY.ob_b.update([(10027.0, 10, 0, 1), (10028.0, 0, 10, 1)])  # ask=10028 при рынке 10350 — мусор GZZ6
stY.open_layer(SHORT_BASIS, 99.70, 10420.0, 10, 1, z=2.0)
rlY = stY._layer_realizable_pnl(stY.layers[0])
check("F-052-1 OB B vs last B (0.7%) -> realizable None", rlY is None, f"rl={rlY}")
check("F-052-1 mismatch blocks exit", stY.check_exit() is None)

# F-052-2: стакан согласован с last -> тейк срабатывает после 3 тиков (регресс-тест гейта)
stY2 = ArbitrageStrategy(ArbParams(**{**cfg, "min_profit_value": 25.0}))
stY2.basis_calc.update_price_a(99.80)
stY2.basis_calc.update_price_b(10450.0)
stY2.ob_a.update([(99.80, 10, 0, 1), (99.82, 0, 10, 1)])
stY2.ob_b.update([(10450.0, 10, 0, 1), (10451.0, 0, 10, 1)])
stY2.open_layer(SHORT_BASIS, 99.75, 10500.0, 10, 1, z=2.0)
rl2 = stY2._layer_realizable_pnl(stY2.layers[0])
sigY = None
for _ in range(3):
    sigY = stY2.check_exit()
check("F-052-2 consistent OB -> take fires after 3 ticks",
      sigY is not None and sigY["reason"] == "profit_target", f"rl={rl2} sig={sigY}")

# F-052-3: нога A (акция, eps=0.2%) расходится с last на 1.2% -> None
stY3 = ArbitrageStrategy(ArbParams(**cfg))
stY3.basis_calc.update_price_a(99.80)
stY3.basis_calc.update_price_b(10320.0)
stY3.ob_a.update([(98.50, 10, 0, 1), (98.60, 0, 10, 1)])
stY3.ob_b.update([(10319.0, 10, 0, 1), (10320.0, 0, 10, 1)])
stY3.open_layer(SHORT_BASIS, 99.70, 10400.0, 10, 1, z=2.0)
check("F-052-3 OB A vs last A (1.2%) -> None",
      stY3._layer_realizable_pnl(stY3.layers[0]) is None)

# F-052-4: широкий, но согласованный стакан (BR-кейс) — интервальная форма пропускает
stY4 = ArbitrageStrategy(ArbParams(**{**cfg, "ticker_a": "BRV6", "symbol_a": "BRV6@RTSX",
                                      "ticker_b": "BRZ6", "symbol_b": "BRZ6@RTSX",
                                      "lots_a": 1, "lots_b": 1, "hedge_ratio": 1.0,
                                      "mult_a": 1.0, "mult_b": 1.0,
                                      "expiration_date": (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")}))
stY4.basis_calc.update_price_a(105.60)
stY4.basis_calc.update_price_b(95.70)
stY4.ob_a.update([(105.50, 5, 0, 1), (105.75, 0, 5, 1)])   # широкий стакан: 0.24%
stY4.ob_b.update([(95.60, 5, 0, 1), (95.80, 0, 5, 1)])
stY4.open_layer(SHORT_BASIS, 105.60, 95.90, 1, 1, z=2.0)
rl4 = stY4._layer_realizable_pnl(stY4.layers[0])
check("F-052-4 wide-but-real OB passes", rl4 is not None, f"rl={rl4}")


print("=== F-058: гибрид C — раздвижечный вход/выход (решение Дмитрия 01.10) ===")

# Общий конфиг гибрида: hedge 100 (GAZP/GZZ6), вход ≥15₽ + ≥1σ, тейк послойно по раздвижке 10₽,
# стоп 30₽, time-stop 240 мин, лимитка выхода 2с. dev_ann_high=0.2 — как в бою (₽-гейты главные).
cfgH = dict(symbol_a="GAZP@MISX", ticker_a="GAZP", symbol_b="GZZ6@RTSX", ticker_b="GZZ6",
            lots_a=10, lots_b=1, hedge_ratio=100.0, mult_a=10.0, mult_b=1.0,
            entry_mode="zscore", dev_ann_high=0.2, dev_ann_low=-1.0,
            dev_lookback=100, dev_push_interval=0.05,
            risk_free_rate=0.145,
            expiration_date=(datetime.now() + timedelta(days=76)).strftime("%Y-%m-%d"),
            contract_size=100, capital=1_000_000, allow_long_basis=False,
            min_profit_type="rub", min_profit_value=10.0,
            entry_dev_rub=15.0, entry_z_sigma=1.0,
            stop_dev_rub=30.0, time_stop_min=240.0,
            exit_mode="spread", leg_a_limit_seconds=2.0,
            slippage_bps=5.0, use_commission=True)

def _feed_dev(st, spot, dev_rub, n=200):
    """Fair-aware deviation history: b = spot*hedge + fair + dev_rub каждый пуш."""
    bc = st.basis_calc
    for i in range(n):
        bc.update_price_a(spot)
        fair = bc.fair_spread
        bc.update_price_b(spot * 100 + fair + dev_rub)
        bc._push_deviation(force=True)
        time.sleep(0.001)

# --- Вход: гейты по ЖИВОМУ СТАКАНУ (F-059, вариант B) ---
# Тест F-059-1: last расходится с OB (>eps) → антиспрей режет сигнал (урок 14:41)
stH = ArbitrageStrategy(ArbParams(**cfgH))
_feed_dev(stH, 97.0, 8.0)
_fairH = stH.basis_calc.fair_spread
stH.basis_calc.update_price_a(97.0)
stH.basis_calc.update_price_b(9700.0 + _fairH + 60.0)   # last B далеко выше OB
stH.ob_a.update([(96.99, 10, 0, 1), (97.01, 0, 10, 1)])
stH.ob_b.update([(9744.0, 10, 0, 1), (9746.0, 0, 10, 1)])
stH.force_unlock()
reason1, _ = stH._entry_gate_rub('short_basis')
check("F-059-1 last-vs-OB spray detected", reason1 is not None and 'antispray' in reason1,
      f"reason={reason1}")
sig_g1 = stH.check_entry()
check("F-059-1b spray blocks entry", sig_g1 is None, f"sig={sig_g1}")

# Тест F-059-2: OB согласован с last, realizable dev +28₽ над fair (≥15), но СТАЦИОНАРНАЯ
# история (dev_rel≈0) → направленный σ режет. OB: bid_B - ask_A*100 = (9700+fair+28-2) - 9701
stH2 = ArbitrageStrategy(ArbParams(**cfgH))
bcH2 = stH2.basis_calc
_fair2 = 0.0
for _i in range(200):
    bcH2.update_price_a(97.0)
    _fair2 = bcH2.fair_spread
    bcH2.update_price_b(9700.0 + _fair2 + 30.0 + ((-1) ** _i) * 2.0)  # вариация ±2₽ → живая σ
    bcH2._push_deviation(force=True)
    time.sleep(0.001)
stH2.basis_calc.update_price_a(97.0)
stH2.basis_calc.update_price_b(9700.0 + _fair2 + 30.0)   # last = fair+30
stH2.ob_a.update([(96.99, 10, 0, 1), (97.01, 0, 10, 1)])
stH2.ob_b.update([(10016.0, 10, 0, 1), (10018.0, 0, 10, 1)])  # bid_B = 10016... hmm depends on fair
# OB dev = bid_B - ask_A*100 - fair = bid_B - 9701 - fair. Хотим ~+28 → bid_B = fair + 9729
stH2.ob_b.update([(9700.0 + _fair2 + 28.0, 10, 0, 1), (9700.0 + _fair2 + 30.0, 0, 10, 1)])
reason2, dev2 = stH2._entry_gate_rub('short_basis')
check("F-059-2 directional sigma blocks flat-dev entry", reason2 is not None and 'dev_rel' in reason2,
      f"reason={reason2} dev={dev2}")

# Тест F-059-3: OB согласован, dev сильно выше среднего → вход проходит, dev_rub от OB
stH3 = ArbitrageStrategy(ArbParams(**cfgH))
bc3 = stH3.basis_calc
_fair3 = 0.0
for i in range(200):
    bc3.update_price_a(97.0)
    _fair3 = bc3.fair_spread
    d = 10.0 + (i % 10)
    bc3.update_price_b(9700.0 + _fair3 + d)
    bc3._push_deviation(force=True)
    time.sleep(0.001)
bc3.update_price_a(97.0)
bc3.update_price_b(9700.0 + _fair3 + 45.0)      # last = fair+45 (mean был 14.5 → dev_rel=30.5 ≥ 2.87)
stH3.ob_a.update([(96.99, 10, 0, 1), (97.01, 0, 10, 1)])
stH3.ob_b.update([(9700.0 + _fair3 + 43.0, 10, 0, 1), (9700.0 + _fair3 + 45.0, 0, 10, 1)])
# OB realizable = bid_B - ask_A*100 = (fair+43) - 9701 + 9700 = fair + 42 → OB dev = 42 ≥ 15
sig_g3 = stH3.check_entry()
check("F-059-3 entry passes OB dev 42 >= 15 + directional sigma ok", sig_g3 is not None and sig_g3["side"] == "short_basis",
      f"sig={sig_g3} fair={_fair3:.1f}")
check("F-059-3b signal carries OB dev_rub >= 15", sig_g3 is not None and (sig_g3.get("dev_rub") or 0) >= 15.0,
      f"dev_rub={sig_g3.get('dev_rub') if sig_g3 else None}")

# Тест F-059-4: протухший OB → вход запрещён полностью (не last-фолбэк!)
stH4 = ArbitrageStrategy(ArbParams(**cfgH))
_feed_dev(stH4, 97.0, 60.0)
_fair4 = stH4.basis_calc.fair_spread
stH4.basis_calc.update_price_a(97.0)
stH4.basis_calc.update_price_b(9700.0 + _fair4 + 60.0)
stH4.ob_a.update([(96.99, 10, 0, 1), (97.01, 0, 10, 1)])
stH4.ob_b.update([(9700.0 + _fair4 + 58.0, 10, 0, 1), (9700.0 + _fair4 + 60.0, 0, 10, 1)])
stH4.ob_a._ts = time.time() - 10
stH4.ob_b._ts = time.time() - 10
reason4, _ = stH4._entry_gate_rub('short_basis')
check("F-059-4 stale OB -> gate rejects (no last fallback)", reason4 is not None and 'протух' in reason4,
      f"reason={reason4}")

# Тест F-059-5: LONG-гейт работает при направленном σ (латентный баг F-058: лонги всегда блокировались)
stL = ArbitrageStrategy(ArbParams(**{**cfgH, "allow_long_basis": True}))
bcL = stL.basis_calc
_fairL = 0.0
for i in range(200):
    bcL.update_price_a(97.0)
    _fairL = bcL.fair_spread
    d = -10.0 - (i % 10)          # отрицательные девиации, mean ≈ -14.5, std ≈ 2.87
    bcL.update_price_b(9700.0 + _fairL + d)
    bcL._push_deviation(force=True)
    time.sleep(0.001)
bcL.update_price_a(97.0)
bcL.update_price_b(9700.0 + _fairL - 45.0)      # last ниже fair на 45 → LONG zscore-сигнал
stL.ob_a.update([(96.99, 10, 0, 1), (97.01, 0, 10, 1)])
stL.ob_b.update([(9700.0 + _fairL - 45.0, 10, 0, 1), (9700.0 + _fairL - 43.0, 0, 10, 1)])
# LONG realizable = ask_B - bid_A*100 = (fair-43) - 9699 + 9700 = fair - 42 → LONG dev = 42 ≥ 15
sigL = stL.check_entry()
check("F-059-5 long entry passes directional sigma (bug fix)", sigL is not None and sigL["side"] == "long_basis",
      f"sig={sigL}")

# --- Тейк: послойно по раздвижке (формулировка Дмитрия) ---
stT = ArbitrageStrategy(ArbParams(**{**cfgH, "use_commission": False}))
stT.basis_calc.update_price_a(97.0)
stT.basis_calc.update_price_b(9730.0)
stT.ob_a.update([(96.99, 10, 0, 1), (97.01, 0, 10, 1)])
stT.ob_b.update([(9729.0, 10, 0, 1), (9731.0, 0, 10, 1)])
# SHORT вошли: A@97.00, B@9730 → entry_basis = 9730 − 9700 = 30; порог тейка = 30 − 10 = 20
stT.open_layer(SHORT_BASIS, 97.00, 9730.0, 10, 1, z=2.0)

# rs = 9731 − 9699 = 32 > 20 — тейка нет
q0 = stT._spread_take_qualifies(stT._layer_realizable_spread(stT.layers[0]), stT.layers[0])
check("F-058-4 no take while spread above entry-10", q0 is False, f"rs={stT._layer_realizable_spread(stT.layers[0])}")
# rs=20 (ровно entry−10): нестрогое <= → проходит
stT.ob_b.update([(9717.0, 10, 0, 1), (9719.0, 0, 10, 1)])
q1 = stT._spread_take_qualifies(stT._layer_realizable_spread(stT.layers[0]), stT.layers[0])
check("F-058-5 borderline rs=entry-10 passes (<=)", q1 is True,
      f"rs={stT._layer_realizable_spread(stT.layers[0])}")
# rs=18 < 20 → да
stT.ob_b.update([(9715.0, 10, 0, 1), (9717.0, 0, 10, 1)])
q2 = stT._spread_take_qualifies(stT._layer_realizable_spread(stT.layers[0]), stT.layers[0])
check("F-058-6 take qualifies rs=18 <= entry-10", q2 is True)

# 3 тика подтверждения: n=1, n=2, n=3 → fire на третьем
sigA = stT.check_exit()
sigB = stT.check_exit()
sigC = stT.check_exit()
check("F-058-7 first two ticks no exit", sigA is None and sigB is None, f"sigA={sigA} sigB={sigB}")
check("F-058-8 take fires on 3rd tick", sigC is not None and sigC["reason"] == "profit_target" and sigC["layer_id"] == stT.layers[0].layer_id,
      f"sigC={sigC}")

# Групповой выход в spread-режиме отключён: слоям нужны ИХ пороги раздвижки, а не сумма PnL
stG2 = ArbitrageStrategy(ArbParams(**{**cfgH, "use_commission": False}))
stG2.basis_calc.update_price_a(97.0)
stG2.basis_calc.update_price_b(9730.0)
stG2.ob_a.update([(96.99, 10, 0, 1), (97.01, 0, 10, 1)])
stG2.ob_b.update([(9714.0, 10, 0, 1), (9716.0, 0, 10, 1)])
# rs = 9716 − 9699 = 17: слой1 (entry 30, порог 20) — проходит; слой2 (entry 40, порог 30) — нет
stG2.open_layer(SHORT_BASIS, 97.00, 9730.0, 10, 1, z=2.0)
stG2.open_layer(SHORT_BASIS, 97.00, 9740.0, 10, 1, z=2.1)
sigG = None
for _ in range(4):
    sigG = stG2.check_exit()
check("F-058-9 group exit disabled in spread mode (only per-layer)",
      sigG is None or (sigG["action"] == "exit" and sigG["layer_id"] is not None),
      f"sig={sigG}")

# --- Стоп по раздвижке +30₽ (послойно → exit_all) ---
stS = ArbitrageStrategy(ArbParams(**{**cfgH, "use_commission": False}))
stS.basis_calc.update_price_a(97.0)
stS.basis_calc.update_price_b(9730.0)
stS.open_layer(SHORT_BASIS, 97.00, 9730.0, 10, 1, z=2.0)   # entry_basis 30
stS.ob_a.update([(96.99, 10, 0, 1), (97.01, 0, 10, 1)])
stS.ob_b.update([(9762.0, 10, 0, 1), (9764.0, 0, 10, 1)])  # rs = 9764 − 9699 = 65 → worse = 35 ≥ 30
sigS = stS.check_exit()
check("F-058-10 dev stop fires at +35 worse", sigS is not None and sigS["reason"] == "dev_stop" and sigS["action"] == "exit_all",
      f"sig={sigS}")

# --- Time-stop 240 мин ---
stM = ArbitrageStrategy(ArbParams(**{**cfgH, "use_commission": False}))
stM.basis_calc.update_price_a(97.0)
stM.basis_calc.update_price_b(9730.0)
stM.open_layer(SHORT_BASIS, 97.00, 9730.0, 10, 1, z=2.0)
stM.layers[0].entry_time = time.time() - 241 * 60
stM.ob_a.update([(96.99, 10, 0, 1), (97.01, 0, 10, 1)])
stM.ob_b.update([(9729.0, 10, 0, 1), (9731.0, 0, 10, 1)])  # раздвижка не вернулась — но время вышло
sigM = stM.check_exit()
check("F-058-11 time stop fires at 241min", sigM is not None and sigM["reason"] == "time_stop",
      f"sig={sigM}")

# --- Стакан протух → решений по раздвижке нет (ни тейка, ни стопа) ---
stN = ArbitrageStrategy(ArbParams(**{**cfgH, "use_commission": False}))
stN.basis_calc.update_price_a(97.0)
stN.basis_calc.update_price_b(9730.0)
stN.open_layer(SHORT_BASIS, 97.00, 9730.0, 10, 1, z=2.0)
stN.ob_a.update([(96.99, 10, 0, 1), (97.01, 0, 10, 1)])
stN.ob_b.update([(9764.0, 10, 0, 1), (9766.0, 0, 10, 1)])
stN.ob_a._ts = time.time() - 10
stN.ob_b._ts = time.time() - 10
sigN = stN.check_exit()
check("F-058-12 stale OB: no dev-stop, no take", sigN is None or sigN["reason"] not in ("dev_stop", "profit_target"),
      f"sig={sigN}")

# --- pnl-режим не сломан (легаси-тейк по realizable PnL при невозврате раздвижки) ---
stP = ArbitrageStrategy(ArbParams(**{**cfgH, "exit_mode": "pnl", "min_profit_value": 25.0, "use_commission": False}))
stP.basis_calc.update_price_a(97.0)
stP.basis_calc.update_price_b(9730.0)
# SHORT закрытие: sell A @ bid (A вырос — профит), buy B @ ask (B упал — профит)
stP.ob_a.update([(97.19, 10, 0, 1), (97.21, 0, 10, 1)])
stP.ob_b.update([(9709.0, 10, 0, 1), (9711.0, 0, 10, 1)])
stP.open_layer(SHORT_BASIS, 97.00, 9730.0, 10, 1, z=2.0)
_rlP = stP._layer_realizable_pnl(stP.layers[0])
sigP = None
for _ in range(3):
    sigP = stP.check_exit()
check("F-058-13 pnl mode still takes profit", sigP is not None and sigP["reason"] == "profit_target",
      f"sig={sigP} realizable={_rlP}")

print(f"\n===== RESULTS: PASS={PASS} FAIL={FAIL} =====")
sys.exit(0 if FAIL == 0 else 1)
