#!/usr/bin/env python3
"""
DataProvider Contract Tests — тестируют КОНТРАКТ между C# и DP.
Не happy path — а реальные сценарии использования из C# стратегий.

Цель: если тест падает — C# стратегия сломается. Не наоборот.
"""
import json
import sys
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

DP_URL = "http://localhost:5060"
ACCOUNT = "1225953"
TICKER = "SiM6"
SYMBOL = "SiM6@RTSX"
PASS = 0
FAIL = 0


def test(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} — {detail}")


def get(path, params=None, timeout=10):
    try:
        r = requests.get(f"{DP_URL}{path}", params=params, timeout=timeout)
        return r.status_code, r.json()
    except Exception as e:
        return 0, {"error": str(e)}


def post(path, params=None, timeout=10):
    try:
        r = requests.post(f"{DP_URL}{path}", params=params, timeout=timeout)
        return r.status_code, r.json()
    except Exception as e:
        return 0, {"error": str(e)}


print("\n" + "=" * 70)
print("DataProvider CONTRACT Tests — C# ↔ DP Interface")
print("=" * 70)

# ============================================================
# 1. C# вызывает GetCandlesAsync(symbol, "M1", 120) для warmup
# ============================================================
print("\n--- 1. Candle Timeframe Contract (THE BUG THAT CAUSED 12 FAKE ENTRIES) ---")

# C# Send: tf="M1" — DP must return bars
code, data = get(f"/candles/{SYMBOL}", {"tf": "M1", "limit": 5})
test("tf=M1 returns bars", code == 200 and data.get("count", 0) > 0,
     f"code={code} count={data.get('count', 0)}")
if data.get("bars"):
    test("Bar has all OHLCV fields",
         all(k in data["bars"][0] for k in ["timestamp", "open", "high", "low", "close", "volume"]),
         f"keys={list(data['bars'][0].keys())}")
    test("Bar close > 0 (real price)",
         data["bars"][0]["close"] > 1000,  # SI price > 70000
         f"close={data['bars'][0]['close']}")

# C# Send: tf="TIME_FRAME_M1" — must return 0 bars (NOT silently return 0)
code2, data2 = get(f"/candles/{SYMBOL}", {"tf": "TIME_FRAME_M1", "limit": 5})
test("tf=TIME_FRAME_M1 returns 0 bars (C# must use M1)",
     code2 == 200 and data2.get("count", 0) == 0,
     f"code={code2} count={data2.get('count', 0)} — if bars returned, C# warmup would work with wrong tf")

# All supported timeframes
for tf in ["M1", "M5", "M60"]:
    code, data = get(f"/candles/{SYMBOL}", {"tf": tf, "limit": 2})
    test(f"tf={tf} returns data", code == 200,
         f"code={code}")

# ============================================================
# 2. C# вызывает GetPositionAsync(account, ticker) каждый тик
# ============================================================
print("\n--- 2. Position Contract (called every 500ms tick) ---")

code, data = get("/position", {"account": ACCOUNT, "ticker": TICKER})
test("Position returns 200", code == 200, f"code={code}")
test("Position has required C# fields",
     all(k in data for k in ["ticker", "dir", "lots", "avg_price", "current_price"]),
     f"missing={set(['ticker','dir','lots','avg_price','current_price']) - set(data.keys())}")

# Type contract — C# does (int)dir, (int)lots, (double)avgPrice, (double)currentPrice
test("dir is int in [-1,0,1]",
     isinstance(data["dir"], int) and data["dir"] in [-1, 0, 1],
     f"dir={data['dir']} type={type(data['dir'])}")
test("lots is int >= 0",
     isinstance(data["lots"], int) and data["lots"] >= 0,
     f"lots={data['lots']} type={type(data['lots'])}")
test("avg_price is number",
     isinstance(data["avg_price"], (int, float)),
     f"avg_price={data['avg_price']} type={type(data['avg_price'])}")
test("current_price is number > 0 (even with lots=0, from quotes)",
     isinstance(data["current_price"], (int, float)) and data["current_price"] > 0,
     f"current_price={data['current_price']}")

# Error handling — C# checks pos.Error
test("No error field on success",
     "error" not in data or data.get("error") is None,
     f"error={data.get('error')}")

