"""F-067 unit tests: Phase Zones gate (Donchian thirds + hysteresis)."""
import sys
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web\robot")

from strategy_of import OFParams, OrderFlowStrategy, LONG, SHORT
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))


class BM:
    def __init__(self, delta):
        self.delta = delta
        self.cvd = 0.0


def make_strategy(**overrides):
    p = OFParams()
    p.use_timing = False
    p.use_vwema = False
    p.use_vah_val = False
    p.enable_ob_filter = False
    p.use_dm_wall = False
    p.use_cvd_accel = False
    p.use_cvd = False
    p.phase_filter = True
    p.phase_window = 20
    p.phase_lower = 0.33
    p.phase_upper = 0.67
    p.phase_hysteresis = 0.05
    for k, v in overrides.items():
        setattr(p, k, v)
    return OrderFlowStrategy(p)


def feed_bars(s, prices):
    """prices: list of (high, low, close) — one bar each; delta alternating +50/-50."""
    for i, (h, l, c) in enumerate(prices):
        s.on_bar_close(BM(50 if i % 2 == 0 else -50), h, l, c)


def test_phase_off_by_default():
    p = OFParams()
    assert p.phase_filter is False, "галочка off по умолчанию (R-002)"


def test_phase_lower_zone_allows_long_blocks_short():
    s = make_strategy()
    # Канал: low 100, high 200 (окно 20), цена 115 → pos 15% (низ) → LONG only
    prices = [(200.0, 100.0, 100.0 + (200.0 - 100.0) * 0.15)] * 25
    feed_bars(s, prices)
    ok_long, _ = s._phase_allows(LONG, 115.0)
    ok_short, reason = s._phase_allows(SHORT, 115.0)
    assert ok_long
    assert not ok_short and "LONG" in reason


def test_phase_upper_zone_allows_short_blocks_long():
    s = make_strategy()
    prices = [(200.0, 100.0, 100.0 + 100.0 * 0.85)] * 25
    feed_bars(s, prices)
    ok_short, _ = s._phase_allows(SHORT, 185.0)
    ok_long, reason = s._phase_allows(LONG, 185.0)
    assert ok_short
    assert not ok_long and "SHORT" in reason


def test_phase_middle_allows_both():
    s = make_strategy()
    prices = [(200.0, 100.0, 150.0)] * 25     # pos = 50% — середина
    feed_bars(s, prices)
    ok_l, r1 = s._phase_allows(LONG, 150.0)
    ok_s, r2 = s._phase_allows(SHORT, 150.0)
    assert ok_l and r1 == "both"
    assert ok_s and r2 == "both"


def test_phase_hysteresis_prevents_flip():
    s = make_strategy()
    # Входим в LONG-зону (10%): _phase_allows вызывается на каждом тике в бою —
    # эмулируем последовательность: 110 (вход в зону) → 136 (36% — в гистерезисе 33+5=38).
    prices = [(200.0, 100.0, 110.0)] * 25
    feed_bars(s, prices)
    ok_first, r_first = s._phase_allows(LONG, 110.0)
    assert ok_first and r_first == "long_zone" and s._phase_dir == 1
    ok_long, _ = s._phase_allows(LONG, 136.0)     # 36% < 38% — гистерезис держит LONG-зону
    blocked_short, r = s._phase_allows(SHORT, 136.0)
    assert ok_long and not blocked_short
    # 45% — вышли из гистерезиса → BOTH (не SHORT: 45 < 67)
    ok_l2, r2 = s._phase_allows(LONG, 145.0)
    assert ok_l2 and r2 == "both"
    assert s._phase_dir == 0


def test_phase_not_ready_neutral():
    s = make_strategy()
    ok, reason = s._phase_allows(LONG, 100.0)     # 0 баров — канал не готов
    assert ok and reason == "not_ready"


def test_phase_state_in_get_status():
    s = make_strategy()
    prices = [(200.0, 100.0, 120.0)] * 25
    feed_bars(s, prices)
    s._current_price = 120.0
    st = s.get_status()
    ph = st["phase"]
    assert ph["enabled"] is True and ph["ready"] is True
    assert ph["dir"] == 1
    assert ph["chanLow"] == 100.0 and ph["chanHigh"] == 200.0


def test_phase_disabled_when_flag_off():
    s = make_strategy(phase_filter=False)
    st = s.get_status()
    assert st["phase"] == {"enabled": False}
    ok, reason = s._phase_allows(SHORT, 110.0)
    assert ok and reason == "off"
