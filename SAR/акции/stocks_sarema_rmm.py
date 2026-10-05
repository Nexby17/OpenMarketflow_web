#!/usr/bin/env python3
"""Stocks SAR/EMA (H1 champion) + R-multiple money management. Redo 03.10.2026.

Слой стратегии — в точности v4/v5 (gazp_sarema.build/run, multi_sarema):
  H1, std-полярность (SAR ныряет под EMA -> LONG), SAR 0.02/0.2, EMA12,
  вход по open следующего бара после кросса, выход по касанию PSAR,
  начальный стоп 3 x ATR(сигнальный бар), издержки 0.1% RT (0.05%/сторона).

Слой R-менеджмента (Ван Тарп):
  1R = equity x risk_pct; размер = 1R / stop_dist (3xATR);
  кэп: номинал позиции <= equity x max_pos_pct;
  R-кратное сделки = net PnL / фактический начальный риск.
Портфель: 10 бумаг, сделки слиты хронологически, equity по закрытиям
(приближение: переоценка открытых позиций не ведётся — отмечено в отчёте).
"""
import sys, os, time, csv
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gazp_sarema import build
from gazp_cvd_volspike import COST_PCT

DATA = "/root/.openclaw/workspace/HedgeFund/backtest/data"
RESULTS = "/root/.openclaw/workspace/HedgeFund/backtest/results"
TICKERS = ["SBER", "GAZP", "LKOH", "ROSN", "TATN", "GMKN", "NVTK", "PLZL", "MTSS", "SNGS"]

INIT_STOP_ATR = 3.0
SAR_AF0, SAR_STEP, EMA_N = 0.02, 0.2, 12


def load_ticker(tic):
    if tic == "GAZP":
        from gazp_cvd_volspike import load
        return load()
    df = pd.read_csv(f"{DATA}/{tic}_2Y_H1.csv")
    df["t"] = pd.to_datetime(df["begin"])
    df["date"] = df["t"].dt.date
    tod = df["t"].dt.hour * 3600 + df["t"].dt.minute * 60
    df = df[(tod >= 7 * 3600) & (tod <= 15 * 3600 + 40 * 60)].reset_index(drop=True)
    df["delta"] = np.sign(df["close"] - df["open"]) * df["volume"]
    return df


def extract_trades(tic):
    """Сделки чемпион-конфига с ценами, временем и ATR на входе (зеркало run())."""
    df = load_ticker(tic)
    g, up, dn = build(df, "60min", SAR_AF0, SAR_STEP, EMA_N)
    sar = g["sar"].values
    o = g["open"].values; h = g["high"].values
    l = g["low"].values; atr = g["atr"].values
    ts = g["begin"].values if "begin" in g else g["t"].values
    n = len(g)
    trades = []
    pos = 0
    entry = stop = 0.0
    entry_i = -1
    risk_share = 0.0
    for i in range(n):
        if pos != 0:
            # трейлинг по SAR (sar_k=0): стоп подтягивается к SAR
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
                trades.append({"tic": tic, "dir": pos, "entry_i": entry_i, "exit_i": i,
                               "t_entry": ts[entry_i], "t_exit": ts[i],
                               "entry": entry, "exit": exit_px,
                               "atr": risk_share / INIT_STOP_ATR,
                               "gross_sh": gross - cost})
                pos = 0
        if pos == 0:
            long_sig = dn[i]   # std: SAR падает под EMA -> LONG
            short_sig = up[i]
            if (long_sig or short_sig) and not np.isnan(atr[i]) and atr[i] > 0 and i + 1 < n:
                pos = 1 if long_sig else -1
                entry = o[i + 1]
                risk_share = INIT_STOP_ATR * atr[i]
                stop = entry - risk_share if pos > 0 else entry + risk_share
                entry_i = i + 1
    return trades


def portfolio_rmm(all_trades, equity0=1_000_000.0, risk_pct=0.01, max_pos_pct=0.40,
                  max_open=None, compound=True):
    """Хронологическое портфельное моделирование с R-сайзингом."""
    eq = equity0
    open_trades = []   # (t_exit) для учёта одновременности
    recs = []
    eq_curve = []
    for t in sorted(all_trades, key=lambda x: x["t_entry"]):
        # позиции, открытые на момент входа
        while open_trades and open_trades[0][0] <= t["t_entry"]:
            open_trades.pop(0)
        if max_open is not None and len(open_trades) >= max_open:
            t["shares"] = 0
            continue
        stop_dist = INIT_STOP_ATR * t["atr"]
        if stop_dist <= 0 or t["entry"] <= 0:
            t["shares"] = 0
            continue
        base = eq if compound else equity0
        risk_budget = base * risk_pct
        shares = int(risk_budget / stop_dist)
        cap_shares = int(base * max_pos_pct / t["entry"])
        shares = min(shares, cap_shares)
        if shares < 1:
            t["shares"] = 0
            continue
        net = t["gross_sh"] * shares
        real_risk = shares * stop_dist
        eq += net
        t["shares"] = shares
        t["net"] = net
        t["R"] = net / real_risk
        recs.append(t)
        open_trades.append((t["t_exit"],))
        open_trades.sort()
        eq_curve.append((t["t_exit"], eq))
    # equity curve -> DD
    eqs = np.array([e for _, e in eq_curve]) if eq_curve else np.array([equity0])
    peak = np.maximum.accumulate(np.concatenate([[equity0], eqs]))
    dd = (peak - np.concatenate([[equity0], eqs])).max()
    # DD в R-единицах (масштабо-независимая)
    Rs = np.array([t["R"] for t in recs]) if recs else np.array([0.0])
    cumR = np.cumsum(Rs)
    pk = np.maximum.accumulate(np.concatenate([[0.0], cumR]))
    ddR = float((pk - np.concatenate([[0.0], cumR])).max())
    return {"equity": eq, "ret_pct": (eq / equity0 - 1) * 100, "maxdd": dd, "dd_R": ddR,
            "n": len(recs), "skipped": sum(1 for t in all_trades if t.get("shares", 0) == 0),
            "trades": recs, "eq_curve": eq_curve}


