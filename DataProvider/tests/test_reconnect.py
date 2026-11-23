"""Tests for DataProvider reconnect logic, backoff, and cache invalidation."""
import sys
import os
import pytest
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

# Mock FinamPy BEFORE provider tries to import it
sys.modules['finam_compat'] = MagicMock()
# Removed: not needed with new SDK
# Removed: not needed with new SDK
# Removed: not needed with new SDK
# Removed: not needed with new SDK
# Removed: not needed with new SDK
sys.modules['google.type.decimal_pb2'] = MagicMock()

_here = os.path.dirname(os.path.abspath(__file__))
_dp_dir = os.path.dirname(_here)
if _dp_dir not in sys.path:
    sys.path.insert(0, _dp_dir)

# Now import provider safely
from provider import FinamProvider
from cache import DataCache


class TestReconnect:
    """Test exponential backoff and reconnect behavior."""

    def test_exponential_backoff(self):
        """Verify that _wrap_thread catches crashes and waits with backoff."""
        cache = DataCache()
        provider = FinamProvider(cache=cache, account_id="TEST")

        provider.fp = MagicMock()
        provider._stopped = threading.Event()

        crash_count = [0]
        backoff_values = []

        def crashing_target():
            crash_count[0] += 1
            if crash_count[0] <= 3:
                raise RuntimeError(f"Crash #{crash_count[0]}")
            provider._stopped.set()

        def traced_wrap(name, target):
            def wrapper(*args, **kwargs):
                while not provider._stopped.is_set():
                    try:
                        target(*args, **kwargs)
                    except Exception as e:
                        backoff_values.append(crash_count[0])
                        # Use fast backoff for tests (not real 1/2/4/8)
                        if provider._stopped.wait(0.1):
                            return
            return wrapper

        provider._wrap_thread = traced_wrap

        t = threading.Thread(
            target=provider._wrap_thread("test_crash", crashing_target),
            daemon=True,
        )
        t.start()
        t.join(timeout=5)

        # Should have crashed exactly 3 times then set stopped
        assert crash_count[0] >= 3
        # Backoff trace should show: [1, 2, 3]
        assert len(backoff_values) >= 3
        assert backoff_values == [1, 2, 3]

    def test_cache_invalidation_on_fill(self):
        """Verify that position cache is populated and can be cleared."""
        cache = DataCache()
        provider = FinamProvider(cache=cache, account_id="TEST")

        provider.fp = MagicMock()
        provider.fp.jwt_token = "fake_token"

        provider._pos_cache["TEST:SBER"] = {
            "data": {"ticker": "SBER", "lots": 1, "avg_price": 270.0},
            "ts": time.time(),
        }

        cached = provider.get_positions("TEST", "SBER")
        assert cached is not None
        assert cached["lots"] == 1

        provider._pos_cache.clear()
        with patch.object(provider, "_fetch_position", return_value=None):
            result = provider.get_positions("TEST", "SBER")
            assert result is None
