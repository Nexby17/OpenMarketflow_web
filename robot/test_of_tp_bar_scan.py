"""F-199: тесты скана баров на пропущенный пробой уровня partial TP."""
import sys, time
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web\robot")
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web")
from strategy_of import OFParams, OrderFlowStrategy, LONG, SHORT


class LE:
    def __init__(self, price, side, lots, age=600.0):
        self.price, self.side, self.lots = price, side, lots
        self.added_ts = time.monotonic() - age


def mk(queue, direction, spread=375, min_profit=160, scan=True, period=60, window=240):
    p = OFParams()
    p.partial_tp = True
    p.spread = spread
    p.min_profit_per_lot = min_profit
    p.close_half_pct = 0
    p.use_timing = False
    p.use_vah_val = False
    p.use_vwema = False
    p.tp_bar_scan_enabled = scan
    p.tp_bar_scan_sec = period
    p.tp_bar_scan_window_sec = window
    p.catchup_tp_enabled = False
    s = OrderFlowStrategy(p)
    s._dir = direction
    s._total_lots = sum(l for _, l in queue)
    s._peak_lots = s._total_lots
    s._avg_price = sum(pr * l for pr, l in queue) / s._total_lots
    s._lot_queue = [LE(pr, direction, l) for pr, l in queue]
    s._current_price = queue[-1][0]
    s._tp_scan_ts = 0.0
    return s


def test_scan_closes_when_level_touched_and_profit_left():
    s = mk([(246775, 1), (246425, 1)], LONG)
    s._fetch_recent_bars = lambda since: [(since + 5, 246800.0, 246400.0)]   # ровно уровень
    acts = s._scan_missed_tp(246810.0)
    assert len(acts) == 1 and acts[0]["reason"] == "tp_bar_scan", acts
    assert s._total_lots == 1
    print("PASS scan closes (level touched)")


def test_scan_skips_when_price_back():
    s = mk([(246775, 1), (246425, 1)], LONG)
    s._fetch_recent_bars = lambda since: [(since + 5, 246800.0, 246400.0)]
    acts = s._scan_missed_tp(246500.0)     # pnl=75 < min_profit
    assert acts == []
    assert s._total_lots == 2
    print("PASS scan skips (price back)")


def test_scan_rate_limited():
    s = mk([(246775, 1), (246425, 1)], LONG)
    s._fetch_recent_bars = lambda since: [(since + 5, 247000.0, 246400.0)]
    first = s._scan_missed_tp(246900.0)
    second = s._scan_missed_tp(246900.0)   # сразу второй раз — период не истёк
    assert len(first) == 1 and second == []
    print("PASS scan rate-limited")


def test_scan_disabled():
    s = mk([(246775, 1), (246425, 1)], LONG, scan=False)
    s._fetch_recent_bars = lambda since: [(since + 5, 247000.0, 246400.0)]
    assert s._scan_missed_tp(246900.0) == []
    print("PASS scan disabled")


def test_scan_no_touch():
    s = mk([(246775, 1), (246425, 1)], LONG)
    s._fetch_recent_bars = lambda since: [(since + 5, 246790.0, 246400.0)]   # до 246800 не дошло
    assert s._scan_missed_tp(246810.0) == []
    print("PASS scan skips (no touch)")


def test_scan_short():
    s = mk([(245000, 1), (246775, 1)], SHORT)
    # SHORT тейк = 246775 - 375 = 246400; в окне low 246350
    s._fetch_recent_bars = lambda since: [(since + 5, 246500.0, 246350.0)]
    acts = s._scan_missed_tp(246380.0)     # pnl = 395 >= 160
    assert len(acts) == 1 and acts[0]["side"] == "buy"
    print("PASS scan short")


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        fn()
    print(f"\nALL {len(tests)} PASS")
