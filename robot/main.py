"""Main robot — broker-position polling at 300ms, broker = source of truth."""
import logging
import os
import signal
import sys
import time
import threading
import requests
from datetime import datetime, timezone, timedelta

from FinamPy import FinamPy

import config
from feed import Feed, Quote, Bar, OrderEvent, TradeEvent
from vp import VolumeProfile
from strategy import Strategy, StrategyParams, BUY, SELL
from orders_dp import OrderManager, PlacedOrder
from state import StateManager
from risk import RiskManager

log = logging.getLogger("robot")

MSK = timezone(timedelta(hours=3))


class Robot:
    def __init__(self, paper: bool = False):
        # gRPC only for warmup
        self.fp: FinamPy | None = None
        self.feed = Feed()
        self.strategy = Strategy(StrategyParams())
        self.vp = VolumeProfile(lookback=self.strategy.params.vp_lookback, bin_size=50, va_percent=0.70)
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
        self._entry_pending_price: float = 0  # price at entry signal time
        self._entry_pending_since: datetime | None = None

        # Close cooldown
        self._last_close_time: datetime | None = None
        self._close_pending: bool = False

        # Last entry for recovery
        self._last_entry_price = 0.0
        self._last_direction = 0

        # Price log
        self._last_price_log: datetime = datetime.now(MSK) - timedelta(minutes=1)
        self._last_price: float = 0
        self._last_price_change: datetime = datetime.now(MSK) - timedelta(minutes=5)

        self._moex_last: float = 0  # last MOEX price for sanity check
        self._moex_check_ts: float = 0  # last MOEX check time

        # Zigzag grid state
        self._filled_prices: list[float] = []  # grid fill prices (sorted)
        self._current_grid_price: float = 0  # pending grid price
        self._current_tp_price: float = 0  # active TP price

        # Current price (from quotes)
        self._current_price: float = 0.0

    # === LIFECYCLE ===

    def start(self):
        log.info(f"Starting robot{' [PAPER MODE]' if self._paper else ''}...")

        # Load saved config
        self._load_config()

        self.orders = OrderManager(dp_url="http://localhost:5060", account=config.FINAM_ACCOUNT_ID, symbol=config.SYMBOL)
        self.feed.connect()

        # Restore state
        s = self.state.load()
        if s.direction != 0 and s.entry_price > 0:
            self.strategy.direction = s.direction
            self.strategy.entry_price = s.entry_price
            # Restore filled_prices from state
            if s.grid_levels and isinstance(s.grid_levels, list):
                if len(s.grid_levels) > 0 and isinstance(s.grid_levels[0], (int, float)):
                    self._filled_prices = sorted([float(x) for x in s.grid_levels])
                else:
                    # Old format (list of dicts) — extract prices
                    self._filled_prices = sorted([float(g.get('price', 0)) for g in s.grid_levels if g.get('status') == 'FILLED'])
            if s.entry_time:
                try:
                    self.strategy.entry_time = datetime.fromisoformat(s.entry_time)
                except:
                    pass
            self._last_entry_price = s.last_entry_price
            self._last_direction = s.last_direction
            log.info(f"State restored: dir={s.direction} entry={s.entry_price:.0f} fills={self._filled_prices}")
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

                # Create filled_prices for grid fills (broker_lots - 1 = grid fills)
                grid_fills = self._broker_lots - 1
                step = self.strategy.params.step_base
                for i in range(1, grid_fills + 1):
                    if self._broker_dir == 1:
                        gp = entry - step * i
                    else:
                        gp = entry + step * i
                    self._filled_prices.append(gp)
                self._filled_prices.sort()

                # Place next grid + TP
                if self._filled_prices:
                    spread = self.strategy.params.spread_base
                    if self._broker_dir == 1:
                        tp_price = self._filled_prices[0] + spread
                        grid_price = self._filled_prices[0] - step
                        grid_side = BUY
                        tp_side = SELL
                    else:
                        tp_price = self._filled_prices[-1] - spread
                        grid_price = self._filled_prices[-1] + step
                        grid_side = SELL
                        tp_side = BUY
                    po = self.orders.place_limit(tp_side, 1, tp_price, "TP")
                    if po:
                        self._tp_order_id = po.order_id
                    po = self.orders.place_limit(grid_side, 1, grid_price, "GRID")
                    if po:
                        self._grid_order_id = po.order_id
                else:
                    # Entry lot only — place first grid + POC-TP
                    if self._broker_dir == 1:
                        gp = entry - step
                        gs = BUY
                    else:
                        gp = entry + step
                        gs = SELL
                    po = self.orders.place_limit(gs, 1, gp, "GRID")
                    if po:
                        self._grid_order_id = po.order_id

            # ALWAYS ensure orders are placed — reset IDs to force re-creation
            self._grid_order_id = None
            self._tp_order_id = None
            self._poc_tp_order_id = None
            self._save_state()

        self._running = True

        # Reset VP — will be recalculated by warmup
        self.strategy.val = 0
        self.strategy.vah = 0
        self.strategy.poc = 0

        # Start immediately, validate price + warmup VP in background
        self._initialized = True
        self._mode = "running"
        self.state.state.mode = "running"
        self.state.save()
        log.info(f"Robot started (VP loading in background)")

        # Background: validate price + warmup VP
        def _bg_startup():
            self._validate_price()
            self._warmup_vp()
            log.info(f"VP ready: VAL={self.strategy.val:.0f} VAH={self.strategy.vah:.0f} POC={self.strategy.poc:.0f}")
        threading.Thread(target=_bg_startup, daemon=True).start()

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
        # Close warmup connection if exists
        if self.fp:
            try: self.fp.close_channel()
            except: pass

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
        if self.strategy.direction != 0 and self.orders:
            d = self.strategy.direction
            entry = self.strategy.entry_price
            step = self.strategy.params.step_base
            spread = self.strategy.params.spread_base

            if self._filled_prices:
                # Have grid fills — place TP + grid
                if d == 1:
                    tp_price = self._filled_prices[0] + spread
                    grid_price = self._filled_prices[0] - step
                    tp_side = SELL
                    grid_side = BUY
                else:
                    tp_price = self._filled_prices[-1] - spread
                    grid_price = self._filled_prices[-1] + step
                    tp_side = BUY
                    grid_side = SELL

                po = self.orders.place_limit(tp_side, 1, tp_price, "TP")
                if po:
                    self._tp_order_id = po.order_id
                    log.info(f"TP restored @ {tp_price:.0f}")
                po = self.orders.place_limit(grid_side, 1, grid_price, "GRID")
                if po:
                    self._grid_order_id = po.order_id
                    log.info(f"GRID restored @ {grid_price:.0f}")
            else:
                # Entry lot only — POC-TP + first grid
                poc = self.strategy.poc
                if poc > 0:
                    if d == 1 and poc > entry:
                        po = self.orders.place_limit(SELL, 1, poc, "POC-TP")
                        if po:
                            self._poc_tp_order_id = po.order_id
                            log.info(f"POC-TP restored @ {poc:.0f}")
                    elif d == -1 and poc < entry:
                        po = self.orders.place_limit(BUY, 1, poc, "POC-TP")
                        if po:
                            self._poc_tp_order_id = po.order_id
                            log.info(f"POC-TP restored @ {poc:.0f}")

                grid_price = entry - step if d == 1 else entry + step
                grid_side = BUY if d == 1 else SELL
                po = self.orders.place_limit(grid_side, 1, grid_price, "GRID")
                if po:
                    self._grid_order_id = po.order_id
                    log.info(f"GRID restored @ {grid_price:.0f}")
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
            time.sleep(0.5)

    def _tick(self):
        """One polling cycle. Compare broker position with expected state."""
        if not getattr(self, '_initialized', False):
            return  # Skip ticks until fully initialized

        # Periodic MOEX sanity check (every 60s)
        now_ts = time.time()
        if now_ts - self._moex_check_ts > 60:
            moex = self._get_moex_price()
            if moex > 0:
                self._moex_last = moex
                self._moex_check_ts = now_ts

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
            if self._close_pending:
                self._close_pending = False
                log.info("Close confirmed by broker (lots=0)")
            if self.strategy.has_position:
                # Broker closed our position (TP or stop hit) — we didn't initiate
                log.info(f"Broker position gone (was {prev_lots}). Syncing close.")
                self._handle_broker_close()
            elif self._entry_pending:
                # Entry order still pending, check timeout
                if self._entry_pending_since:
                    elapsed = (datetime.now(MSK) - self._entry_pending_since).total_seconds()
                    if elapsed > 5:
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
                # This is our entry fill — use entry_pending_price (price at signal time)
                entry_price = self._entry_pending_price if self._entry_pending_price > 0 else self._broker_avg
                if entry_price <= 0:
                    entry_price = self._current_price
                log.info(f"Entry fill detected: dir={cur_dir} lots={cur_lots} @ {entry_price:.0f} (broker_avg={self._broker_avg:.0f})")
                self._handle_entry_fill(cur_dir, entry_price, cur_lots)
            elif self._close_pending:
                # Close sent but broker still shows position — wait
                if cur_lots == 0:
                    self._close_pending = False
                    log.info("Close confirmed by broker (lots=0)")
                else:
                    log.info(f"Waiting for close confirm: broker_lots={cur_lots}")
                return
            else:
                # Orphan position — restore
                log.warning(f"Orphan position: dir={cur_dir} lots={cur_lots} — restoring")
                ep = self._broker_avg if self._broker_avg > 0 else self._current_price
                self._handle_entry_fill(cur_dir, ep, cur_lots)
            return

        # === Direction mismatch ===
        if self.strategy.direction != cur_dir:
            log.warning(f"Dir mismatch! Robot={self.strategy.direction} Broker={cur_dir}")
            self._close_all("Dir mismatch")
            return

        # === Same direction, check for lot changes ===
        robot_lots = 1 + len(self._filled_prices)

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

        # Check exits for remaining position
        if self.strategy.direction != 0 and cur_lots > 0:
            self._check_exits()

    # === ENTRY ===

    def _check_entry_signal(self):
        """Check if we should enter."""
        price = self._current_price
        if price <= 0:
            return
        if self._close_pending:
            return
        # Don't enter until VP is ready (after warmup)
        if self.strategy.val <= 0 or self.strategy.vah <= 0:
            return
        # Frozen price guard — don't enter if price hasn't changed in 30s
        price_age = (datetime.now(MSK) - self._last_price_change).total_seconds()
        if price_age > 30:
            return
        sig = self.strategy.check_entry(price)
        if sig:
            log.info(f"ENTRY SIGNAL: {sig.tag} @ {price:.0f}")
        if not sig:
            return

        # Execute entry
        side = BUY if sig.direction == 1 else SELL
        tag = f"ENTRY-{'LONG' if sig.direction == 1 else 'SHORT'}"
        po = self.orders.place_market(side, 1, tag)
        if po:
            self._entry_pending = True
            self._entry_pending_dir = sig.direction
            self._entry_pending_price = price  # remember signal price
            self._entry_pending_since = datetime.now(MSK)
            log.info(f"Entry order sent: {tag} @ market")
        else:
            log.warning("Entry order failed")

    def _handle_entry_fill(self, direction: int, price: float, broker_lots: int):
        """Entry confirmed by broker. Set up first grid + POC-TP."""
        self.strategy.direction = direction
        self.strategy.entry_price = price
        self.strategy.entry_time = datetime.now(MSK)
        self.strategy.grid_levels = []  # no pre-created levels
        self._entry_pending = False
        self._last_entry_price = price
        self._last_direction = direction
        self._filled_prices = []

        # Place first grid
        step = self.strategy.params.step_base
        if direction == 1:
            grid_price = price - step
            grid_side = BUY
        else:
            grid_price = price + step
            grid_side = SELL

        self._current_grid_price = grid_price
        po = self.orders.place_limit(grid_side, 1, grid_price, "GRID-1")
        if po:
            self._grid_order_id = po.order_id
            log.info(f"GRID-1 placed @ {grid_price:.0f}")

        # POC-TP for entry lot
        poc = self.strategy.poc
        if poc > 0:
            if direction == 1 and poc > price:
                po = self.orders.place_limit(SELL, 1, poc, "POC-TP")
                if po:
                    self._poc_tp_order_id = po.order_id
                    log.info(f"POC-TP placed @ {poc:.0f}")
            elif direction == -1 and poc < price:
                po = self.orders.place_limit(BUY, 1, poc, "POC-TP")
                if po:
                    self._poc_tp_order_id = po.order_id
                    log.info(f"POC-TP placed @ {poc:.0f}")

        # If broker has more lots than 1, handle grid fills too
        if broker_lots > 1:
            delta = broker_lots - 1
            log.info(f"Entry fill with {delta} extra lots from grid")
            self._handle_grid_fill(delta)

        self._save_state()

    # === GRID ===

    def _handle_grid_fill(self, count: int):
        """Grid fills detected. Add to filled_prices, place next grid + TP (zigzag)."""
        d = self.strategy.direction
        entry = self.strategy.entry_price
        step = self.strategy.params.step_base
        spread = self.strategy.params.spread_base

        for i in range(count):
            # Calculate grid price based on current fill count
            fp = self._current_grid_price
            if fp <= 0:
                log.warning("No pending grid price for fill")
                break
            self._filled_prices.append(fp)
            self._filled_prices.sort()
            log.info(f"Grid filled @ {fp:.0f} (filled_prices: {self._filled_prices})")

            # Update _current_grid_price for next fill in batch
            if d == 1:
                next_g = self._filled_prices[0] - step
            else:
                next_g = self._filled_prices[-1] + step
            self._current_grid_price = next_g

        # Cancel old TP
        self._cancel_tp()
        # Cancel old grid
        self._cancel_grid()

        if self._filled_prices:
            # TP = deepest filled + spread (LONG) or highest filled - spread (SHORT)
            if d == 1:
                tp_price = self._filled_prices[0] + spread
            else:
                tp_price = self._filled_prices[-1] - spread

            # TP always profitable with positive spread
            self._current_tp_price = tp_price
            tp_side = SELL if d == 1 else BUY
            po = self.orders.place_limit(tp_side, 1, tp_price, f"TP")
            if po:
                self._tp_order_id = po.order_id
                log.info(f"TP placed @ {tp_price:.0f}")

            # Next grid = one step deeper
            if d == 1:
                grid_price = self._filled_prices[0] - step
            else:
                grid_price = self._filled_prices[-1] + step

            # Guard: grid must not fill instantly
            if d == 1 and grid_price < self.strategy.entry_price:
                self._current_grid_price = grid_price
                po = self.orders.place_limit(BUY, 1, grid_price, f"GRID")
                if po:
                    self._grid_order_id = po.order_id
                    log.info(f"GRID placed @ {grid_price:.0f}")
            elif d == -1 and grid_price > self.strategy.entry_price:
                self._current_grid_price = grid_price
                po = self.orders.place_limit(SELL, 1, grid_price, f"GRID")
                if po:
                    self._grid_order_id = po.order_id
                    log.info(f"GRID placed @ {grid_price:.0f}")
            else:
                log.info(f"Grid price {grid_price:.0f} would fill instantly — skipping")

        self._save_state()

    # === TP ===

    def _cancel_grid(self):
        if self._grid_order_id and self.orders:
            self.orders.cancel(self._grid_order_id)
            self._grid_order_id = None

    def _cancel_tp(self):
        if self._tp_order_id and self.orders:
            self.orders.cancel(self._tp_order_id)
            self._tp_order_id = None

    def _handle_tp_fill(self, count: int):
        """TP fills detected. Remove from filled_prices, place grid back + new TP (zigzag)."""
        d = self.strategy.direction
        entry = self.strategy.entry_price
        step = self.strategy.params.step_base
        spread = self.strategy.params.spread_base

        for i in range(count):
            if not self._filled_prices:
                log.info("TP fill but no filled prices — entry lot TP")
                break

            # Remove the TP'd price from filled_prices
            if d == 1:
                tp_price = self._filled_prices[0] + spread  # first TP target
                removed = self._filled_prices.pop(0)  # remove lowest
            else:
                tp_price = self._filled_prices[-1] - spread  # first TP target
                removed = self._filled_prices.pop()  # remove highest

            log.info(f"TP filled @ {tp_price:.0f}, removed grid @ {removed:.0f} (remaining: {len(self._filled_prices)})")

        # Cancel old grid + TP
        self._cancel_grid()
        self._cancel_tp()

        if self._filled_prices:
            # Still have grid positions — place new TP and grid
            if d == 1:
                tp_price = self._filled_prices[0] + spread
                grid_price = self._filled_prices[0] - step
            else:
                tp_price = self._filled_prices[-1] - spread
                grid_price = self._filled_prices[-1] + step

            # TP
            self._current_tp_price = tp_price
            tp_side = SELL if d == 1 else BUY
            po = self.orders.place_limit(tp_side, 1, tp_price, "TP")
            if po:
                self._tp_order_id = po.order_id
                log.info(f"TP placed @ {tp_price:.0f}")
            else:
                log.warning(f"TP place FAILED @ {tp_price:.0f}")

            # Grid (one step back — re-use!)
            if d == 1 and grid_price < self.strategy.entry_price:
                self._current_grid_price = grid_price
                po = self.orders.place_limit(BUY, 1, grid_price, "GRID")
                if po:
                    self._grid_order_id = po.order_id
                    log.info(f"GRID placed @ {grid_price:.0f}")
            elif d == -1 and grid_price > self.strategy.entry_price:
                self._current_grid_price = grid_price
                po = self.orders.place_limit(SELL, 1, grid_price, "GRID")
                if po:
                    self._grid_order_id = po.order_id
                    log.info(f"GRID placed @ {grid_price:.0f}")
            else:
                log.info(f"Grid {grid_price:.0f} would fill instantly — skipping")

        else:
            # No more grid fills — entry lot only, restore POC-TP
            if self.strategy.poc > 0:
                poc = self.strategy.poc
                if d == 1 and poc > entry:
                    po = self.orders.place_limit(SELL, 1, poc, "POC-TP")
                    if po:
                        self._poc_tp_order_id = po.order_id
                        log.info(f"POC-TP restored @ {poc:.0f}")
                elif d == -1 and poc < entry:
                    po = self.orders.place_limit(BUY, 1, poc, "POC-TP")
                    if po:
                        self._poc_tp_order_id = po.order_id
                        log.info(f"POC-TP restored @ {poc:.0f}")

            # Re-place first grid
            if d == 1:
                grid_price = entry - step
            else:
                grid_price = entry + step

            if d == 1 and grid_price < self.strategy.entry_price:
                self._current_grid_price = grid_price
                po = self.orders.place_limit(BUY, 1, grid_price, "GRID")
                if po:
                    self._grid_order_id = po.order_id
                    log.info(f"GRID-1 re-placed @ {grid_price:.0f}")
            elif d == -1 and grid_price > self.strategy.entry_price:
                self._current_grid_price = grid_price
                po = self.orders.place_limit(SELL, 1, grid_price, "GRID")
                if po:
                    self._grid_order_id = po.order_id
                    log.info(f"GRID-1 re-placed @ {grid_price:.0f}")

        self._save_state()

    def _handle_broker_close(self):
        """Broker reports 0 lots. Close everything in robot state."""
        # Don't false-close if we just entered (< 3s)
        if self._entry_pending:
            return
        if self.strategy.entry_time:
            elapsed = (datetime.now(MSK) - self.strategy.entry_time).total_seconds()
            if elapsed < 2:
                log.info(f"Ignoring broker close — position opened {elapsed:.1f}s ago")
                return
        if self.strategy.has_position and self.strategy.entry_price > 0:
            pnl = self.strategy.calc_unrealized_pnl(self._current_price)
            self.state.state.round_trips += 1
            self.state.state.realized_pnl += pnl
            dir_str = "LONG" if self.strategy.direction == 1 else "SHORT"
            log.info(f"Round trip #{self.state.state.round_trips}: {dir_str} entry={self.strategy.entry_price:.0f} PnL={pnl:.0f} | Total: RT={self.state.state.round_trips} PnL={self.state.state.realized_pnl:.0f}")

        self.strategy.on_close_all()
        self._cancel_all_orders()
        self._reset_tracked()
        self._filled_prices = []
        self._current_grid_price = 0
        self._current_tp_price = 0
        self._last_close_time = datetime.now(MSK)
        self._close_pending = True
        self._save_state()

    # === EXITS ===

    def _check_exits(self):
        """Check exit conditions based on current state."""
        price = self._current_price
        if price <= 0:
            return

        d = self.strategy.direction
        if d == 0:
            return

        total_lots = 1 + len(self._filled_prices)  # entry + grid fills

        # Risk check
        entry = self.strategy.entry_price
        if d == 1:
            pnl = (price - entry) * total_lots
        else:
            pnl = (entry - price) * total_lots

        ok, msg = self.risk.check_pnl(pnl)
        if not ok:
            log.warning(f"Risk stop: {msg}")
            self._close_all(msg)
            return

        # 1 lot: POC hit
        if total_lots == 1 and self.strategy.poc > 0:
            if d == 1 and price >= self.strategy.poc:
                self._close_all(f"POC hit LONG: {price:.0f} >= {self.strategy.poc:.0f}")
                return
            if d == -1 and price <= self.strategy.poc:
                self._close_all(f"POC hit SHORT: {price:.0f} <= {self.strategy.poc:.0f}")
                return

        # 2+ lots: PnL/lot >= min_profit
        if total_lots >= 2:
            per_lot = pnl / total_lots
            if per_lot >= self.strategy.params.min_profit_per_lot:
                self._close_all(f"PnL/lot={per_lot:.0f} >= {self.strategy.params.min_profit_per_lot}")
                return

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
        self._filled_prices = []
        self._current_grid_price = 0
        self._current_tp_price = 0
        self._last_close_time = datetime.now(MSK)
        self._close_pending = True
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
        """Quote callback — update price with MOEX sanity check."""
        new_price = q.last
        # Reject if price is way off MOEX reality
        if new_price > 0 and self._moex_last > 0 and abs(new_price - self._moex_last) > 200:
            return  # Stale quote, ignore
        if new_price != self._last_price and new_price > 0:
            self._last_price = new_price
            self._last_price_change = datetime.now(MSK)
        self._current_price = new_price
        self.strategy.current_price = new_price

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
        """Get broker position via DataProvider REST."""
        try:
            r = requests.get(
                "http://localhost:5060/position",
                params={"account": config.FINAM_ACCOUNT_ID, "ticker": config.TICKER},
                timeout=2,
            )
            data = r.json()
            d = data.get("dir", 0)
            l = data.get("lots", 0)
            a = data.get("avg_price", 0.0)
            if d != 0 and l > 0:
                return (d, l, a)
            return (0, 0, 0.0)
        except Exception as e:
            log.debug(f"Position error: {e}")
            return None

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
        try:
            fp = FinamPy(config.FINAM_TOKEN)
        except Exception as e:
            log.error(f"Warmup connect error: {e}")
            return
        try:
            from google.protobuf.timestamp_pb2 import Timestamp
            from google.type.interval_pb2 import Interval
            import FinamPy.grpc.marketdata_service_pb2 as md_pb2

            finam_tf, _, _ = fp.timeframe_to_finam_timeframe(config.TIMEFRAME)
            now = datetime.now(timezone.utc)
            start = now - timedelta(days=7)  # 7 days to cover weekends

            resp = fp.call_function(
                fp.marketdata_stub.Bars,
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
                all_bars = list(resp.bars)
                lb = self.strategy.params.vp_lookback
                bs = self.vp.bin_size
                # Feed only last `lookback` bars into warmup VP
                warmup_vp = VolumeProfile(lookback=lb, bin_size=bs, va_percent=self.vp.va_percent)
                for bar in all_bars[-lb:]:
                    warmup_vp.add_bar(float(bar.close.value), float(bar.volume.value))
                result = warmup_vp.calculate()
                # If range < bin_size, auto-shrink bin_size so VP always returns a result
                if result is None and warmup_vp._buffer:
                    prices = [p for p, v in warmup_vp._buffer]
                    price_range = max(prices) - min(prices)
                    if price_range > 0:
                        adjusted_bs = max(1, int(price_range // 2))
                        warmup_vp = VolumeProfile(lookback=lb, bin_size=adjusted_bs, va_percent=self.vp.va_percent)
                        for bar in all_bars[-lb:]:
                            warmup_vp.add_bar(float(bar.close.value), float(bar.volume.value))
                        result = warmup_vp.calculate()
                        log.info(f"Warmup: bin_size {bs}→{adjusted_bs} (range={price_range:.0f})")
                if result:
                    self.strategy.poc = result.poc
                    self.strategy.vah = result.vah
                    self.strategy.val = result.val
                    log.info(f"Warmup ({lb} bars): VAL={result.val:.0f} VAH={result.vah:.0f} POC={result.poc:.0f}")
                else:
                    log.warning(f"Warmup: no VP result with lookback={lb}")
        except Exception as e:
            log.error(f"Warmup error: {e}", exc_info=True)
        finally:
            fp.close_channel()


    def _get_moex_price(self) -> float:
        """Get last price from MOEX REST API."""
        try:
            url = f"https://iss.moex.com/iss/engines/futures/markets/forts/securities.json?iss.only=marketdata&securities={config.TICKER}"
            r = requests.get(url, timeout=5)
            data = r.json()
            cols = data['marketdata']['columns']
            for vals in data['marketdata']['data']:
                d = dict(zip(cols, vals))
                if d.get('SECID') == config.TICKER:
                    last = d.get('LAST')
                    if last and float(last) > 0:
                        return float(last)
                    # Fallback to settle price (available on weekends)
                    settle = d.get('SETTLEPRICE')
                    if settle and float(settle) > 0:
                        return float(settle)
        except Exception as e:
            log.warning(f"MOEX price error: {e}")
        return 0

    def _validate_price(self):
        """Wait for quote price to match MOEX (reject stale 71979)."""
        moex_price = self._get_moex_price()
        if moex_price <= 0:
            log.warning("MOEX price unavailable, skipping validation")
            return

        log.info(f"MOEX price: {moex_price:.0f}, waiting for quote to match...")
        # Set quote filter baseline from MOEX
        self.feed._quote_filter.set_baseline(moex_price)
        for i in range(30):  # Wait up to 30 seconds
            if self._current_price > 0 and abs(self._current_price - moex_price) < 200:
                log.info(f"Quote price validated: {self._current_price:.0f} (MOEX={moex_price:.0f})")
                return
            time.sleep(1)

        # Price still stale — force MOEX price
        log.warning(f"Quote price stale ({self._current_price:.0f}), using MOEX {moex_price:.0f}")
        self._current_price = moex_price
        self.strategy.current_price = moex_price
        self._last_price = moex_price
        self._last_price_change = datetime.now(MSK)

    def _load_config(self):
        """Load config from /tmp/robot-config.json if exists."""
        try:
            import json
            path = "/tmp/robot-config.json"
            if os.path.exists(path):
                with open(path) as f:
                    cfg = json.load(f)
                p = self.strategy.params
                if 'max_levels' in cfg: p.max_levels = cfg['max_levels']
                if 'step_base' in cfg: p.step_base = cfg['step_base']
                if 'spread_base' in cfg: p.spread_base = cfg['spread_base']
                if 'max_hold_minutes' in cfg: p.max_hold_minutes = cfg['max_hold_minutes']
                if 'min_profit_per_lot' in cfg: p.min_profit_per_lot = cfg['min_profit_per_lot']
                if 'vp_lookback' in cfg:
                    p.vp_lookback = cfg['vp_lookback']
                    self.vp = VolumeProfile(lookback=p.vp_lookback, bin_size=self.vp.bin_size, va_percent=self.vp.va_percent)
                    threading.Thread(target=self._warmup_vp, daemon=True).start()
                if 'vp_bin_size' in cfg: p.vp_bin_size = cfg['vp_bin_size']
                if 'vp_va_percent' in cfg: p.vp_va_percent = cfg['vp_va_percent']
                if 'rv_adaptation' in cfg: p.rv_adaptation = cfg['rv_adaptation']
                log.info(f"Config loaded from file: step={p.step_base} spread={p.spread_base}")
        except Exception as e:
            log.warning(f"Config load error: {e}")

    def _save_state(self):
        s = self.state.state
        s.direction = self.strategy.direction
        s.entry_price = self.strategy.entry_price
        s.filled_levels = len(self._filled_prices)
        s.entry_time = self.strategy.entry_time.isoformat() if self.strategy.entry_time else ""
        s.grid_levels = self._filled_prices  # save fill prices for recovery
        s.last_entry_price = self._last_entry_price
        s.last_direction = self._last_direction
        self.state.save()

    def get_status(self) -> dict:
        d = self.strategy.direction
        total_lots = (1 + len(self._filled_prices)) if d != 0 else 0
        entry = self.strategy.entry_price
        price = self.strategy.current_price
        if d != 0 and price > 0 and entry > 0:
            pnl = (price - entry) * total_lots if d == 1 else (entry - price) * total_lots
        else:
            pnl = 0
        return {
            "mode": self._mode,
            "paper": self._paper,
            "direction": d,
            "dir_str": "LONG" if d == 1 else "SHORT" if d == -1 else "FLAT",
            "entry_price": entry,
            "total_lots": total_lots,
            "filled_levels": len(self._filled_prices),
            "grid_levels": len(self._filled_prices),
            "pnl": round(pnl, 1),
            "round_trips": self.state.state.round_trips,
            "realized_pnl": round(self.state.state.realized_pnl, 1),
            "poc": round(self.strategy.poc, 0),
            "vah": round(self.strategy.vah, 0),
            "val": round(self.strategy.val, 0),
            "current_price": price,
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
