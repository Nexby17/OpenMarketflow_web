"""Indicators for MTLR robot: EMA, PSAR, ATR, RSI, regime oscillator (EMA−SAR)/ATR.

Pure Python, no external deps except stdlib. All functions take lists of floats
and return lists aligned to input (None where not yet defined).
"""

from typing import List, Optional


def ema(values: List[float], period: int) -> List[Optional[float]]:
    """Exponential moving average. First value defined at index period-1 (SMA seed)."""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n == 0 or period <= 0:
        return out
    k = 2.0 / (period + 1)
    seed = 0.0
    for i in range(min(period, n)):
        seed += values[i]
    if n < period:
        return out
    seed /= period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def psar(highs: List[float], lows: List[float],
         af_start: float = 0.03, af_step: float = 0.008, af_max: float = 0.2
         ) -> List[Optional[float]]:
    """Parabolic SAR (Wilder). Returns list aligned to input."""
    n = len(highs)
    out: List[Optional[float]] = [None] * n
    if n < 2:
        return out
    # Initial direction: up if close[0] < close[1] proxy via highs/lows trend
    up = highs[1] >= lows[0]
    sar = lows[0] if up else highs[0]
    ep = highs[1] if up else lows[1]   # extreme point
    af = af_start
    out[1] = sar
    for i in range(2, n):
        prev_sar = sar
        sar = prev_sar + af * (ep - prev_sar)
        if up:
            # SAR may not be above prior two lows
            sar = min(sar, lows[i - 1], lows[i - 2])
            if lows[i] < sar:            # reversal to down
                up = False
                sar = ep
                ep = lows[i]
                af = af_start
            else:
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + af_step, af_max)
        else:
            sar = max(sar, highs[i - 1], highs[i - 2])
            if highs[i] > sar:           # reversal to up
                up = True
                sar = ep
                ep = highs[i]
                af = af_start
            else:
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + af_step, af_max)
        out[i] = sar
    return out


def atr(highs: List[float], lows: List[float], closes: List[float],
        period: int = 14) -> List[Optional[float]]:
    """Average True Range (Wilder smoothing)."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n < period + 1 or period <= 0:
        return out
    trs = [0.0] * n
    for i in range(1, n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs[i] = tr
    # seed: SMA of first `period` TRs (i=1..period)
    seed = sum(trs[1:period + 1]) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """RSI (Wilder)."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n < period + 1 or period <= 0:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    ag = gains / period
    al = losses / period
    out[period] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(period + 1, n):
        ch = closes[i] - closes[i - 1]
        g = ch if ch > 0 else 0.0
        l = -ch if ch < 0 else 0.0
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def regime_osc(ema_vals: List[Optional[float]], sar_vals: List[Optional[float]],
               atr_vals: List[Optional[float]]) -> List[Optional[float]]:
    """Regime oscillator: (EMA − SAR)/ATR. >0 LONG regime, <0 SHORT, 0 = flip.

    Note: in PSAR the SAR sits BELOW price in uptrend, ABOVE in downtrend,
    so EMA−SAR > 0 means uptrend (SAR under EMA).
    """
    n = len(ema_vals)
    out: List[Optional[float]] = [None] * n
    for i in range(n):
        e, s, a = ema_vals[i], sar_vals[i], atr_vals[i]
        if e is None or s is None or a is None or a == 0:
            continue
        out[i] = (e - s) / a
    return out
