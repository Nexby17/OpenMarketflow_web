"""Test strategy: verify signal generation logic."""
from strategy import Strategy, StrategyParams, BUY, SELL

errors = []

# === 1. Entry signals ===
s = Strategy(StrategyParams(step_base=31, spread_base=31))
s.val = 71700
s.vah = 71900
s.poc = 71825

# Should LONG below VAL
sig = s.check_entry(71680)
assert sig and sig.action == "ENTRY" and sig.direction == 1, f"LONG signal: {sig}"
print(f"1a. Entry LONG @ {sig.price:.0f}: OK")

# Should SHORT above VAH
sig = s.check_entry(71920)
assert sig and sig.action == "ENTRY" and sig.direction == -1, f"SHORT signal: {sig}"
print(f"1b. Entry SHORT @ {sig.price:.0f}: OK")

# No signal inside VA
sig = s.check_entry(71800)
assert sig is None, f"Should be None inside VA: {sig}"
print("1c. No signal inside VA: OK")

# === 2. Entry fill → grid + POC-TP ===
s2 = Strategy(StrategyParams(step_base=31, spread_base=31))
s2.val = 71700
s2.vah = 71900
s2.poc = 71825

signals = s2.on_entry_fill(1, 71680)  # LONG @ 71680
assert len(signals) >= 1, f"Expected grid signal after entry fill: {signals}"
grid = [s for s in signals if s.action == "GRID"]
assert len(grid) == 1, f"Expected 1 grid signal: {grid}"
g = grid[0]
assert g.direction == BUY, f"Grid should be BUY (average down): {g.direction}"
assert g.price == 71680 - 31, f"Grid-1 price: {g.price} expected {71680-31}"
assert g.level == 1, f"Grid level: {g.level}"
print(f"2a. Entry fill → Grid-1 BUY @ {g.price:.0f}: OK")

poc_tp = [s for s in signals if s.action == "PLACE_POC_TP"]
assert len(poc_tp) == 1, f"Expected POC-TP: {signals}"
print(f"2b. Entry fill → POC-TP @ {poc_tp[0].price:.0f}: OK")

# === 3. Grid fill → next grid + TP ===
signals = s2.on_grid_fill(1, 71649)  # Grid-1 filled @ 71649
grid2 = [s for s in signals if s.action == "GRID"]
tp = [s for s in signals if s.action == "TP"]

assert len(grid2) == 1, f"Expected next grid: {signals}"
assert grid2[0].level == 2, f"Should be level 2: {grid2[0].level}"
assert grid2[0].price == 71680 - 62, f"Grid-2 price: {grid2[0].price}"
print(f"3a. Grid fill → Grid-2 BUY @ {grid2[0].price:.0f}: OK")

assert len(tp) == 1, f"Expected TP signal: {signals}"
assert tp[0].direction == SELL, f"TP should be SELL (LONG position)"
assert tp[0].price == 71649 + 31, f"TP price: {tp[0].price} expected {71649+31}"
print(f"3b. Grid fill → TP SELL @ {tp[0].price:.0f}: OK")

# === 4. Exit: POC hit (1 lot) ===
s3 = Strategy(StrategyParams())
s3.direction = 1
s3.entry_price = 71680
s3.filled_levels = 0
s3.poc = 71825
s3.current_price = 71830  # Above POC

sig = s3.check_exit()
assert sig and sig.action == "CLOSE_ALL", f"POC exit: {sig}"
print("4a. Exit: POC hit LONG (1 lot): OK")

# === 5. Exit: PnL/lot (2+ lots) ===
s4 = Strategy(StrategyParams(min_profit_per_lot=29))
s4.direction = 1
s4.entry_price = 71680
s4.filled_levels = 2  # 3 lots total
s4.current_price = 71750  # +70 pts from entry

sig = s4.check_exit()
assert sig and sig.action == "CLOSE_ALL", f"PnL exit: {sig}"
pnl_per_lot = s4.calc_unrealized_pnl(71750) / 3
print(f"5a. Exit: PnL/lot={pnl_per_lot:.0f} >= 29: OK")

# === 6. SHORT entry ===
s5 = Strategy(StrategyParams(step_base=31, spread_base=31))
s5.val = 71700
s5.vah = 71900
s5.poc = 71825

signals = s5.on_entry_fill(-1, 71920)  # SHORT @ 71920
grid = [s for s in signals if s.action == "GRID"]
assert grid[0].direction == SELL, f"Short grid should be SELL: {grid[0].direction}"
assert grid[0].price == 71920 + 31, f"Grid-1 above entry: {grid[0].price}"
print(f"6a. SHORT entry → Grid-1 SELL @ {grid[0].price:.0f}: OK")

# Grid fill → TP below
signals = s5.on_grid_fill(1, 71951)
tp = [s for s in signals if s.action == "TP"]
assert tp[0].direction == BUY, f"TP should be BUY (SHORT position)"
assert tp[0].price == 71951 - 31, f"TP price: {tp[0].price}"
print(f"6b. SHORT grid fill → TP BUY @ {tp[0].price:.0f}: OK")

# === 7. Close all resets ===
s5.on_close_all()
assert s5.direction == 0
assert s5.entry_price == 0
assert s5.filled_levels == 0
print("7. Close all resets state: OK")

# === 8. TP fill reduces levels ===
s6 = Strategy(StrategyParams())
s6.direction = 1
s6.entry_price = 71680
s6.filled_levels = 3
s6.on_tp_fill()
assert s6.filled_levels == 2, f"Should be 2: {s6.filled_levels}"
print("8. TP fill reduces levels: OK")

print("\n✅ ALL STRATEGY TESTS PASSED")
