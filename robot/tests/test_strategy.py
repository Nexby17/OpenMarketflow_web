"""Test strategy v2: verify grid_levels list tracking, no duplicates."""
from strategy import Strategy, StrategyParams, GridLevel, BUY, SELL

errors = []

# === 1. Entry signals ===
s = Strategy(StrategyParams())
s.val = 71700
s.vah = 71900
s.poc = 71825

sig = s.check_entry(71680)
assert sig and sig.action == "ENTRY" and sig.direction == 1
print("1a. Entry LONG: OK")

sig = s.check_entry(71920)
assert sig and sig.action == "ENTRY" and sig.direction == -1
print("1b. Entry SHORT: OK")

# No signal inside VA
assert s.check_entry(71800) is None
print("1c. No signal inside VA: OK")

# Reject price=0
assert s.check_entry(0) is None
print("1d. Reject price=0: OK")

# === 2. Entry fill → grid levels ===
s2 = Strategy(StrategyParams(step_base=31, spread_base=31))
s2.val = 71700
s2.vah = 71900
s2.poc = 71825

signals = s2.on_entry_fill(1, 71680)
assert s2.direction == 1
assert s2.entry_price == 71680
assert len(s2.grid_levels) == 1  # Grid-1 registered
assert s2.grid_levels[0].level == 1
assert s2.grid_levels[0].price == 71680 - 31  # 71649
assert s2.grid_levels[0].status == "PENDING"
print("2a. Entry fill → Grid-1 registered: OK")

# === 3. Grid fill → next grid + TP ===
signals = s2.on_grid_fill(71649)  # Grid-1 filled
assert s2.grid_levels[0].status == "FILLED"
assert s2.grid_levels[0].tp_price == 71649 + 31  # TP at spread
assert len(s2.grid_levels) == 2  # Grid-2 added
assert s2.grid_levels[1].level == 2
assert s2.grid_levels[1].price == 71680 - 62  # 71618
print("3a. Grid-1 fill → Grid-2 + TP: OK")

# === 4. Grid fill DUPLICATE rejected ===
signals = s2.on_grid_fill(71649)  # Grid-1 already filled!
assert signals == [], f"Duplicate fill should return empty: {signals}"
assert len(s2.grid_levels) == 2  # No new level
print("4a. Duplicate grid fill REJECTED: OK")

# === 5. TP fill ===
result = s2.on_tp_fill(71680)  # Grid-1 TP filled
assert result == True
assert s2.grid_levels[0].tp_price == 0  # TP cleared
print("5a. TP fill clears tp_price: OK")

# Wrong TP price
result = s2.on_tp_fill(99999)
assert result == False
print("5b. Wrong TP price rejected: OK")

# === 6. Grid fill → Grid-3 ===
signals = s2.on_grid_fill(71618)  # Grid-2 filled
assert s2.grid_levels[1].status == "FILLED"
assert len(s2.grid_levels) == 3  # Grid-3
assert s2.grid_levels[2].level == 3
print("6a. Grid-2 fill → Grid-3: OK")

# === 7. Paper fill detection ===
s3 = Strategy(StrategyParams(step_base=31, spread_base=31))
s3.val = 71700
s3.vah = 71900
s3.poc = 71825
s3.on_entry_fill(-1, 71920)  # SHORT

# Price reaches grid-1 (71951)
grid = s3.check_paper_grid_fill(71951)
assert grid is not None and grid.level == 1
print("7a. Paper grid fill detected: OK")

# Price NOT reaching grid
grid = s3.check_paper_grid_fill(71940)
assert grid is None
print("7b. Paper grid NOT reached: OK")

# === 8. Paper TP fill detection ===
s3.on_grid_fill(71951)  # Fill grid-1
# TP is at 71951 - 31 = 71920
tp_hit = False
for g in s3.grid_levels:
    if g.status == "FILLED" and g.tp_price > 0 and g.tp_price >= 71920:
        tp_hit = True
assert tp_hit
print("8a. Paper TP level exists: OK")

# === 9. POC exit (1 lot) ===
s4 = Strategy(StrategyParams())
s4.direction = 1
s4.entry_price = 71680
s4.poc = 71825
s4.current_price = 71830

sig = s4.check_exit()
assert sig and sig.action == "CLOSE_ALL"
print("9a. POC exit 1 lot: OK")

# === 10. PnL/lot exit (2+ lots) ===
s5 = Strategy(StrategyParams(min_profit_per_lot=29))
s5.direction = 1
s5.entry_price = 71680
s5.on_entry_fill(1, 71680)
s5.on_grid_fill(71649)  # filled_levels=1, total_lots=2
s5.current_price = 71750

sig = s5.check_exit()
assert sig and sig.action == "CLOSE_ALL"
print("10a. PnL/lot exit: OK")

# === 11. Close all resets everything ===
s5.on_close_all()
assert s5.direction == 0
assert s5.entry_price == 0
assert len(s5.grid_levels) == 0
print("11a. Close all resets: OK")

# === 12. calc_unrealized_pnl with entry=0 returns 0 ===
s6 = Strategy(StrategyParams())
s6.direction = 1
s6.entry_price = 0  # Bug guard
s6.current_price = 71750
assert s6.calc_unrealized_pnl(71750) == 0
print("12a. PnL with entry=0 returns 0: OK")

print("\n✅ ALL STRATEGY V2 TESTS PASSED")
