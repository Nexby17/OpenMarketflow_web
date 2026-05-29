"""Test risk manager."""
from risk import RiskManager

rm = RiskManager()

errors = []

# PnL limits
ok, _ = rm.check_pnl(500)
assert ok, "Positive PnL should be OK"
ok, _ = rm.check_pnl(-5000)
assert ok, "PnL -5000 should be OK"
ok, msg = rm.check_pnl(-8000)
assert not ok, "PnL -8000 should trigger stop"
print(f"1. PnL limits: OK (stop at {msg})")

# Lots limits
ok, _ = rm.check_lots(50)
assert ok, "50 lots should be OK"
ok, msg = rm.check_lots(150)
assert not ok, "150 lots should be rejected"
print(f"2. Lots limits: OK (reject at {msg})")

# Time checks (just verify they return bools)
ok, msg = rm.can_trade()
print(f"3. Can trade now: {ok} ({msg})")

is_clearing = rm.is_clearing()
print(f"4. Is clearing: {is_clearing}")

# Custom params
rm2 = RiskManager(max_loss=-1000, max_lots=10)
ok, _ = rm2.check_pnl(-999)
assert ok
ok, _ = rm2.check_pnl(-1001)
assert not ok
ok, _ = rm2.check_lots(10)
assert ok
ok, _ = rm2.check_lots(11)
assert not ok
print("5. Custom params: OK")

print("\n✅ ALL RISK TESTS PASSED")
