#!/usr/bin/env python3
"""v4 SAR/EMA setup on multiple tickers (H1): per-ticker grid + walk-forward of GAZP champion."""

import numpy as np
import pandas as pd
import time

from gazp_cvd_volspike import LOT
from gazp_sarema import build, run

DATA = "/root/.openclaw/workspace/HedgeFund/backtest/data"
import glob as _glob
TICKERS = [p.split("/")[-1].split("_")[0] for p in sorted(_glob.glob(f"{DATA}/*_2Y_H1.csv"))]


def load_ticker(tic):
    if tic == "GAZP":
        from gazp_cvd_volspike import load
        return load()  # M1 -> build() сам ресемплит в 60min
    df = pd.read_csv(f"{DATA}/{tic}_2Y_H1.csv")
    df["t"] = pd.to_datetime(df["begin"])
    df["date"] = df["t"].dt.date
    tod = df["t"].dt.hour * 3600 + df["t"].dt.minute * 60
    df = df[(tod >= 7 * 3600) & (tod <= 15 * 3600 + 40 * 60)].reset_index(drop=True)
    df["delta"] = np.sign(df["close"] - df["open"]) * df["volume"]
    return df


def main():
    t0 = time.time()
    champ = (0.02, 0.2, 12, "sar0")  # GAZP champion: SAR af0/step, EMA, exit
    rows = []
    for tic in TICKERS:
        df = load_ticker(tic)
        best = None
        champ_r = None
        for af0, af_step in ((0.02, 0.2), (0.03, 0.008)):
            for ema_n in (12, 20):
                g, up, dn = build(df, "60min", af0, af_step, ema_n)
                for pol, ls, ss in (("std", dn, up), ("inv", up, dn)):
                    for ex, kw in (("sar0", dict(exit_mode="sar", sar_k=0.0)),
                                   ("sar1", dict(exit_mode="sar", sar_k=1.0)),
                                   ("cross", dict(exit_mode="cross"))):
                        sar_arr = g["sar"].values if ex.startswith("sar") else None
                        r = run(g, ls, ss, sar=sar_arr, **kw)
                        r.update(tic=tic, pol=pol, sar=f"{af0}/{af_step}", ema=ema_n, ex=ex)
                        rows.append(r)
                        if (af0, af_step, ema_n, ex) == champ and pol == "std":
                            champ_r = r
                        if best is None or r["total"] > best["total"]:
                            best = r
        print(f"{tic:5s} bars={len(g):5d} | CHAMP(SAR.02/.2 EMA12 sar0 std): tr={champ_r['trades']:4d} "
              f"WR={champ_r['wr']:4.1f}% PF={champ_r['pf']:5.2f} {champ_r['total']*LOT:+7.0f}₽ DD={champ_r['maxdd']*LOT:6.0f}₽ | "
              f"best-of-grid: {best['pol']} SAR{best['sar']} EMA{best['ema']} {best['ex']}: "
              f"PF={best['pf']:5.2f} {best['total']*LOT:+7.0f}₽ [{time.time()-t0:.0f}s]")

    # walk-forward of GAZP champion config per ticker
    print("\n=== Walk-forward чемпиона (std SAR0.02/0.2 EMA12 sar0) по полугодиям ===")
    for tic in TICKERS:
        df = load_ticker(tic)
        g, up, dn = build(df, "60min", 0.02, 0.2, 12)
        g["lsig"] = dn   # std polarity: SAR падает под EMA → LONG
        g["ssig"] = up
        g["half"] = g["begin"].dt.year.astype(str) + "H" + ((g["begin"].dt.month > 6).astype(int) + 1).astype(str)
        parts = []
        for half, seg in g.groupby("half"):
            seg = seg.reset_index(drop=True)
            r = run(seg, seg["lsig"], seg["ssig"], exit_mode="sar", sar=seg["sar"].values, sar_k=0.0)
            parts.append(f"{half}:{r['pf']:4.2f}")
        print(f"{tic:5s} {' '.join(parts)}")

    ok15 = sum(1 for r in rows if r["pol"] == "std" and r["ex"] == "sar0" and r["pf"] >= 1.5)
    ok13 = sum(1 for r in rows if r["pol"] == "std" and r["ex"] == "sar0" and r["pf"] >= 1.3)
    n_setups = len(TICKERS) * 4
    print(f"\nЧемп-конфиг (std sar0, все SAR/EMA): PF>=1.5 у {ok15}/{n_setups}, PF>=1.3 у {ok13}/{n_setups} сетапов")


if __name__ == "__main__":
    main()
