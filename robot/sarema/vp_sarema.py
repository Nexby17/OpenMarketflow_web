"""F-065: Volume Profile (VAH/VAL/POC) для SAREMA — фильтр входа как у OF-роботов.

Отличия от OF-версии (strategy_of.VolumeProfileCalculator):
  - источник данных — H1-бары (у OF тиковые сделки): цена бина = типичная цена
    (high+low+close)/3, объём = объём бара;
  - окно скользящее по последним N закрытым H1-барам (vah_val_lookback),
    не «с полуночи» — у SAREMA TF H1 и дневная сессия 10:00-18:40 МСК;
  - авто-бин: vah_val_bin=0 → размер бина подбирается из размаха окна
    (размах/30, округление до 1/2/2.5/5×10^k) — параметры акций различаются
    на порядки (SBER ~300₽, LKOH ~7000₽).

Гейт входа — чистая функция gate_check(): range = торгуем только внутри VA,
fade/breakout = вход только от краёв VA (внутри VA — блок). Смена направления
сигнала НЕ блочится (сигнал SAR/EMA остаётся как был) — см. strategy_sarema.
"""
from __future__ import annotations


def _nice_bin(raw: float) -> float:
    """Округление бина до красивого шага (1/2/2.5/5 × 10^k)."""
    if raw <= 0:
        return 0.0
    exp = int(f"{raw:e}".split("e")[1])
    mag = 10.0 ** exp
    x = raw / mag
    for nice in (1.0, 2.0, 2.5, 5.0):
        if x <= nice + 1e-9:
            return nice * mag
    return 10.0 * mag


class VolumeProfileSarema:
    """Скользящий Volume Profile по последним lookback H1-барам."""

    def __init__(self, bin_size: float = 0.0, va_pct: float = 70.0, lookback: int = 200):
        self.bin_size = max(0.0, float(bin_size))
        self.va_pct = min(100.0, max(50.0, float(va_pct)))
        self.lookback = max(30, int(lookback))
        self._bars: list[tuple[float, float, float]] = []  # (typ_price, volume, ts_str)
        self._vah = 0.0
        self._val = 0.0
        self._poc = 0.0
        self._bins: dict[float, float] = {}
        self._effective_bin = 0.0

    # ---------- properties ----------
    @property
    def vah(self) -> float:
        return self._vah

    @property
    def val(self) -> float:
        return self._val

    @property
    def poc(self) -> float:
        return self._poc

    @property
    def ready(self) -> bool:
        return self._vah > 0 and self._val > 0 and self._vah > self._val

    def state(self) -> dict:
        """Слепок для /status (UI-панель)."""
        return {
            "ready": self.ready,
            "vah": round(self._vah, 4),
            "val": round(self._val, 4),
            "poc": round(self._poc, 4),
            "bars": len(self._bars),
            "lookback": self.lookback,
            "bin": round(self._effective_bin, 4),
            "va_pct": self.va_pct,
        }

    # ---------- наполнение ----------
    def add_h1_bar(self, high: float, low: float, close: float, volume: float,
                   ts: str = "") -> None:
        """Добавить закрытый H1-бар (типичная цена (H+L+C)/3 — как VP backfill OF)."""
        if high <= 0 or low <= 0 or close <= 0 or volume <= 0:
            return
        typ = (high + low + close) / 3.0
        self._bars.append((typ, float(volume), str(ts)))
        if len(self._bars) > self.lookback:
            self._bars = self._bars[-self.lookback:]

    def feed_bars(self, g) -> None:
        """Перезалить окно из DataFrame закрытых H1-баров (колонки high/low/close/volume/begin).

        Стратегия пересчитывает индикаторы на всей истории каждый бар — VP тоже
        строится с нуля: дешёво (≤2000 строк) и без инкрементального дрейфа.
        """
        self._bars = []
        try:
            for r in g.itertuples():
                ts = str(getattr(r, "begin", ""))
                self.add_h1_bar(float(r.high), float(r.low), float(r.close),
                                float(r.volume), ts)
        except Exception:
            self._bars = []

    # ---------- расчёт ----------
    def calculate(self) -> bool:
        """Пересчитать VAH/VAL/POC. False если данных мало (нет готовности)."""
        self._vah = self._val = self._poc = 0.0
        self._bins = {}
        if len(self._bars) < 30:
            return False
        bin_size = self.bin_size
        if bin_size <= 0:
            prices = sorted(p for p, _, _ in self._bars)
            raw = (prices[-1] - prices[0]) / 30.0
            if raw <= 0:  # плоское распределение — бин от масштаба цены (~0.01%)            
                raw = max(1e-6, prices[len(prices) // 2] * 1e-4)
            bin_size = _nice_bin(raw)
            if bin_size <= 0:
                return False
        self._effective_bin = bin_size
        bins: dict[float, float] = {}
        for typ, vol, _ts in self._bars:
            key = typ // bin_size * bin_size
            bins[key] = bins.get(key, 0.0) + vol
        total = sum(bins.values())
        if total <= 0:
            return False
        # POC — бин с максимальным объёмом; центр бина = key + bin/2 (как у OF)
        poc_key = max(bins, key=lambda k: bins[k])
        # Value Area: расширение от POC, пока не соберём va_pct объёма
        sorted_keys = sorted(bins)
        poc_idx = sorted_keys.index(poc_key)
        captured = bins[poc_key]
        lo_idx = hi_idx = poc_idx
        target = total * self.va_pct / 100.0
        while captured < target:
            down = bins.get(sorted_keys[lo_idx - 1], -1.0) if lo_idx > 0 else -1.0
            up = bins.get(sorted_keys[hi_idx + 1], -1.0) if hi_idx < len(sorted_keys) - 1 else -1.0
            if down < 0 and up < 0:
                break
            if down >= up:
                lo_idx -= 1
                captured += bins[sorted_keys[lo_idx]]
            else:
                hi_idx += 1
                captured += bins[sorted_keys[hi_idx]]
        self._bins = bins
        self._val = float(sorted_keys[lo_idx])
        self._vah = float(sorted_keys[hi_idx] + bin_size)
        self._poc = float(poc_key + bin_size / 2.0)
        return self.ready


def gate_check(mode: str, price: float, vah: float, val: float) -> tuple[bool, str, str]:
    """Чистая функция VA-гейта (порт семантики OF-роботов).

    mode="range":    вход только ВНУТРИ Value Area (вне VA — блок).
    mode="fade":     вход только от краёв VA (VAH/VAL); внутри VA — блок.
    mode="breakout": вход только от краёв VA, зона = подтверждение пробоя; внутри VA — блок.
    Возвращает (allowed, reason, zone); zone = "" | "VAH" | "VAL".
    """
    if vah <= 0 or val <= 0 or vah <= val:
        return False, "vp_not_ready", ""
    if mode == "range":
        if val <= price <= vah:
            return True, "range_inside", ""
        return False, "range_outside", ""
    if price >= vah:
        return True, "zone_pass", "VAH"
    if price <= val:
        return True, "zone_pass", "VAL"
    return False, "inside_va", ""
