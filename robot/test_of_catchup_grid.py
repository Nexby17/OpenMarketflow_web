# -*- coding: utf-8 -*-
"""F-093: тест решётки catch-up усреднений + fill-замены по сигнальной цене."""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(HERE)
for p in (os.path.join(HERE, "py4"), HERE, _PROJ):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

from strategy_of import OFParams, OrderFlowStrategy, LONG, LotEntry  # noqa: E402
from orderflow_engine import BarMetrics  # noqa: E402

s_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, s_dir)

def make():
    p = OFParams()
    p.lots = 1
    p.step_average = 36
    p.max_average_levels = 10
    p.enable_max_levels = True
    s = OrderFlowStrategy(p)
    s._dir = LONG
    s._total_lots = 1
    s._avg_price = 8500.0
    s._entry_price = 8500.0
    s._lot_queue.append(LotEntry(price=8500.0, side=LONG, lots=1, added_ts=0.0))
    for _ in range(20):
        m = BarMetrics.__new__(BarMetrics)
        m.delta = 0
        m.buy = 0
        m.sell = 0
        s.signals.add_bar(m, 8400, 8390, 8395)
    return s

def t_catchup_grid():
    s = make()
    a = s._check_averaging(8320.0)  # distance 180 -> missed 5
    assert len(a) == 5, f"expected 5 levels, got {len(a)}"
    prices = [x["price"] for x in a]
    assert prices == [8320.0, 8284.0, 8248.0, 8212.0, 8176.0], prices
    # очередь: решётка вместо кучи одинаковых цен
    q_prices = [e.price for e in s._lot_queue]
    assert len(set(q_prices[1:])) == 5, f"grid prices must be distinct: {q_prices}"

def t_fill_replaces_matching_layer():
    s = make()
    s._check_averaging(8320.0)
    # fills приходят в порядке действий, каждый со своей сигнальной ценой:
    s.update_fill_price(8318.0, "average", 8320.0)
    s.update_fill_price(8282.0, "average", 8284.0)
    q_prices = [e.price for e in s._lot_queue]
    assert 8318.0 in q_prices and 8282.0 in q_prices, q_prices
    # сигнальные цены заменились, остальные уровни решётки на месте:
    assert 8320.0 not in q_prices, q_prices
    assert 8284.0 not in q_prices, q_prices

if __name__ == "__main__":
    ok = True
    try:
        t_catchup_grid()
        print("  PASS catchup_grid")
    except Exception as e:
        ok = False
        print("  FAIL catchup_grid:", e)
    try:
        t_fill_replaces_matching_layer()
        print("  PASS fill_replaces_matching_layer")
    except Exception as e:
        ok = False
        print("  FAIL fill_replaces_matching_layer:", e)
    sys.exit(0 if ok else 1)
