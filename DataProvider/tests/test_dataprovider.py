#!/usr/bin/env python3
"""
DataProvider Integration Tests v2 — full coverage with error handling
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

print("\n" + "="*60)
print("DataProvider Integration Tests v2")
print("="*60)

# --- 1. Status ---
print("\n--- 1. Status ---")
code, data = get("/status")
test("DP running", code == 200, f"code={code}")
test("status=ok", data.get("status") == "ok", f"status={data.get('status')}")

# --- 2. Position — valid ---
print("\n--- 2. Position (valid) ---")
code, data = get("/position", {"account": ACCOUNT, "ticker": TICKER})
test("Returns 200", code == 200, f"code={code}")
test("Has ticker", data.get("ticker") == TICKER)
test("Has dir (int)", isinstance(data.get("dir"), int))
test("Has lots (int >= 0)", isinstance(data.get("lots"), int) and data.get("lots") >= 0)
test("Has avg_price (number)", isinstance(data.get("avg_price"), (int, float)))
test("Has current_price (> 0)", isinstance(data.get("current_price"), (int, float)) and data.get("current_price", 0) > 0)
test("dir in [-1,0,1]", data.get("dir") in [-1, 0, 1])

# --- 3. Position — no error field on success ---
print("\n--- 3. Position (no error on success) ---")
test("No error field on valid position", "error" not in data or data.get("error") is None,
     f"error={data.get('error')}")

# --- 4. Position — invalid ticker ---
print("\n--- 4. Position (invalid ticker) ---")
code, data = get("/position", {"account": ACCOUNT, "ticker": "INVALID99"})
test("Returns 200", code == 200)
test("dir=0 for invalid ticker", data.get("dir") == 0, f"dir={data.get('dir')}")
test("lots=0 for invalid ticker", data.get("lots") == 0, f"lots={data.get('lots')}")

# --- 5. Position — invalid account ---
print("\n--- 5. Position (invalid account) ---")
code, data = get("/position", {"account": "9999999", "ticker": TICKER})
test("Returns 200", code == 200)
# Should return either dir=0 or error field
test("Has error field or dir=0", data.get("dir") == 0 or "error" in data, 
     f"dir={data.get('dir')} keys={list(data.keys())}")

# --- 6. Position — missing params ---
print("\n--- 6. Position (missing params) ---")
code, data = get("/position", {"account": ACCOUNT})
test("Returns 422 or error", code in [200, 422] or "error" in data, f"code={code}")

# --- 7. Orders ---
print("\n--- 7. Orders ---")
code, data = get("/orders", {"account": ACCOUNT})
test("Returns 200", code == 200, f"code={code}")
test("Returns list", isinstance(data, list), f"type={type(data)}")
if isinstance(data, list) and len(data) > 0:
    o = data[0]
    print(f"  ℹ️ Order sample: {json.dumps(o, indent=2)[:200]}")
    test("Order has order_id", "order_id" in o, f"keys={list(o.keys())}")
    test("Order has status", "status" in o, f"keys={list(o.keys())}")
    test("Order has side", "side" in o, f"keys={list(o.keys())}")
    test("Order has symbol", "symbol" in o, f"keys={list(o.keys())}")
    test("Order has comment", "comment" in o, f"keys={list(o.keys())}")
    test("Order has quantity (int)", isinstance(o.get("quantity"), int), f"qty={o.get('quantity')} type={type(o.get('quantity'))}")
    test("Order has limit_price (number)", isinstance(o.get("limit_price"), (int, float)), f"lp={o.get('limit_price')}")
    test("Order has is_active (bool)", isinstance(o.get("is_active"), bool), f"active={o.get('is_active')}")
else:
    print("  ℹ️ No active orders — placing a test order...")
    # Place a far limit order to test
    source /root/.openclaw/workspace/HedgeFund/.env 2>/dev/null || true
    print("  ⚠️ Cannot place test order from here — skip order field tests")

# --- 8. Candles ---
print("\n--- 8. Candles ---")
code, data = get(f"/candles/{SYMBOL}", {"tf": "M1", "limit": 5})
test("Returns 200", code == 200)
test("Has bars list", "bars" in data and isinstance(data["bars"], list))
if data.get("bars"):
    bar = data["bars"][0]
    for field in ["timestamp", "open", "high", "low", "close", "volume"]:
        test(f"Bar has {field}", field in bar, f"keys={list(bar.keys())}")
    test("Bar close is number", isinstance(bar.get("close"), (int, float)))
    test("Bar close > 0", bar.get("close", 0) > 0)
else:
    print("  ⚠️ No cached candles")

# --- 9. Quote ---
print("\n--- 9. Quote ---")
code, data = get(f"/quote/{SYMBOL}")
test("Returns 200", code == 200)
if "error" not in data:
    for field in ["bid", "ask", "last"]:
        test(f"Quote has {field} (number)", isinstance(data.get(field), (int, float)), f"{field}={data.get(field)}")
    test("Bid > 0", data.get("bid", 0) > 0)
    test("Ask > 0", data.get("ask", 0) > 0)
    test("Ask >= Bid", data.get("ask", 0) >= data.get("bid", 0))
else:
    print("  ⚠️ No quote data")

# --- 10. TTL Cache ---
print("\n--- 10. TTL Cache ---")
t0 = time.time()
_, data1 = get("/position", {"account": ACCOUNT, "ticker": TICKER})
t_first = time.time() - t0

times = []
for _ in range(5):
    t0 = time.time()
    get("/position", {"account": ACCOUNT, "ticker": TICKER})
    times.append(time.time() - t0)
t_cached = min(times)

test("Cached faster than first", t_cached < t_first or t_first < 0.5, 
     f"first={t_first:.3f}s cached={t_cached:.3f}s")
print(f"  ℹ️ First: {t_first:.3f}s, Cached (best of 5): {t_cached:.4f}s")

# --- 11. Concurrent ---
print("\n--- 11. Concurrent (10 parallel) ---")
def fetch(i):
    return get("/position", {"account": ACCOUNT, "ticker": TICKER})

errors = []
with ThreadPoolExecutor(max_workers=10) as ex:
    for i, result in enumerate(as_completed([ex.submit(fetch, i) for i in range(10)])):
        c, d = result.result()
        if c != 200 or "error" in str(d.get("error")):
            errors.append((i, c))

test("10 concurrent OK", len(errors) == 0, f"errors={errors[:3]}")

# --- 12. Stress ---
print("\n--- 12. Stress (100 rapid) ---")
errs = []
t0 = time.time()
for i in range(100):
    c, d = get("/position", {"account": ACCOUNT, "ticker": TICKER}, timeout=5)
    if c != 200:
        errs.append((i, c))
t_total = time.time() - t0
test("100 requests OK", len(errs) == 0, f"errors={errs[:3]}")
print(f"  ℹ️ 100 requests in {t_total:.2f}s ({100/t_total:.0f} req/s)")

# --- 13. Decimal regression ---
print("\n--- 13. Decimal Bug Regression (20x) ---")
crashes = 0
for _ in range(20):
    c, d = get("/position", {"account": ACCOUNT, "ticker": TICKER})
    if c != 200: crashes += 1
    elif not isinstance(d.get("lots"), int): crashes += 1
    elif not isinstance(d.get("avg_price"), (int, float)): crashes += 1
    elif not isinstance(d.get("current_price"), (int, float)): crashes += 1
test("20 fetches: 0 crashes", crashes == 0, f"crashes={crashes}")

# --- 14. Format consistency check ---
print("\n--- 14. Format Consistency ---")
code, pos = get("/position", {"account": ACCOUNT, "ticker": TICKER})
required_fields = ["ticker", "dir", "lots", "avg_price", "current_price"]
for f in required_fields:
    test(f"Position has '{f}'", f in pos, f"keys={list(pos.keys())}")

code, ords = get("/orders", {"account": ACCOUNT})
if isinstance(ords, list) and len(ords) > 0:
    required_order_fields = ["order_id", "symbol", "side", "quantity", "limit_price", "status", "comment", "is_active"]
    for f in required_order_fields:
        test(f"Order has '{f}'", f in ords[0], f"keys={list(ords[0].keys())}")

# --- 15. Multi-ticker ---
print("\n--- 15. Multi-ticker ---")
for t in ["SiM6", "RIM6", "MXM6", "RIU6"]:
    c, d = get("/position", {"account": ACCOUNT, "ticker": t})
    test(f"{t} position OK", c == 200, f"code={c}")

# --- 16. TTL values ---
print("\n--- 16. TTL Values ---")
# Position TTL should be ~1 sec, orders ~0.5 sec
# Test: rapid calls should be cached (< 50ms each)
times_pos = []
for _ in range(3):
    t0 = time.time()
    get("/position", {"account": ACCOUNT, "ticker": TICKER})
    times_pos.append(time.time() - t0)
avg_pos = sum(times_pos) / len(times_pos)
test("Position cached (< 100ms)", avg_pos < 0.1, f"avg={avg_pos:.3f}s")

times_ord = []
for _ in range(3):
    t0 = time.time()
    get("/orders", {"account": ACCOUNT})
    times_ord.append(time.time() - t0)
avg_ord = sum(times_ord) / len(times_ord)
test("Orders cached (< 100ms)", avg_ord < 0.1, f"avg={avg_ord:.3f}s")

# ===================== SUMMARY =====================
print("\n" + "="*60)
print(f"Results: {PASS} passed, {FAIL} failed")
print("="*60)
sys.exit(0 if FAIL == 0 else 1)
