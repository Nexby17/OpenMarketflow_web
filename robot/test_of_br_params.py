# -*- coding: utf-8 -*-
"""Test: BR robot configs vs docs/ORDERFLOW_ROBOT_BR.md section 11 table.

Checks of_config_br.json (BR main, port 5082) and of_config_br_edp.json
(BR edp, port 5083) field-by-field against the documented production
parameters, plus config_of_br.py symbol (BRV6@RTSX), merged strategy_of.py
defaults needed by the BR robot (fmt_price, session window, max_average_levels)
and load_params() override semantics.

Run: cwd = robot/  ->  python test_of_br_params.py
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(HERE)
for p in (os.path.join(HERE, "py4"), HERE, _PROJ):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def load_cfg(name):
    path = os.path.join(HERE, name)
    with io.open(path, encoding="utf-8-sig") as f:
        return json.load(f)


# === Section 11 expectations ===
COMMON = {
    "timeframe": "M5",
    "lots": 1,
    "stop_loss_mode": "rub",
    "stop_loss_value": 1000,
    "max_hold_minutes": 999,
    "close_half_pct": 50,
    "max_pyramid_levels": 1,
    "step_pyramid": 25,
    "use_vah_val": True,
    "vah_val_mode": "range",
    "vah_val_bin_size": 0.35,
    "vah_val_pct": 95,
    "hl_filter": True,
    "cvd_accel_threshold": 150,
    "cvd_accel_period": 10,
    "cvd_ema_fast": 5,
    "cvd_ema_slow": 25,
    "use_cvd": True,
    "use_cvd_accel": True,
    "use_agg_ratio": True,
    "agg_window": 10,
    "agg_ratio_threshold": 0.35,
    "signal_confirm_count": 1,
    "signal_confirm_exit": 18,
    "ob_min_lots": 20,
    "ob_scan_radius": 50,
    "session_start": "",
    "session_end": "",
    "vwema_avg_exit": False,
}

MAIN_OVERRIDES = {
    "direction_filter": "short",
    "spread": 0.15,
    "step_average": 0.35,
    "max_average_levels": 5,
    "min_profit_per_lot": 0.30,
    "hl_window": 24,
    "hl_delta": 120,
    "use_vwema": False,
}

EDP_OVERRIDES = {
    "direction_filter": "both",
    "spread": 0.23,
    "step_average": 0.40,
    "max_average_levels": 15,
    "min_profit_per_lot": 0.17,
    "hl_window": 12,
    "hl_delta": 75,
    "use_vwema": True,
    "vwema_fast": 20,
    "vwema_slow": 40,
    "vwema_flat_th": 1,
}

MAIN_EXPECTED = {**COMMON, **MAIN_OVERRIDES}
EDP_EXPECTED = {**COMMON, **EDP_OVERRIDES}

print("=== of_config_br.json (BR main) vs section 11 ===")
main_cfg = load_cfg("of_config_br.json")
for k, v in MAIN_EXPECTED.items():
    actual = main_cfg.get(k, "<missing>")
    check(f"main.{k} == {v!r}", actual == v, f"got {actual!r}")

print("=== of_config_br_edp.json (BR edp) vs section 11 ===")
edp_cfg = load_cfg("of_config_br_edp.json")
for k, v in EDP_EXPECTED.items():
    actual = edp_cfg.get(k, "<missing>")
    check(f"edp.{k} == {v!r}", actual == v, f"got {actual!r}")

diff_keys = {k for k in set(main_cfg) | set(edp_cfg)
             if main_cfg.get(k) != edp_cfg.get(k)}
allowed_diffs = set(MAIN_OVERRIDES) | set(EDP_OVERRIDES)
check("configs differ ONLY in documented overrides",
      diff_keys <= allowed_diffs,
      f"unexpected diffs: {sorted(diff_keys - allowed_diffs)}")

print("=== config_of_br.py ===")
import config_of_br as config  # noqa: E402
check("config.SYMBOL == BRV6@RTSX", config.SYMBOL == "BRV6@RTSX", f"got {config.SYMBOL!r}")
check("config.TICKER == BRU6", config.TICKER == "BRU6", f"got {config.TICKER!r}")

print("=== strategy_of.py merged defaults needed by BR robot ===")
from strategy_of import OFParams, fmt_price  # noqa: E402

p = OFParams()
check("OFParams.max_average_levels default == 100", p.max_average_levels == 100,
      f"got {p.max_average_levels}")
check("OFParams has session_start/session_end",
      hasattr(p, "session_start") and hasattr(p, "session_end"))
check("fmt_price(97.51) == '97.51'", fmt_price(97.51) == "97.51", f"got {fmt_price(97.51)!r}")
check("fmt_price(86397.0) == '86397'", fmt_price(86397.0) == "86397",
      f"got {fmt_price(86397.0)!r}")

print("=== load_params() override semantics (config_of_br.py + of_config_br.json) ===")
import main_of_br  # noqa: E402  (module-level init only; no broker connection)
lp = main_of_br.load_params()
check("load_params: direction_filter from json = short", lp.direction_filter == "short",
      f"got {lp.direction_filter!r}")
check("load_params: max_average_levels from json = 5", lp.max_average_levels == 5,
      f"got {lp.max_average_levels}")
check("load_params: spread from json = 0.15", lp.spread == 0.15, f"got {lp.spread}")
check("load_params: symbol = BRV6@RTSX", main_of_br.SYMBOL == "BRV6@RTSX",
      f"got {main_of_br.SYMBOL!r}")

print(f"\n===== RESULTS: PASS={PASS} FAIL={FAIL} =====")
sys.exit(0 if FAIL == 0 else 1)
