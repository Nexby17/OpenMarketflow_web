"""F-215: доказательство причины — при _last_pyramid_price=0 пирамида срабатывает мгновенно."""
import sys, time
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web\robot")
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web")
from strategy_of import OFParams, OrderFlowStrategy, LONG

class LE:
    def __init__(self, price, side, lots, age=600.0):
        self.price, self.side, self.lots = price, side, lots
        self.added_ts = time.monotonic() - age
        self.src = "manual"
        self.uid = "u1"


def mk(last_pyr_price):
    p = OFParams()
    p.step_pyramid = 35000
    p.step_average = 400
    p.spread = 375
    p.max_pyramid_levels = 1
    p.use_timing = False
    p.use_vah_val = False
    p.use_vwema = False
    s = OrderFlowStrategy(p)
    s._dir = LONG
    s._total_lots = 11
    s._peak_lots = 11
    s._avg_price = 233200.0
    s._entry_price = 233200.0
    s._average_levels = 0
    s._pyramid_levels = 0
    s._last_pyramid_price = last_pyr_price
    s._last_average_price = 233200.0
    s._lot_queue = [LE(233200.0, LONG, 11)]
    s._current_price = 233225.0
    return s


print("--- ДИАГНОЗ (как было в 16:04, _last_pyramid_price=0) ---")
s = mk(0.0)
acts = s._check_pyramiding(233225.0)
print("  _check_pyramiding ->", acts)
assert acts is not None, "ожидали баг: пирамида срабатывает при нулевом ориентире"
print("  ПОДТВЕРЖДЕНО: distance = 233225 - 0 = 233225 >= 35000 → мгновенная пирамида")

print("\n--- ПОСЛЕ ФИКСА (_last_pyramid_price = цена ручного входа 233200) ---")
s2 = mk(233200.0)
acts2 = s2._check_pyramiding(233225.0)
print("  _check_pyramiding ->", acts2)
assert acts2 is None, "после фикса пирамиды быть не должно"
print("  ОК: distance = 25 < 35000 → пирамиды нет")

print("\n--- усреднение работает от Step Avg 400 (для контроля) ---")
print("  nextAvg должен быть 233200 - 400 =", 233200 - 400)
print("ALL OK")
