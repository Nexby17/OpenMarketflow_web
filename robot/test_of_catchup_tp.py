"""F-198: тесты догона пропущенных partial TP после обрыва данных."""
import sys, os, time
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web\robot")
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web")
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web")

from strategy_of import OFParams, OrderFlowStrategy, LONG, SHORT, FLAT


class LE:
    def __init__(self, price, side, lots, age=60.0):
        self.price, self.side, self.lots = price, side, lots
        self.added_ts = time.monotonic() - age


def mk(price_lots, direction, spread=375, min_profit=160, catchup=True):
    p = OFParams()
    p.partial_tp = True
    p.spread = spread
    p.min_profit_per_lot = min_profit
    p.catchup_tp_enabled = catchup
    p.catchup_tp_min_gap_sec = 30
    p.close_half_pct = 0
    p.use_timing = False
    s = OrderFlowStrategy(p)
    s._dir = direction
    s._total_lots = sum(l for _, l in price_lots)
    s._peak_lots = s._total_lots
    s._lot_queue = [LE(pr, direction, l) for pr, l in price_lots]
    s._avg_price = sum(pr * l for pr, l in price_lots) / s._total_lots
    s._current_price = price_lots[-1][0]
    return s


def test_catchup_long_closes_when_level_passed_and_profit_left():
    s = mk([(247250, 1), (246775, 1), (246425, 1)], LONG)
    # окно: 60 сек назад, в нём был high 246825 >= 246425+375=246800
    s._blind_since = time.time() - 60
    s._fetch_recent_bars = lambda since: [(since + 5, 246825.0, 246400.0)]
    acts = s._catchup_missed_tp(246810.0)     # цена ещё выше входа -> профит есть
    assert len(acts) == 1, acts
    assert acts[0]["action"] == "partial_tp" and acts[0]["reason"] == "catchup_tp"
    assert acts[0]["qty"] == 1 and acts[0]["entryPrice"] == 246425.0
    assert s._total_lots == 2
    print("PASS long catchup closes lot")


def test_catchup_skips_when_price_back_below_min_profit():
    s = mk([(247250, 1), (246775, 1), (246425, 1)], LONG)
    s._blind_since = time.time() - 60
    s._fetch_recent_bars = lambda since: [(since + 5, 246825.0, 246400.0)]
    acts = s._catchup_missed_tp(246500.0)     # тейк был, но цена вернулась: pnl=75 < min_profit=160
    assert acts == [], acts
    assert s._total_lots == 3
    print("PASS long catchup skips (price came back)")


def test_catchup_skips_when_level_not_passed():
    s = mk([(247250, 1), (246775, 1), (246425, 1)], LONG)
    s._blind_since = time.time() - 60
    s._fetch_recent_bars = lambda since: [(since + 5, 246790.0, 246400.0)]   # до 246800 не дошло
    acts = s._catchup_missed_tp(246810.0)
    assert acts == []
    print("PASS catchup skips (level not reached)")


def test_catchup_short():
    s = mk([(246425, 1), (246775, 1)], SHORT, spread=375, min_profit=160)
    s._blind_since = time.time() - 90
    # для SHORT тейк = 246775 - 375 = 246400; в окне low 246350 -> пройден
    s._fetch_recent_bars = lambda since: [(since + 5, 246500.0, 246350.0)]
    acts = s._catchup_missed_tp(246380.0)     # pnl = 246775-246380 = 395 >= 160
    assert len(acts) == 1 and acts[0]["side"] == "buy"
    assert s._total_lots == 1
    print("PASS short catchup")


def test_catchup_disabled_by_param():
    s = mk([(246425, 1)], LONG, catchup=False)
    s._blind_since = time.time() - 60
    s._fetch_recent_bars = lambda since: [(since + 5, 247000.0, 246400.0)]
    assert s._catchup_missed_tp(246900.0) == []
    print("PASS disabled by param")


def test_catchup_short_gap_ignored():
    s = mk([(246425, 1)], LONG)
    s._blind_since = time.time() - 5      # 5 сек < 30 порога
    s._fetch_recent_bars = lambda since: [(since + 1, 247000.0, 246400.0)]
    assert s._catchup_missed_tp(246900.0) == []
    print("PASS short gap ignored")


def test_catchup_runs_once():
    # LIFO-лот должен быть НИЖНИМ (246425), иначе уровень тейка выше окна
    s = mk([(246775, 1), (246425, 1)], LONG)
    s._blind_since = time.time() - 60
    s._fetch_recent_bars = lambda since: [(since + 5, 247000.0, 246400.0)]
    first = s._catchup_missed_tp(246900.0)
    second = s._catchup_missed_tp(246900.0)
    assert len(first) == 1 and second == []
    print("PASS runs once (window reset)")


if __name__ == "__main__":
    _tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for _name, _fn in _tests:
        _fn()
    print(f"\nALL {len(_tests)} PASS")