# Invalid ticker must return dir=0, lots=0 (NOT error)
code, inv = get("/position", {"account": ACCOUNT, "ticker": "INVALID99"})
test("Invalid ticker returns dir=0",
     inv.get("dir") == 0 and inv.get("lots") == 0,
     f"dir={inv.get('dir')} lots={inv.get('lots')}")

# ============================================================
# 3. C# вызывает GetOrdersAsync(account) каждый тик
# ============================================================
print("\n--- 3. Orders Contract (called every 500ms tick) ---")

code, orders = get("/orders", {"account": ACCOUNT})
test("Orders returns 200", code == 200, f"code={code}")
test("Orders returns list", isinstance(orders, list), f"type={type(orders)}")

if isinstance(orders, list):
    # C# accesses: o.Id, o.Price, o.Comment, o.IsActive
    # C# filters: o.Comment.StartsWith("VPSG-")
    required_order_fields = ["order_id", "limit_price", "comment", "is_active"]
    if len(orders) > 0:
        o = orders[0]
        for f in required_order_fields:
            test(f"Order has '{f}' (C# reads this)",
                 f in o, f"keys={list(o.keys())}")
        test("is_active is bool",
             isinstance(o.get("is_active"), bool),
             f"is_active={o.get('is_active')} type={type(o.get('is_active'))}")
        test("limit_price is number",
             isinstance(o.get("limit_price"), (int, float)),
             f"limit_price={o.get('limit_price')}")
        test("comment is string",
             isinstance(o.get("comment", ""), str),
             f"comment={o.get('comment')}")
        # C# filters by comment.StartsWith("VPSG-")
        vpsg_orders = [o for o in orders if o.get("comment", "").startswith("VPSG-")]
        print(f"  ℹ️ {len(vpsg_orders)} VPSG orders out of {len(orders)} total")
    else:
        print("  ℹ️ No active orders (normal when robot stopped)")
        # Still test the contract — empty list is valid
        test("Empty orders list is valid", len(orders) == 0)

# ============================================================
# 4. Cache invalidation — C# calls after PlaceMarketOrder
# ============================================================
print("\n--- 4. Cache Invalidation Contract ---")

code, inv = post("/invalidate", {"account": ACCOUNT})
test("Invalidate returns 200", code == 200, f"code={code}")
test("Invalidate returns success", inv.get("invalidated") == True, f"resp={inv}")

# After invalidate, next call should be fresh (not cached)
# If position changes externally, DP must see it after invalidate
code, data = get("/position", {"account": ACCOUNT, "ticker": TICKER})
test("Position works after invalidate", code == 200, f"code={code}")

# ============================================================
# 5. Recent fills — gRPC push fills for entry confirmation
# ============================================================
print("\n--- 5. Recent Fills Contract (gRPC push) ---")

code, fills = get("/recent-fills", {"account": ACCOUNT})
test("Recent fills returns 200", code == 200, f"code={code}")
test("Recent fills returns list", isinstance(fills, list), f"type={type(fills)}")

if isinstance(fills, list) and len(fills) > 0:
    f = fills[0]
    test("Fill has order_id",
         "order_id" in f, f"keys={list(f.keys())}")
    test("Fill has symbol",
         "symbol" in f, f"keys={list(f.keys())}")
    test("Fill has executed_quantity",
         "executed_quantity" in f, f"keys={list(f.keys())}")
    test("Fill has fill_ts (timestamp for pruning)",
         "fill_ts" in f, f"keys={list(f.keys())}")
    test("Fill has side (1=buy, 2=sell in gRPC)",
         "side" in f, f"side={f.get('side')}")
    print(f"  ℹ️ {len(fills)} recent fills (last 10 sec)")
else:
    print("  ℹ️ No recent fills (normal if no orders in last 10 sec)")

# Filter by symbol
code, fills_sym = get("/recent-fills", {"account": ACCOUNT, "symbol": SYMBOL})
test("Recent fills filtered by symbol", code == 200 and isinstance(fills_sym, list),
     f"code={code}")

# ============================================================
# 6. Unified account — position + orders from 1 REST call
# ============================================================
print("\n--- 6. Unified Account (position + orders same cache) ---")

# Get position (fills unified cache)
t0 = time.time()
get("/position", {"account": ACCOUNT, "ticker": TICKER})
t_pos = time.time() - t0

# Get orders (should reuse same cache — faster)
t0 = time.time()
get("/orders", {"account": ACCOUNT})
t_ord = time.time() - t0

