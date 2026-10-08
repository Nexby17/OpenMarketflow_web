"""F-061 Stage 1: SAR×EMA H1 signal engine — exact port of the backtest.

Порт 1-в-1: SAR/акции/gazp_sarema.py (psar, build) + stocks_sarema_rmm.extract_trades.
Без портфельной логики и R-сайзинга (этап 2). Std-полярность: SAR нырок под EMA = LONG.
"""
import os
import numpy as np
import pandas as pd

COST_PCT = 0.0005          # 0.05% на сторону
INIT_STOP_ATR = 3.0
SAR_AF0, SAR_STEP, EMA_N = 0.02, 0.2, 12

# Окно данных бэктеста (первые входы 2024-09-02, последний выход 2026-09-04)
T_START = pd.Timestamp("2024-09-02 00:00:00")
T_END = pd.Timestamp("2026-09-05 00:00:00")

TICKERS = ["SBER", "GAZP", "LKOH", "ROSN", "TATN", "GMKN", "NVTK", "PLZL", "MTSS", "SNGS"]


def load_m1(tic: str, data_dir: str) -> pd.DataFrame:
    """M1 CSV -> DataFrame с фильтром основной сессии 07:00-15:40 UTC."""
    df = pd.read_csv(os.path.join(data_dir, f"{tic}_2Y_M1.csv"))
    df["t"] = pd.to_datetime(df["begin"])
    df = df[(df["t"] >= T_START) & (df["t"] < T_END)].reset_index(drop=True)
    df["date"] = df["t"].dt.date
    tod = df["t"].dt.hour * 3600 + df["t"].dt.minute * 60
    df = df[(tod >= 7 * 3600) & (tod <= 15 * 3600 + 40 * 60)].reset_index(drop=True)
    return df


def tf_bars(df1: pd.DataFrame, freq: str = "60min") -> pd.DataFrame:
    """Агрегация M1 в H1 по часам внутри дня (volume>0), порт tf_bars без trend/CVD."""
    bucket = df1["t"].dt.floor(freq)
    g = (df1.assign(_b=bucket)
         .groupby(["date", "_b"], sort=True)
         .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
              close=("close", "last"), volume=("volume", "sum"))
         .reset_index()
         .rename(columns={"_b": "begin"}))
    g = g[g["volume"] > 0].reset_index(drop=True)
    return g


def m5_extras(g: pd.DataFrame) -> pd.DataFrame:
    """ATR: TR со сбросом на первом баре дня (TR=High-Low), EWM alpha=1/14."""
    pc = g["close"].shift(1)
    same_day = g["date"].eq(g["date"].shift(1))
    tr = pd.concat([g["high"] - g["low"],
                    (g["high"] - pc).abs(),
                    (g["low"] - pc).abs()], axis=1).max(axis=1)
    tr[~same_day] = g["high"] - g["low"]
    g["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    return g


def psar(df: pd.DataFrame, af0: float, af_step: float, af_max: float = 0.2):
    """Точный порт psar() из gazp_sarema.py (включая стартовый сид и clamping)."""
    high = df["high"].values
    low = df["low"].values
    n = len(df)
    sar = np.zeros(n)
    trend = np.zeros(n)
    sar[0] = low[0]
    trend[0] = 1
    af = af0
    ep = high[0]
    for i in range(1, n):
        tr = trend[i - 1]
        s = sar[i - 1] + af * (ep - sar[i - 1])
        if tr == 1:
            s = min(s, low[i - 1], low[max(0, i - 2)])
        else:
            s = max(s, high[i - 1], high[max(0, i - 2)])
        if tr == 1 and low[i] < s:
            tr, s, ep, af = -1, ep, low[i], af0
        elif tr == -1 and high[i] > s:
            tr, s, ep, af = 1, ep, high[i], af0
        else:
            if tr == 1 and high[i] > ep:
                ep = high[i]
                af = min(af + af_step, af_max)
            elif tr == -1 and low[i] < ep:
                ep = low[i]
                af = min(af + af_step, af_max)
        trend[i] = tr
        sar[i] = s
    return sar, trend


def build(df: pd.DataFrame, freq: str, af0: float, af_step: float, ema_n: int, af_max: float = 0.2):
    """Возвращает (g, up, dn): std-полярность — dn (SAR нырок под EMA) = LONG сигнал."""
    g = m5_extras(tf_bars(df, freq))
    sar, trend = psar(g, af0, af_step, af_max)
    g["sar"] = sar
    g["ema"] = g["close"].ewm(span=ema_n, adjust=False).mean()
    g = g.reset_index(drop=True)
    up = (g["sar"].shift(1) < g["ema"].shift(1)) & (g["sar"] >= g["ema"])
    dn = (g["sar"].shift(1) > g["ema"].shift(1)) & (g["sar"] <= g["ema"])
    return g, up.values, dn.values


def extract_trades(tic: str, data_dir: str, g_cache: dict | None = None):
    """Точный порт extract_trades из stocks_sarema_rmm.py (сигнальный слой)."""
    if g_cache is not None and tic in g_cache:
        g = g_cache[tic]
    else:
        df = load_m1(tic, data_dir)
        g, _up, _dn = build(df, "60min", SAR_AF0, SAR_STEP, EMA_N)
        if g_cache is not None:
            g_cache[tic] = g
    # сигналы пересчитываются из колонок g (sar/ema) — работает и при кэш-попадании
    up = ((g["sar"].shift(1) < g["ema"].shift(1)) & (g["sar"] >= g["ema"])).values
    dn = ((g["sar"].shift(1) > g["ema"].shift(1)) & (g["sar"] <= g["ema"])).values
    sar = g["sar"].values
    o = g["open"].values
    h = g["high"].values
    l = g["low"].values
    atr = g["atr"].values
    ts = g["begin"].values
    n = len(g)
    trades = []
    pos = 0
    entry = stop = 0.0
    entry_i = -1
    risk_share = 0.0
    for i in range(n):
        if pos != 0:
            if pos > 0:
                stop = max(stop, sar[i])
            else:
                stop = min(stop, sar[i])
            exit_px = None
            if (pos > 0 and o[i] <= stop) or (pos < 0 and o[i] >= stop):
                exit_px = o[i]                       # гэп через стоп
            elif (pos > 0 and l[i] <= stop) or (pos < 0 and h[i] >= stop):
                exit_px = stop                        # касание
            if exit_px is not None:
                gross = (exit_px - entry) * pos
                cost = (exit_px + entry) * COST_PCT
                trades.append({
                    "tic": tic, "dir": pos, "t_entry": ts[entry_i], "t_exit": ts[i],
                    "entry": entry, "exit": exit_px,
                    "atr": risk_share / INIT_STOP_ATR,
                    "gross_sh": gross - cost})
                pos = 0
        if pos == 0:
            long_sig = dn[i]
            short_sig = up[i]
            if (long_sig or short_sig) and not np.isnan(atr[i]) and atr[i] > 0 and i + 1 < n:
                pos = 1 if long_sig else -1
                entry = o[i + 1]
                risk_share = INIT_STOP_ATR * atr[i]
                stop = entry - risk_share if pos > 0 else entry + risk_share
                entry_i = i + 1
    return trades, g
