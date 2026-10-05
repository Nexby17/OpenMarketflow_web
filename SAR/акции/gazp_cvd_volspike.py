#!/usr/bin/env python3
"""
CVD Trend + Volume Spike backtest v1 — GAZP 2Y M1.

Logic:
  Direction (top-down): M60 CVD trend + M15 CVD trend must agree (long/short).
  Trigger: M5 volume spike (vol >= K * SMA20(prev vol)) in direction of delta.
  Entry: next M1 open. Exit: ATR stop / M15 trend flip / 18:30 MSK session close.

Data caveat: no tick data for stocks -> proxy delta = sign(close-open)*volume.
  Real tick CVD would differ; this is a feasibility spike.
"""

import numpy as np
import pandas as pd
import time

DATA = "/root/.openclaw/workspace/HedgeFund/backtest/data/GAZP_2Y_M1.csv"

# session in UTC (data is UTC; MSK = UTC+3): main 10:00-18:40 MSK -> 07:00-15:40 UTC
SESSION_START = "07:00"
ENTRY_CUTOFF = "14:00"   # MSK 17:00, no new entries in last hour
SESSION_CLOSE = "15:30"  # MSK 18:30 force close

COST_PCT = 0.0005        # per side: 0.04% commission + 0.01% slippage
LOT = 10                 # GAZP lot = 10 shares

TREND_EMA = {"5min": 20, "15min": 12, "60min": 6}


def load():
    df = pd.read_csv(DATA)
    df = df.rename(columns={"begin": "t"})
    df["t"] = pd.to_datetime(df["t"])
    df["date"] = df["t"].dt.date
    df["delta"] = np.sign(df["close"] - df["open"]) * df["volume"]
    tod = df["t"].dt.hour * 3600 + df["t"].dt.minute * 60
    df["tod"] = tod.astype("int64") * 10**9  # ns since midnight
    df = df[(tod >= 7 * 3600) & (tod <= 15 * 3600 + 40 * 60)].reset_index(drop=True)
    return df[["t", "date", "open", "high", "low", "close", "volume", "delta", "tod"]]


def tf_bars(df1, freq):
    g = (df1.set_index("t")
         .groupby("date")
         .resample(freq)
         .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
              close=("close", "last"), volume=("volume", "sum"), delta=("delta", "sum")))
    g = g[g["volume"] > 0].reset_index().rename(columns={"t": "begin"})
    g["end"] = g["begin"] + pd.Timedelta(freq)
    cvd = g.groupby("date")["delta"].cumsum()
    n = TREND_EMA[freq]
    ema = cvd.ewm(span=n, adjust=False).mean()
    g["trend"] = np.where((cvd > ema) & (ema > ema.shift(2)), 1,
                 np.where((cvd < ema) & (ema < ema.shift(2)), -1, 0))
    return g


