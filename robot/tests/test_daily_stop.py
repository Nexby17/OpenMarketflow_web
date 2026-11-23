"""Tests for daily stop loss mechanism in OrderFlow strategy."""
import sys
import os
import pytest
import time
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, timezone, timedelta

# Ensure robot dir is in sys.path so imports work
_here = os.path.dirname(os.path.abspath(__file__))
_robot_dir = os.path.dirname(_here)
if _robot_dir not in sys.path:
    sys.path.insert(0, _robot_dir)

# We cant use patch to stop strategy_of from importing orderflow_engine at module level.
# Instead we import strategy_of directly after setting up mocks.

# Mock the orderflow_engine module BEFORE strategy_of tries to import it
sys.modules['orderflow_engine'] = MagicMock()

# Now import strategy_of safely
MSK = timezone(timedelta(hours=3))


class TestDailyStop:
    """Test daily stop loss functionality."""

    def test_daily_stop_closes_position(self):
        """Verify that when daily_stop_hit returns True, strategy produces close_all action."""
        from strategy_of import OrderFlowStrategy, OFParams, LONG

        params = OFParams(
            lots=1,
            max_average_levels=10,
            max_pyramid_levels=5,
            stop_loss_value=7000,
            enable_daily_stop=True,
        )

        st = OrderFlowStrategy(params)
        st._dir = LONG
        st._entry_price = 27000.0
        st._avg_price = 27000.0
        st._total_lots = 2
        st._lot_queue.append(
            type("LotEntry", (), {"price": 27000.0, "side": LONG, "lots": 2, "added_ts": time.monotonic() - 10})()
        )
        st._current_price = 26500.0

        st._daily_pnl = -7100.0
        st._daily_pnl_date = datetime.now(MSK).strftime("%Y-%m-%d")

        actions = st._close_all(25000.0, "daily_stop")
        assert actions is not None
        assert actions["action"] == "close_all"
        assert actions["qty"] == 2

    def test_daily_stop_blocks_new_entries(self):
        """After daily stop hit, no new entries should be generated."""
        from strategy_of import OrderFlowStrategy, OFParams

        params = OFParams(
            lots=1,
            stop_loss_value=7000,
            enable_daily_stop=True,
        )

        st = OrderFlowStrategy(params)
        st._daily_pnl = -7100.0
        st._daily_pnl_date = datetime.now(MSK).strftime("%Y-%m-%d")

        assert st._daily_stop_hit() is True

        st.signals.has_enough_data = True
        now = datetime.now(MSK)
        result = st._check_entry(27000.0, now)
        assert result is None

    def test_daily_stop_reset_at_7am(self):
        """After 07:00 MSK, daily PnL should reset and trading re-enabled."""
        from strategy_of import OrderFlowStrategy, OFParams

        params = OFParams(
            lots=1,
            stop_loss_value=7000,
            enable_daily_stop=True,
        )

        st = OrderFlowStrategy(params)
        yesterday = datetime.now(MSK) - timedelta(days=1)
        st._daily_pnl = -7100.0
        st._daily_pnl_date = yesterday.strftime("%Y-%m-%d")

        now = datetime.now(MSK)
        st._check_daily_reset(now)
        assert st._daily_pnl == 0.0
        assert st._daily_pnl_date == now.strftime("%Y-%m-%d")
        assert st._daily_stop_hit() is False
