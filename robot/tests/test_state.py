"""Test state: save, load, modify, verify persistence."""
import os
import sys

from state import StateManager, TrackedOrder, RobotState

TEST_FILE = "/tmp/test-robot-state.json"

# Clean up
if os.path.exists(TEST_FILE):
    os.remove(TEST_FILE)

sm = StateManager(TEST_FILE)
errors = []

# 1. Default state
s = sm.state
assert s.direction == 0, "Default direction should be 0"
assert s.entry_price == 0, "Default entry should be 0"
print("1. Default state: OK")

# 2. Save and reload
sm.state.direction = 1
sm.state.entry_price = 71825.0
sm.state.filled_levels = 3
sm.state.last_entry_price = 71825.0
sm.state.last_direction = 1
sm.state.round_trips = 5
sm.state.realized_pnl = 1234.5
sm.state.mode = "running"
sm.save()

sm2 = StateManager(TEST_FILE)
s2 = sm2.load()
assert s2.direction == 1, f"direction: {s2.direction}"
assert s2.entry_price == 71825.0, f"entry_price: {s2.entry_price}"
assert s2.filled_levels == 3, f"filled_levels: {s2.filled_levels}"
assert s2.round_trips == 5, f"round_trips: {s2.round_trips}"
assert s2.realized_pnl == 1234.5, f"realized_pnl: {s2.realized_pnl}"
assert s2.mode == "running", f"mode: {s2.mode}"
print("2. Save/load: OK")

# 3. Tracked orders
sm2.state.direction = -1
sm2.state.entry_price = 71900.0
sm2.add_tracked_order(TrackedOrder(
    order_id="order-123",
    client_order_id="cli-456",
    side=2,
    order_type="GRID",
    price=71931.0,
    level=1,
    status="ACTIVE",
    placed_at="2026-05-22T10:00:00+03:00",
))
sm2.add_tracked_order(TrackedOrder(
    order_id="order-789",
    client_order_id="cli-012",
    side=1,
    order_type="TP",
    price=71869.0,
    level=1,
    status="ACTIVE",
    placed_at="2026-05-22T10:00:01+03:00",
))
sm2.save()

sm3 = StateManager(TEST_FILE)
s3 = sm3.load()
assert s3.direction == -1, f"direction after grid: {s3.direction}"
assert len(s3.tracked_orders) == 2, f"tracked_orders count: {len(s3.tracked_orders)}"
print("3. Tracked orders: OK")

# 4. Update order status
sm3.update_tracked_order("order-123", "FILLED")
active = sm3.get_active_orders()
assert len(active) == 1, f"active orders after fill: {len(active)}"
assert active[0]["order_id"] == "order-789"
print("4. Update order: OK")

# 5. Get active by type
grid_active = sm3.get_active_orders("GRID")
assert len(grid_active) == 0, f"grid active after fill: {len(grid_active)}"
tp_active = sm3.get_active_orders("TP")
assert len(tp_active) == 1, f"tp active: {len(tp_active)}"
print("5. Filter by type: OK")

# 6. Clear
sm3.clear()
sm4 = StateManager(TEST_FILE)
s4 = sm4.load()
assert s4.direction == 0, f"direction after clear: {s4.direction}"
assert len(s4.tracked_orders) == 0, f"orders after clear: {len(s4.tracked_orders)}"
print("6. Clear: OK")

# 7. Atomic write test (corrupt recovery)
with open(TEST_FILE, "w") as f:
    f.write("{corrupt json")
sm5 = StateManager(TEST_FILE)
s5 = sm5.load()
assert s5.direction == 0, "Should get defaults on corrupt file"
print("7. Corrupt recovery: OK")

# Cleanup
os.remove(TEST_FILE)

print("\n✅ ALL TESTS PASSED")
