"""Volume Profile calculator — POC, VAH, VAL from price/volume buffer."""
from dataclasses import dataclass


@dataclass
class VPResult:
    poc: float  # Point of Control (price with max volume)
    vah: float  # Value Area High
    val: float  # Value Area Low
    bins: dict  # {bin_start: volume} for debugging


class VolumeProfile:
    """Calculates Volume Profile from a rolling window of (price, volume) data."""

    def __init__(self, lookback: int = 33, bin_size: int = 50, va_percent: float = 0.70):
        self.lookback = lookback
        self.bin_size = bin_size
        self.va_percent = va_percent

        self._buffer: list[tuple[float, float]] = []  # (price, volume)
        self._last_result: VPResult | None = None

    @property
    def poc(self) -> float:
        return self._last_result.poc if self._last_result else 0

    @property
    def vah(self) -> float:
        return self._last_result.vah if self._last_result else 0

    @property
    def val(self) -> float:
        return self._last_result.val if self._last_result else 0

    @property
    def result(self) -> VPResult | None:
        return self._last_result

    def add_bar(self, close: float, volume: float):
        """Add a bar's close price and volume to the buffer."""
        self._buffer.append((close, volume))
        if len(self._buffer) > self.lookback:
            self._buffer.pop(0)

    def calculate(self) -> VPResult | None:
        """Recalculate VP from current buffer. Returns None if not enough data."""
        if len(self._buffer) < 20:
            return None

        prices = [p for p, _ in self._buffer]
        volumes = [v for _, v in self._buffer]

        min_p = min(prices)
        max_p = max(prices)

        if max_p - min_p < self.bin_size:
            return None

        # Build bins
        min_bin = int(min_p // self.bin_size) * self.bin_size
        max_bin = int(max_p // self.bin_size) * self.bin_size + self.bin_size

        bins: dict[int, float] = {}
        for b in range(min_bin, max_bin + self.bin_size, self.bin_size):
            bins[b] = 0.0

        for price, vol in zip(prices, volumes):
            b = int(price // self.bin_size) * self.bin_size
            if b in bins:
                bins[b] += vol

        total_vol = sum(bins.values())
        if total_vol == 0:
            return None

        # POC = bin with max volume
        poc_bin = max(bins, key=lambda k: bins[k])
        poc = poc_bin + self.bin_size / 2.0

        # Value Area: expand from POC until we capture va_percent of total volume
        sorted_bins = sorted(bins.items(), key=lambda x: -x[1])  # descending by volume
        target_vol = total_vol * self.va_percent
        cum_vol = 0.0
        va_bins: list[int] = []

        for bin_start, vol in sorted_bins:
            va_bins.append(bin_start)
            cum_vol += vol
            if cum_vol >= target_vol:
                break

        vah = max(va_bins) + self.bin_size
        val = min(va_bins)

        self._last_result = VPResult(poc=poc, vah=vah, val=val, bins=dict(bins))
        return self._last_result
