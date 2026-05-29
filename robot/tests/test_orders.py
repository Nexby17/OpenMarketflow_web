"""Test orders: connect, get active orders, verify gRPC order management works."""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")

from FinamPy import FinamPy
from orders import OrderManager, BUY, SELL
import config

print("=== ORDERS TEST ===")

# 1. Connect
print("1. Connecting...")
fp = FinamPy(os.environ["FINAM_TOKEN"])
print(f"   Connected. Accounts: {fp.account_ids}")

om = OrderManager(fp)

# 2. Get active orders
print("2. Getting active orders...")
active = om.get_active_orders()
print(f"   Active orders: {len(active)}")
for o in active:
    side_str = "BUY" if o.side == BUY else "SELL"
    print(f"   - {o.order_type} {side_str} qty={o.quantity} @ {o.price:.0f} id={o.order_id} [{o.status}]")

# 3. Test that we can query account positions via gRPC
print("3. Getting account info...")
from FinamPy.grpc.accounts_service_pb2 import GetAccountRequest
account = fp.call_function(
    fp.accounts_stub.GetAccount,
    GetAccountRequest(account_id=config.FINAM_ACCOUNT_ID),
)
if account:
    print(f"   Account: {len(account.positions)} positions")
    for pos in account.positions:
        qty = float(pos.quantity.value) if pos.quantity else 0
        avg = float(pos.average_price.value) if pos.average_price else 0
        cur = float(pos.current_price.value) if pos.current_price else 0
        print(f"   - {pos.symbol}: qty={qty:.0f} avg={avg:.0f} cur={cur:.0f}")
else:
    print("   Account: None (may not support this API)")

fp.close_channel()

errors = []
if not isinstance(active, list):
    errors.append("get_active_orders didn't return list")

if errors:
    print(f"\n❌ TEST FAILED:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print(f"\n✅ TEST PASSED: Order manager functional")
