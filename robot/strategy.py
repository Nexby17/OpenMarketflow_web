"""VP Scalp Grid strategy — signal generation and position management."""
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

MSK = timezone(timedelta(hours=3))

BUY = 1
SELL = 2


@dataclass
class GridLevel:
    level: int
    price: float
    side: int        # BUY or SELL
    status: str      # PENDING, FILLED, CLOSED, CANCELLED
    tp_price: float = 0  # TP price for this level
    tp_closed_price: float = 0  # Price at which TP filled (for realized PnL calc)


@dataclass
class StrategyParams:
    max_levels: int = 100
    step_base: int = 31
    spread_base: int = 31
    max_hold_minutes: int = 99999999999999
    min_profit_per_lot: int = 29
    commission: float = 0.90
    vp_lookback: int = 33
    vp_bin_size: int = 50
    vp_va_percent: float = 0.70
    rv_adaptation: bool = False


@dataclass
class Signal:
    action: str          # ENTRY, GRID, TP, CLOSE_ALL
    direction: int = 0   # 1=LONG, -1=SHORT
    price: float = 0.0
    quantity: int = 1
    level: int = 0
    tag: str = ""


class Strategy:
    """VP Scalp Grid strategy logic.

    Position tracking via grid_levels list — single source of truth.
    No separate _grid_price/_grid_level/_tp_price variables.
    """

    def __init__(self, params: StrategyParams = None):
        self.params = params or StrategyParams()

        # Position state
        self.direction: int = 0       # 1=LONG, -1=SHORT, 0=FLAT
        self.entry_price: float = 0
        self.entry_time: Optional[datetime] = None

        # Grid levels — SINGLE SOURCE OF TRUTH
        self.grid_levels: list[GridLevel] = []

        # VP indicators (set externally)
        self.poc: float = 0
        self.vah: float = 0
        self.val: float = 0
        self.current_price: float = 0

    def reset(self):
        """Clear all position state."""
        self.direction = 0
        self.entry_price = 0
        self.entry_time = None
        self.grid_levels = []
        self.current_price = 0

    def update_vp(self, poc, val, vah):
        """Update VP indicators."""
        if poc > 0:
            self.poc = poc
        if val > 0:
            self.val = val
        if vah > 0:
            self.vah = vah

    @property
    def filled_levels(self) -> int:
        return sum(1 for g in self.grid_levels if g.status == "FILLED")

    @property
    def open_levels(self) -> int:
        """Grid levels that are FILLED but TP not yet filled (= still open)."""
        return sum(1 for g in self.grid_levels if g.status == "FILLED" and g.tp_price > 0)

    @property
    def total_lots(self) -> int:
        """Only lots that are actually open (entry + grids without TP fill)."""
        return 1 + self.open_levels if self.direction != 0 else 0

    @property
    def realized_pnl(self) -> float:
        """PnL from TP fills that already closed."""
        total = 0.0
        for g in self.grid_levels:
            if g.status == "CLOSED":
                if self.direction == 1:
                    total += (g.tp_closed_price - g.price) - self.params.commission
                elif self.direction == -1:
                    total += (g.price - g.tp_closed_price) - self.params.commission
        return total

    @property
    def hold_minutes(self) -> int:
        if not self.entry_time:
            return 0
        return int((datetime.now(MSK) - self.entry_time).total_seconds() / 60)

    @property
    def has_position(self) -> bool:
        return self.direction != 0

    def get_pending_grid(self) -> Optional[GridLevel]:
        """Get the single pending grid level (or None)."""
        for g in self.grid_levels:
            if g.status == "PENDING" and g.tp_price == 0:
                return g
        return None

    def get_pending_tp(self) -> Optional[GridLevel]:
        """Get a pending TP level (any filled grid without TP filled)."""
        for g in self.grid_levels:
            if g.status == "FILLED" and g.tp_price > 0:
                # Check if there's a corresponding pending TP
                pass
        return None

    def get_active_grid(self) -> Optional[GridLevel]:
        """Get active (pending) grid order."""
        for g in self.grid_levels:
            if g.status == "PENDING":
                return g
        return None

    def get_active_tps(self) -> list[GridLevel]:
        """Get all filled grids whose TP hasn't filled yet."""
        return [g for g in self.grid_levels if g.status == "FILLED" and g.tp_price > 0]

    # === SIGNALS ===

    def check_entry(self, price: float) -> Optional[Signal]:
        """Check if we should enter. Returns Signal or None."""
        if self.direction != 0:
            return None
        if self.val <= 0 or self.vah <= 0:
            return None
        if price <= 0:
            return None

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
        """Called when entry order is filled. Create ALL grid levels in memory, place first."""
        self.direction = direction
        self.entry_price = price
        self.entry_time = datetime.now(MSK)
        self.grid_levels = []

        # Create ALL grid levels in memory
        step = self.params.step_base
        for i in range(1, self.params.max_levels + 1):
            if direction == 1:
                gp = price - step * i
                side = BUY
            else:
                gp = price + step * i
                side = SELL
            self.grid_levels.append(GridLevel(
                level=i, price=gp, side=side, status="PENDING", tp_price=0, tp_closed_price=0,
            ))

        signals = []
        # Place FIRST grid level only
        if self.grid_levels:
            g = self.grid_levels[0]
            signals.append(Signal(
                action="GRID", direction=g.side, price=g.price, quantity=1, level=g.level, tag=f"Grid-{g.level}",
            ))
        # POC-TP for entry lot
        if self.poc > 0:
            if direction == 1 and self.poc > price:
                signals.append(Signal(action="PLACE_POC_TP", direction=SELL, price=self.poc, quantity=1, tag="POC-TP"))
            elif direction == -1 and self.poc < price:
                signals.append(Signal(action="PLACE_POC_TP", direction=BUY, price=self.poc, quantity=1, tag="POC-TP"))
        return signals

    def on_grid_fill(self, grid_price: float, level: int = 0) -> list[Signal]:
        """Called when a grid order is filled. Returns next grid + trailing TP move."""
        filled = None
        for g in self.grid_levels:
            if g.status == "PENDING":
                if level > 0 and g.level == level:
                    g.status = "FILLED"
                    filled = g
                    break
                elif abs(g.price - grid_price) <= 1:  # match by price with 1pt tolerance
                    g.status = "FILLED"
                    filled = g
                    break

        if filled is None:
            return []

        signals = []
        # Find NEXT PENDING level and place it (one at a time)
        for g in self.grid_levels:
            if g.status == "PENDING" and g.level > filled.level:
                signals.append(Signal(
                    action="GRID", direction=g.side, price=g.price, quantity=1, level=g.level, tag=f"Grid-{g.level}",
                ))
                break  # only one

        # Per-level TP (one at a time): cancel old + place new at grid_price + spread
        spread = self.params.spread_base
        if self.direction == 1:
            tp_price = grid_price + spread
            tp_side = SELL
        else:
            tp_price = grid_price - spread
            tp_side = BUY

        filled.tp_price = tp_price
        signals.append(Signal(
            action="TP",
            direction=tp_side,
            price=tp_price,
            quantity=1,
            level=filled.level,
            tag=f"TP-{filled.level}",
        ))

        return signals

    def on_tp_fill(self, tp_price: float) -> bool:
        """Called when a TP order is filled. Mark level as CLOSED."""
        for g in self.grid_levels:
            if g.status == "FILLED" and g.tp_price == tp_price:
                g.tp_closed_price = tp_price
                g.status = "CLOSED"
                return True
        return False

    def on_close_all(self):
        """Reset position state after close."""
        self.direction = 0
        self.entry_price = 0
        self.entry_time = None
        self.grid_levels = []

    def serialize_grid(self) -> list[dict]:
        """Serialize grid_levels for state persistence."""
        return [
            {"level": g.level, "price": g.price, "side": g.side,
             "status": g.status, "tp_price": g.tp_price, "tp_closed_price": g.tp_closed_price}
            for g in self.grid_levels
        ]

    def restore_grid(self, data: list[dict]):
        """Restore grid_levels from state."""
        self.grid_levels = []
        for d in data:
            self.grid_levels.append(GridLevel(
                level=d["level"], price=d["price"], side=d["side"],
                status=d["status"], tp_price=d.get("tp_price", 0),
                tp_closed_price=d.get("tp_closed_price", 0),
            ))

    # === HELPERS ===

    def calc_unrealized_pnl(self, price: float) -> float:
        """Calculate unrealized PnL for OPEN lots only."""
        if self.direction == 0 or self.entry_price <= 0:
            return 0
        open_lots = self.total_lots
        if open_lots <= 0:
            return 0
        # Entry lot + open grid levels
        pnl = (price - self.entry_price) * self.direction  # entry lot
        for g in self.grid_levels:
            if g.status == "FILLED" and g.tp_price > 0:
                pnl += (price - g.price) * self.direction  # open grid lots
        pnl -= open_lots * self.params.commission
        return pnl

    def _create_grid_signal(self, level: int) -> Optional[Signal]:
        """Create grid signal and register level."""
        if self.direction == 0:
            return None
        if level > self.params.max_levels:
            return None

        step = self.params.step_base
        if self.direction == 1:
            grid_price = self.entry_price - step * level
            side = BUY
        else:
            grid_price = self.entry_price + step * level
            side = SELL

        # Register the level
        self.grid_levels.append(GridLevel(
            level=level,
            price=grid_price,
            side=side,
            status="PENDING",
            tp_price=0,
        ))

        return Signal(
            action="GRID",
            direction=side,
            price=grid_price,
            quantity=1,
            level=level,
            tag=f"Grid-{level}",
        )

    def check_paper_grid_fill(self, price: float) -> Optional[GridLevel]:
        """Paper mode: check if price hit any pending grid level. Returns filled level or None."""
        for g in self.grid_levels:
            if g.status != "PENDING":
                continue
            if self.direction == 1 and price <= g.price:
                return g
            if self.direction == -1 and price >= g.price:
                return g
        return None

    def check_paper_tp_fill(self, price: float) -> Optional[GridLevel]:
        """Paper mode: check if price hit any pending TP level. Returns filled level or None."""
        for g in self.grid_levels:
            if g.status != "FILLED" or g.tp_price <= 0:
                continue
            if self.direction == 1 and price >= g.tp_price:
                return g
            if self.direction == -1 and price <= g.tp_price:
                return g
        return None
