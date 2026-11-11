# -*- coding: utf-8 -*-
"""F-191/F-192: regression tests for CVD Trend entry gating — no broker needed.

Инцидент 11.11.2026 (Si, 5080):
  CVD Trend CROSSOVER → SHORT @ 83459.5, но HL-фильтр (Δ=55) отбил вход,
  потому что до хая окна было 154.5 пункта (ATR=151). Сигнал при этом был
  уничтожен: `_pending_cvd_trend` обнулялся ДО гейтов, поэтому повтора не было,
  хотя через 2 минуты HL уже разрешал шорт.

Проверяем:
  1) pending не съедается, если вход отбит (HL / direction);
  2) pending снимается, когда вход реально исполнен;
  3) TTL отбрасывает протухший сигнал;
  4) HL по-прежнему блокирует импульсные (не cvd_trend) сигналы;
  5) hl_apply_cvd_trend=False — трендовый вход проходит мимо HL (новое поведение);
  6) hl_apply_cvd_trend=True — старое поведение (HL блокирует и тренд);
  7) смена cvd_ema_fast/slow на ходу пересчитывает альфы (F-192).

Run: cwd=robot, python test_of_pending_signal.py
"""
import sys
import os
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(HERE)
for p in (os.path.join(HERE, "py4"), HERE, _PROJ):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

from strategy_of import OFParams, OrderFlowStrategy, LONG, SHORT, MSK  # noqa: E402
from orderflow_engine import BarMetrics, OFSignal  # noqa: E402

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


def msk_dt(h, m):
    return datetime(2026, 11, 11, h, m, tzinfo=MSK)


def make_strategy(**overrides):
    p = OFParams()
    p.lots = 1
    p.use_cvd = True
    p.use_cvd_accel = False          # сигналы задаём вручную
    p.direction_filter = "both"
    p.hl_filter = True
    p.hl_window = 15
    p.hl_delta = 55
    p.phase_filter = False
    p.use_vah_val = False
    p.use_vwema = False
    p.use_timing = False
    p.enable_ob_filter = False
    p.signal_confirm_count = 1
    for k, v in overrides.items():
        setattr(p, k, v)
    s = OrderFlowStrategy(p)
    # достаточно истории, чтобы пройти signals.has_enough_data
    for i in range(max(p.cvd_lookback + 2, 15)):
        s.signals._bar_history.append(BarMetrics(
            buy_volume=0, sell_volume=0, delta=0, delta_ratio=0.0,
            total_volume=0, price_change=0.0, cvd=float(i), timestamp=0.0))
    s._last_price_update = time.time()
    return s


def stub(s, hl=(83614.0, 83300.0), sigs=None):
    """Подменяем рыночные источники сигналов/окна HL."""
    s.signals.get_hl_window = lambda w: hl
    s.signals.generate_signals = lambda *a, **k: list(sigs or [])
    return s


def set_pending(s, direction, age_sec=0.0, price=83459.5):
    s._pending_cvd_trend = {"direction": direction, "signal_type": "cvd_trend",
                            "ts": time.time() - age_sec, "price": price}


PRICE_FAR_FROM_HIGH = 83459.5   # 154.5 ниже хая 83614 → HL(55) блокирует шорт


def entry_action(act):
    """process_tick возвращает СПИСОК действий — достаём entry, если он есть."""
    if not act:
        return None
    if isinstance(act, dict):
        return act if act.get("action") == "entry" else None
    for a in act:
        if isinstance(a, dict) and a.get("action") == "entry":
            return a
    return None


# ---------- 1. pending выживает при блоке HL (старое поведение HL) ----------

def t_pending_survives_hl_block():
    s = stub(make_strategy(hl_apply_cvd_trend=True))
    set_pending(s, -1)
    act = s.process_tick(PRICE_FAR_FROM_HIGH, msk_dt(15, 15))
    assert act is None or act == [], f"entry must be blocked, got {act}"
    assert s._pending_cvd_trend is not None, "pending must survive HL rejection (F-191)"


# ---------- 2. pending снимается после реального входа ----------

def t_pending_cleared_on_entry():
    s = stub(make_strategy(hl_apply_cvd_trend=False))
    set_pending(s, -1)
    act = s.process_tick(PRICE_FAR_FROM_HIGH, msk_dt(15, 15))
    e = entry_action(act)
    assert e is not None, f"expected entry, got {act}"
    assert e.get("side") == "sell", f"expected sell, got {act}"
    assert s._pending_cvd_trend is None, "pending must be cleared once entry executed"


# ---------- 3. TTL ----------

def t_pending_ttl_expires():
    s = stub(make_strategy(pending_signal_ttl_sec=300, hl_apply_cvd_trend=False))
    set_pending(s, -1, age_sec=400)
    act = s.process_tick(PRICE_FAR_FROM_HIGH, msk_dt(15, 15))
    assert act is None or act == [], f"stale signal must not enter, got {act}"
    assert s._pending_cvd_trend is None, "stale pending must be dropped"


def t_pending_within_ttl_enters():
    s = stub(make_strategy(pending_signal_ttl_sec=300, hl_apply_cvd_trend=False))
    set_pending(s, -1, age_sec=120)
    act = s.process_tick(PRICE_FAR_FROM_HIGH, msk_dt(15, 15))
    assert entry_action(act) is not None, f"fresh pending must enter, got {act}"


