"""Фикс 22.11.2026: catch-up бары, закрывшиеся ДО входа в позицию, не могут её закрывать.

Инцидент: старт с галкой «Позиция при старте» из mode=stopped → scan() догонял
замороженную историю (режим SHORT), первый же старый бар закрыл свежий LONG
по «regime≠pos» за 10 секунд после входа.

Сценарии:
  1. Инцидент: LONG-позиция, catch-up история содержит SHORT-бары и flip,
     НО все они закрылись ДО входа → close НЕ генерится.
  2. Защита 18.11 (regime≠pos): бар с противоположным режимом закрылся
     ПОСЛЕ входа → close «regime» генерится.
  3. Flip после входа: flip-бар закрылся ПОСЛЕ входа → close «flip» генерится.
  4. Flip до входа (pb-вход при развёрнутом режиме): flip-бар старже входа →
     flip-close НЕ генерится; закрывает следующий (актуальный) бар «regime».

Offline: синтетические свечи, без сети.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_posi import MtlrParams
from strategy_posi import StrategyEngine, LONG, SHORT

TF = 1800  # M30
NOW = time.time()
BASE = int(NOW) - 80 * TF  # 80 баров назад — все бары «закрыты» давно


def mk_candles(phases):
    """phases: [(dir, n), ...] dir=+1 рост / -1 падение. t = BASE + i*TF."""
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


def mk_engine(candles):
    p = MtlrParams()
    p.paper = True
    eng = StrategyEngine(p)
    eng.recompute(candles)
    return eng


def set_long(eng, entry_t, src="cross"):
    eng.position = {"side": LONG, "qty": 10, "entry": 1000.0, "entry_t": entry_t,
                    "tp": None, "sl": None, "peak": 1000.0, "trail_active": False, "src": src}


def closes(evs):
    return [e for e in evs if e.get("action") == "close"]


def phase_signs(eng, candles):
    return [("L" if (eng.osc_v[i] or 0) > 0 else "S") for i in range(len(candles))]


def test_1_incident_no_close_on_stale_bars():
    # 30 рост → 10 падение (SHORT+flip) → 15 рост (flip обратно LONG)
    candles = mk_candles([(1, 30), (-1, 10), (1, 15)])
    eng = mk_engine(candles)
    signs = phase_signs(eng, candles)
    assert signs[20] == "L" and signs[35] == "S" and signs[-1] == "L", \
        f"фазы не те: {signs[14:]}"

    last = candles[-1]
    entry_t = last["t"] + TF + 60          # вход ПОСЛЕ закрытия последнего бара
    set_long(eng, entry_t)
    eng.last_bar_t = candles[20]["t"]      # catch-up с 21-го бара (вся история)
    evs = closes(eng.scan())
    assert not evs, f"старые бары закрыли свежую позицию: {evs} (инцидент 22.11)"
    print("OK 1: catch-up стилей старше входа НЕ закрывают позицию")


def test_2_regime_mismatch_after_entry_closes():
    # 30 рост (LONG), вход, потом 5 падение (SHORT-режим, бары закрываются после входа)
    candles = mk_candles([(1, 30), (-1, 5)])
    eng = mk_engine(candles)
    k = 30                                  # первый падающий бар
    entry_t = candles[k - 1]["t"] + TF + 60  # вход после закрытия 29-го бара
    set_long(eng, entry_t)
    eng.last_bar_t = candles[k - 1]["t"]
    evs = closes(eng.scan())
    assert evs and evs[0]["reason"] == "regime", f"защита 18.11 не сработала: {evs}"
    print("OK 2: противоположный режим ПОСЛЕ входа закрывает (regime≠pos)")


def test_3_flip_after_entry_closes():
    candles = mk_candles([(1, 30), (-1, 5)])
    eng = mk_engine(candles)
    entry_t = candles[25]["t"] + TF + 60     # вход в LONG-фазе, до flip
    set_long(eng, entry_t)
    eng.last_bar_t = candles[25]["t"]
    evs = closes(eng.scan())
    assert evs, "flip после входа не закрыл"
    assert evs[0]["reason"] in ("flip", "regime"), evs
    print(f"OK 3: flip после входа закрывает ({evs[0]['reason']})")


def test_4_flip_before_entry_no_flip_close_but_regime_guard():
    # pb-вход LONG при уже развёрнутом SHORT-режиме: flip был ДО входа
    candles = mk_candles([(1, 30), (-1, 6)])
    eng = mk_engine(candles)
    k = 32                                   # flip на баре 30; вход после закрытия 32-го
    entry_t = candles[k]["t"] + TF + 60
    set_long(eng, entry_t, src="pb")
    eng.last_bar_t = candles[k]["t"]
    evs = closes(eng.scan())
    # flip-бар (30) старже входа и не в catch-up, но бары 33..35 (после входа) — SHORT
    assert evs and evs[0]["reason"] == "regime", f"ожидаем regime-close на актуальном баре: {evs}"
    print("OK 4: flip до входа не срабатывает, актуальный SHORT-бар закрывает «regime»")


if __name__ == "__main__":
    test_1_incident_no_close_on_stale_bars()
    test_2_regime_mismatch_after_entry_closes()
    test_3_flip_after_entry_closes()
    test_4_flip_before_entry_no_flip_close_but_regime_guard()
    print("\n✅ 4/4 — фикс 22.11 работает: старые бары не закрывают, актуальные — да")
