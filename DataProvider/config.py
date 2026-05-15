"""Configuration loader for DataProvider."""
import json
import os
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config(path: str | None = None) -> dict:
    """Load config from JSON file, env overrides."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = {}
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = json.load(f)

    # Env overrides
    if v := os.environ.get("DP_PORT"):
        cfg["port"] = int(v)
    if v := os.environ.get("DP_ACCOUNT_ID"):
        cfg["account_id"] = v
    if v := os.environ.get("DP_SYMBOLS"):
        cfg["symbols"] = [s.strip() for s in v.split(",") if s.strip()]
    if v := os.environ.get("DP_TIMEFRAMES"):
        cfg["timeframes"] = [s.strip() for s in v.split(",") if s.strip()]
    if v := os.environ.get("DP_CANDLE_CACHE_SIZE"):
        cfg["candle_cache_size"] = int(v)

    # Defaults
    cfg.setdefault("port", 5060)
    cfg.setdefault("account_id", "")
    cfg.setdefault("symbols", [])
    cfg.setdefault("timeframes", ["M1", "M5"])
    cfg.setdefault("candle_cache_size", 500)

    return cfg
