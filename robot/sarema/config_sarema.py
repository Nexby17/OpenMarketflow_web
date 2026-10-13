"""F-061: Per-instance config for SAR×EMA stocks robot (1 robot = 1 ticker).

Env (per-instance, паттерн arb-роботов):
  SAREMA_CONFIG_FILE  — путь к JSON-конфигу экземпляра
  SAREMA_STATE_FILE   — путь к стейт-файлу
  SAREMA_TICKER       — тикер (SBER, GAZP, ...)
  SAREMA_PORT         — HTTP порт API
  SAREMA_ACCOUNT_ID   — счёт (по умолчанию FINAM_ACCOUNT из .env)
  FINAM_API_KEY       — общий секрет

Лимиты Дмитрия (D2): max_qty (штуки, от 0 до ∞; null = без лимита),
max_value_rub (₽, null = без лимита). Семантика: 0 = сделки запрещены.
"""
import json
import os

# === Broker ===
TICKER = os.environ.get("SAREMA_TICKER", "SBER")
SYMBOL = f"{TICKER}@MISX"
ACCOUNT_ID = os.environ.get("SAREMA_ACCOUNT_ID", os.environ.get("FINAM_ACCOUNT", ""))
PORT = int(os.environ.get("SAREMA_PORT", "5084"))
CONFIG_FILE = os.environ.get("SAREMA_CONFIG_FILE", "")
STATE_FILE = os.environ.get("SAREMA_STATE_FILE", "")
DP_URL = os.environ.get("DP_URL", "http://localhost:5060")

# === Strategy (сигнальные параметры — фикс, смена только командой Дмитрия, R-002) ===
SAR_AF0 = 0.02
SAR_STEP = 0.2
SAR_MAX = 0.2
EMA_N = 12
INIT_STOP_ATR = 3.0
TIMEFRAME_MIN = 60          # H1
SESSION_UTC = (7 * 3600, 15 * 3600 + 40 * 60)   # 10:00-18:40 MSK

# === R-sizing defaults (risk-поля — редактируемые Дмитрием через /params) ===
DEFAULTS = {
    # === Сигнальные (SAR/EMA) — редактируемые Дмитрием в popup, с диапазонами ТЗ ===
    "sar_af0": 0.02,
    "sar_step": 0.2,
    "sar_max": 0.2,
    "ema_n": 12,
    "init_stop_atr": 3.0,
    "tf_min": 60,
    "base": 1_000_000.0,
    "risk_pct": 0.005,
    "compound": False,
    # === Limits Dmitry (D2): null = без лимита, 0 = направление НЕ торгуется ===
    # Предельная позиция ЛОНГ/ШОРТ в штуках (запрос Дмитрия 09.10, как у OF-роботов)
    "max_long_qty": None,
    "max_short_qty": None,
    "max_value_rub": None,
    "liq_frac": 0.05,
    "day_kill_R": None,
    "warmup_bars": 300,
    # === F-065: Volume Profile фильтр входа (как у OF-Si «VP Mode») ===
    # mode: off = как раньше (без фильтра); range = вход только внутри VA;
    # fade/breakout = вход только от краёв VA. Значения None = дефолт подтипа:
    # bin None → авто из размаха окна; pct None → 70; lookback None → 200 баров H1.
    "vah_val_mode": "off",
    "vah_val_bin": None,
    "vah_val_pct": None,
    "vah_val_lookback": None,
}


def load_config(path: str | None = None) -> dict:
    cfg = dict(DEFAULTS)
    p = path or CONFIG_FILE
    if p and os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            user = json.load(f)
        for k in DEFAULTS:
            if k in user:
                cfg[k] = user[k]
    return cfg