def stats(recs, label):
    if not recs:
        return f"{label}: нет сделок"
    Rs = np.array([t["R"] for t in recs])
    pnls = np.array([t["net"] for t in recs])
    wr = (pnls > 0).mean() * 100
    return (f"{label}: сделок {len(recs)} | WR {wr:.1f}% | avgR {Rs.mean():+.3f} | "
            f"sumR {Rs.sum():+.1f} | medR {np.median(Rs):+.3f} | p90R {np.quantile(Rs, 0.9):+.2f} | "
            f"p10R {np.quantile(Rs, 0.1):+.2f}")


def main():
    t0 = time.time()
    all_trades = []
    per_ticker = {}
    for tic in TICKERS:
        tr = extract_trades(tic)
        # базовая пер-шерная статистика (для сверки с v5)
        g = np.array([t["gross_sh"] for t in tr])
        pf = g[g > 0].sum() / max(1e-9, -g[g < 0].sum()) if (g < 0).any() else float("inf")
        per_ticker[tic] = {"n": len(tr), "wr": (g > 0).mean() * 100,
                           "pf": pf, "sum_sh": g.sum()}
        all_trades.extend(tr)
        print(f"{tic:5s}: tr={len(tr):4d} WR={(g > 0).mean()*100:4.1f}% PF={pf:5.2f} "
              f"per-share {g.sum():+9.2f}₽ [{time.time()-t0:.0f}s]", flush=True)

    print(f"\nВсего сделок: {len(all_trades)}")

    # --- Р-портфели ---
    L = []
    L.append("# Stocks SAR/EMA (H1) + R-multiple MM — redo 03.10.2026\n")
    L.append(f"**Стратегия:** H1, std-полярность, SAR {SAR_AF0}/{SAR_STEP}, EMA{EMA_N}, "
             f"выход по касанию PSAR, начальный стоп {INIT_STOP_ATR}xATR, издержки 0.1% RT")
    L.append(f"**Бумаги:** {', '.join(TICKERS)} | **Данные:** ~09.2024-09.2026 (2 года, H1)")
    L.append(f"**R-слой:** 1R = equity x risk_pct; shares = 1R/(3xATR); кэп номинала max_pos_pct; "
             f" старт 1,000,000₽\n")

    L.append("## Пер-шерная сверка с v5 (05.09.2026)")
    L.append("| Тикер | Сделок | WR% | PF | Σ per-share ₽ | v5 PF (справка) |")
    L.append("|---|---|---|---|---|---|")
    v5 = {"PLZL": 2.66, "GAZP": 2.34, "TATN": 2.16, "GMKN": 2.14, "NVTK": 2.10,
          "ROSN": 2.08, "LKOH": 2.02, "MTSS": 1.82, "SBER": 1.81, "SNGS": 1.46}
    for tic in TICKERS:
        pt = per_ticker[tic]
        L.append(f"| {tic} | {pt['n']} | {pt['wr']:.1f} | {pt['pf']:.2f} | {pt['sum_sh']:+.2f} | {v5.get(tic, '-')} |")

    L.append("\n## R-портфели. НЕкомпаунд (риск всегда от стартового 1М) — реалистично для исполнения")
    L.append("| risk% | maxPos% | Сделок | Σnet₽ | Доход% | MaxDD₽ | DD% | DD_R |")
    L.append("|---|---|---|---|---|---|---|---|")
    best = None
    for rp, mp in [(0.005, 0.40), (0.01, 0.40), (0.02, 0.40)]:
        res = portfolio_rmm([dict(t) for t in all_trades], risk_pct=rp, max_pos_pct=mp, compound=False)
        wr = (np.array([t["net"] for t in res["trades"]]) > 0).mean() * 100 if res["trades"] else 0
        row = f"| {rp*100:.1f} | {mp*100:.0f} | {res['n']} | {res['equity']-1e6:+,.0f} | {res['ret_pct']:+.1f} | {res['maxdd']:,.0f} | {res['maxdd']/1e6*100:.0f} | {res['dd_R']:.1f}R |"
        L.append(row); print(row)
        if rp == 0.01: best = (rp, mp, res)

    L.append("\n## R-портфели. Компаунд (математика, ликвидность НЕ ограничена — верхняя оценка)")
    L.append("| risk% | maxPos% | Сделок | Фин. equity | Доход% | MaxDD₽ |")
    L.append("|---|---|---|---|---|---|")
    for rp, mp in [(0.005, 0.25), (0.005, 0.40), (0.01, 0.40), (0.02, 0.40)]:
        res = portfolio_rmm([dict(t) for t in all_trades], risk_pct=rp, max_pos_pct=mp, compound=True)
        row = f"| {rp*100:.1f} | {mp*100:.0f} | {res['n']} | {res['equity']:,.0f} | {res['ret_pct']:+,.1f} | {res['maxdd']:,.0f} |"
        L.append(row); print(row)

    rp, mp, res = best
    L.append(f"\n## Базовый R-портфель (некомпаунд): risk {rp*100:.1f}%, maxPos {mp*100:.0f}%")
    L.append(f"Финал: **{res['equity']:,.0f}₽** ({res['ret_pct']:+.1f}% за ~2 года) | "
             f"MaxDD {res['maxdd']:,.0f}₽ ({res['maxdd']/res['equity']*100:.1f}% от фин.) | "
             f"пропущено сигналов (мало equity/кэп): {res['skipped']}")
    L.append(stats(res["trades"], "R-статистика"))

    # по полугодиям
    df_tr = pd.DataFrame([{**t, "half": pd.Timestamp(t["t_exit"]).year * 10 + (pd.Timestamp(t["t_exit"]).month > 6) * 5 + pd.Timestamp(t["t_exit"]).month // 6.01} for t in res["trades"]])
    L.append("\n### По периодам (сделки базового портфеля)")
    L.append("| Период | Сделок | Σnet₽ | avgR |")
    L.append("|---|---|---|---|")
    df_tr["period"] = pd.to_datetime(df_tr["t_exit"]).dt.to_period("Q").astype(str)
    for per, grp in df_tr.groupby("period"):
        L.append(f"| {per} | {len(grp)} | {grp['net'].sum():+,.0f} | {grp['R'].mean():+.3f} |")

    # по тикерам
    L.append("\n### Вклад тикеров (базовый портфель)")
    L.append("| Тикер | Сделок | Σnet₽ | avgR | Σshares (млн) |")
    L.append("|---|---|---|---|---|")
    for tic, grp in df_tr.groupby("tic"):
        L.append(f"| {tic} | {len(grp)} | {grp['net'].sum():+,.0f} | {grp['R'].mean():+.3f} | {grp['shares'].sum()/1e6:.2f} |")

    # распределение R
    Rs = np.array([t["R"] for t in res["trades"]])
    L.append("\n### Распределение R-кратных (базовый портфель)")
    L.append(f"- avg {Rs.mean():+.3f} | med {np.median(Rs):+.3f} | std {Rs.std():.3f}")
    for q in (0.05, 0.10, 0.25, 0.75, 0.90, 0.95):
        L.append(f"- p{int(q*100):02d}: {np.quantile(Rs, q):+.2f}R")
    L.append(f"- доля сделок >= +2R: {(Rs >= 2).mean()*100:.1f}% | <= -1R: {(Rs <= -1).mean()*100:.1f}%")
    L.append(f"- expectancy на сделку: {Rs.mean():+.3f}R при risk {rp*100:.1f}% → ~{Rs.mean()*rp*100:.2f}% equity/сделку")

    L.append("\n## ⚠️ Оговорки")
    L.append("1. Equity учитывается по ЗАКРЫТЫМ сделкам (переоценка открытых позиций не ведётся) —")
    L.append("   MaxDD занижен относительно marked-to-market.")
    L.append("2. Лотность акций (SBER/GAZP лот=10 и т.д.) игнорируется — расчёт в акциях. "
             "Округление до лотов добавит шум на дорогих бумагах (LKOH/PLZL лот=1 — там точно).")
    L.append("3. Одновременность позиций ограничена только кэпом номинала на входе; переоценки нет.")
    L.append("4. Данные те же, что в v5 (Finam H1, ~2 года): 2022-й гэп-риск не прожит.")
    L.append("5. Овернайт-риск: позиции переносятся (как в v5) — фондирование/гэпы не смоделированы.")

    path = os.path.join(RESULTS, "STOCKS_SAREMA_RMM_REPORT.md")
    with open(path, "w") as f:
        f.write("\n".join(L))
    print(f"\nReport: {path}")
    # CSV сделок
    cpath = os.path.join(RESULTS, "stocks_sarema_rmm_trades.csv")
    keys = ["tic", "dir", "t_entry", "t_exit", "entry", "exit", "atr", "shares", "net", "R", "gross_sh"]
    with open(cpath, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for t in res["trades"]:
            row = {k: t.get(k) for k in keys}
            row["t_entry"] = pd.Timestamp(t["t_entry"])
            row["t_exit"] = pd.Timestamp(t["t_exit"])
            w.writerow(row)
    print(f"Trades CSV: {cpath}")


if __name__ == "__main__":
    main()
