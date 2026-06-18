    def _reestablish_orders(self):
        """Re-establish grid + TP orders after clearing wiped them."""
        d = self.strategy.direction
        step = self.strategy.params.step_base
        spread = self.strategy.params.spread_base
        total_lots = 1 + len(self._filled_prices)

        if total_lots <= 1:
            # 1 lot — no grid, place POC-TP only
            if self.strategy.poc > 0:
                log.info(f"Re-establishing: 1 lot, waiting for POC/exit")
            return

        # Step 1: Cancel old orders and confirm
        old_tp_id = self._tp_order_id
        old_grid_id = self._grid_order_id
        self._cancel_grid()
        self._cancel_tp()

        # Step 2: Confirm cancelled by checking active orders
        for attempt in range(6):  # max 3s
            time.sleep(0.5)
            try:
                active = self.orders.get_active_orders(symbol=config.SYMBOL)
                if not active or len(active) == 0:
                    break
                log.info(f"Re-establish: waiting for cancel confirm, {len(active)} orders remain")
            except Exception as e:
                log.warning(f"Re-establish: active orders check failed: {e}")
                break

        # Reset IDs after confirmed cancelled
        self._tp_order_id = None
        self._grid_order_id = None

        # Step 3: Place TP + grid from filled_prices
        if d == 1:
            tp_price = self._filled_prices[0] + spread
            grid_price = self._filled_prices[0] - step
        else:
            tp_price = self._filled_prices[-1] - spread
            grid_price = self._filled_prices[-1] + step

        tp_side = SELL if d == 1 else BUY
        po = self.orders.place_limit(tp_side, 1, tp_price, "TP")
        if po:
            self._tp_order_id = po.order_id
            self._current_tp_price = tp_price
            log.info(f"TP re-established @ {tp_price:.0f}")

        grid_side = BUY if d == 1 else SELL
        po = self.orders.place_limit(grid_side, 1, grid_price, "GRID")
        if po:
            self._grid_order_id = po.order_id
            self._current_grid_price = grid_price
            log.info(f"GRID re-established @ {grid_price:.0f}")