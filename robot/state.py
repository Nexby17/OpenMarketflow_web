"""State persistence — save/restore robot state to JSON file."""
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("state")

MSK = timezone(timedelta(hours=3))


@dataclass
class TrackedOrder:
    order_id: str
    client_order_id: str
    side: int          # 1=BUY, 2=SELL
    order_type: str    # ENTRY, GRID, TP, CLOSE
    price: float
    level: int         # grid level (0 for entry/close)
    status: str        # PENDING, ACTIVE, FILLED, CANCELLED
    placed_at: str = ""


@dataclass
class RobotState:
    # Position
    direction: int = 0           # 1=LONG, -1=SHORT, 0=FLAT
    entry_price: float = 0.0
    filled_levels: int = 0
    entry_time: str = ""         # ISO format

    # Recovery
    last_entry_price: float = 0.0
    last_direction: int = 0

    # Orders
    tracked_orders: list = field(default_factory=list)

    # Stats
    round_trips: int = 0
    realized_pnl: float = 0.0

    # Meta
    ts: str = ""
    mode: str = "stopped"        # running, paused, stopped


class StateManager:
    """Manages robot state persistence."""

    def __init__(self, path: str = "/tmp/robot-state.json"):
        self.path = path
        self._state = RobotState()

    @property
    def state(self) -> RobotState:
        return self._state

    def save(self):
        """Save current state to disk."""
        self._state.ts = datetime.now(MSK).isoformat()
        try:
            data = asdict(self._state)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)  # atomic
        except Exception as e:
            log.error(f"Save error: {e}")

    def load(self) -> RobotState:
        """Load state from disk. Returns default if file missing/corrupt."""
        try:
            if os.path.exists(self.path):
                with open(self.path) as f:
                    data = json.load(f)
                self._state = RobotState(
                    direction=data.get("direction", 0),
                    entry_price=data.get("entry_price", 0),
                    filled_levels=data.get("filled_levels", 0),
                    entry_time=data.get("entry_time", ""),
                    last_entry_price=data.get("last_entry_price", 0),
                    last_direction=data.get("last_direction", 0),
                    tracked_orders=data.get("tracked_orders", []),
                    round_trips=data.get("round_trips", 0),
                    realized_pnl=data.get("realized_pnl", 0),
                    mode=data.get("mode", "stopped"),
                )
                log.info(f"State loaded: dir={self._state.direction} entry={self._state.entry_price:.0f}")
        except Exception as e:
            log.warning(f"State load error: {e}, using defaults")
            self._state = RobotState()
        return self._state

    def clear(self):
        """Reset state to defaults."""
        self._state = RobotState()
        self.save()

    def add_tracked_order(self, order: TrackedOrder):
        """Add or update a tracked order."""
        # Remove existing with same order_id
        self._state.tracked_orders = [
            o for o in self._state.tracked_orders
            if o.get("order_id") != order.order_id
        ]
        self._state.tracked_orders.append(asdict(order))
        self.save()

    def update_tracked_order(self, order_id: str, status: str):
        """Update status of a tracked order."""
        for o in self._state.tracked_orders:
            if o.get("order_id") == order_id:
                o["status"] = status
                break
        self.save()

    def remove_tracked_order(self, order_id: str):
        """Remove a tracked order."""
        self._state.tracked_orders = [
            o for o in self._state.tracked_orders
            if o.get("order_id") != order_id
        ]
        self.save()

    def get_active_orders(self, order_type: str = "") -> list[dict]:
        """Get tracked orders that are still active."""
        result = []
        for o in self._state.tracked_orders:
            if o.get("status") in ("PENDING", "ACTIVE"):
                if not order_type or o.get("order_type") == order_type:
                    result.append(o)
        return result
