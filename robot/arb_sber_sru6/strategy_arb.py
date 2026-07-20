"""Arbitrage Strategy — entry/exit logic, position tracking, PnL.

Entry: |Z| > entry_z → limit on leg A + market on leg B
Exit: net_pnl > min_profit (configurable metric) OR risk trigger
"""
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from arb_engine import BasisCalculator, OrderBookTracker

log = logging.getLogger("strategy_arb")

MSK = timezone(timedelta(hours=3))

LONG_BASIS = 1     # buy A, sell B (basis too low, expect reversion up)
SHORT_BASIS = -1    # sell A, buy B (basis too high, expect reversion down)
FLAT = 0


@dataclass
class ArbParams:
    """User-configurable parameters — hot-reloadable via API."""
    # Instruments
    symbol_a: str = "GAZP@RTSX"
    ticker_a: str = "GAZP"
    symbol_b: str = "GZM6@RTSX"
    ticker_b: str = "GZM6"
    lots_a: int = 10
    lots_b: int = 1
    hedge_ratio: float = 10.0
    # Multiplier: ₽ per 1 unit price move per 1 lot
    # GAZP: 10 shares/lot → mult=10 (1₽ share move × 10 shares = 10₽/lot)
    # GZM6: step_price=1₽ → mult=1 (1 point move = 1₽/contract)
    mult_a: float = 10.0
    mult_b: float = 1.0

    # Z-score
    entry_z: float = 1.5          # SHORT basis: enter when z > entry_z
    entry_z_long: float = -1.5    # LONG basis: enter when z < entry_z_long
    lookback: int = 50

    # Entry mode: "zscore" (default) | "spread_rub" (absolute thresholds)
    entry_mode: str = "zscore"
    spread_rub_high: float = 400.0   # SHORT when spread_rub > this
    spread_rub_low: float = 200.0    # LONG when spread_rub < this

    # Fair value (cost of carry)
    risk_free_rate: float = 0.16         # CBR key rate (annual)
    expiration_date: str = "2026-09-18"  # futures expiration date
    contract_size: int = 100             # shares per futures contract (GAZP=100)

    # Min profit exit (single choice)
    # type: "pts" | "rub" | "pct"
    min_profit_type: str = "rub"
    min_profit_value: float = 20.0

    # Risk (single choice)
    # type: "stop_loss_rub" | "time_stop_min" | "max_dd_pct" | "kill_switch"
    risk_type: str = "stop_loss_rub"
    risk_value: float = 5000.0

    # Execution
    leg_a_timeout: int = 5
    min_fill_ratio: float = 0.5
    slippage_bps: float = 5.0

    # Capital
    capital: float = 1_000_000.0
    go_per_contract_b: float = 2000.0  # GO per futures contract (from MOEX ISS)

    # Direction
    allow_long_basis: bool = False       # SHORT basis always allowed; LONG needs this flag

    # Commission
    use_commission: bool = True            # master switch: deduct commission everywhere
    commission_stock_pct: float = 0.04    # % of turnover per side (stock)
    commission_futures_rt: float = 0.9    # ₽ per contract round-trip (futures)

    # Session
    session_start: str = "10:00"
    session_end: str = "18:45"
    use_evening: bool = False
    evening_start: str = "19:00"
    evening_end: str = "23:50"


@dataclass
class ArbLayer:
    """Single arbitrage layer (one entry/exit pair)."""
    side: int = FLAT              # LONG_BASIS or SHORT_BASIS
    entry_price_a: float = 0.0    # fill price of leg A
    entry_price_b: float = 0.0    # fill price of leg B
    entry_basis: float = 0.0
    entry_z: float = 0.0
    entry_time: float = 0.0       # unix ts
    lots_a: int = 0
    lots_b: int = 0
    layer_id: int = 0             # unique sequential id


