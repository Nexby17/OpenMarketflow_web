"""F-215b: страховка — при загрузке state с открытой позицией ориентир пирамиды не 0."""
import sys
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web\robot")
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web")
from strategy_of import OFParams, OrderFlowStrategy, LONG, FLAT


def make():
    p = OFParams()
    p.step_pyramid = 35000
    p.use_timing = False
    p.use_vah_val = False
    p.use_vwema = False
    return OrderFlowStrategy(p)


st_bad = {
    "dir": 1, "totalLots": 11, "avgPrice": 233200.0, "entryPrice": 233200.0,
    "lastPyramidPrice": 0.0, "pyramidLevels": 0, "averageLevels": 2,
    "peakLots": 11, "peak_lots": 11,
    "lotQueue": [{"price": 233200.0, "lots": 11, "side": 1}],
}
s = make()
s.load_state(st_bad)
print("после load_state: lastPyramidPrice =", s._last_pyramid_price, "| dir:", s._dir)
assert s._last_pyramid_price > 0, "ориентир должен быть восстановлен"

acts = s._check_pyramiding(233225.0)
print("  _check_pyramiding(233225) ->", acts)
assert acts is None, "пирамиды быть не должно"

st_ok = dict(st_bad, lastPyramidPrice=233200.0)
s2 = make()
s2.load_state(st_ok)
print("\nнормальный state: lastPyramidPrice =", s2._last_pyramid_price)
print("  _check_pyramiding(233225) ->", s2._check_pyramiding(233225.0))

st_flat = {"dir": 0, "totalLots": 0, "avgPrice": 0.0, "lastPyramidPrice": 0.0, "lotQueue": []}
s3 = make()
s3.load_state(st_flat)
print("\nFLAT state: lastPyramidPrice =", s3._last_pyramid_price, "(должно остаться 0)")
assert s3._last_pyramid_price == 0.0

print("\nALL OK")
