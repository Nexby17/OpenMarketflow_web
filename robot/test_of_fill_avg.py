# -*- coding: utf-8 -*-
"""F-086: тест update_fill_price — avg строго от очереди fill-цен (без сигнального дрейфа)."""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(HERE)
for p in (os.path.join(HERE, "py4"), HERE, _PROJ):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

from strategy_of import OFParams, OrderFlowStrategy, LotEntry, LONG  # noqa: E402
import time  # noqa: E402

RESULTS = []

def check(name, fn):
    try:
        fn()
        RESULTS.append(True)
        print(f"  PASS {name}")
    except AssertionError as ex:
        RESULTS.append(False)
        print(f"  FAIL {name}: {ex}")
    except Exception as ex:
        RESULTS.append(False)
        print(f"  FAIL {name}: {type(ex).__name__}: {ex}")

def t_fill_avg_no_signal_drift():
    """Инцидент 19.10: avg дрейфовал, т.к. update_fill_price подмешивал fill
    к сигнальному avg. Теперь avg строго от очереди fill-цен."""
    p = OFParams()
    p.lots = 1
    s = OrderFlowStrategy(p)
    sig_fills = [(83595.5, 83597.0), (83622.0, 83627.0), (83648.0, 83648.0), (83677.5, 83687.0)]
    for sig, fill in sig_fills:
        # стратегия сначала пишет сигнальную цену (как _check_pyramiding)
        s._lot_queue.append(LotEntry(price=sig, side=LONG, lots=1, added_ts=time.monotonic()))
        s._total_lots += 1
        s._avg_price = sum(e.price for e in s._lot_queue) / s._total_lots
        # затем main_of вызывает fill-обновление
        s.update_fill_price(fill, "entry" if fill == 83597.0 else "pyramid")
    broker_avg = (83597 + 83627 + 83648 + 83687) / 4
    assert abs(s._avg_price - broker_avg) < 1e-9, f"{s._avg_price} != {broker_avg}"
    # PnL по журналу совпадает с брокерским gross
    pnl = (83651 - s._avg_price) * 4
    assert abs(pnl - 45.0) < 1e-6, pnl

def t_fill_avg_short():
    p = OFParams()
    p.lots = 1
    s = OrderFlowStrategy(p)
    for sig, fill in [(83637.5, 83633.0), (83609.5, 83604.0), (83582.5, 83582.0)]:
        s._lot_queue.append(LotEntry(price=sig, side=-1, lots=1, added_ts=time.monotonic()))
        s._total_lots += 1
        s.update_fill_price(fill, "entry" if fill == 83633.0 else "pyramid")
    broker_avg = (83633 + 83604 + 83582) / 3
    assert abs(s._avg_price - broker_avg) < 1e-9, f"{s._avg_price} != {broker_avg}"

def t_fill_zero_price_ignored():
    p = OFParams()
    p.lots = 1
    s = OrderFlowStrategy(p)
    s._lot_queue.append(LotEntry(price=100.0, side=LONG, lots=1, added_ts=time.monotonic()))
    s._total_lots = 1
    s._avg_price = 100.0
    s.update_fill_price(0, "pyramid")  # не должен ничего менять
    assert s._avg_price == 100.0
    assert s._lot_queue[-1].price == 100.0


if __name__ == "__main__":
    print("=== F-086 fill avg tests ===")
    check("fill_avg_no_signal_drift", t_fill_avg_no_signal_drift)
    check("fill_avg_short", t_fill_avg_short)
    check("fill_zero_price_ignored", t_fill_zero_price_ignored)
    passed = sum(1 for r in RESULTS if r)
    print(f"=== {passed}/{len(RESULTS)} PASS ===")
    sys.exit(0 if passed == len(RESULTS) else 1)
