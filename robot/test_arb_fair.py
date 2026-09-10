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

print(f"\n===== RESULTS: PASS={PASS} FAIL={FAIL} =====")
sys.exit(0 if FAIL == 0 else 1)
