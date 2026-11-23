"""Candles + last price source for MTLR robot: MOEX ISS (public, no key).

- Candles history: /iss/engines/stock/markets/shares/boards/TQBR/securities/MTLR/candles
  Pages of 500; walk backwards with `start` until we have `history_bars` or hit `from` date.
- Live quote: /iss/engines/stock/markets/shares/boards/TQBR/securities/MTLR marketdata
  (LAST, BID, OFFER — near real-time).
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

log = logging.getLogger("mtlr.candles")

ISS_BASE = ("https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR"
            "/securities/{sec}")
MSK = timezone(timedelta(hours=3))

# Candle: {t: epoch sec (bar open, MSK->UTC epoch), o,h,l,c,v, dt: "YYYY-MM-DD HH:MM"}
Candle = Dict


class CandleSource:
    def __init__(self, secid: str = "MTLR", history_bars: int = 400):
        self.secid = secid
        self.history_bars = history_bars
        self._lock = threading.Lock()
        self.candles: List[Candle] = []
        self.last_quote: Dict = {}      # {last, bid, offer, ts}
        self.last_error: Optional[str] = None

    # ---------- history ----------
    _BARS_PER_DAY = {1: 700, 5: 140, 10: 70, 15: 47, 30: 24, 60: 9, 24: 1}

    def fetch_history(self, iss_interval: int, bars: Optional[int] = None) -> List[Candle]:
        want = bars or self.history_bars
        url = (ISS_BASE.format(sec=self.secid) + "/candles.json")
        bpd = self._BARS_PER_DAY.get(iss_interval, 100)
        days_back = int(want / bpd) + 4
        if days_back < 3:
            days_back = 3
        frm = (datetime.now(MSK) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        out: List[Candle] = []
        start = 0
        try:
            # идём до конца периода (до сегодня), не до want — иначе хвост устареет;
            # потом обрезаем последние want
            while start < 60000:
                r = requests.get(url, params={
                    "iss.meta": "off", "interval": iss_interval,
                    "from": frm, "start": start,
                }, timeout=10)
                r.raise_for_status()
                data = r.json().get("candles", {}).get("data", [])
                if not data:
                    break
                # columns: open, close, high, low, value, volume, begin, end
                for row in data:
                    begin = row[6]
                    dt = datetime.strptime(begin, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)
                    out.append({
                        "t": int(dt.timestamp()),
                        "dt": begin,
                        "o": float(row[0]), "c": float(row[1]),
                        "h": float(row[2]), "l": float(row[3]),
                        "v": int(row[5]),
                    })
                start += len(data)
                time.sleep(0.15)  # be polite
        except Exception as e:
            self.last_error = f"candles: {e}"
            log.error(self.last_error)
        with self._lock:
            # dedup by t, keep order, обрезать до последних want
            seen = {}
            for c in out:
                seen[c["t"]] = c
            keys = sorted(seen.keys())[-want:]
            self.candles = [seen[k] for k in keys]
        return self.candles

    def refresh_tail(self, iss_interval: int) -> List[Candle]:
        """Инкрементально дотянуть хвост: от последней известной свечи (или 2 дня, если пусто).
        ISS отдаёт страницы с начала периода — идём до конца, обычно 1-2 страницы."""
        url = (ISS_BASE.format(sec=self.secid) + "/candles.json")
        with self._lock:
            last_dt = self.candles[-1]["dt"] if self.candles else None
        frm = last_dt or (datetime.now(MSK) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        # срезаем секунды (совпадёт с началом минутной свечи)
        frm = frm[:16] + ":00"
        fetched: List[Candle] = []
        start = 0
        try:
            while start < 3000:
                r = requests.get(url, params={
                    "iss.meta": "off", "interval": iss_interval,
                    "from": frm, "start": start,
                }, timeout=10)
                r.raise_for_status()
                data = r.json().get("candles", {}).get("data", [])
                if not data:
                    break
                for row in data:
                    begin = row[6]
                    dt = datetime.strptime(begin, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)
                    fetched.append({
                        "t": int(dt.timestamp()), "dt": begin,
                        "o": float(row[0]), "c": float(row[1]),
                        "h": float(row[2]), "l": float(row[3]),
                        "v": int(row[5]),
                    })
                start += len(data)
                time.sleep(0.1)
        except Exception as e:
            self.last_error = f"refresh: {e}"
            log.error(self.last_error)
            return []
        with self._lock:
            if not fetched:
                return []
            by_t = {c["t"]: c for c in self.candles}
            for c in fetched:
                by_t[c["t"]] = c
            # не даём копиться: для M1 сырых держим до 20 дней, иначе history_bars*3
            all_t = sorted(by_t.keys())
            keep_n = 28800 if iss_interval == 1 else max(self.history_bars * 3, 500)
            keep = all_t[-keep_n:]
            self.candles = [by_t[t] for t in keep]
        return self.candles

    # ---------- live quote ----------
    def fetch_quote(self) -> Dict:
        url = ISS_BASE.format(sec=self.secid) + ".json"
        try:
            r = requests.get(url, params={
                "iss.meta": "off",
                "marketdata.columns": "SECID,LAST,LASTCHANGEPRICETSENT,BID,OFFER,UPDATETIME",
                "securities.columns": "SECID",
            }, timeout=6)
            r.raise_for_status()
            md = r.json().get("marketdata", {})
            cols = md.get("columns", [])
            data = md.get("data", [])
            if data:
                row = dict(zip(cols, data[0]))
                q = {
                    "last": row.get("LAST") or 0.0,
                    "bid": row.get("BID") or 0.0,
                    "offer": row.get("OFFER") or 0.0,
                    "updated": row.get("UPDATETIME") or "",
                    "ts": time.time(),
                }
                self.last_quote = q
                return q
        except Exception as e:
            self.last_error = f"quote: {e}"
            log.error(self.last_error)
        return self.last_quote

    def snapshot(self, tail: int = 500) -> List[Candle]:
        with self._lock:
            return self.candles[-tail:]


# ---------- Агрегация M1 -> старшие ТФ (ISS не отдаёт 5/15/30 для TQBR) ----------
def aggregate_m1(m1: List[Candle], tf_minutes: int) -> List[Candle]:
    """Свёртка минутных свечей в N-минутные, выравнивание по времени начала.
    Только завершённые N-минутные бары + последний незавершённый (как есть)."""
    if tf_minutes <= 1:
        return m1
    out: List[Candle] = []
    cur: Optional[Candle] = None
    cur_bucket = -1
    for c in m1:
        bucket = c["t"] // (tf_minutes * 60)
        if cur is None or bucket != cur_bucket:
            if cur is not None:
                out.append(cur)
            cur = {"t": c["t"] - c["t"] % (tf_minutes * 60), "dt": c["dt"],
                   "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c["v"]}
            cur_bucket = bucket
        else:
            cur["h"] = max(cur["h"], c["h"])
            cur["l"] = min(cur["l"], c["l"])
            cur["c"] = c["c"]
            cur["v"] += c["v"]
    if cur is not None:
        out.append(cur)
    return out


def iss_interval_for(tf: str) -> int:
    """ISS-интервал для ТФ: M5/M15/M30 строятся из M1 (агрегация, ISS не отдаёт их для TQBR)."""
    if tf in ("M5", "M15", "M30"):
        return 1
    from config_plzl import TF_TO_ISS
    return TF_TO_ISS.get(tf, 60)


def needs_aggregation(tf: str) -> bool:
    return tf in ("M5", "M15", "M30")
