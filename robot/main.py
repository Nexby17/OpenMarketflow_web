"""Main robot — broker-position polling at 300ms, broker = source of truth."""
import logging
import os
import signal
import sys
import time
import threading
import threading
import time
from datetime import datetime, timezone, timedelta

from FinamPy import FinamPy

import config
from feed import Feed, Quote, Bar, OrderEvent, TradeEvent
from vp import VolumeProfile
from strategy import Strategy, StrategyParams, BUY, SELL, GridLevel, Signal
from orders import OrderManager, PlacedOrder
from state import StateManager
from risk import RiskManager

log = logging.getLogger("robot")

MSK = timezone(timedelta(hours=3))


class Robot:
    def __init__(self, paper: bool = False):
        self.fp: FinamPy | None = None
        self.feed = Feed()
        self.vp = VolumeProfile(lookback=33, bin_size=50, va_percent=0.70)
        self.strategy = Strategy(StrategyParams())
        self.orders: OrderManager | None = None
        self.state = StateManager("/tmp/robot-state.json")
        self.risk = RiskManager()

        self._paper = paper
        self._running = False
        self._mode = "stopped"

        # Broker position state (previous tick)
        self._broker_dir: int = 0
        self._broker_lots: int = 0
        self._broker_avg: float = 0.0

        # Order tracking
        self._grid_order_id: str | None = None   # only one grid at a time
        self._tp_order_id: str | None = None      # only one TP at a time
        self._poc_tp_order_id: str | None = None

        # Entry pending
        self._entry_pending = False
        self._entry_pending_dir: int = 0
        self._entry_pending_since: datetime | None = None

        # Close cooldown
        self._last_close_time: datetime | None = None

        # Last entry for recovery
        self._last_entry_price = 0.0
        self._last_direction = 0

        # Price log
        self._last_price_log: datetime = datetime.now(MSK) - timedelta(minutes=1)

        # Current price (from quotes)
        self._current_price: float = 0.0

    # === LIFECYCLE ===

    def start(self):
        log.info(f"Starting robot{' [PAPER MODE]' if self._paper else ''}...")
        self.fp = FinamPy(config.FINAM_TOKEN)
        self.orders = OrderManager(self.fp)
        self.feed.connect()

        # Restore state
        s = self.state.load()
        if s.direction != 0 and s.entry_price > 0:
            self.strategy.direction = s.direction
            self.strategy.entry_price = s.entry_price
            if s.grid_levels:
                self.strategy.restore_grid(s.grid_levels)
                log.info(f"Grid restored: {len(s.grid_levels)} levels")
            if s.entry_time:
                try:
                    self.strategy.entry_time = datetime.fromisoformat(s.entry_time)
                except:
                    pass
            self._last_entry_price = s.last_entry_price
            self._last_direction = s.last_direction
            log.info(f"State restored: dir={s.direction} entry={s.entry_price:.0f} levels={len(s.grid_levels)}")
        elif s.direction != 0 and s.entry_price <= 0:
            log.warning("Corrupt state — resetting")
            self.state.clear()

        self._warmup_vp()

        # Wire callbacks (only for price/VP, NOT for trade logic)
        self.feed.on_quote = self._on_quote
        self.feed.on_bar = self._on_bar
        self.feed._on_stale = self._on_stale_streams
        # Ignore gRPC order/trade events — broker polling is our truth
        self.feed.on_order = lambda evt: None
        self.feed.on_trade = lambda evt: None

        self.feed.subscribe_all()

        # Initial broker sync — and set up orders if we have position
        self._poll_broker_position()

        if self._broker_lots > 0:
            # We have position at broker — make sure we have grid levels and orders
            if not self.strategy.has_position:
                # No state — restore from broker
                entry = self._broker_avg if self._broker_avg > 0 else self._current_price
                log.info(f"Restoring position from broker: dir={self._broker_dir} lots={self._broker_lots} @ {entry:.0f}")
                self.strategy.direction = self._broker_dir
                self.strategy.entry_price = entry
                self.strategy.entry_time = datetime.now(MSK)
                self._last_entry_price = entry
                self._last_direction = self._broker_dir

                # Create grid levels for filled lots (broker_lots - 1 = grid fills)
                grid_fills = self._broker_lots - 1
                step = self.strategy.params.step_base
                spread = self.strategy.params.spread_base
                for i in range(1, grid_fills + 1):
                    if self._broker_dir == 1:
                        gp = entry - step * i
                        tp = gp + spread
                    else:
                        gp = entry + step * i
                        tp = gp - spread
                    self.strategy.grid_levels.append(GridLevel(
                        level=i, price=gp,
                        side=BUY if self._broker_dir == 1 else SELL,
                        status="FILLED", tp_price=tp, tp_closed_price=0,
                    ))

                # Place next grid level
                next_level = len(self.strategy.grid_levels) + 1
                if next_level <= self.strategy.params.max_levels:
                    grid_sig = self.strategy._create_grid_signal(next_level)
                    if grid_sig:
                        self._place_grid(grid_sig)

            # ALWAYS ensure orders are placed — reset IDs to force re-creation
            self._grid_order_id = None
            self._tp_order_id = None
            self._poc_tp_order_id = None
            self._ensure_orders()
            self._save_state()

        self._running = True
        self._initialized = True  # Allow tick processing after full startup
        self._mode = "running"
        self.state.state.mode = "running"
        self.state.save()
        log.info(f"Robot started. VP: VAL={self.strategy.val:.0f} VAH={self.strategy.vah:.0f} POC={self.strategy.poc:.0f}")

        # Start 300ms polling loop
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def stop(self, close_position: bool = False):
        log.info("Stopping robot...")
        self._running = False
        self._mode = "stopped"

        if close_position and self.strategy.has_position and self.orders:
            self._close_all("Manual stop")
        else:
            # Don't cancel orders — just stop managing them
            log.info("Stopping without closing position")

        self.feed.disconnect()
        if self.fp:
            self.fp.close_channel()

        self.state.state.mode = "stopped"
        self.state.save()
        log.info("Robot stopped")

    def pause(self):
        self._mode = "paused"
        if self.orders:
            self._cancel_all_orders()
        log.info("Robot paused")

    def resume(self):
        self._mode = "running"
        self._poll_broker_position()
        # Restore grid/TP orders if we have a position
        if self.strategy.has_position and self.orders:
            # Restore POC-TP for entry lot
            if not self._poc_tp_order_id and self.strategy.poc > 0 and self.strategy.filled_levels == 0:
                poc = self.strategy.poc
                side = SELL if self.strategy.direction == 1 else BUY
                entry = self.strategy.entry_price
                if (self.strategy.direction == 1 and poc > entry) or (self.strategy.direction == -1 and poc < entry):
                    po = self.orders.place_limit(side, 1, poc, "POC-TP")
                    if po:
                        self._poc_tp_order_id = po.order_id
                        log.info(f"POC-TP restored @ {poc:.0f}")
            # Restore pending grid
            pending = self.strategy.get_active_grid()
            if pending and not self._grid_order_id:
                grid_side = SELL if self.strategy.direction == -1 else BUY
                po = self.orders.place_limit(grid_side, 1, pending.price, f"GRID-{pending.level}")
                if po:
                    self._grid_order_id = po.order_id
                    log.info(f"GRID-{pending.level} restored @ {pending.price:.0f}")
            # Restore TP for last filled level
            if self.strategy.filled_levels > 0 and not self._tp_order_id:
                self._place_single_tp()
        log.info("Robot resumed")

    # === 300MS POLL LOOP ===

    def _poll_loop(self):
        """Main loop: poll broker position every 300ms."""
        while self._running:
            try:
                if self._mode == "running" and not self._paper:
                    self._tick()
            except Exception as e:
                log.error(f"Poll tick error: {e}")
            time.sleep(0.3)

    def _tick(self):
        """One polling cycle. Compare broker position with expected state."""
        if not getattr(self, '_initialized', False):
            return  # Skip ticks until fully initialized

        pos = self._get_broker_position()
        prev_dir = self._broker_dir
        prev_lots = self._broker_lots

        # pos=None means gRPC error — DON'T change state, skip this tick
        if pos is None:
            return

        self._broker_dir, self._broker_lots, self._broker_avg = pos

        cur_dir = self._broker_dir
        cur_lots = self._broker_lots

        # === NO POSITION at broker ===
        if cur_lots == 0:
            if self.strategy.has_position:
                # Broker closed our position (TP or stop hit) — we didn't initiate
                log.info(f"Broker position gone (was {prev_lots}). Syncing close.")
                self._handle_broker_close()
            elif self._entry_pending:
                # Entry order still pending, check timeout
                if self._entry_pending_since:
                    elapsed = (datetime.now(MSK) - self._entry_pending_since).total_seconds()
                    if elapsed > 10:
                        log.warning(f"Entry pending {elapsed:.0f}s, no broker position — resetting")
                        self._entry_pending = False
            elif not self._entry_pending:
                # Check for new entry signal
                self._check_entry_signal()
            return

        # === POSITION exists at broker ===
        if not self.strategy.has_position:
            # Robot doesn't know about position — restore from broker
            if self._entry_pending and self._entry_pending_dir == cur_dir:
                # This is our entry fill
                entry_price = self._broker_avg if self._broker_avg > 0 else self._current_price
                log.info(f"Entry fill detected: dir={cur_dir} lots={cur_lots} @ {entry_price:.0f}")
                self._handle_entry_fill(cur_dir, entry_price, cur_lots)
            elif self._last_close_time and (datetime.now(MSK) - self._last_close_time).total_seconds() < 1:
                # Just closed — ignore ghost position
                log.info(f"Ignoring ghost position (closed {self._last_close_time.strftime('%H:%M:%S')}): dir={cur_dir} lots={cur_lots}")
                return
            else:
                # Orphan position — restore
                log.warning(f"Orphan position: dir={cur_dir} lots={cur_lots} — restoring")
                self._handle_entry_fill(cur_dir, self._broker_avg if self._broker_avg > 0 else self._current_price, cur_lots)
            return

        # === Direction mismatch ===
        if self.strategy.direction != cur_dir:
            log.warning(f"Dir mismatch! Robot={self.strategy.direction} Broker={cur_dir}")
            self._close_all("Dir mismatch")
            return

        # === Same direction, check for lot changes ===
        robot_lots = self.strategy.total_lots

        if cur_lots > robot_lots:
            # More lots at broker = grid fill
            delta = cur_lots - robot_lots
            log.info(f"Grid fill detected: +{delta} lots (robot={robot_lots} broker={cur_lots})")
            self._handle_grid_fill(delta)

        elif cur_lots < robot_lots:
            # Fewer lots at broker = TP fill or partial close
            delta = robot_lots - cur_lots
            if cur_lots == 0:
                log.info(f"Position closed at broker (was {robot_lots})")
                self._handle_broker_close()
            else:
                log.info(f"TP fill detected: -{delta} lots (robot={robot_lots} broker={cur_lots})")
                self._handle_tp_fill(delta)

        # Check exits for remaining position (ensure orders every 10s)
        if self.strategy.has_position and cur_lots > 0:
            now = time.time()
            if not hasattr(self, '_last_ensure') or now - self._last_ensure > 10:
                self._ensure_orders()
                self._last_ensure = now
            self._check_exits()

    # === ENTRY ===

    def _check_entry_signal(self):
        """Check if we should enter."""
        price = self._current_price
        if price <= 0:
            return
        sig = self.strategy.check_entry(price)
        if not sig:
            return

        # Execute entry
        side = BUY if sig.direction == 1 else SELL
        tag = f"ENTRY-{'LONG' if sig.direction == 1 else 'SHORT'}"
        po = self.orders.place_market(side, 1, tag)
        if po:
            self._entry_pending = True
            self._entry_pending_dir = sig.direction
            self._entry_pending_since = datetime.now(MSK)
            log.info(f"Entry order sent: {tag} @ market")
        else:
            log.warning("Entry order failed")

    def _handle_entry_fill(self, direction: int, price: float, broker_lots: int):
        """Entry confirmed by broker. Set up grid + TP."""
        signals = self.strategy.on_entry_fill(direction, price)
        self._entry_pending = False
        self._last_entry_price = price
        self._last_direction = direction

        for sig in signals:
            if sig.action == "GRID":
                self._place_grid(sig)
            elif sig.action == "PLACE_POC_TP":
                self._place_poc_tp(sig)

        # If broker has more lots than 1, handle grid fills too
        if broker_lots > 1:
            delta = broker_lots - 1
            log.info(f"Entry fill with {delta} extra lots from grid")
            self._handle_grid_fill(delta)

        self._save_state()

    # === GRID ===

    def _handle_grid_fill(self, count: int):
        """Grid fills detected. Mark them and place next grid + TP."""
        for i in range(count):
            # Find the PENDING grid level and mark as FILLED
            filled = None
            for g in self.strategy.grid_levels:
                if g.status == "PENDING":
                    g.status = "FILLED"
                    filled = g
                    break

            if not filled:
                log.warning(f"No pending grid level for fill #{i+1}")
                continue

            # Set TP price for this level
            spread = self.strategy.params.spread_base
            if self.strategy.direction == 1:
                filled.tp_price = filled.price + spread
            else:
                filled.tp_price = filled.price - spread

            # Place next PENDING grid level
            for g in self.strategy.grid_levels:
                if g.status == "PENDING" and g.level > filled.level:
                    self._place_grid(Signal(
                        action="GRID", direction=g.side, price=g.price,
                        quantity=1, level=g.level, tag=f"Grid-{g.level}",
                    ))
                    break  # one at a time

        # Place ONE TP for the last filled level
        self._place_single_tp()
        # Always ensure all orders are in place
        self._ensure_orders()
        self._save_state()

    def _place_grid(self, sig):
        """Place grid limit order."""
        if self._paper or not self.orders:
            log.info(f"[PAPER] GRID-{sig.level} @ {sig.price:.0f}")
            return
        # Cancel previous pending grid
        if self._grid_order_id:
            self.orders.cancel(self._grid_order_id)
            self._grid_order_id = None
        po = self.orders.place_limit(sig.direction, sig.quantity, sig.price, f"GRID-{sig.level}")
        if po:
            self._grid_order_id = po.order_id
            log.info(f"GRID-{sig.level} placed @ {sig.price:.0f} id={po.order_id}")
        else:
            log.warning(f"GRID-{sig.level} failed")

    # === TP ===

    def _cancel_grid(self):
        if self._grid_order_id and self.orders:
            self.orders.cancel(self._grid_order_id)
            self._grid_order_id = None

    def _cancel_tp(self):
        if self._tp_order_id and self.orders:
            self.orders.cancel(self._tp_order_id)
            self._tp_order_id = None

    def _place_next_grid(self):
        """Place ONE grid at the next PENDING level."""
        if self._current_price <= 0:
            return
        for g in self.strategy.grid_levels:
            if g.status == "PENDING":
                # Guard: grid must not fill instantly
                if self.strategy.direction == -1 and g.price <= self._current_price:
                    continue
                if self.strategy.direction == 1 and g.price >= self._current_price:
                    continue
                grid_side = SELL if self.strategy.direction == -1 else BUY
                po = self.orders.place_limit(grid_side, 1, g.price, f"GRID-{g.level}")
                if po:
                    self._grid_order_id = po.order_id
                    log.info(f"GRID-{g.level} placed @ {g.price:.0f}")
                return
        log.info("No more PENDING grid levels")

    def _place_single_tp(self):
        """Place one TP for the latest filled grid level. Cancel previous TP."""
        if not self.orders:
            return

        # Cancel previous TP
        if self._tp_order_id:
            self.orders.cancel(self._tp_order_id)
            self._tp_order_id = None

        # Find last filled level with TP price
        tp_level = None
        for g in reversed(self.strategy.grid_levels):
            if g.status == "FILLED" and g.tp_price > 0:
                tp_level = g
                break

        if not tp_level:
            return

        tp_side = SELL if self.strategy.direction == 1 else BUY
        po = self.orders.place_limit(tp_side, 1, tp_level.tp_price, f"TP-{tp_level.level}")
        if po:
            self._tp_order_id = po.order_id
            log.info(f"TP-{tp_level.level} placed @ {tp_level.tp_price:.0f} id={po.order_id}")
        else:
            log.warning(f"TP-{tp_level.level} failed")

    def _place_poc_tp(self, sig):
        """Place POC-TP for entry lot."""
        if self._paper or not self.orders:
            log.info(f"[PAPER] POC-TP @ {sig.price:.0f}")
            return
        if self._poc_tp_order_id:
            self.orders.cancel(self._poc_tp_order_id)
            self._poc_tp_order_id = None
        po = self.orders.place_limit(sig.direction, sig.quantity, sig.price, "POC-TP")
        if po:
            self._poc_tp_order_id = po.order_id
            log.info(f"POC-TP placed @ {sig.price:.0f} id={po.order_id}")

    def _handle_tp_fill(self, count: int):
        """TP fills detected. Mark CLOSED, place next grid + TP."""
        for i in range(count):
            # Find first FILLED level with TP and mark CLOSED
            for g in self.strategy.grid_levels:
                if g.status == "FILLED" and g.tp_price > 0:
                    g.tp_closed_price = g.tp_price
                    g.status = "CLOSED"
                    log.info(f"TP-{g.level} filled @ {g.tp_price:.0f}")
                    break

        self._robot_lots = self._broker_lots

        # Cancel old TP, place new for last filled
        self._cancel_tp()
        self._place_single_tp()

        # Place next PENDING grid (no re-use!)
        self._cancel_grid()
        self._place_next_grid()

        # If no filled levels left but still has position (entry lot only)
        if self.strategy.has_position and self.strategy.filled_levels == 0:
            if not self._poc_tp_order_id and self.strategy.poc > 0:
                poc = self.strategy.poc
                side = SELL if self.strategy.direction == 1 else BUY
                entry = self.strategy.entry_price
                if (self.strategy.direction == 1 and poc > entry) or (self.strategy.direction == -1 and poc < entry):
                    po = self.orders.place_limit(side, 1, poc, "POC-TP")
                    if po:
                        self._poc_tp_order_id = po.order_id
                        log.info(f"POC-TP restored @ {poc:.0f}")

        self._save_state()

    def _handle_broker_close(self):
        """Broker reports 0 lots. Close everything in robot state."""
        if self.strategy.has_position and self.strategy.entry_price > 0:
            pnl = self.strategy.calc_unrealized_pnl(self._current_price)
            self.state.state.round_trips += 1
            self.state.state.realized_pnl += pnl
            dir_str = "LONG" if self.strategy.direction == 1 else "SHORT"
            log.info(f"Round trip #{self.state.state.round_trips}: {dir_str} entry={self.strategy.entry_price:.0f} PnL={pnl:.0f} | Total: RT={self.state.state.round_trips} PnL={self.state.state.realized_pnl:.0f}")

        self.strategy.on_close_all()
        self._cancel_all_orders()
        self._reset_tracked()
        self._last_close_time = datetime.now(MSK)
        self._save_state()

    # === EXITS ===

    def _check_exits(self):
        """Check exit conditions based on current state."""
        price = self._current_price
        if price <= 0:
            return

        # Risk check
        pnl = self.strategy.calc_unrealized_pnl(price)
        ok, msg = self.risk.check_pnl(pnl)
        if not ok:
            log.warning(f"Risk stop: {msg}")
            self._close_all(msg)
            return

        # Strategy exit check
        self.strategy.current_price = price
        sig = self.strategy.check_exit()
        if sig:
            self._close_all(sig.tag)

    def _close_all(self, reason: str):
        """Close all positions — use BROKER lots, not robot state."""
        # Get actual broker lots
        pos = self._get_broker_position()
        broker_lots = pos[1] if pos else 0
        broker_dir = pos[0] if pos else self.strategy.direction

        pnl = 0
        if self.strategy.has_position and self.strategy.entry_price > 0:
            pnl = self.strategy.calc_unrealized_pnl(self._current_price)
            log.info(f"CLOSE ALL: {reason} | PnL={pnl:.0f} | broker_lots={broker_lots}")

        if not self._paper and self.orders:
            # Cancel all orders first
            self._cancel_all_orders()
            # Close position with BROKER lots
            if broker_lots > 0:
                close_side = SELL if broker_dir == 1 else BUY
                self.orders.place_market(close_side, broker_lots, f"CLOSE-{reason}")

        # Stats
        if self.strategy.has_position and self.strategy.entry_price > 0:
            self.state.state.round_trips += 1
            self.state.state.realized_pnl += pnl
            dir_str = "LONG" if self.strategy.direction == 1 else "SHORT"
            log.info(f"Round trip #{self.state.state.round_trips}: {dir_str} entry={self.strategy.entry_price:.0f} PnL={pnl:.0f} | Total: RT={self.state.state.round_trips} PnL={self.state.state.realized_pnl:.0f}")

        self.strategy.on_close_all()
        self._reset_tracked()
        self._last_close_time = datetime.now(MSK)
        self._save_state()

    # === CALLBACKS (price/VP only) ===

    def _on_stale_streams(self):
        log.warning("Reconnecting stale streams...")
        self.feed.disconnect()
        time.sleep(2)
        self.feed.connect()
        self._warmup_vp()
        self.feed.subscribe_all()
        log.info(f"Reconnected. VP: VAL={self.strategy.val:.0f} VAH={self.strategy.vah:.0f} POC={self.strategy.poc:.0f}")

    def _on_quote(self, q: Quote):
        """Quote callback — just update price."""
        self._current_price = q.last
        self.strategy.current_price = q.last

        # Periodic price log
        if (datetime.now(MSK) - self._last_price_log).seconds >= 30:
            self._last_price_log = datetime.now(MSK)
            log.info(f"Price: {q.last:.0f} | VP: VAL={self.strategy.val:.0f} VAH={self.strategy.vah:.0f} POC={self.strategy.poc:.0f}")

    def _on_bar(self, b: Bar):
        """Bar callback — update VP."""
        if not self._running or self._mode != "running":
            return

        self.vp.add_bar(b.close, b.volume)
        result = self.vp.calculate()
        if result:
            old_poc = self.strategy.poc
            self.strategy.poc = result.poc
            self.strategy.vah = result.vah
            self.strategy.val = result.val
            # Move POC-TP if POC changed
            if (self.strategy.has_position and self._poc_tp_order_id
                    and old_poc > 0 and abs(result.poc - old_poc) >= 1):
                self._move_poc_tp(result.poc)

    def _move_poc_tp(self, new_poc: float):
        if not self._poc_tp_order_id or not self.orders:
            return
        d = self.strategy.direction
        if d == 0:
            return
        if d == 1 and new_poc <= self.strategy.entry_price:
            return
        if d == -1 and new_poc >= self.strategy.entry_price:
            return
        self.orders.cancel(self._poc_tp_order_id)
        side = SELL if d == 1 else BUY
        po = self.orders.place_limit(side, 1, new_poc, "POC-TP")
        if po:
            self._poc_tp_order_id = po.order_id
            log.info(f"POC-TP moved to {new_poc:.0f}")

    # === BROKER ===

    def _get_broker_position(self) -> tuple | None:
        """Get broker position via gRPC. Returns (dir, lots, avg) or None on ERROR.
        Returns (0, 0, 0.0) if successfully queried but no position."""
        if not self.fp:
            return None
        try:
            from FinamPy.grpc.accounts_service_pb2 import GetAccountRequest
            account = self.fp.call_function(
                self.fp.accounts_stub.GetAccount,
                GetAccountRequest(account_id=config.FINAM_ACCOUNT_ID),
            )
            if not account:
                return None  # error, not empty
            for pos in account.positions:
                if config.SYMBOL in pos.symbol or config.TICKER in pos.symbol:
                    qty = float(pos.quantity.value or '0') if pos.quantity else 0
                    avg = float(pos.average_price.value or '0') if pos.average_price else 0
                    if qty > 0:
                        return (1, int(qty), avg)
                    elif qty < 0:
                        return (-1, int(abs(qty)), avg)
            return (0, 0, 0.0)  # successfully queried, no position
        except Exception as e:
            log.error(f"Broker position error: {e}")
            return None  # error — don't change state

    def _poll_broker_position(self):
        """Initial broker position load."""
        pos = self._get_broker_position()
        if pos:
            self._broker_dir, self._broker_lots, self._broker_avg = pos
        else:
            self._broker_dir = 0
            self._broker_lots = 0
            self._broker_avg = 0.0

    # === HELPERS ===

    def _cancel_all_orders(self):
        """Cancel all tracked orders + fallback broker cancel."""
        if not self.orders:
            return
        for oid in [self._grid_order_id, self._tp_order_id, self._poc_tp_order_id]:
            if oid:
                self.orders.cancel(oid)
        try:
            active = self.orders.get_active_orders()
            for o in active:
                if o.status == "ACTIVE":
                    self.orders.cancel(o.order_id)
        except:
            pass

    def _reset_tracked(self):
        self._grid_order_id = None
        self._tp_order_id = None
        self._poc_tp_order_id = None

    def _warmup_vp(self):
        if not self.fp:
            return
        try:
            from google.protobuf.timestamp_pb2 import Timestamp
            from google.type.interval_pb2 import Interval
            import FinamPy.grpc.marketdata_service_pb2 as md_pb2

            finam_tf, _, _ = self.fp.timeframe_to_finam_timeframe(config.TIMEFRAME)
            now = datetime.now(timezone.utc)
            start = now - timedelta(hours=3)

            resp = self.fp.call_function(
                self.fp.marketdata_stub.Bars,
                md_pb2.BarsRequest(
                    symbol=config.SYMBOL,
                    timeframe=finam_tf,
                    interval=Interval(
                        start_time=Timestamp(seconds=int(start.timestamp())),
                        end_time=Timestamp(seconds=int(now.timestamp())),
                    ),
                ),
            )
            if resp and resp.bars:
                for bar in resp.bars[-config.WARMUP_BARS:]:
                    self.vp.add_bar(float(bar.close.value), float(bar.volume.value))
                result = self.vp.calculate()
                if result:
                    self.strategy.poc = result.poc
                    self.strategy.vah = result.vah
                    self.strategy.val = result.val
                    log.info(f"Warmup: {len(list(resp.bars))} bars, VAL={result.val:.0f} VAH={result.vah:.0f} POC={result.poc:.0f}")
        except Exception as e:
            log.error(f"Warmup error: {e}")

    def _ensure_orders(self):
        """Check if tracked orders are still active. Don't place new ones — let _tick handle fills."""
        if not self.strategy.has_position or not self.orders:
            return

        # Check if our tracked orders are still active at broker
        active_ids = set()
        try:
            from FinamPy.grpc.orders_service_pb2 import OrdersRequest
            resp = self.orders.fp.call_function(
                self.orders.fp.orders_stub.GetOrders,
                OrdersRequest(account_id=config.FINAM_ACCOUNT_ID)
            )
            for o in resp.orders:
                if o.status == 1:  # NEW/active
                    active_ids.add(o.order_id)
        except:
            return  # Error — don't clear any IDs

        # Only clear IDs if order is gone — but DON'T place new ones
        # Next tick will detect the fill and handle properly
        if self._grid_order_id and self._grid_order_id not in active_ids:
            self._grid_order_id = None
        if self._tp_order_id and self._tp_order_id not in active_ids:
            self._tp_order_id = None
        if self._poc_tp_order_id and self._poc_tp_order_id not in active_ids:
            self._poc_tp_order_id = None

        # Only place missing orders if NO fills are pending
        # (broker_lots == robot_lots means no fills to process)
        if self._broker_lots != self._robot_lots:
            return  # Fills pending — don't place orders, let _tick handle

        if not self._grid_order_id:
            for g in self.strategy.grid_levels:
                if g.status == "PENDING":
                    if self.strategy.direction == -1 and g.price <= (self._current_price or 999999):
                        continue
                    if self.strategy.direction == 1 and g.price >= (self._current_price or 0):
                        continue
                    grid_side = SELL if self.strategy.direction == -1 else BUY
                    po = self.orders.place_limit(grid_side, 1, g.price, f"GRID-{g.level}")
                    if po:
                        self._grid_order_id = po.order_id
                        log.info(f"Ensured grid: GRID-{g.level} @ {g.price:.0f}")
                    break

        has_filled_with_tp = any(g.status == "FILLED" and g.tp_price > 0 for g in self.strategy.grid_levels)
        if has_filled_with_tp and not self._tp_order_id:
            self._place_single_tp()

        if self.strategy.filled_levels == 0 and not self._poc_tp_order_id and self.strategy.poc > 0:
            poc = self.strategy.poc
            side = SELL if self.strategy.direction == 1 else BUY
            if (self.strategy.direction == 1 and poc > self.strategy.entry_price) or (self.strategy.direction == -1 and poc < self.strategy.entry_price):
                po = self.orders.place_limit(side, 1, poc, "POC-TP")
                if po:
                    self._poc_tp_order_id = po.order_id
                    log.info(f"Ensured POC-TP @ {poc:.0f}")

    def _save_state(self):
        s = self.state.state
        s.direction = self.strategy.direction
        s.entry_price = self.strategy.entry_price
        s.filled_levels = self.strategy.filled_levels
        s.entry_time = self.strategy.entry_time.isoformat() if self.strategy.entry_time else ""
        s.grid_levels = self.strategy.serialize_grid()
        s.last_entry_price = self._last_entry_price
        s.last_direction = self._last_direction
        self.state.save()

    def get_status(self) -> dict:
        pnl = self.strategy.calc_unrealized_pnl(self.strategy.current_price) if self.strategy.has_position else 0
        return {
            "mode": self._mode,
            "paper": self._paper,
            "direction": self.strategy.direction,
            "dir_str": "LONG" if self.strategy.direction == 1 else "SHORT" if self.strategy.direction == -1 else "FLAT",
            "entry_price": self.strategy.entry_price,
            "total_lots": self.strategy.total_lots,
            "filled_levels": self.strategy.filled_levels,
            "grid_levels": len(self.strategy.grid_levels),
            "pnl": round(pnl, 1),
            "round_trips": self.state.state.round_trips,
            "realized_pnl": round(self.state.state.realized_pnl, 1),
            "poc": round(self.strategy.poc, 0),
            "vah": round(self.strategy.vah, 0),
            "val": round(self.strategy.val, 0),
            "current_price": self.strategy.current_price,
            "hold_minutes": self.strategy.hold_minutes,
            "connected": self.feed.connected,
        }


# === ENTRY POINT ===

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    robot = Robot(paper=args.paper)

    def shutdown(sig, frame):
        log.info("Shutdown signal received")
        robot.stop(close_position=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    import uvicorn
    from api import app, set_robot
    set_robot(robot)
    api_thread = threading.Thread(
        target=lambda: uvicorn.run(app, host="0.0.0.0", port=5070, log_level="warning"),
        daemon=True,
    )
    api_thread.start()
    log.info("API server started on port 5070")

    robot.start()

    try:
        while robot._running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    robot.stop(close_position=False)


if __name__ == "__main__":
    main()