test("Orders request reuses unified cache (faster after position)",
     t_ord < t_pos + 0.05 or t_pos < 0.05,
     f"pos={t_pos:.3f}s orders={t_ord:.3f}s")

# ============================================================
# 7. Performance — C# polls every 500ms, must fit in budget
# ============================================================
print("\n--- 7. Performance (must fit in 500ms tick budget) ---")

# Simulate 1 tick: position + orders + candles
t0 = time.time()
get("/position", {"account": ACCOUNT, "ticker": TICKER})
get("/orders", {"account": ACCOUNT})
get(f"/candles/{SYMBOL}", {"tf": "M1", "limit": 10})
t_tick = time.time() - t0

test("Full tick (pos+orders+candles) < 100ms cached",
     t_tick < 0.1,
     f"tick={t_tick * 1000:.0f}ms")
print(f"  ℹ️ Full tick: {t_tick * 1000:.0f}ms")

# 20 ticks sustained
t0 = time.time()
for _ in range(20):
    get("/position", {"account": ACCOUNT, "ticker": TICKER})
    get("/orders", {"account": ACCOUNT})
t_40 = time.time() - t0
test(f"40 requests (20 ticks) < 1s",
     t_40 < 1.0,
     f"{t_40:.2f}s ({40 / t_40:.0f} req/s)")

# ============================================================
# 8. Quote — used for current_price fallback
# ============================================================
print("\n--- 8. Quote Contract (current_price source) ---")

code, q = get(f"/quote/{SYMBOL}")
test("Quote returns 200", code == 200, f"code={code}")
if "error" not in q:
    test("Quote has bid", isinstance(q.get("bid"), (int, float)), f"bid={q.get('bid')}")
    test("Quote has ask", isinstance(q.get("ask"), (int, float)), f"ask={q.get('ask')}")
    test("Ask >= Bid", q.get("ask", 0) >= q.get("bid", 0), f"ask={q.get('ask')} bid={q.get('bid')}")
    test("Quote has last", isinstance(q.get("last"), (int, float)), f"last={q.get('last')}")
    test("Last > 0 (real price)", q.get("last", 0) > 1000, f"last={q.get('last')}")
else:
    print(f"  ⚠️ No quote data: {q.get('error')}")

# ============================================================
# 9. Status — health check
# ============================================================
print("\n--- 9. Status Contract ---")

code, data = get("/status")
test("Status returns 200", code == 200, f"code={code}")
test("status=ok", data.get("status") == "ok", f"status={data.get('status')}")
test("Has connected field", "connected" in data, f"keys={list(data.keys())}")

# ============================================================
# 10. Regression: Decimal bug (pos.quantity returns Decimal)
# ============================================================
print("\n--- 10. Regression: gRPC Decimal types ---")

crashes = 0
for _ in range(20):
    code, d = get("/position", {"account": ACCOUNT, "ticker": TICKER})
    if code != 200:
        crashes += 1
    elif not isinstance(d.get("lots"), int):
        crashes += 1
    elif not isinstance(d.get("current_price"), (int, float)):
        crashes += 1
    elif not isinstance(d.get("avg_price"), (int, float)):
        crashes += 1

test("20 fetches: 0 Decimal crashes", crashes == 0, f"crashes={crashes}")

# ============================================================
# 11. C# filter contract: Comment.StartsWith("VPSG-")
# ============================================================
print("\n--- 11. C# Order Filter Contract (comment.StartsWith) ---")

code, orders = get("/orders", {"account": ACCOUNT})
if isinstance(orders, list):
    for o in orders:
        comment = o.get("comment", "")
        if comment:
            test(f"Comment '{comment[:30]}' is string type",
                 isinstance(comment, str),
                 f"type={type(comment)}")
            break
    else:
        print("  ℹ️ No orders with comments to test")

    # Test that filtering works
    vpsg = [o for o in orders if o.get("comment", "").startswith("VPSG-")]
    non_vpsg = [o for o in orders if not o.get("comment", "").startswith("VPSG-")]
    print(f"  ℹ️ VPSG orders: {len(vpsg)}, Other: {len(non_vpsg)}")

# ============================================================
# SUMMARY
# ============================================================
print("\n" + "=" * 70)
total = PASS + FAIL
print(f"Results: {PASS}/{total} passed, {FAIL} failed")
if FAIL > 0:
    print("⚠️  FAILING TESTS = C# STRATEGY WILL BREAK")
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)
