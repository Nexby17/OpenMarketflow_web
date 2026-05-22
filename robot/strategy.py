"""VP Scalp Grid strategy — signal generation and position management."""
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

MSK = timezone(timedelta(hours=3))


@dataclass
class StrategyParams:
    max_levels: int = 100
    step_base: int = 31
    spread_base: int = 31
    max_hold_minutes: int = 999
    min_profit_per_lot: int = 29
    commission: float = 0.90


@dataclass
class Signal:
    action: str          # ENTRY, GRID, TP, CLOSE_ALL, PLACE_POC_TP
    direction: int = 0   # 1=LONG, -1=SHORT
    price: float = 0.0
    quantity: int = 1
    level: int = 0
    tag: str = ""


class Strategy:
    """VP Scalp Grid strategy logic.

    Receives market data and fill events, produces trading signals.
    No direct API calls — only pure logic.
    """

    def __init__(self, params: StrategyParams = None):
        self.params = params or StrategyParams()

        # Position state
        self.direction: int = 0       # 1=LONG, -1=SHORT, 0=FLAT
        self.entry_price: float = 0
        self.filled_levels: int = 0
        self.entry_time: Optional[datetime] = None

        # VP indicators (set externally)
        self.poc: float = 0
        self.vah: float = 0
        self.val: float = 0

        # Current price (set externally)
        self.current_price: float = 0

    @property
    def total_lots(self) -> int:
        return 1 + self.filled_levels if self.direction != 0 else 0

    @property
    def hold_minutes(self) -> int:
        if not self.entry_time:
            return 0
        return int((datetime.now(MSK) - self.entry_time).total_seconds() / 60)

    @property
    def has_position(self) -> bool:
        return self.direction != 0

    # === SIGNALS ===

    def check_entry(self, price: float) -> Optional[Signal]:
        """Check if we should enter. Returns Signal or None."""
        if self.direction != 0:
            return None  # Already in position
        if self.val <= 0 or self.vah <= 0:
            return None  # No VP data

        if price < self.val:
            return Signal(action="ENTRY", direction=1, price=price, tag="LONG below VAL")
        elif price > self.vah:
            return Signal(action="ENTRY", direction=-1, price=price, tag="SHORT above VAH")
        return None

    def check_exit(self) -> Optional[Signal]:
        """Check if we should exit. Returns Signal or None."""
        if self.direction == 0:
            return None
        if self.current_price <= 0:
            return None

        # Timeout
        if self.hold_minutes >= self.params.max_hold_minutes:
            return Signal(action="CLOSE_ALL", tag=f"Timeout ({self.hold_minutes} min)")

        price = self.current_price

        # 1 lot: POC hit unconditional
        if self.total_lots == 1 and self.poc > 0:
            if self.direction == 1 and price >= self.poc:
                return Signal(action="CLOSE_ALL", tag=f"POC hit LONG: {price:.0f} >= {self.poc:.0f}")
            if self.direction == -1 and price <= self.poc:
                return Signal(action="CLOSE_ALL", tag=f"POC hit SHORT: {price:.0f} <= {self.poc:.0f}")

        # 2+ lots: PnL/lot >= min_profit
        if self.total_lots >= 2:
            unrealized = self.calc_unrealized_pnl(price)
            per_lot = unrealized / self.total_lots if self.total_lots > 0 else 0
            if per_lot >= self.params.min_profit_per_lot:
                return Signal(action="CLOSE_ALL", tag=f"PnL/lot={per_lot:.0f} >= {self.params.min_profit_per_lot}")

        return None

    def on_entry_fill(self, direction: int, price: float) -> list[Signal]:
        """Called when entry order is filled. Returns signals for grid + POC-TP."""
        self.direction = direction
        self.entry_price = price
        self.filled_levels = 0
        self.entry_time = datetime.now(MSK)

        signals = []
        # Place first grid
        grid_sig = self._next_grid_signal()
        if grid_sig:
            signals.append(grid_sig)
        # Place POC-TP for entry lot
        if self.poc > 0:
            side = SELL if direction == 1 else BUY
            signals.append(Signal(
                action="PLACE_POC_TP",
                direction=side,
                price=self.poc,
                quantity=self.total_lots,
                tag="POC-TP",
            ))
        return signals

    def on_grid_fill(self, level: int, fill_price: float) -> list[Signal]:
        """Called when a grid order is filled. Returns next grid + TP signals."""
        self.filled_levels += 1

        signals = []
        # Place next grid
        grid_sig = self._next_grid_signal()
        if grid_sig:
            signals.append(grid_sig)
        # Place TP for filled level
        tp_sig = self._tp_signal(fill_price)
        if tp_sig:
            signals.append(tp_sig)
        return signals

    def on_tp_fill(self) -> list[Signal]:
        """Called when a TP order is filled. Decreases filled levels."""
        if self.filled_levels > 0:
            self.filled_levels -= 1
        return []

    def on_close_all(self):
        """Reset position state after close."""
        self.direction = 0
        self.entry_price = 0
        self.filled_levels = 0
        self.entry_time = None

    # === HELPERS ===

    def calc_unrealized_pnl(self, price: float) -> float:
        """Calculate unrealized PnL at given price."""
        if self.direction == 0:
            return 0
        main = (price - self.entry_price) * self.direction * self.total_lots
        return main - self.total_lots * self.params.commission

    def _next_grid_signal(self) -> Optional[Signal]:
        """Generate signal for next grid level."""
        next_level = self.filled_levels + 1
        if next_level > self.params.max_levels:
            return None
        if self.direction == 0:
            return None

        step = self.params.step_base
        if self.direction == 1:
            grid_price = self.entry_price - step * next_level
            side = BUY  # Buy lower (average down)
        else:
            grid_price = self.entry_price + step * next_level
            side = SELL  # Sell higher (average up)

        return Signal(
            action="GRID",
            direction=side,
            price=grid_price,
            quantity=1,
            level=next_level,
            tag=f"Grid-{next_level}",
        )

    def _tp_signal(self, fill_price: float) -> Optional[Signal]:
        """Generate TP signal for a filled grid level."""
        if self.direction == 0:
            return None
        spread = self.params.spread_base
        if self.direction == 1:
            tp_price = fill_price + spread
            side = SELL
        else:
            tp_price = fill_price - spread
            side = BUY

        return Signal(
            action="TP",
            direction=side,
            price=tp_price,
            quantity=1,
            tag=f"TP",
        )


# Constants for side
BUY = 1
SELL = 2
