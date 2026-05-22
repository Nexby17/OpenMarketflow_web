"""Test feed: connect, subscribe, verify data arrives."""
import logging
import sys
import time
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

from feed import Feed

feed = Feed()

quote_count = 0
bar_count = 0
order_count = 0
trade_count = 0

def on_quote(q):
    global quote_count
    quote_count += 1
    if quote_count <= 3:
        print(f"  QUOTE #{quote_count}: bid={q.bid:.0f} ask={q.ask:.0f} last={q.last:.0f}")

def on_bar(b):
    global bar_count
    bar_count += 1
    print(f"  BAR #{bar_count}: O={b.open:.0f} H={b.high:.0f} L={b.low:.0f} C={b.close:.0f} V={b.volume:.0f} @ {b.timestamp:%H:%M}")

def on_order(e):
    global order_count
    order_count += 1
    print(f"  ORDER #{order_count}: id={e.order_id} side={e.side} status={e.status} qty={e.executed_quantity}")

def on_trade(e):
    global trade_count
    trade_count += 1
    print(f"  TRADE #{trade_count}: id={e.trade_id} price={e.price:.0f} qty={e.quantity}")

feed.on_quote = on_quote
feed.on_bar = on_bar
feed.on_order = on_order
feed.on_trade = on_trade

print("=== FEED TEST ===")
print("1. Connecting...")
try:
    feed.connect()
    print(f"   OK. Connected={feed.connected}")
except Exception as e:
    print(f"   FAIL: {e}")
    sys.exit(1)

print("2. Subscribing...")
try:
    feed.subscribe_all()
    print("   OK. Subscriptions started.")
except Exception as e:
    print(f"   FAIL: {e}")
    sys.exit(1)

print("3. Waiting 30 seconds for data...")
time.sleep(30)

print(f"\n=== RESULTS ===")
print(f"  Quotes received: {quote_count}")
print(f"  Bars received:   {bar_count}")
print(f"  Orders received: {order_count}")
print(f"  Trades received: {trade_count}")

# Latest state
lq = feed.latest_quote
if lq:
    print(f"  Latest quote: bid={lq.bid:.0f} ask={lq.ask:.0f} last={lq.last:.0f}")
else:
    print(f"  Latest quote: None")

feed.disconnect()

# Verdict
if quote_count > 0:
    print("\n✅ TEST PASSED: Quotes received")
else:
    print("\n❌ TEST FAILED: No quotes received in 30 seconds")
    sys.exit(1)
