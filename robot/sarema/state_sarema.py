"""F-061: Per-instance state persistence (позиция, стоп, base, лимиты, kill-day).

Рестарт без потери состояния; startup-always-stopped (урок F-059).
"""
import json
import os
from datetime import date
from typing import Optional


class SaremaState:
    def __init__(self, path: str):
        self.path = path
        self.mode = "stopped"          # stopped | running | paused (startup-always-stopped)
        self.direction = 0             # 1 long, -1 short, 0 flat
        self.lots_qty = 0              # позиция в штуках
        self.entry_price = 0.0
        self.stop_price = 0.0
        self.entry_time: Optional[str] = None
        self.base = 1_000_000.0
        self.pending_signal = None     # {'dir': ±1, 'signal_bar_close': ts} на вход в начале часа
        self.kill_day: Optional[str] = None    # день, на который заблокированы входы
        self.day_realized_R: dict = {}         # 'YYYY-MM-DD' -> сумма R
        self.total_pnl = 0.0
        self.trades_done = 0

    def save(self) -> None:
        data = {
            "mode": "stopped",             # сохраняем всегда stopped (F-059)
            "direction": self.direction,
            "lots_qty": self.lots_qty,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "entry_time": self.entry_time,
            "base": self.base,
            "pending_signal": self.pending_signal,
            "kill_day": self.kill_day,
            "day_realized_R": self.day_realized_R,
            "total_pnl": self.total_pnl,
            "trades_done": self.trades_done,
        }
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def load(self) -> bool:
        if not os.path.exists(self.path):
            return False
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.direction = int(data.get("direction", 0))
        self.lots_qty = int(data.get("lots_qty", 0))
        self.entry_price = float(data.get("entry_price", 0.0))
        self.stop_price = float(data.get("stop_price", 0.0))
        self.entry_time = data.get("entry_time")
        self.base = float(data.get("base", 1_000_000.0))
        self.pending_signal = data.get("pending_signal")
        self.kill_day = data.get("kill_day")
        self.day_realized_R = {k: float(v) for k, v in data.get("day_realized_R", {}).items()}
        self.total_pnl = float(data.get("total_pnl", 0.0))
        self.trades_done = int(data.get("trades_done", 0))
        self.mode = "stopped"          # startup-always-stopped (F-059)
        return True

    def entries_blocked(self, today: date) -> bool:
        return self.kill_day is not None and str(today) == self.kill_day

    def record_trade_R(self, exit_day: str, r: float) -> None:
        self.day_realized_R[exit_day] = self.day_realized_R.get(exit_day, 0.0) + r
        self.trades_done += 1