class ArbitrageStrategy:
    """Arbitrage strategy logic — signals, position tracking, PnL."""

    def __init__(self, params: ArbParams):
        self.p = params
        self.basis_calc = BasisCalculator(
            lookback=params.lookback,
            hedge_ratio=params.hedge_ratio,
            risk_free_rate=params.risk_free_rate,
            expiration_date=params.expiration_date,
            contract_size=params.contract_size,
        )
        self.ob_a = OrderBookTracker()
        self.ob_b = OrderBookTracker()

        self.layers: list[ArbLayer] = []
        self._next_layer_id: int = 1
        self.realized_pnl: float = 0.0
        self.peak_pnl: float = 0.0
        self.max_dd: float = 0.0
        self.trade_history: list[dict] = []
        self.entry_lock: bool = False
        self._lock_time: float = 0.0

        # Broker sync (updated from FinamPy every 30s)
        self.broker_equity: float = 0.0
        self.broker_pnl_today: float = 0.0
        self.broker_pnl_total: float = 0.0
        self.broker_positions: list[dict] = []
        self.broker_sync_time: float = 0.0

    # === GO / Capital ===

    def _go_per_contract_b(self) -> float:
        """Estimate GO per futures contract. Updated from config or MOEX."""
        return getattr(self.p, 'go_per_contract_b', 2000.0)

    def _stock_cost_per_lot_a(self) -> float:
        """Stock GO per lot A = price × mult_a × margin_rate (50% for Russian stocks)."""
        margin_rate = getattr(self.p, 'stock_margin_rate', 0.5)
        return self.basis_calc.price_a * self.p.mult_a * margin_rate

    def _layer_cost(self) -> float:
        """Capital required for one layer."""
        stock = self.p.lots_a * self._stock_cost_per_lot_a()
        futures_go = self.p.lots_b * self._go_per_contract_b()
        return stock + futures_go

    def _used_capital(self) -> float:
        """Total capital currently deployed in layers."""
        return sum(
            l.lots_a * l.entry_price_a * self.p.mult_a +
            l.lots_b * self._go_per_contract_b()
            for l in self.layers
        )

    def _can_open_layer(self) -> bool:
        """Check if we have enough capital for another layer."""
        return self._used_capital() + self._layer_cost() <= self.p.capital

    def open_layers_count(self) -> int:
        return len(self.layers)

    # === State ===

    def get_state(self) -> dict:
        return {
            "layers": [
                {
                    "layerId": l.layer_id,
                    "side": l.side,
                    "entryPriceA": l.entry_price_a,
                    "entryPriceB": l.entry_price_b,
                    "entryBasis": round(l.entry_basis, 2),
                    "entryZ": round(l.entry_z, 3),
                    "entryTime": datetime.fromtimestamp(l.entry_time, MSK).isoformat(),
                    "lotsA": l.lots_a,
                    "lotsB": l.lots_b,
                    "holdMinutes": int((time.time() - l.entry_time) / 60),
                    "unrealizedPnl": round(self._layer_unrealized_pnl(l), 2),
                }
                for l in self.layers
            ],
            "position": {  # backward compat for UI — represents aggregate
                "side": self.layers[0].side if self.layers else FLAT,
                "entryPriceA": self.layers[-1].entry_price_a if self.layers else 0,
                "entryPriceB": self.layers[-1].entry_price_b if self.layers else 0,
                "entryBasis": round(self.layers[-1].entry_basis, 2) if self.layers else 0,
                "entryZ": round(self.layers[-1].entry_z, 3) if self.layers else 0,
                "entryTime": datetime.fromtimestamp(self.layers[-1].entry_time, MSK).isoformat() if self.layers else "",
                "lotsA": sum(l.lots_a for l in self.layers),
                "lotsB": sum(l.lots_b for l in self.layers),
                "holdMinutes": int((time.time() - self.layers[0].entry_time) / 60) if self.layers else 0,
            } if self.layers else None,
            "openLayers": len(self.layers),
            "usedCapital": round(self._used_capital(), 2),
            "maxCapital": self.p.capital,
            "realizedPnl": round(self.realized_pnl, 2),
            "peakPnl": round(self.peak_pnl, 2),
            "maxDd": round(self.max_dd, 2),
            "trades": len(self.trade_history),
            "basis": self.basis_calc.get_state(),
            "obA": self._ob_state(self.ob_a),
            "obB": self._ob_state(self.ob_b),
            "entryLock": self.entry_lock,
        }

    def _ob_state(self, ob: OrderBookTracker) -> dict:
        return {
            "bestBid": ob.best_bid,
            "bestAsk": ob.best_ask,
            "mid": round(ob.mid, 4),
            "bidVol": ob.bid_vol,
            "askVol": ob.ask_vol,
            "totalVol": ob.total_volume,
            "hasData": ob.has_data,
        }

    def get_status(self) -> dict:
        """Full status for API."""
        state = self.get_state()
        unrealized = sum(self._layer_unrealized_pnl(l) for l in self.layers)
        state["unrealizedPnl"] = round(unrealized, 2)
        state["totalPnl"] = round(self.realized_pnl + unrealized, 2)
        state["tradeHistory"] = self.trade_history[-200:]
        # Broker sync data
        state["broker"] = {
            "equity": round(self.broker_equity, 2),
            "pnlToday": round(self.broker_pnl_today, 2),
            "pnlTotal": round(self.broker_pnl_total, 2),
            "positions": self.broker_positions,
            "syncAgeSec": round(time.time() - self.broker_sync_time, 1) if self.broker_sync_time else None,
        }
        return state

    def save_state(self) -> dict:
        return {
            "realized_pnl": self.realized_pnl,
            "peak_pnl": self.peak_pnl,
            "max_dd": self.max_dd,
            "trade_history": self.trade_history[-500:],
            "layers": [
                {
                    "side": l.side,
                    "entry_price_a": l.entry_price_a,
                    "entry_price_b": l.entry_price_b,
                    "entry_basis": l.entry_basis,
                    "entry_z": l.entry_z,
                    "entry_time": l.entry_time,
                    "lots_a": l.lots_a,
                    "lots_b": l.lots_b,
                    "layer_id": l.layer_id,
                }
                for l in self.layers
            ],
            "next_layer_id": self._next_layer_id,
        }

    def load_state(self, state: dict):
        self.trade_history = state.get("trade_history", [])
        # Recalculate realized_pnl from trade history
        if self.trade_history:
            self.realized_pnl = sum(t.get("pnl", 0) for t in self.trade_history)
            self.peak_pnl = max(self.realized_pnl, state.get("peak_pnl", 0))
            self.max_dd = state.get("max_dd", 0)
        else:
            self.realized_pnl = state.get("realized_pnl", 0)
            self.peak_pnl = state.get("peak_pnl", 0)
            self.max_dd = state.get("max_dd", 0)
        # Load layers (or migrate old single position)
        layers_data = state.get("layers", [])
        if not layers_data:
            # Migrate old single position format
            pos_data = state.get("position")
            if pos_data and pos_data.get("side", 0) != FLAT:
                layers_data = [pos_data]
        self.layers = []
        for ld in layers_data:
            self.layers.append(ArbLayer(
                side=ld["side"],
                entry_price_a=ld["entry_price_a"],
                entry_price_b=ld["entry_price_b"],
                entry_basis=ld.get("entry_basis", 0),
                entry_z=ld.get("entry_z", 0),
                entry_time=ld["entry_time"],
                lots_a=ld["lots_a"],
                lots_b=ld["lots_b"],
                layer_id=ld.get("layer_id", self._next_layer_id),
            ))
            self._next_layer_id = max(self._next_layer_id, ld.get("layer_id", 0) + 1)

    # === Signals ===

    def _is_duplicate_entry(self, side: str, z: float) -> bool:
        """Check if this signal duplicates a recent layer."""
        if not self.layers:
            return False
        last = self.layers[-1]
        side_int = LONG_BASIS if side == "long_basis" else SHORT_BASIS
        if last.side != side_int:
            return False
        # Same direction, Z within 0.3, less than 3 sec ago → duplicate
        age = time.time() - last.entry_time
        if age < 2.0 and abs(last.entry_z - z) < 0.3:
            return True
        return False

    def check_entry(self) -> Optional[dict]:
        """Check for entry signal — can open new layer if signal persists."""
        if self.entry_lock or self.entry_active:
            return None

        if not self.basis_calc.has_enough_data:
            log.warning(f"Entry blocked: insufficient data ({self.basis_calc.data_points}/{self.basis_calc.lookback})")
            return None

        # Capital check — stop adding layers if we can't afford another
        if not self._can_open_layer():
            log.warning(f"Entry blocked: capital (used={self._used_capital():.0f} + layer={self._layer_cost():.0f} > capital={self.p.capital})")
            return None

        if self.p.entry_mode == "spread_rub":
            signal = self._check_entry_spread_rub()
        else:
            signal = self._check_entry_zscore()

        if signal and self._is_duplicate_entry(signal["side"], signal["z"]):
            return None

        # Averaging check: only add layer if basis continues to trend
        # SHORT: basis must be > last layer entry_basis (expanding)
        # LONG: basis must be < last layer entry_basis (contracting)
        if self.layers and not self._allows_averaging(signal["side"]):
            return None

        return signal

    def _allows_averaging(self, side: str) -> bool:
        """Check if current basis allows adding a new layer (averaging).
        Only allow when basis continues to move in the entry direction.
        SHORT: current basis > last layer entry_basis (basis expanding)
        LONG: current basis < last layer entry_basis (basis contracting)"""
        if not self.layers:
            return True  # No layers — first entry always allowed
        last = self.layers[-1]
        side_int = LONG_BASIS if side == "long_basis" else SHORT_BASIS
        if last.side != side_int:
            return False  # Opposite direction — don't average
        basis_now = self.basis_calc.basis
        if side_int == SHORT_BASIS:
            ok = basis_now > last.entry_basis
        else:
            ok = basis_now < last.entry_basis
        if not ok:
            log.debug(f"Averaging blocked: basis={basis_now:.2f} vs last entry_basis={last.entry_basis:.2f}")
        return ok

    def _check_entry_zscore(self) -> Optional[dict]:
        """Variant 2 (default): Z-score based entry."""
        z = self.basis_calc.zscore_no_push

        if z > self.p.entry_z:
            return {
                "action": "entry",
                "side": "short_basis",
                "z": z,
                "basis": self.basis_calc.basis,
                "price_a": self.basis_calc.price_a,
                "price_b": self.basis_calc.price_b,
            }
        elif z < self.p.entry_z_long:
            if not self.p.allow_long_basis:
                return None
            return {
                "action": "entry",
                "side": "long_basis",
                "z": z,
                "basis": self.basis_calc.basis,
                "price_a": self.basis_calc.price_a,
                "price_b": self.basis_calc.price_b,
            }
        return None

    def _check_entry_spread_rub(self) -> Optional[dict]:
        """Variant 1: absolute spread thresholds in ₽."""
        spread = self.basis_calc.spread_rub
        z = self.basis_calc.zscore_no_push

        if spread > self.p.spread_rub_high:
            return {
                "action": "entry",
                "side": "short_basis",
                "z": z,
                "spread_rub": spread,
                "basis": self.basis_calc.basis,
                "price_a": self.basis_calc.price_a,
                "price_b": self.basis_calc.price_b,
            }
        elif spread < self.p.spread_rub_low:
            if not self.p.allow_long_basis:
                return None
            return {
                "action": "entry",
                "side": "long_basis",
                "z": z,
                "spread_rub": spread,
                "basis": self.basis_calc.basis,
                "price_a": self.basis_calc.price_a,
                "price_b": self.basis_calc.price_b,
            }
        return None

    def check_exit(self) -> Optional[dict]:
        """Check for exit signal across all layers. Returns first exit or None."""
        if not self.layers:
            return None

        # Check each layer for min_profit exit
        per_layer_pnls = []
        for layer in self.layers:
            unrealized = self._layer_unrealized_pnl(layer)
            hold_min = (time.time() - layer.entry_time) / 60
            per_layer_pnls.append((layer, unrealized, hold_min))

            if self._meets_min_profit(unrealized, layer):
                return {
                    "action": "exit",
                    "reason": "profit_target",
                    "pnl": unrealized,
                    "hold_min": hold_min,
                    "layer_id": layer.layer_id,
                }

        # Also close ALL layers if total unrealized >= min_profit and ALL profitable
        if len(per_layer_pnls) > 1 and all(pnl > 0 for _, pnl, _ in per_layer_pnls):
            total = sum(pnl for _, pnl, _ in per_layer_pnls)
            if total >= self.p.min_profit_value:
                max_hold = max(hold for _, _, hold in per_layer_pnls)
                log.info(f"EXIT ALL (total profit target): total={total:.2f} >= {self.p.min_profit_value}, layers={len(per_layer_pnls)}")
                return {
                    "action": "exit_all",
                    "reason": "profit_target_total",
                    "pnl": total,
                    "hold_min": max_hold,
                    "layer_id": None,
                }

        # Check risk on total portfolio PnL
        total_unrealized = sum(self._layer_unrealized_pnl(l) for l in self.layers)
        total_pnl = self.realized_pnl + total_unrealized
        # Use max hold time across all open layers for time_stop
        max_hold = max((time.time() - l.entry_time) / 60 for l in self.layers) if self.layers else 0
        risk_hit = self._check_risk(total_unrealized, max_hold)
        if risk_hit:
            # Risk hit — close ALL layers
            return {
                "action": "exit_all",
                "reason": risk_hit,
                "pnl": total_unrealized,
                "hold_min": 0,
                "layer_id": None,  # all layers
            }

        return None

    def _meets_min_profit(self, unrealized: float, layer: ArbLayer = None) -> bool:
        """Check if unrealized PnL meets the configured min profit threshold."""
        if unrealized <= 0:
            return False

        t = self.p.min_profit_type
        v = self.p.min_profit_value

        if t == "rub":
            return unrealized >= v
        elif t == "pts":
            if not layer:
                return False
            basis_move = abs(self.basis_calc.basis - layer.entry_basis)
            return basis_move >= v
        elif t == "pct":
            if not layer:
                return False
            cost = abs(layer.entry_price_a * layer.lots_a * self.p.mult_a) + \
                   abs(layer.entry_price_b * layer.lots_b * self.p.go_per_contract_b())
            if cost > 0:
                return (unrealized / cost * 100) >= v
            return False
        return False

    def _check_risk(self, unrealized: float, hold_min: float) -> Optional[str]:
        """Check risk conditions. Returns reason string or None."""
        t = self.p.risk_type
        v = self.p.risk_value

        if t == "stop_loss_rub":
            if unrealized <= -v:
                return "stop_loss"
        elif t == "time_stop_min":
            if hold_min >= v:
                return "time_stop"
        elif t == "max_dd_pct":
            total = self.realized_pnl + unrealized
            self.peak_pnl = max(self.peak_pnl, total)
            dd = self.peak_pnl - total
            if self.peak_pnl > 0 and (dd / abs(self.peak_pnl) * 100) >= v:
                return "max_dd"
        elif t == "kill_switch":
            # kill_switch is handled externally — always blocked here
            return None
        return None

    def _is_futures(self, ticker: str) -> bool:
        """Check if instrument is a futures (has digit in ticker like BRQ6, GZM6)."""
        return any(c.isdigit() for c in (ticker or ''))

    def _calc_commission(self, entry_price_a: float, exit_price_a: float,
                            lots_a: int, lots_b: int) -> float:
        """Calculate total round-trip commission.
        Stock: commission_stock_pct% of turnover per side (entry + exit).
        Futures: commission_futures_rt ₽ per contract round-trip.
        Auto-detects instrument type from ticker.
        Returns 0 if use_commission is False.
        """
        if not self.p.use_commission:
            return 0.0

        comm = 0.0
        # Leg A
        if self._is_futures(self.p.ticker_a):
            # Futures A: fixed ₽ per contract RT
            comm += self.p.commission_futures_rt * lots_a
        else:
            # Stock A: % of turnover
            stock_turnover = (abs(entry_price_a) + abs(exit_price_a)) * lots_a * self.p.mult_a
            comm += stock_turnover * self.p.commission_stock_pct / 100.0
        # Leg B (always futures in this robot)
        comm += self.p.commission_futures_rt * lots_b
        return comm

    def _layer_unrealized_pnl(self, layer: ArbLayer) -> float:
        """Calculate unrealized PnL using market bid/ask (net of commission).
        For FORTS leg B: fall back to last price if bid/ask deviates >0.2% from last.
        To close: pay ask when buying, receive bid when selling."""
        bid_a = self.basis_calc.bid_a or self.basis_calc.price_a
        ask_a = self.basis_calc.ask_a or self.basis_calc.price_a
        bid_b = self.basis_calc.bid_b or self.basis_calc.price_b
        ask_b = self.basis_calc.ask_b or self.basis_calc.price_b

        # For FORTS (leg B): check if bid/ask are stale — deviate >0.2% from last price
        price_b = self.basis_calc.price_b
        if price_b > 0:
            stale = False
            for val in (bid_b, ask_b):
                if val > 0 and abs(val - price_b) / price_b > 0.002:
                    stale = True
                    break
            if stale:
                bid_b = price_b
                ask_b = price_b
                if not getattr(self, '_stale_warned_ts', 0) or time.time() - self._stale_warned_ts > 5:
                    log.warning(f"OB stale: bid_b={self.basis_calc.bid_b:.2f} ask_b={self.basis_calc.ask_b:.2f} vs price_b={price_b:.2f} — using last")
                    self._stale_warned_ts = time.time()
        else:
            self._stale_warned_ts = 0

        if bid_a <= 0 or ask_a <= 0 or bid_b <= 0 or ask_b <= 0:
            bid_a = ask_a = layer.entry_price_a
            bid_b = ask_b = layer.entry_price_b
            if bid_a <= 0 or bid_b <= 0:
                return 0.0

        if layer.side == LONG_BASIS:
            # LONG: entered sell A / buy B. Close: buy A @ ask, sell B @ bid
            pnl_a = (layer.entry_price_a - ask_a) * layer.lots_a * self.p.mult_a
            pnl_b = (bid_b - layer.entry_price_b) * layer.lots_b * self.p.mult_b
        else:
            # SHORT: entered buy A / sell B. Close: sell A @ bid, buy B @ ask
            pnl_a = (bid_a - layer.entry_price_a) * layer.lots_a * self.p.mult_a
            pnl_b = (layer.entry_price_b - ask_b) * layer.lots_b * self.p.mult_b

        gross = pnl_a + pnl_b
        comm = self._calc_commission(layer.entry_price_a, bid_a if layer.side == SHORT_BASIS else ask_a,
                                      layer.lots_a, layer.lots_b)
        return gross - comm

    def _calc_unrealized_pnl(self) -> float:
        """Total unrealized across all layers."""
        return sum(self._layer_unrealized_pnl(l) for l in self.layers)

    # === Layer Management ===

    def open_layer(self, side: int, price_a: float, price_b: float,
                   lots_a: int, lots_b: int, z: float):
        """Open a new layer after both legs filled."""
        entry_basis = price_b - price_a * self.p.hedge_ratio
        layer = ArbLayer(
            side=side,
            entry_price_a=price_a,
            entry_price_b=price_b,
            entry_basis=entry_basis,
            entry_z=z,
            entry_time=time.time(),
            lots_a=lots_a,
            lots_b=lots_b,
            layer_id=self._next_layer_id,
        )
        self._next_layer_id += 1
        self.layers.append(layer)
        # Brief anti-duplicate lock (main loop ticks every 0.5s)
        self._set_lock(2.0)
        side_str = "LONG" if side == LONG_BASIS else "SHORT"
        log.info(f"LAYER #{layer.layer_id} OPEN {side_str} | A={price_a:.2f} ×{lots_a} | B={price_b:.2f} ×{lots_b} | basis={entry_basis:.2f} Z={z:.2f} | layers={len(self.layers)}")

    def get_layer(self, layer_id: int) -> Optional[ArbLayer]:
        """Find layer by id."""
        for l in self.layers:
            if l.layer_id == layer_id:
                return l
        return None

    def close_layer(self, layer: ArbLayer, exit_price_a: float, exit_price_b: float, reason: str):
        """Close a specific layer and calculate realized PnL."""
        if layer.side == LONG_BASIS:
            pnl_b = (exit_price_b - layer.entry_price_b) * layer.lots_b * self.p.mult_b
            pnl_a = (layer.entry_price_a - exit_price_a) * layer.lots_a * self.p.mult_a
        else:
            pnl_b = (layer.entry_price_b - exit_price_b) * layer.lots_b * self.p.mult_b
            pnl_a = (exit_price_a - layer.entry_price_a) * layer.lots_a * self.p.mult_a

        pnl = pnl_b + pnl_a
        comm = self._calc_commission(layer.entry_price_a, exit_price_a, layer.lots_a, layer.lots_b)
        pnl_net = pnl - comm

        self.realized_pnl += pnl_net
        self.peak_pnl = max(self.peak_pnl, self.realized_pnl)
        self.max_dd = max(self.max_dd, self.peak_pnl - self.realized_pnl)

        hold_min = (time.time() - layer.entry_time) / 60

        trade = {
            "layerId": layer.layer_id,
            "entryTime": datetime.fromtimestamp(layer.entry_time, MSK).strftime("%Y-%m-%d %H:%M:%S"),
            "exitTime": datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S"),
            "side": "LONG" if layer.side == LONG_BASIS else "SHORT",
            "entryPriceA": layer.entry_price_a,
            "entryPriceB": layer.entry_price_b,
            "exitPriceA": exit_price_a,
            "exitPriceB": exit_price_b,
            "entryBasis": round(layer.entry_basis, 2),
            "exitBasis": round(exit_price_b - exit_price_a * self.p.hedge_ratio, 2),
            "entryZ": round(layer.entry_z, 3),
            "lotsA": layer.lots_a,
            "lotsB": layer.lots_b,
            "holdMin": round(hold_min, 1),
            "pnl": round(pnl_net, 2),
            "commission": round(comm, 2),
            "reason": reason,
        }
        self.trade_history.append(trade)

        side_str = "LONG" if layer.side == LONG_BASIS else "SHORT"
        log.info(f"LAYER #{layer.layer_id} CLOSE {side_str} | PnL={pnl_net:+.2f}₽ (gross={pnl:+.2f}₽ comm={comm:.2f}₽) | reason={reason} | hold={hold_min:.0f}min | layers={len(self.layers)-1}")

        self.layers.remove(layer)
        self._set_lock(1.0)  # 1 sec cooldown after exit
        return pnl_net

    def close_all_layers(self, exit_price_a: float, exit_price_b: float, reason: str):
        """Close all layers (used for risk stop / manual stop)."""
        closed = []
        # Make a copy since close_layer mutates the list
        for layer in list(self.layers):
            pnl = self.close_layer(layer, exit_price_a, exit_price_b, reason)
            closed.append((layer.layer_id, pnl))
        self._set_lock(5.0)
        return closed

    def _set_lock(self, seconds: float):
        self.entry_lock = True
        self._lock_time = time.time() + seconds

    def update_lock(self):
        """Call periodically to release expired locks."""
        if self.entry_lock and time.time() > self._lock_time:
            self.entry_lock = False

    def set_entry_active(self):
        """Block entries while an order is being executed."""
        self._entry_active = True

    def clear_entry_active(self):
        """Allow entries again after order execution completes."""
        self._entry_active = False

    @property
    def entry_active(self) -> bool:
        return getattr(self, '_entry_active', False)

    def force_unlock(self):
        self.entry_lock = False
        self._entry_active = False

    def is_in_session(self) -> bool:
        """Check if within trading session."""
        now = datetime.now(MSK)
        hhmm = now.strftime("%H:%M")
        if self.p.session_start <= hhmm <= self.p.session_end:
            return True
        if self.p.use_evening and self.p.evening_start <= hhmm <= self.p.evening_end:
            return True
        return False