def m5_extras(g5):
    pc = g5["close"].shift(1)
    same_day = g5["date"].eq(g5["date"].shift(1))
    tr = pd.concat([g5["high"] - g5["low"],
                    (g5["high"] - pc).abs(),
                    (g5["low"] - pc).abs()], axis=1).max(axis=1)
    tr[~same_day] = g5["high"] - g5["low"]
    g5["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    g5["sma_vol"] = g5["volume"].rolling(20).mean().shift(1)
    return g5


def to_m1(m1, g, cols):
    src = g.sort_values("end")[["end"] + cols].rename(columns={"end": "t"})
    return pd.merge_asof(m1, src, on="t", direction="backward")


def run(m1arr, spike, dsign, atr_mult, fade=False, gates=True):
    # arrays: tod(ns since midnight), o,h,l,c, tr15, tr60, atr
    tod, o, h, l, c, tr15, tr60, atr = m1arr
    n = len(tod)
    t_cut = 14 * 3600 * 10**9            # 14:00 UTC = 17:00 MSK, no new entries after
    t_cls = 15 * 3600 * 10**9 + 30 * 60 * 10**9  # 15:30 UTC = 18:30 MSK force close

    pos = 0
    entry = stop = 0.0
    entry_i = 0
    entry_hour = 0
    pnl = 0.0
    peak = 0.0
    maxdd = 0.0
    trades = []
    hour_stats = {}

    for i in range(n):
        # session close
        if pos != 0 and tod[i] >= t_cls:
            px = o[i]
            p = (px - entry) * pos - (px + entry) * COST_PCT
            pnl += p
            trades.append(p)
            hour_stats[entry_hour][0] += 1
            hour_stats[entry_hour][1] += p
            pos = 0
            continue
        if pos != 0:
            # stop intrabar
            if (pos > 0 and l[i] <= stop) or (pos < 0 and h[i] >= stop):
                px = stop
                p = (px - entry) * pos - (px + entry) * COST_PCT
                pnl += p
                trades.append(p)
                hour_stats[entry_hour][0] += 1
                hour_stats[entry_hour][1] += p
                pos = 0
            # M15 flip (known at bar open)
            elif tr15[i] == -pos:
                px = o[i]
                p = (px - entry) * pos - (px + entry) * COST_PCT
                pnl += p
                trades.append(p)
                hour_stats[entry_hour][0] += 1
                hour_stats[entry_hour][1] += p
                pos = 0
        if pos == 0 and tod[i] < t_cut:
            g60 = (tr60[i] == 1) if gates else True
            g15 = (tr15[i] == 1) if gates else True
            ds = dsign[i] if not fade else -dsign[i]
            tr_ok = 0
            if g60 and g15 and spike[i] and ds > 0:
                tr_ok = 1
            else:
                g60s = (tr60[i] == -1) if gates else True
                g15s = (tr15[i] == -1) if gates else True
                if g60s and g15s and spike[i] and ds < 0:
                    tr_ok = -1
            if tr_ok != 0 and not np.isnan(atr[i]) and atr[i] > 0:
                pos = tr_ok
                entry = o[i]
                risk = atr_mult * atr[i]
                stop = entry - risk if pos > 0 else entry + risk
                entry_i = i
                entry_hour = (tod[i] // 3_600_000_000_000 + 3) % 24
                hour_stats.setdefault(entry_hour, [0, 0.0])
                # same-bar stop
                if (pos > 0 and l[i] <= stop) or (pos < 0 and h[i] >= stop):
                    p = (stop - entry) * pos - (stop + entry) * COST_PCT
                    pnl += p
                    trades.append(p)
                    hour_stats[entry_hour][0] += 1
                    hour_stats[entry_hour][1] += p
                    pos = 0
        # mark-to-market dd
        eq = pnl + (c[i] - entry) * pos if pos != 0 else pnl
        if eq > peak:
            peak = eq
        if peak - eq > maxdd:
            maxdd = peak - eq

    tr = np.array(trades)
    out = {
        "trades": len(tr),
        "wr": (tr > 0).mean() * 100 if len(tr) else 0,
        "pf": tr[tr > 0].sum() / max(1e-9, -tr[tr < 0].sum()) if (tr < 0).any() else float("inf"),
        "avg": tr.mean() if len(tr) else 0,
        "total": tr.sum() if len(tr) else 0,
        "maxdd": maxdd,
        "hours": hour_stats,
    }
    return out

def main():
    t0 = time.time()
    df = load()
    print(f"loaded {len(df)} M1 bars, {df['date'].nunique()} days, {time.time()-t0:.1f}s")

    g5 = m5_extras(tf_bars(df, "5min"))
    g15 = tf_bars(df, "15min")
    g60 = tf_bars(df, "60min")

    m1 = df[["t", "tod", "open", "high", "low", "close"]].copy()
    m1 = to_m1(m1, g5, ["trend", "atr", "sma_vol"]).rename(columns={"trend": "tr5"})
    m1 = to_m1(m1, g15, ["trend"]).rename(columns={"trend": "tr15"})
    m1 = to_m1(m1, g60, ["trend"]).rename(columns={"trend": "tr60"})
    m1 = m1.dropna(subset=["sma_vol"]).reset_index(drop=True)

    # m5 bar data aligned to m1 (the closed bar): spike + delta sign
    g5s = g5[["end", "volume", "sma_vol", "delta", "high", "low", "open", "close"]].copy()
    spikes = {}
    dsigns = {}
    for K in (1.5, 2.0, 2.5):
        g5s[f"sp{K}"] = (g5s["volume"] >= K * g5s["sma_vol"]) & g5s["sma_vol"].notna()
        spikes[K] = g5s["sp" + str(K)]
        dsigns[K] = np.sign(g5s["delta"])

    arr = (m1["tod"].values.astype("int64"), m1["open"].values, m1["high"].values,
           m1["low"].values, m1["close"].values, m1["tr15"].values,
           m1["tr60"].values, m1["atr"].values)

    # per-K spike/delta mapped to m1
    def map_col(vals):
        tmp = pd.DataFrame({"t": g5s["end"].values, "v": vals}).sort_values("t")
        return pd.merge_asof(m1[["t"]], tmp, on="t", direction="backward")["v"].values

    results = []
    modes = [
        ("mom+gates", False, True),
        ("FADE+gates", True, True),
        ("FADE", True, False),
        ("mom", False, False),
    ]
    for mode, fade, gates in modes:
        for K in (1.5, 2.0, 2.5):
            sp = map_col(spikes[K].values.astype(bool))
            ds = map_col(dsigns[K].values)
            for am in (1.0, 1.5, 2.0):
                r = run(arr, sp, ds, am, fade=fade, gates=gates)
                r["mode"], r["K"], r["am"] = mode, K, am
                results.append(r)
                print(f"{mode:11s} K={K} ATRx={am}: trades={r['trades']:4d} WR={r['wr']:5.1f}% "
                      f"PF={r['pf']:5.2f} avg={r['avg']*LOT:+7.1f}₽/lot total={r['total']*LOT:+10.0f}₽ "
                      f"maxDD={r['maxdd']*LOT:8.0f}₽  [{time.time()-t0:.0f}s]")
        print()

    results.sort(key=lambda r: r["total"], reverse=True)
    best = results[0]
    print("=== BEST: %s K=%.1f ATRx=%.1f ===" % (best["mode"], best["K"], best["am"]))
    print(f"trades={best['trades']} WR={best['wr']:.1f}% PF={best['pf']:.2f} "
          f"avg={best['avg']*LOT:.1f}₽/lot total={best['total']*LOT:.0f}₽ maxDD={best['maxdd']*LOT:.0f}₽")
    print("PnL by entry hour (MSK):")
    for hh in sorted(best["hours"]):
        cnt, s = best["hours"][hh]
        if cnt:
            print(f"  {hh:02d}:00 -> {cnt:3d} trades, {s*LOT:+9.0f}₽")


if __name__ == "__main__":
    main()
