"""F-061 unit tests: config + Dmitry limits (D2) semantics."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))          # repo root
sys.path.insert(0, os.path.join(_ROOT, "robot"))         # .../robot  (sarema pkg)
sys.path.insert(0, _ROOT)

from sarema.config_sarema import apply_limits_qty, validate_params, load_config, DEFAULTS  # noqa: E402


def test_apply_limits_risk_only():
    cfg = dict(DEFAULTS)  # no limits
    qty, reason = apply_limits_qty(42, 100.0, cfg)
    assert qty == 42 and reason == "risk_sizing"


def test_apply_limits_max_qty():
    """09.10: max_qty заменён направленными max_long_qty/max_short_qty."""
    cfg = dict(DEFAULTS, max_long_qty=10, max_short_qty=400)
    qty, reason = apply_limits_qty(42, 100.0, cfg, direction=1)
    assert qty == 10 and reason == "max_long_qty"
    qty2, reason2 = apply_limits_qty(42, 100.0, cfg, direction=-1)
    assert qty2 == 42 and reason2 == "risk_sizing"


def test_apply_limits_max_value():
    cfg = dict(DEFAULTS, max_value_rub=100_000.0)
    # 100_000 / 100 = 1000 shares cap < risk sizing 5000
    qty, reason = apply_limits_qty(5000, 100.0, cfg, direction=1)
    assert qty == 1000 and reason == "max_value_rub"


def test_apply_limits_zero_forbids():
    cfg = dict(DEFAULTS, max_long_qty=0)
    qty, reason = apply_limits_qty(42, 100.0, cfg, direction=1)
    assert qty == 0 and reason == "max_long_qty=0"
    cfg = dict(DEFAULTS, max_value_rub=0)
    qty, reason = apply_limits_qty(42, 100.0, cfg, direction=1)
    assert qty == 0 and reason == "max_value_rub=0"


def test_apply_limits_risk_small():
    cfg = dict(DEFAULTS)
    qty, _ = apply_limits_qty(0, 100.0, cfg)
    assert qty == 0


def test_validate_params_limits():
    clean, errors = validate_params({
        "max_long_qty": None, "max_short_qty": None, "max_value_rub": 200_000,
        "risk_pct": 0.005, "base": 500_000,
    })
    assert errors == []
    assert clean["max_long_qty"] is None and clean["max_short_qty"] is None
    assert clean["max_value_rub"] == 200_000


def test_validate_params_rejects_negative():
    clean, errors = validate_params({"max_long_qty": -5, "max_value_rub": -1, "risk_pct": 0.5})
    assert len(errors) == 3
    assert "max_long_qty" not in clean


def test_validate_params_day_kill():
    clean, errors = validate_params({"day_kill_R": -4.0})
    assert errors == [] and clean["day_kill_R"] == -4.0
    clean, errors = validate_params({"day_kill_R": None})
    assert errors == [] and clean["day_kill_R"] is None
    _, errors = validate_params({"day_kill_R": 3.0})
    assert len(errors) == 1


def test_defaults_safe():
    assert DEFAULTS["risk_pct"] == 0.005
    assert DEFAULTS["compound"] is False
    assert DEFAULTS["day_kill_R"] is None
