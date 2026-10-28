"""F-061: H1 bar feed для SAR-робота — Finam REST через DataProvider/напрямую.

Прогрев ≥300 баров (чанки), QC дыр/дублей, алерт просрочки >2 баров.
Незакрытый бар не используется (сигнал только по завершённому H1).
"""
import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

log = logging.getLogger("sarema.feed")

SESSION_UTC = (7 * 3600, 15 * 3600 + 40 * 60)   # 10:00-18:40 MSK


def _tod_seconds(ts: pd.Timestamp) -> int:
    return ts.hour * 3600 + ts.minute * 60


class H1Feed:
    """История H1 из M1/чанков + детект просрочки. Локальная M1-история для прогрева."""

    def __init__(self, data_dir: str | None = None):
        self.data_dir = data_dir
        self.bars: pd.DataFrame | None = None    # колонки: date, begin, open, high, low, close, volume
        self.last_bar_begin: pd.Timestamp | None = None

    # ---------- прогрев из локальной истории ----------
    def warmup_from_csv(self, tic: str, min_bars: int = 300) -> bool:
        if not self.data_dir:
            return False
        path = f"{self.data_dir}/{tic}_2Y_M1.csv"
        try:
            df = pd.read_csv(path)
        except Exception as e:
            log.error(f"warmup read {path}: {e}")
            return False
        df["t"] = pd.to_datetime(df["begin"])
        tod = df["t"].dt.hour * 3600 + df["t"].dt.minute * 60
        df = df[(tod >= SESSION_UTC[0]) & (tod <= SESSION_UTC[1])]
        g = self._aggregate_h1(df)
        if len(g) < min_bars:
            log.warning(f"warmup short: {len(g)} < {min_bars}")
        self.bars = g
        self.last_bar_begin = g["begin"].iloc[-1] if len(g) else None
        log.info(f"warmup ok: {len(g)} H1 bars, last={self.last_bar_begin}")
        return len(g) >= min_bars

    @staticmethod
    def _aggregate_h1(m1: pd.DataFrame) -> pd.DataFrame:
        m1 = m1.copy()
        m1["date"] = m1["t"].dt.date
        bucket = m1["t"].dt.floor("60min")
        g = (m1.assign(_b=bucket)
             .groupby(["date", "_b"], sort=True)
             .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                  close=("close", "last"), volume=("volume", "sum"))
             .reset_index().rename(columns={"_b": "begin"}))
        g = g[g["volume"] > 0].reset_index(drop=True)
        return g

    # ---------- append закрытого бара (из REST/гРПС подписки) ----------
    def append_closed_bar(self, begin: datetime, o: float, h: float, l: float,
                          c: float, volume: float) -> bool:
        """Добавляет ЗАКРЫТЫЙ бар. Бар с begin в будущем (незакрытый) отклоняется."""
        begin = pd.Timestamp(begin)
        now = pd.Timestamp.now(tz=None)
        if begin + pd.Timedelta(hours=1) > now + pd.Timedelta(minutes=1):
            log.warning(f"reject unclosed bar {begin}")
            return False
        if self.bars is None:
            self.bars = pd.DataFrame(columns=["date", "begin", "open", "high", "low", "close", "volume"])
        if len(self.bars) and begin <= self.bars["begin"].iloc[-1]:
            log.warning(f"duplicate/late bar {begin} ignored")
            return False
        row = pd.DataFrame([{"date": begin.date(), "begin": pd.Timestamp(begin), "open": o,
                             "high": h, "low": l, "close": c, "volume": volume}])
        self.bars = pd.concat([self.bars, row], ignore_index=True)
        # F-114 fix: держим типы колонок жёстко (object → datetime ломает tf_bars .dt.floor)
        self.bars["begin"] = pd.to_datetime(self.bars["begin"])
        self.last_bar_begin = begin
        return True

    # ---------- QC ----------
    def check_stale(self, now_utc: datetime | None = None, max_gap_bars: int = 2) -> bool:
        """True если фид просрочен (последний бар старше допустимого). Алерт-условие."""
        if self.last_bar_begin is None:
            return True
        now = now_utc or datetime.utcnow()
        expected_gap = timedelta(hours=1)
        # выходные: сб/вс не считаем просрочкой
        age = now - self.last_bar_begin.to_pydatetime().replace(tzinfo=None)
        if now.weekday() >= 5:
            return False
        trading_hours = SESSION_UTC[0] <= now.hour * 3600 + now.minute * 60 <= SESSION_UTC[1]
        if trading_hours and age > expected_gap * (max_gap_bars + 1):
            return True
        return False

    def check_gaps(self) -> int:
        """Число пропусков (дыр) внутри торговых часов за историю."""
        if self.bars is None or len(self.bars) < 2:
            return 0
        b = self.bars["begin"].values
        gaps = 0
        for i in range(1, len(b)):
            delta = (b[i] - b[i - 1]) / np.timedelta64(1, "h")
            prev_tod = _tod_seconds(pd.Timestamp(b[i - 1]))
            if prev_tod + 3600 <= SESSION_UTC[1] and abs(delta - 1) > 1e-6:
                gaps += 1
        return gaps
