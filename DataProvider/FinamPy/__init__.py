"""FinamPy compatibility shim — maps old FinamPy API to finam-trade-api SDK."""
import os
import logging
import threading
from finam_trade_api import FinamClient

logger = logging.getLogger("FinamPy")


class FinamPy:
    """Drop-in replacement for old FinamPy class using finam-trade-api SDK."""

    def __init__(self):
        self._client = None
        self._secret = None
        self._connected = False
        # Event callbacks (old API used .subscribe())
        self.on_new_bar = _EventHook()
        self.on_quote = _EventHook()
        self.on_order = _EventHook()
        # Stub attributes for backward compat
        self.orders_stub = None
        self.market_data_stub = None
        self.accounts_stub = None

    @property
    def connected(self):
        return self._connected

    def connect(self, secret=None):
        """Connect to Finam gRPC with API key."""
        self._secret = secret or os.environ.get("FINAM_API_KEY", "")
        if not self._secret:
            raise ValueError("FINAM_API_KEY not set")
        self._client = FinamClient(secret=self._secret)
        self._client.__enter__()
        self._connected = True
        logger.info("FinamPy shim: connected to Finam")
        return self._client

    def close_channel(self):
        """Close gRPC channel."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        self._connected = False
        self._client = None

    def call_function(self, stub, request):
        """Generic unary call — delegates to underlying client.
        For backward compat, stub is ignored (new SDK uses typed methods)."""
        # This is a simplified shim — specific calls should use direct methods
        raise NotImplementedError(
            "call_function is deprecated. Use specific client methods instead."
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close_channel()


class _EventHook:
    """Simple event hook mimicking old FinamPy .subscribe() pattern."""

    def __init__(self):
        self._handlers = []

    def subscribe(self, handler):
        self._handlers.append(handler)

    def unsubscribe(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)

    def emit(self, *args, **kwargs):
        for h in self._handlers:
            try:
                h(*args, **kwargs)
            except Exception as e:
                logger.error(f"Event hook error: {e}")
