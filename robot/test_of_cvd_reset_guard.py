# -*- coding: utf-8 -*-
"""F-217: «daily reset» CVD не должен срабатывать днём.

Кейс 23.11: CVD -581 → бар с нулевой дельтой в 11:05 → история обнулилась,
сигналы пропали. Сброс теперь только около 07:00 MSK.
"""
import os
import sys
from datetime import datetime

HERE = r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web\robot"
for p in (os.path.join(HERE, "py4"), HERE, os.path.dirname(HERE)):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

from orderflow_engine import SignalEngine, BarMetrics  # noqa: E402
from datetime import timezone, timedelta  # noqa: E402

MSK = timezone(timedelta(hours=3))
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


def bar(cvd, ts):
    return BarMetrics(buy_volume=0, sell_volume=0, delta=0, delta_ratio=0.0,
                      total_volume=0, price_change=0.0, cvd=cvd, timestamp=ts)


def make_engine(prev_cvd, cur_cvd, when):
    e = SignalEngine(cvd_lookback=10)
    ts = when.timestamp()
    for i in range(15):
        e._bar_history.append(bar(float(prev_cvd), ts - (15 - i) * 300))
    e._cvd_ema_fast = float(prev_cvd)
    e._cvd_ema_slow = float(prev_cvd)
    e._cvd_trend_dir = -1
    e._cvd_trend_prev_cvd = float(prev_cvd)
    e._cvd_trend_ready = True
    e._bars_added = 100
    e._cvd_trend_last_bar = 99      # чтобы бар считался новым
    e._bar_history.append(bar(float(cur_cvd), ts))
    return e


def engine_with_history():
    return make_engine(-581.0, 0.0, datetime(2026, 11, 23, 11, 5, tzinfo=MSK))


def t_no_reset_midday():
    e = engine_with_history()
    e.check_cvd_trend(82450.0)
    assert e._cvd_ema_fast != 0.0, "EMA обнулились — значит сброс всё-таки сработал"
    assert abs(e._cvd_ema_fast) > 1.0, e._cvd_ema_fast


def t_reset_near_0700():
    e = make_engine(-5000.0, 10.0, datetime(2026, 11, 23, 7, 5, tzinfo=MSK))
    e.check_cvd_trend(82450.0)
    assert e._cvd_trend_dir == 0, "около 07:00 сброс должен произойти"


def t_no_reset_small_prev():
    e = make_engine(-100.0, 0.0, datetime(2026, 11, 23, 7, 5, tzinfo=MSK))
    e.check_cvd_trend(82450.0)
    assert e._cvd_ema_fast != 0.0, "малый CVD не повод обнулять историю"


print("F-217 CVD daily reset guard")
check("днём сброса нет", t_no_reset_midday)
check("около 07:00 сброс есть", t_reset_near_0700)
check("малый prev — без сброса", t_no_reset_small_prev)

passed = sum(RESULTS)
print(f"\n{passed}/{len(RESULTS)} PASS")
sys.exit(0 if passed == len(RESULTS) else 1)
