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

print(f"\n===== RESULTS: PASS={PASS} FAIL={FAIL} =====")
sys.exit(0 if FAIL == 0 else 1)
