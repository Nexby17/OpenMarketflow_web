"""Сеть безопасности (23.11): режим действует, позиции нет → повторный вход.

Инциденты 23.11: MTLR/PLZL стояли во флэте в действующем SHORT-режиме —
кросс-входы гасились STALE-OPEN (бары M30/H1 опаздывают на 16-30 мин),
сигнал одноразовый и больше не повторялся.

Сценарии:
  1. Флэт + установленный SHORT-режим + бар закрыт после старта → open re-regime.
  2. Позиция есть → повторного входа НЕТ.
  3. Кулдаун: бар закрылся ДО последнего выхода → входа НЕТ; после — есть.
  4. D1-фильтр против направления → входа НЕТ.
  5. Строгий дончиан (use_cross=use_pullback=False) → сеть НЕ работает.
  6. На баре кросса уже есть open → дубля НЕТ (cross, не re-regime).
  7. Стоп-выход (sl) в живом режиме → re-regime в ту же сторону ЗАПРЕЩЁН
     до смены режима (приказ Самурая 23.11 12:48).

Offline: синтетические свечи, без сети.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_mtlr import MtlrParams
from strategy_mtlr import StrategyEngine, LONG, SHORT

TF = 1800  # M30
NOW = time.time()
BASE = int(NOW) - 80 * TF


def mk_candles(phases):
    candles = []
    c = 1000.0
    for d, n in phases:
        for _ in range(n):
            o = c
            c = c + d * 60.0
            h = max(o, c) + 10.0
            l = min(o, c) - 10.0
            candles.append({"t": BASE + len(candles) * TF, "o": o, "h": h, "l": l, "c": c})
    return candles


def mk_engine(candles, **over):
    p = MtlrParams()
    p.paper = True
    p.timeframe = "M30"  # синтетика теста — M30
    p.use_cross = True
    p.use_pullback = False
    p.use_donchian = False
    p.use_rsi = False
    p.use_regime = False
    p.use_d1 = False
    p.entry_by_limit = False
    for k, v in over.items():
        setattr(p, k, v)
    eng = StrategyEngine(p)
    eng.recompute(candles)
    return eng


def opens(evs):
    return [e for e in evs if e.get("action") == "open"]


def test_1_flat_in_regime_reenters():
    candles = mk_candles([(1, 30), (-1, 10)])
    eng = mk_engine(candles)
    k = 33                                   # глубокий SHORT-режим, кросс давно позади
    eng.last_bar_t = candles[k]["t"]         # catch-up обработает бары k+1..
    eng.trading_since = candles[k]["t"]      # старт до этих баров
    evs = opens(eng.scan())
    assert evs and all(e["reason"] == "re-regime" and e["side"] == SHORT for e in evs), evs
    print("OK 1: флэт в действующем режиме → повторный вход re-regime")


def test_2_position_blocks_reentry():
    candles = mk_candles([(1, 30), (-1, 10)])
    eng = mk_engine(candles)
    eng.position = {"side": LONG, "qty": 10, "entry": 1000.0, "entry_t": NOW,
                    "tp": None, "sl": None, "peak": 1000.0, "trail_active": False, "src": "cross"}
    eng.last_bar_t = candles[33]["t"]
    eng.trading_since = candles[33]["t"]
    assert not opens(eng.scan()), "при позиции сеть не должна входить"
    print("OK 2: при позиции повторного входа нет")


def test_3_exit_cooldown():
    candles = mk_candles([(1, 30), (-1, 10)])
    eng = mk_engine(candles)
    k = 33
    eng.last_bar_t = candles[k]["t"]
    eng.trading_since = candles[k]["t"]
    eng._last_close_t = candles[-1]["t"] + TF  # выход «после» всех баров → кулдаун
    assert not opens(eng.scan()), "бары, закрывшиеся до выхода, не входят"
    print("OK 3: кулдаун после выхода — бары старже выхода не входят")


def test_4_d1_blocks_against_trend():
    candles = mk_candles([(1, 30), (-1, 10)])
    eng = mk_engine(candles, use_d1=True)
    eng.d1_dir = LONG                        # дневной тренд против SHORT-режима
    eng.last_bar_t = candles[33]["t"]
    eng.trading_since = candles[33]["t"]
    assert not opens(eng.scan()), "D1 против направления — входа нет"
    print("OK 4: D1-фильтр против направления блокирует повторный вход")


def test_5_donchian_strict_untouched():
    candles = mk_candles([(1, 30), (-1, 10)])
    eng = mk_engine(candles, use_cross=False, use_pullback=False, use_donchian=True)
    eng.last_bar_t = candles[33]["t"]
    eng.trading_since = candles[33]["t"]
    assert not opens(eng.scan()), "строгий дончиан — сеть выключена"
    print("OK 5: строгий дончиан — сеть безопасности не срабатывает")


def test_6_no_duplicate_on_cross_bar():
    candles = mk_candles([(1, 30), (-1, 10)])
    eng = mk_engine(candles, use_regime=False)
    # фактический флип ищем по знакам osc — без предположений о номере бара
    flip_i = next(i for i in range(5, len(candles))
                  if (eng.osc_v[i] or 0) < 0 and (eng.osc_v[i - 1] or 0) > 0)
    evs = [e for e in eng._on_bar(flip_i) if e.get("action") == "open"]
    assert len(evs) == 1 and evs[0]["reason"] == "cross", \
        f"на баре кросса должен быть ровно один вход cross: {evs}"
    evs2 = [e for e in eng._on_bar(flip_i + 1) if e.get("action") == "open"]
    assert len(evs2) == 1 and evs2[0]["reason"] == "re-regime", \
        f"на следующем баре — один re-regime: {evs2}"
    print("OK 6: на баре кросса один вход cross, далее re-regime — без дублей")


def test_7_stop_exit_blocks_reregime():
    candles = mk_candles([(1, 30), (-1, 10)])
    eng = mk_engine(candles)
    eng.last_bar_t = candles[33]["t"]
    eng.trading_since = candles[33]["t"]
    # эмуляция: шорт закрыт по SL в действующем SHORT-режиме
    eng.position = {"side": SHORT, "qty": 10, "entry": 1000.0, "entry_t": NOW,
                    "tp": None, "sl": None, "peak": 1000.0, "trail_active": False, "src": "cross"}
    eng._open_trade = {"side": "SHORT", "qty": 10, "entry_price": 1000.0,
                       "entry_time": NOW - 100, "reason_in": "cross"}
    eng.on_close_fill(1005.0, NOW, "sl")
    assert eng._no_reentry_side == SHORT, "после sl-выхода блок должен стоять"
    eng._last_close_t = 0.0   # кулдаун проверяем отдельным тестом — изолируем блок
    assert not opens(eng.scan()), "re-regime не должен входить после стоп-выхода"
    # смена режима (кросс) снимает блок — catch-up заново
    eng._no_reentry_side = None
    eng.last_bar_t = candles[33]["t"]
    evs = opens(eng.scan())
    assert evs and evs[0]["reason"] == "re-regime", evs
    # tp-выход блок НЕ ставит
    eng.position = {"side": SHORT, "qty": 10, "entry": 1000.0, "entry_t": NOW,
                    "tp": None, "sl": None, "peak": 1000.0, "trail_active": False, "src": "cross"}
    eng._open_trade = {"side": "SHORT", "qty": 10, "entry_price": 1000.0,
                       "entry_time": NOW - 100, "reason_in": "cross"}
    eng.on_close_fill(900.0, NOW, "tp")
    assert eng._no_reentry_side is None, "tp-выход не блокирует"
    print("OK 7: после sl-выхода re-regime заблокирован до кросса; tp — нет")




def test_9_stop_block_survives_restart():
    """Инцидент PLZL 23.11 17:01: SL-выход → рестарт → блок сброшен → re-regime вошёл.
    Теперь блок персистится и самовосстанавливается из журнала."""
    candles = mk_candles([(1, 30), (-1, 10)])
    eng = mk_engine(candles)
    st = {"position": None, "pending": None, "open_trade": None,
          "journal": [{"side": -1, "qty": 10, "entry_price": 1000.0,
                       "entry_time": NOW - 3600, "reason_in": "cross",
                       "exit_price": 1005.0, "exit_time": NOW - 60,
                       "reason_out": "sl", "pnl": -50.0, "commission": 10.0}],
          "realized_pnl": -50.0, "commission_paid": 10.0,
          "peak_pnl": 0.0, "max_dd": 0.0, "last_bar_t": 0}
    eng.load_state(st)
    assert eng._no_reentry_side == SHORT, "блок должен восстановиться из журнала (sl-выход, позиций нет)"
    d = eng.state_dict()
    assert d["no_reentry_side"] == -1 and "last_close_t" in d, "блок должен персиститься"
    # при живой позиции блок не выставляется зря
    st2 = dict(st); st2["position"] = {"side": -1, "qty": 5, "entry": 1000.0, "entry_t": NOW}
    eng2 = mk_engine(candles); eng2.load_state(st2)
    assert eng2._no_reentry_side is None
    print("OK 9: стоп-блок переживает рестарт (персист + журнал-самовосстановление)")


if __name__ == "__main__":
    test_1_flat_in_regime_reenters()
    test_2_position_blocks_reentry()
    test_3_exit_cooldown()
    test_4_d1_blocks_against_trend()
    test_5_donchian_strict_untouched()
    test_6_no_duplicate_on_cross_bar()
    test_7_stop_exit_blocks_reregime()
    test_9_stop_block_survives_restart()
    print("\n✅ 8/8 — сеть работает; после стоп-выхода ждём только кросс")
