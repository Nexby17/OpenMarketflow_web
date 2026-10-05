#!/usr/bin/env python3
"""
GAZP v4 — наш SAR/EMA (как на фьючах) на акциях: вход по кроссу PSAR×EMA,
выход: противоположный кросс / chandelier ATR / SAR±k×ATR.

Обе полярности (у V8 на SI инвертированная логика — проверяем обе).
ТФ: H1 и M15. Овертейлы разрешены. Начальный стоп 3×ATR всегда.
"""

import numpy as np
import pandas as pd
import time

from gazp_cvd_volspike import load, tf_bars, m5_extras, LOT, COST_PCT


def psar(df, af0, af_step, af_max=0.2):
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


def run(bars, long_sig, short_sig, exit_mode, sar=None, chand_k=None, sar_k=None,
        init_stop_atr=3.0, cost=COST_PCT):
    n = len(bars)
    if long_sig is None:
        long_sig = np.zeros(n, dtype=bool)
    if short_sig is None:
        short_sig = np.zeros(n, dtype=bool)
    o = bars["open"].values
    h = bars["high"].values
    l = bars["low"].values
    c = bars["close"].values
    atr = bars["atr"].values

    pos = 0
    entry = stop = 0.0
    hi = lo = 0.0
    pnl = 0.0
    peak = 0.0
    maxdd = 0.0
    trades = []
    hold = []

    def close_pos(i, px):
        nonlocal pnl, pos, peak, maxdd
        p = (px - entry) * pos - (px + entry) * cost
        pnl += p
        trades.append(p)
        hold.append(i - entry_i)
        pos = 0

    for i in range(n):
        if pos != 0:
            # трейлинг-обновления
            if exit_mode == "chand":
                if pos > 0:
                    hi = max(hi, c[i])
                    stop = max(stop, hi - chand_k * atr[i])
                else:
                    lo = min(lo, c[i])
                    stop = min(stop, lo + chand_k * atr[i])
            elif exit_mode == "sar" and sar is not None:
                if pos > 0:
                    stop = max(stop, sar[i] - sar_k * atr[i])
                else:
                    stop = min(stop, sar[i] + sar_k * atr[i])
            # выходы
            if (pos > 0 and o[i] <= stop) or (pos < 0 and o[i] >= stop):
                close_pos(i, o[i])          # гэп
            elif (pos > 0 and l[i] <= stop) or (pos < 0 and h[i] >= stop):
                close_pos(i, stop)
            elif exit_mode == "cross" and ((pos > 0 and short_sig[i]) or (pos < 0 and long_sig[i])):
                close_pos(i, o[i])
        if pos == 0 and (long_sig[i] or short_sig[i]) and not np.isnan(atr[i]) and atr[i] > 0:
            pos = 1 if long_sig[i] else -1
            entry = o[i + 1] if i + 1 < n else c[i]
            risk = init_stop_atr * atr[i]
            stop = entry - risk if pos > 0 else entry + risk
            hi = lo = entry
            entry_i = i + 1
        eq = pnl + (c[i] - entry) * pos if pos != 0 else pnl
        if eq > peak:
            peak = eq
        if peak - eq > maxdd:
            maxdd = peak - eq

    tr = np.array(trades)
    return {
        "trades": len(tr),
        "wr": (tr > 0).mean() * 100 if len(tr) else 0,
        "pf": tr[tr > 0].sum() / max(1e-9, -tr[tr < 0].sum()) if (tr < 0).any() else float("inf"),
        "avg": tr.mean() if len(tr) else 0,
        "total": tr.sum() if len(tr) else 0,
        "maxdd": maxdd,
        "hold": np.mean(hold) if hold else 0,
    }


def build(df, freq, af0, af_step, ema_n):
    g = m5_extras(tf_bars(df, freq))
    sar, trend = psar(g, af0, af_step)
    g["sar"] = sar
    g["ema"] = g["close"].ewm(span=ema_n, adjust=False).mean()
    g = g.reset_index(drop=True)
    up = (g["sar"].shift(1) < g["ema"].shift(1)) & (g["sar"] >= g["ema"])
    dn = (g["sar"].shift(1) > g["ema"].shift(1)) & (g["sar"] <= g["ema"])
    return g, up.values, dn.values


def main():
    t0 = time.time()
    df = load()
    all_results = []
    for freq in ("60min", "15min"):
        for af0, af_step in ((0.03, 0.008), (0.02, 0.2)):
            for ema_n in (12, 20):
                g, up, dn = build(df, freq, af0, af_step, ema_n)
                for pol, long_sig, short_sig in (("std", dn, up), ("inv", up, dn)):
                    variants = [
                        ("cross", dict(exit_mode="cross")),
                        ("chand3", dict(exit_mode="chand", chand_k=3.0)),
                        ("chand2", dict(exit_mode="chand", chand_k=2.0)),
                        ("sar+0", dict(exit_mode="sar", sar=g["sar"].values, sar_k=0.0)),
                        ("sar+1", dict(exit_mode="sar", sar=g["sar"].values, sar_k=1.0)),
                    ]
                    for name, kw in variants:
                        r = run(g, long_sig, short_sig, **kw)
                        r.update(tf=freq, pol=pol, sar=f"{af0}/{af_step}", ema=ema_n, ex=name)
                        all_results.append(r)
        print(f"[{freq}] done [{time.time()-t0:.0f}s]")

    all_results.sort(key=lambda r: r["total"], reverse=True)
    print(f"\n=== TOP-12 из {len(all_results)} (₽/лот) ===")
    for r in all_results[:12]:
        print(f"{r['tf']:6s} {r['pol']} SAR{r['sar']} EMA{r['ema']:2d} exit={r['ex']:6s}: "
              f"tr={r['trades']:4d} WR={r['wr']:4.1f}% PF={r['pf']:5.2f} "
              f"{r['total']*LOT:+8.0f}₽ maxDD={r['maxdd']*LOT:7.0f}₽ hold={r['hold']:5.1f}б")

    best = all_results[0]
    g, up, dn = build(df, best["tf"], *(float(x) for x in best["sar"].split("/")), best["ema"])
    long_sig, short_sig = (dn, up) if best["pol"] == "std" else (up, dn)
    g["lsig"] = long_sig
    g["ssig"] = short_sig
    kw = {"cross": dict(exit_mode="cross"), "chand3": dict(exit_mode="chand", chand_k=3.0),
          "chand2": dict(exit_mode="chand", chand_k=2.0),
          "sar+0": dict(exit_mode="sar", sar_k=0.0),
          "sar+1": dict(exit_mode="sar", sar_k=1.0)}[best["ex"]]
    g["half"] = g["begin"].dt.year.astype(str) + "H" + ((g["begin"].dt.month > 6).astype(int) + 1).astype(str)
    print(f"\n=== Walk-forward лучшего: {best['tf']} {best['pol']} SAR{best['sar']} EMA{best['ema']} exit={best['ex']} ===")
    for half, seg in g.groupby("half"):
        seg = seg.reset_index(drop=True)
        sar_arr = seg["sar"].values if best["ex"].startswith("sar") else None
        r = run(seg, seg["lsig"].values, seg["ssig"].values, sar=sar_arr, **kw)
        print(f"{half}: tr={r['trades']:3d} WR={r['wr']:4.1f}% PF={r['pf']:5.2f} {r['total']*LOT:+8.0f}₽ maxDD={r['maxdd']*LOT:.0f}₽")


if __name__ == "__main__":
    main()
