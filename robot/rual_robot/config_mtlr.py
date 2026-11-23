"""MTLR robot config: all user-tunable parameters, persisted to mtlr_config.json."""
import json
import os
from dataclasses import dataclass, asdict, field
from typing import Optional

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mtlr_config.json")


@dataclass
class MtlrParams:
    # === Instrument / account ===
    symbol: str = "MTLR@MISX"
    ticker: str = "MTLR"
    account: str = "2049688"           # edp
    price_step: float = 0.01
    lot_size: int = 1               # акций в лоте (18.11: SNGS=100, RUAL=10)           # 1 пункт = 0.01 ₽ (MOEX TQBR MTLR)
    commission_pct: float = 0.035      # % за оборот (обе стороны), как у арбитражных

    # === Timeframe ===
    timeframe: str = "H1"              # M1|M5|M15|M30|H1|D

    # === Signals (independent checkboxes each) ===
    use_cross: bool = True             # вход по кроссу SAR×EMA (смена режима)
    use_pullback: bool = True          # откат: отдельный сигнал (пуллбэк к EMA по наклону);
                                       # с 16.11.2026 не зависит от use_cross — работает параллельно
    use_donchian: bool = False         # Дончиан: пробой канала N баров, вход маркет по закрытию
                                       # бара (16.11); выход — только люстра k×ATR14
    donch_n: int = 30                  # длина канала Дончиана (баров)
    donch_k_atr: float = 3.0           # люстра: peak − k×ATR(14), перестановка на закрытии бара
    # PSAR
    sar_start: float = 0.03
    sar_step: float = 0.008
    sar_max: float = 0.2
    # EMA
    ema_period: int = 12
    # ATR (для осциллятора режима и отката)
    atr_period: int = 14
    # Откат: параметры
    pb_bars: int = 6                   # максимум баров ожидания отката после кросса
    pb_depth_atr: float = 0.8          # глубина отката: цена подходит к EMA ближе чем depth×ATR (или ниже)
    pb_touch_only: bool = False        # True: достаточно касания EMA (low<=EMA для лонга)
    entry_by_limit: bool = True        # True (заказ 14.11): вход лимиткой ровно на EMA у брокера —
                                       # касание исполняет биржа, без ожидания закрытия бара

    # === Filters (independent checkbox each) ===
    use_rsi: bool = False
    rsi_period: int = 14
    rsi_upper: float = 70.0            # лонг запрещён при RSI >= upper
    rsi_lower: float = 30.0            # шорт запрещён при RSI <= lower
    use_regime: bool = False
    regime_threshold: float = 0.3
    use_d1: bool = False             # D1-фильтр: вход только по тренду последнего ЗАКРЫТОГО
                                     # дня (close vs EMA20 дневки, без подглядывания)
    d1_ema_period: int = 20          # период EMA дневного тренда      # лонг при osc > thr, шорт при osc < -thr

    # === Risk: TP / SL / Trailing (checkbox + unit + value each) ===
    tp_enabled: bool = False
    tp_unit: str = "pct"               # pct|pts|rub
    tp_value: float = 2.0
    sl_enabled: bool = True
    sl_unit: str = "pct"              # % | pts | rub | sar; "sar" = SL на SAR,
                                     # следует за SAR на закрытии бара (заказ 16.11)
    sl_value: float = 1.5
    trail_enabled: bool = False
    trail_unit: str = "pct"            # отступ trailing stop
    trail_value: float = 1.0
    trail_activate_pct: float = 0.0    # активация после прохода X% в плюс (0 = сразу)

    # === Position sizing ===
    max_capital: float = 100000.0      # ₽, потолок для открытия позиции
    long_qty: Optional[int] = None     # None (пусто) = авто по max_capital; 0 = запрет; N = фикс
    short_qty: Optional[int] = None    # None (пусто) = авто; 0 = запрет; N = фикс

    # === Session (MSK) ===
    session_start: str = "10:00"
    session_end: str = "18:45"
    use_evening: bool = True
    evening_start: str = "19:00"
    evening_end: str = "23:50"

    # === Misc ===
    entry_on_start: bool = False       # ☑ Позиция при старте: сразу войти по режиму (без фильтров)
    paper: bool = False                # paper = сделки симулируются (для проверки)
    history_bars: int = 400            # сколько свечей грузить для индикаторов


def load_params() -> MtlrParams:
    p = MtlrParams()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                if hasattr(p, k):
                    setattr(p, k, v)
        except Exception:
            pass
    return p


def save_params(p: MtlrParams) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(asdict(p), f, ensure_ascii=False, indent=2)


# timeframe -> ISS interval id
TF_TO_ISS = {"M1": 1, "M5": 5, "M10": 10, "M15": 15, "M30": 30, "H1": 60, "D": 24}
# timeframe -> seconds
TF_SECONDS = {"M1": 60, "M5": 300, "M10": 600, "M15": 900, "M30": 1800, "H1": 3600, "D": 86400}
