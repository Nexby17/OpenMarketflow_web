"""Integration test: start robot, let it run 15 sec, verify it works."""
import logging
import sys
import time
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

from main import Robot

robot = Robot()
print("=== INTEGRATION TEST ===")
print("1. Starting robot...")

try:
    robot.start()
    print("   Started OK")
except Exception as e:
    print(f"   FAIL: {e}")
    sys.exit(1)

# Check status
status = robot.get_status()
print(f"2. Status: mode={status['mode']} dir={status['direction']} "
      f"VP: VAL={status['val']:.0f} VAH={status['vah']:.0f} POC={status['poc']:.0f} "
      f"price={status['current_price']:.0f}")

errors = []
if status['mode'] != 'running':
    errors.append(f"Mode is {status['mode']}, expected running")
if status['poc'] <= 0:
    errors.append("POC not calculated")
# Don't check current_price immediately — quotes take a moment to arrive

# Wait and check VP updates
print("3. Waiting 15 seconds for VP updates...")
time.sleep(15)

status2 = robot.get_status()
print(f"   After 15s: price={status2['current_price']:.0f} poc={status2['poc']:.0f}")

if status2['current_price'] <= 0:
    errors.append("No price after 15 sec")

# Stop
robot.stop(close_position=False)
print("4. Stopped")

if errors:
    print(f"\n❌ TEST FAILED:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print(f"\n✅ INTEGRATION TEST PASSED")
