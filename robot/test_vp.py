"""Test VP: load real bars via gRPC, calculate VP, verify sanity."""
from FinamPy import FinamPy
from google.protobuf.timestamp_pb2 import Timestamp
from google.type.interval_pb2 import Interval
from datetime import datetime, timezone, timedelta
import os

from vp import VolumeProfile

fp = FinamPy(os.environ["FINAM_TOKEN"])
symbol = "SiM6@RTSX"
finam_tf, tf_range, _ = fp.timeframe_to_finam_timeframe("M1")

# Fetch last hour of bars
now = datetime.now(timezone.utc)
start = now - timedelta(hours=1)

bars_resp = fp.call_function(
    fp.marketdata_stub.Bars,
    __import__("FinamPy.grpc.marketdata_service_pb2", fromlist=["BarsRequest"]).BarsRequest(
        symbol=symbol,
        timeframe=finam_tf,
        interval=Interval(
            start_time=Timestamp(seconds=int(start.timestamp())),
            end_time=Timestamp(seconds=int(now.timestamp())),
        ),
    ),
)

if not bars_resp or not bars_resp.bars:
    print("No bars received")
    exit(1)

bars = list(bars_resp.bars)
print(f"Fetched {len(bars)} bars")

# Feed bars into VP
vp = VolumeProfile(lookback=33, bin_size=50, va_percent=0.70)

for bar in bars:
    close = float(bar.close.value)
    vol = float(bar.volume.value)
    vp.add_bar(close, vol)

result = vp.calculate()

if result is None:
    print("VP calculation returned None")
    exit(1)

last_close = float(bars[-1].close.value)

print(f"\n=== VP RESULT ===")
print(f"  POC        = {result.poc:.0f}")
print(f"  VAH        = {result.vah:.0f}")
print(f"  VAL        = {result.val:.0f}")
print(f"  VA width   = {result.vah - result.val:.0f}")
print(f"  Last close = {last_close:.0f}")

# Top 5 bins
top_bins = sorted(result.bins.items(), key=lambda x: -x[1])[:5]
print(f"\n  Top 5 bins:")
max_vol = max(v for _, v in top_bins)
for bin_start, vol in top_bins:
    bar = "█" * int(vol / max_vol * 30)
    print(f"    {bin_start:8.0f}-{bin_start+50:.0f}: vol={vol:.0f} {bar}")

# Sanity checks
errors = []
if result.poc <= 0:
    errors.append("POC <= 0")
if result.vah <= result.val:
    errors.append(f"VAH ({result.vah:.0f}) <= VAL ({result.val:.0f})")
if result.poc < result.val or result.poc > result.vah:
    errors.append(f"POC ({result.poc:.0f}) outside VA [{result.val:.0f}, {result.vah:.0f}]")
if result.vah - result.val < 50:
    errors.append(f"VA too narrow: {result.vah - result.val:.0f}")

fp.close_channel()

if errors:
    print(f"\n❌ TEST FAILED:")
    for e in errors:
        print(f"  - {e}")
    exit(1)
else:
    print(f"\n✅ TEST PASSED: VP calculated correctly")
    print(f"  POC={result.poc:.0f} VA=[{result.val:.0f}, {result.vah:.0f}] width={result.vah-result.val:.0f}")