def t_pending_ttl_zero_means_unlimited():
    s = stub(make_strategy(pending_signal_ttl_sec=0, hl_apply_cvd_trend=False))
    set_pending(s, -1, age_sec=10 ** 6)
    act = s.process_tick(PRICE_FAR_FROM_HIGH, msk_dt(15, 15))
    assert entry_action(act) is not None, f"ttl=0 must not expire, got {act}"


# ---------- 4. pending выживает при блоке direction_filter ----------

def t_pending_survives_direction_block():
    s = stub(make_strategy(direction_filter="short", hl_apply_cvd_trend=False))
    set_pending(s, +1)                      # LONG при direction=short
    act = s.process_tick(PRICE_FAR_FROM_HIGH, msk_dt(15, 15))
    assert act is None or act == [], f"LONG must be blocked, got {act}"
    assert s._pending_cvd_trend is not None, "pending must survive direction rejection (F-191)"


# ---------- 5. HL продолжает блокировать импульсные сигналы ----------

def t_hl_blocks_accel_signal():
    s = stub(make_strategy(hl_apply_cvd_trend=False))
    s.signals.generate_signals = lambda *a, **k: [OFSignal(
        signal_type="cvd_accel", direction=SHORT, strength=0.9,
        price=PRICE_FAR_FROM_HIGH, timestamp=time.time())]
    act = s.process_tick(PRICE_FAR_FROM_HIGH, msk_dt(15, 16))
    assert act is None or act == [], f"HL must still block cvd_accel far from high, got {act}"


def t_hl_passes_accel_near_high():
    s = stub(make_strategy(hl_apply_cvd_trend=False), hl=(83614.0, 83300.0))
    s.signals.generate_signals = lambda *a, **k: [OFSignal(
        signal_type="cvd_accel", direction=SHORT, strength=0.9,
        price=83600.0, timestamp=time.time())]
    act = s.process_tick(83600.0, msk_dt(15, 16))
    assert entry_action(act) is not None, f"accel near high must enter, got {act}"


# ---------- 6. hl_apply_cvd_trend=True — старое поведение ----------

def t_hl_applies_to_trend_when_enabled():
    s = stub(make_strategy(hl_apply_cvd_trend=True))
    s.signals.generate_signals = lambda *a, **k: [OFSignal(
        signal_type="cvd_trend", direction=SHORT, strength=0.9,
        price=PRICE_FAR_FROM_HIGH, timestamp=time.time())]
    act = s.process_tick(PRICE_FAR_FROM_HIGH, msk_dt(15, 17))
    assert act is None or act == [], f"hl_apply_cvd_trend=True must block trend, got {act}"


# ---------- 7. F-192: смена периодов CVD EMA на ходу ----------

def t_cvd_ema_recalc():
    s = make_strategy(cvd_ema_fast=5, cvd_ema_slow=15)
    assert abs(s.signals._cvd_trend_alpha_f - 2.0 / 6) < 1e-12
    assert abs(s.signals._cvd_trend_alpha_s - 2.0 / 16) < 1e-12
    assert s.apply_cvd_trend_params() is False, "no change → no-op"
    s.p.cvd_ema_fast = 7
    s.p.cvd_ema_slow = 21
    assert s.apply_cvd_trend_params() is True, "changed → applied"
    assert abs(s.signals._cvd_trend_alpha_f - 2.0 / 8) < 1e-12, s.signals._cvd_trend_alpha_f
    assert abs(s.signals._cvd_trend_alpha_s - 2.0 / 22) < 1e-12, s.signals._cvd_trend_alpha_s
    assert s.signals._cvd_trend_ready is False, "EMAs must be re-seeded to avoid fake crossover"
    assert s.apply_cvd_trend_params() is False, "second call with same values → no-op"


def t_cvd_ema_rejects_garbage():
    s = make_strategy(cvd_ema_fast=7, cvd_ema_slow=21)
    s.p.cvd_ema_fast = "abc"
    assert s.apply_cvd_trend_params() is False, "garbage must be ignored, not crash"


print("F-191/F-192 CVD Trend entry gating")
check("pending survives HL rejection (old HL behaviour)", t_pending_survives_hl_block)
check("pending cleared once entry executed", t_pending_cleared_on_entry)
check("pending TTL expires -> dropped", t_pending_ttl_expires)
check("pending within TTL -> enters", t_pending_within_ttl_enters)
check("pending TTL=0 -> unlimited", t_pending_ttl_zero_means_unlimited)
check("pending survives direction rejection", t_pending_survives_direction_block)
check("HL still blocks cvd_accel far from high", t_hl_blocks_accel_signal)
check("HL passes cvd_accel near high", t_hl_passes_accel_near_high)
check("hl_apply_cvd_trend=True blocks trend too", t_hl_applies_to_trend_when_enabled)
check("CVD EMA periods recalculated at runtime", t_cvd_ema_recalc)
check("CVD EMA garbage params ignored", t_cvd_ema_rejects_garbage)

passed = sum(RESULTS)
print(f"\n{passed}/{len(RESULTS)} PASS")
sys.exit(0 if passed == len(RESULTS) else 1)