def validate_params(patch: dict) -> tuple[dict, list[str]]:
    """Валидация /params от Дмитрия. Возвращает (очищенный патч, список ошибок)."""
    errors = []
    clean = {}
    if "base" in patch:
        v = patch["base"]
        if not isinstance(v, (int, float)) or v <= 0:
            errors.append("base must be > 0")
        else:
            clean["base"] = float(v)
    if "risk_pct" in patch:
        v = patch["risk_pct"]
        if not isinstance(v, (int, float)) or not (0 < v <= 0.1):
            errors.append("risk_pct must be in (0, 0.1]")
        else:
            clean["risk_pct"] = float(v)
    for k in ("max_long_qty", "max_short_qty", "max_value_rub"):
        if k in patch:
            v = patch[k]
            if v is None:
                clean[k] = None                       # без лимита
            elif isinstance(v, (int, float)) and v >= 0:
                clean[k] = int(v) if k != "max_value_rub" else float(v)
            else:
                errors.append(f"{k} must be null (no limit) or >= 0")
    if "liq_frac" in patch:
        v = patch["liq_frac"]
        if v is None:
            clean["liq_frac"] = None
        elif isinstance(v, (int, float)) and 0 < v <= 1:
            clean["liq_frac"] = float(v)
        else:
            errors.append("liq_frac must be in (0, 1] or null")
    if "day_kill_R" in patch:
        v = patch["day_kill_R"]
        if v is None:
            clean["day_kill_R"] = None
        elif isinstance(v, (int, float)) and v < 0:
            clean["day_kill_R"] = float(v)
        else:
            errors.append("day_kill_R must be null (off) or negative R")
    if "compound" in patch:
        clean["compound"] = bool(patch["compound"])
    # Сигнальные SAR/EMA (диапазоны ТЗ: sar_af0 0.02-0.03; sar_step 0.008-0.2;
    # ema_n 12-20; init_stop_atr 3.0 (допуск 1-10 для тюнинга); tf 5-240 мин)
    sig_specs = {
        "sar_af0": (0.005, 0.05, float),
        "sar_step": (0.002, 0.2, float),
        "sar_max": (0.05, 0.5, float),
        "ema_n": (5, 50, int),
        "init_stop_atr": (1.0, 10.0, float),
        "tf_min": (5, 240, int),
    }
    for k, (lo, hi, typ) in sig_specs.items():
        if k in patch:
            v = patch[k]
            if not isinstance(v, (int, float)) or not (lo <= v <= hi):
                errors.append(f"{k} must be in [{lo}, {hi}]")
            else:
                clean[k] = typ(v)
    # F-065: VP Mode Range/Fade/Breakout + подтипы
    if "vah_val_mode" in patch:
        m = patch["vah_val_mode"]
        if m in ("off", "range", "fade", "breakout"):
            clean["vah_val_mode"] = m
        else:
            errors.append("vah_val_mode must be off|range|fade|breakout")
    for k in ("vah_val_bin", "vah_val_pct", "vah_val_lookback"):
        if k in patch:
            v = patch[k]
            if v is None:
                clean[k] = None                      # дефолт подтипа
            elif isinstance(v, (int, float)) and v >= 0:
                # 0 = «авто/дефолт» (bin: авто из размаха; pct/lookback: дефолт 70/200)
                if v == 0:
                    clean[k] = 0.0 if k == "vah_val_bin" else None
                else:
                    clean[k] = float(v) if k == "vah_val_bin" else int(v)
            else:
                errors.append(f"{k} must be null/0 (default) or > 0")
    if "vah_val_pct" in clean and clean["vah_val_pct"] is not None and clean["vah_val_pct"] != 0 and not (50 <= clean["vah_val_pct"] <= 100):
        errors.append("vah_val_pct must be in [50, 100] or null/0")
    if "vah_val_lookback" in clean and clean["vah_val_lookback"] is not None and not (30 <= clean["vah_val_lookback"] <= 2000):
        errors.append("vah_val_lookback must be in [30, 2000] or null")
    return clean, errors


def apply_limits_qty(risk_sizing: int, price: float, cfg: dict, direction: int = 0) -> tuple[int, str]:
    """Финальный размер по D2: min(risk_sizing, предельная позиция направления, max_value_rub/price).

    direction: 1 = LONG (лимит max_long_qty), -1 = SHORT (max_short_qty).
    Предельная позиция 0 → направление не торгуется (замечание Дмитрия: «шорт 0 — шорт не торгует»).
    None-лимит не ограничивает; 0 запрещает. Возвращает (qty, reason) для журнала.
    """
    if risk_sizing < 1:
        return 0, "risk_sizing<1"
    cap = risk_sizing
    reason = "risk_sizing"
    if direction == 1:
        mq = cfg.get("max_long_qty")
    elif direction == -1:
        mq = cfg.get("max_short_qty")
    else:
        mq = None
    if mq is not None:
        if mq <= 0:
            return 0, ("max_long_qty=0" if direction == 1 else "max_short_qty=0")
        if mq < cap:
            cap, reason = int(mq), ("max_long_qty" if direction == 1 else "max_short_qty")
    mv = cfg.get("max_value_rub")
    if mv is not None:
        if mv <= 0:
            return 0, "max_value_rub=0"
        v_cap = int(mv / price) if price > 0 else 0
        if v_cap < cap:
            cap, reason = v_cap, "max_value_rub"
    return cap, reason
