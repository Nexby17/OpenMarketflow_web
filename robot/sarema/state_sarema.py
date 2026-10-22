"""F-061: Per-instance state persistence (позиция, стоп, base, лимиты, kill-day).

Рестарт без потери состояния; startup-always-stopped (урок F-059).
"""
import json
import os
from datetime import date
from typing import Optional


class SaremaState:
    def __init__(self, path: str, ticker: str = ""):
        self.path = path
        self.ticker = ticker
        self.mode = "stopped"          # stopped | running | paused (startup-always-stopped)
        self.direction = 0             # 1 long, -1 short, 0 flat
        self.lots_qty = 0              # позиция в штуках
        self.entry_price = 0.0
        self.stop_price = 0.0
        self.initial_stop_price = 0.0   # F-098: первоначальный стоп (база трейлинга)
        self.entry_time: Optional[str] = None
        self.base = 1_000_000.0
        self.pending_signal = None     # {'dir': ±1, 'signal_bar_close': ts} на вход в начале часа
        self.kill_day: Optional[str] = None    # день, на который заблокированы входы
        self.day_realized_R: dict = {}         # 'YYYY-MM-DD' -> сумма R
        self.total_pnl = 0.0
        self.trades_done = 0
        # F-071: последний ОТРАБОТАННЫЙ сигнал-бар (защита от повторного входа на том же кроссе)
        self.last_entry_signal_bar: Optional[str] = None
        # F-059a (Дмитрий 14.10: «выключаю только я»): режим running переживает рестарт процесса
        self.persist_mode = True
        # F-076: авто-старт при подъёме процесса (24/7). ⏹ ставит disabled=True; ▶ сбрасывает.
        self.auto_start_disabled = False
        # F-061 журнал позиций (логика таблицы Дмитрия): открытые сверху, закрытие дозаписывается
        self.trade_history = []

    def save(self) -> None:
        data = {
            # F-059a: persist_mode — running переживает рестарт процесса (выключение только кнопкой ⏹)
            "mode": self.mode if self.persist_mode else "stopped",
            "direction": self.direction,
            "lots_qty": self.lots_qty,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "initial_stop_price": self.initial_stop_price,
            "entry_time": self.entry_time,
            "base": self.base,
            "pending_signal": self.pending_signal,
            "kill_day": self.kill_day,
            "day_realized_R": self.day_realized_R,
            "total_pnl": self.total_pnl,
            "trades_done": self.trades_done,
            "last_entry_signal_bar": self.last_entry_signal_bar,
            "auto_start_disabled": self.auto_start_disabled,
            "trade_history": self.trade_history[-500:],
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
        self.initial_stop_price = float(data.get("initial_stop_price", 0.0))
        self.entry_time = data.get("entry_time")
        self.base = float(data.get("base", 1_000_000.0))
        self.pending_signal = data.get("pending_signal")
        self.kill_day = data.get("kill_day")
        self.day_realized_R = {k: float(v) for k, v in data.get("day_realized_R", {}).items()}
        self.total_pnl = float(data.get("total_pnl", 0.0))
        self.trades_done = int(data.get("trades_done", 0))
        self.trade_history = list(data.get("trade_history", []))
        # F-071: последний отработанный сигнал-бар
        self.last_entry_signal_bar = data.get("last_entry_signal_bar")
        self.auto_start_disabled = bool(data.get("auto_start_disabled", False))
        # F-059a: режим из файла (running переживает рестарт процесса)
        self.mode = data.get("mode", "stopped")
        return True

    def entries_blocked(self, today: date) -> bool:
        return self.kill_day is not None and str(today) == self.kill_day

    def record_trade_R(self, exit_day: str, r: float) -> None:
        self.day_realized_R[exit_day] = self.day_realized_R.get(exit_day, 0.0) + r
        self.trades_done += 1

    # --- Журнал позиций (логика таблицы Дмитрия: открытие слева, закрытие справа) ---
    def journal_open(self, rec: dict) -> None:
        """Новая открытая позиция: {tic, tf, date_open, time_open, dir L/S,
        entry, qty, volume, risk_pct, risk_pts, stop, status:'open', close:None}"""
        rec["status"] = "open"
        rec["close"] = None
        self.trade_history.append(rec)
        if len(self.trade_history) > 500:
            self.trade_history = self.trade_history[-500:]

    def journal_close(self, close_rec: dict) -> dict | None:
        """Дозаписать закрытие в последнюю открытую позицию. Возвращает обновлённую запись."""
        rec = next((r for r in reversed(self.trade_history) if r.get("status") == "open"), None)
        if not rec:
            return None
        rec["close"] = close_rec
        rec["status"] = "closed"
        return rec
