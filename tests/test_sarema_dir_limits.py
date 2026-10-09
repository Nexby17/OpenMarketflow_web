"""F-061: directional limits tests (Dmitry 09.10: предельная позиция лонг/шорт, 0 = не торгует)."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "robot"))

from sarema.config_sarema import apply_limits_qty, validate_params, DEFAULTS  # noqa: E402


def test_long_limit_applies():
    cfg = dict(DEFAULTS, max_long_qty=50)
    qty, reason = apply_limits_qty(200, 100.0, cfg, direction=1)
    assert qty == 50 and reason == "max_long_qty"


def test_short_limit_applies():
    cfg = dict(DEFAULTS, max_short_qty=30)
    qty, reason = apply_limits_qty(200, 100.0, cfg, direction=-1)
    assert qty == 30 and reason == "max_short_qty"


def test_zero_long_forbids_long_only():
    """Предельная позиция лонг 0 → лонг не торгуется, шорт работает."""
    cfg = dict(DEFAULTS, max_long_qty=0, max_short_qty=100)
    qty, reason = apply_limits_qty(200, 100.0, cfg, direction=1)
    assert qty == 0 and reason == "max_long_qty=0"
    qty2, _ = apply_limits_qty(200, 100.0, cfg, direction=-1)
    assert qty2 == 100


def test_zero_short_forbids_short_only():
    """Предельная позиция шорт 0 → шорт-робот не торгует, лонг работает."""
    cfg = dict(DEFAULTS, max_long_qty=100, max_short_qty=0)
    qty, reason = apply_limits_qty(200, 100.0, cfg, direction=-1)
    assert qty == 0 and reason == "max_short_qty=0"
    qty2, _ = apply_limits_qty(200, 100.0, cfg, direction=1)
    assert qty2 == 100


def test_null_direction_limit_no_cap():
    cfg = dict(DEFAULTS, max_long_qty=None, max_short_qty=None)
    assert apply_limits_qty(200, 100.0, cfg, direction=1)[0] == 200
    assert apply_limits_qty(200, 100.0, cfg, direction=-1)[0] == 200


def test_both_directions_set_min_wins():
    cfg = dict(DEFAULTS, max_long_qty=10, max_short_qty=400, max_value_rub=1000000)
    # LONG: min(200, 10) = 10
    assert apply_limits_qty(200, 100.0, cfg, direction=1)[0] == 10
    # SHORT: min(200, 400, 1000000/100=10000) = 200 (risk wins)
    q, r = apply_limits_qty(200, 100.0, cfg, direction=-1)
    assert q == 200 and r == "risk_sizing"


def test_validate_params_directional():
    clean, errors = validate_params({"max_long_qty": 100, "max_short_qty": 0})
    assert errors == []
    assert clean["max_long_qty"] == 100 and clean["max_short_qty"] == 0
    _, errors2 = validate_params({"max_long_qty": -5})
    assert len(errors2) == 1


def test_validate_params_legacy_max_qty_rejected():
    """Старое поле max_qty больше не принимается (не в сигнатуре)."""
    clean, errors = validate_params({"max_qty": 100})
    assert "max_qty" not in clean
