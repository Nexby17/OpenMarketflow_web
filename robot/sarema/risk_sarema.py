"""F-061: Risk sizing — R-менеджмент + жёсткие лимиты Дмитрия (D2) + kill-switch.

Риск от base (некомпаунд, compound=false по умолчанию). Lотность — параметр
(в роботе приходит из Finam /v1/assets, не хардкод).
"""
from datetime import date, timedelta
from typing import Optional


def risk_sizing(base: float, risk_pct: float, stop_dist: float) -> int:
    """risk_sizing = floor(base × risk_pct / stop_dist)."""
    if stop_dist <= 0 or base <= 0 or risk_pct <= 0:
        return 0
    return int(base * risk_pct / stop_dist)


def apply_lot_rounding(qty: int, lot_size: float) -> int:
    """Округление вниз до целых лотов; lot_size <= 0 или дробный -> 1."""
    if lot_size is None or lot_size <= 0:
        lot_size = 1
    lot = int(lot_size)
    if lot < 1:
        lot = 1
    return (qty // lot) * lot


def apply_liq_cap(qty: int, bar_volume: float, liq_frac: Optional[float]) -> int:
    """Лимит участия: qty <= liq_frac × volume H1-бара входа."""
    if liq_frac is None or liq_frac <= 0:
        return qty
    cap = int(liq_frac * float(bar_volume))
    return min(qty, max(0, cap))


def check_kill_switch(state, today: date, day_kill_R: Optional[float]) -> Optional[str]:
    """Если вчера/в предыдущий торговый день реализованный R <= day_kill_R,
    ставим блокировку на следующий торговый день (календарное приближение:
    пропускаем выходные — суббота/воскресенье). Возвращает kill_day или None.
    """
    if day_kill_R is None:
        return None
    # ищем предыдущий торговый день (пн-пт) с реализованным R
    d = today - timedelta(days=1)
    for _ in range(7):
        if d.weekday() < 5:
            r = state.day_realized_R.get(str(d))
            if r is not None:
                if r <= day_kill_R and state.kill_day != str(today):
                    return str(today)
                return None
        d -= timedelta(days=1)
    return None
