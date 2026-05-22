"""Risk manager — time, PnL, and position limits."""
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))


class RiskManager:
    def __init__(
        self,
        max_loss: float = -7000,
        max_lots: int = 101,
        no_trade_start: int = 2350,  # MSK HHMM
        no_trade_end: int = 700,
        clearing_start: int = 1359,
        clearing_end: int = 1406,
    ):
        self.max_loss = max_loss
        self.max_lots = max_lots
        self.no_trade_start = no_trade_start
        self.no_trade_end = no_trade_end
        self.clearing_start = clearing_start
        self.clearing_end = clearing_end

    def _msk_hhmm(self) -> int:
        now = datetime.now(MSK)
        return now.hour * 100 + now.minute

    def can_trade(self) -> tuple[bool, str]:
        """Check if trading is allowed right now."""
        hhmm = self._msk_hhmm()

        # Night
        if hhmm >= self.no_trade_start or hhmm < self.no_trade_end:
            return False, f"Night mode ({hhmm:04d} MSK)"

        # Clearing
        if self.clearing_start <= hhmm <= self.clearing_end:
            return False, f"Clearing ({hhmm:04d} MSK)"

        return True, "OK"

    def is_clearing(self) -> bool:
        hhmm = self._msk_hhmm()
        return self.clearing_start <= hhmm <= self.clearing_end

    def check_pnl(self, unrealized_pnl: float) -> tuple[bool, str]:
        """Check if PnL is within limits."""
        if unrealized_pnl <= self.max_loss:
            return False, f"PnL limit: {unrealized_pnl:.0f} <= {self.max_loss:.0f}"
        return True, "OK"

    def check_lots(self, lots: int) -> tuple[bool, str]:
        """Check if position size is within limits."""
        if lots > self.max_lots:
            return False, f"Lots limit: {lots} > {self.max_lots}"
        return True, "OK"
