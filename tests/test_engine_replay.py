"""F-061 Stage 1 tests: engine replay vs reference CSV + indicator units.

Эталон: sar_trades_original.csv (независимый прогон на тех же данных, 5898 сделок).
Полный replay по 10 тикерам помечен @pytest.mark.slow; быстрый GAZP — без маркера.
Запуск: python -m pytest tests/test_engine_replay.py -v
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "robot"))

from sarema.sarema_engine import (psar, build, extract_trades, load_m1,   # noqa: E402
                                  tf_bars, m5_extras, TICKERS)

DATA_DIR = r"C:\Users\User\.openclaw-autoclaw\workspace\.openclaw\tmp\sar_backtest\data"
REF_CSV = r"C:\Users\User\.openclaw-autoclaw\workspace\sar_trades_original.csv"


def _ref():
    df = pd.read_csv(REF_CSV)
    df["t_entry"] = pd.to_datetime(df["t_entry"])
    df["t_exit"] = pd.to_datetime(df["t_exit"])
    return df


def _mine(tickers):
    cache = {}
    rows = []
    for tic in tickers:
        tr, _ = extract_trades(tic, DATA_DIR, cache)
        rows.extend(tr)
    df = pd.DataFrame(rows)
    df["t_entry"] = pd.to_datetime(df["t_entry"])
    df["t_exit"] = pd.to_datetime(df["t_exit"])
    return df


def _assert_replay(mine: pd.DataFrame, ref: pd.DataFrame):
    key = ["tic", "dir", "t_entry", "t_exit"]
    m = ref.merge(mine, on=key, how="outer", suffixes=("_ref", "_my"), indicator=True)
    matched = (m["_merge"] == "both").sum()
    rate = matched / len(ref)
    assert rate >= 0.99, f"replay rate {rate:.4f} < 0.99 ({matched}/{len(ref)})"
    both = m[m["_merge"] == "both"]
    d_entry = (both["entry_ref"].astype(float) - both["entry_my"].astype(float)).abs().max()
    d_exit = (both["exit_ref"].astype(float) - both["exit_my"].astype(float)).abs().max()
    assert d_entry <= 0.01 + 1e-9, f"entry deviation {d_entry}"
    assert d_exit <= 0.01 + 1e-9, f"exit deviation {d_exit}"


@pytest.mark.slow
def test_engine_replay_all_10():
    ref = _ref()
    mine = _mine(TICKERS)
    _assert_replay(mine, ref)


def test_engine_replay_gazp():
    ref = _ref()
    mine = _mine(["GAZP"])
    _assert_replay(mine[mine["tic"] == "GAZP"], ref[ref["tic"] == "GAZP"])


def _synth(n=400, seed=7):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 1.0, n).cumsum()
    close = 300 + steps
    high = close + rng.uniform(0.2, 1.2, n)
    low = close - rng.uniform(0.2, 1.2, n)
    open_ = np.roll(close, 1) + rng.normal(0, 0.2, n)
    open_[0] = close[0]
    t = pd.date_range("2025-01-06 07:00", periods=n, freq="60min")
    df = pd.DataFrame({"date": t.date, "begin": t, "t": t, "open": open_, "high": high,
                       "low": low, "close": close, "volume": 10000.0})
    return df


def test_psar_reference():
    df = _synth()
    sar, trend = psar(df, 0.02, 0.2)
    assert len(sar) == len(df)
    assert sar[0] == df["low"].iloc[0]
    assert trend[0] == 1
    # в трейлинге up-тренда стоп не отдаляется вниз (после первого бара тренда,
    # где происходит flip-перенос EP)
    up_idx = np.where(trend == 1)[0]
    runs = np.split(up_idx, np.where(np.diff(up_idx) != 1)[0] + 1)
    for r in runs:
        if len(r) > 2:
            assert (np.diff(sar[r[1:]]) >= -1e-12).all(), f"sar fell in up-run at {r}"
    dn_idx = np.where(trend == -1)[0]
    runs = np.split(dn_idx, np.where(np.diff(dn_idx) != 1)[0] + 1)
    for r in runs:
        if len(r) > 2:
            assert (np.diff(sar[r[1:]]) <= 1e-12).all(), f"sar rose in down-run at {r}"
    # flip семантика: тренд меняется только когда цена пробила SAR
    for i in range(1, len(df)):
        if trend[i] != trend[i - 1]:
            if trend[i] == -1:
                assert df["low"].iloc[i] < sar[i - 1] + 1e-12 or df["low"].iloc[i] < sar[i]
            else:
                assert df["high"].iloc[i] > sar[i - 1] - 1e-12 or df["high"].iloc[i] > sar[i]


def test_std_polarity():
    df = _synth(600, seed=3)
    g, up, dn = build(df, "60min", 0.02, 0.2, 12)
    # сигнал LONG = SAR пересёк EMA сверху вниз (нырок)
    i = np.where(dn)[0]
    assert len(i) > 0
    for k in i:
        assert g["sar"].iloc[k - 1] > g["ema"].iloc[k - 1] - 1e-12
        assert g["sar"].iloc[k] <= g["ema"].iloc[k] + 1e-12
    j = np.where(up)[0]
    assert len(j) > 0
    for k in j:
        assert g["sar"].iloc[k - 1] < g["ema"].iloc[k - 1] + 1e-12
        assert g["sar"].iloc[k] >= g["ema"].iloc[k] - 1e-12


def test_atr_dayreset():
    df = _synth(48, seed=11)
    g = m5_extras(tf_bars(df, "60min"))
    same_day = g["date"].eq(g["date"].shift(1))
    new_day = ~same_day
    # на первом баре дня ATR-источник = High-Low этого дня
    for k in np.where(new_day)[0]:
        tr_expected = g["high"].iloc[k] - g["low"].iloc[k]
        if k == 0:
            assert g["atr"].iloc[k] == pytest.approx(tr_expected)
        else:
            # после дневного сброса TR первого бара дня равен High-Low
            tr_prev = g["high"].iloc[k - 1] - g["low"].iloc[k - 1]
            atr_prev = g["atr"].iloc[k - 1]
            atr_new = atr_prev + (tr_expected - atr_prev) / 14
            assert g["atr"].iloc[k] == pytest.approx(atr_new, rel=1e-9)


def test_session_filter_and_window():
    df = load_m1("GAZP", DATA_DIR)
    tod = df["t"].dt.hour * 3600 + df["t"].dt.minute * 60
    assert (tod >= 7 * 3600).all() and (tod <= 15 * 3600 + 40 * 60).all()
    assert df["t"].min() >= pd.Timestamp("2024-09-02")
    assert df["t"].max() < pd.Timestamp("2026-09-05")
