# finam_compat.py
"""Compatibility shim: provides FinamPy interface on top of finam-trade-api SDK.

Drop-in replacement:
    from finam_compat import FinamPyCompat as FinamPy

The shim wraps FinamClient (new official SDK) and exposes:
- Event-based subscriptions (on_new_bar, on_quote, on_order, on_trade, etc.)
- Blocking subscription threads with auto-reconnect
- call_function() wrapper for unary RPCs
- Stub accessors (marketdata_stub, orders_stub, accounts_stub)
- metadata property (empty tuple — new SDK injects auth via interceptor)
- timeframe_to_finam_timeframe() helper
"""

import grpc as _grpc
from finam_trade_api import Client as _SdkClient
from finam_trade_api.base_client.token_manager import TokenManager
from finam_trade_api.proto.grpc.tradeapi.v1.marketdata import marketdata_service_pb2_grpc as _md_grpc
from finam_trade_api.proto.grpc.tradeapi.v1.orders import orders_service_pb2_grpc as _ord_grpc
from finam_trade_api.proto.grpc.tradeapi.v1.accounts import accounts_service_pb2_grpc as _acc_grpc

_GRPC_TARGET = "api.finam.ru:443"


class FinamClient(_SdkClient):
    """Compat shim over SDK 4.3.3 Client.

    gRPC market/orders/accounts stubs live here (SDK 4.x is REST-only).
    REST sub-clients (account/instruments/orders REST) come from SDK via
    composition, not inheritance (SDK sets self.orders etc in __init__)."""

    def __init__(self, token: str):
        # SDK Client не наследуем: его __init__ перезаписывает self.orders/orders_stub
        self._sdk = _SdkClient(TokenManager(token), auto_refresh_tokens=True)
        self._token_manager = TokenManager(token)
        # JWT для gRPC: сразу получаем (не ждём REST-цикла)
        try:
            import asyncio
            from finam_trade_api.access.access_token import TokenClient as _TC
            tc = _TC(self._token_manager)
            asyncio.get_event_loop().run_until_complete(tc.set_jwt_token())
        except Exception:
            pass
        creds = _grpc.ssl_channel_credentials()
        self._grpc_channel = _grpc.secure_channel(_GRPC_TARGET, creds)
        self._grpc_md = _md_grpc.MarketDataServiceStub(self._grpc_channel)
        self._grpc_ord = _ord_grpc.OrdersServiceStub(self._grpc_channel)
        self._grpc_acc = _acc_grpc.AccountsServiceStub(self._grpc_channel)

    @property
    def market_data(self):
        return self._grpc_md  # сырой stub; _CompatStub добавляется в FinamPyCompat.marketdata_stub

    @property
    def orders(self):
        return self._grpc_ord

    @property
    def accounts(self):
        return self._grpc_acc

    def get_token(self) -> str:
        """Return current JWT (old FinamClient API)."""
        return self._token_manager.jwt_token or self._token_manager.token
from finam_trade_api.proto.grpc.tradeapi.v1.marketdata import marketdata_service_pb2 as md
from finam_trade_api.proto.grpc.tradeapi.v1.orders import orders_service_pb2 as ord_pb
from finam_trade_api.proto.grpc.tradeapi.v1.accounts import accounts_service_pb2 as accts_pb
from finam_trade_api.proto.grpc.tradeapi.v1 import side_pb2 as side
from finam_trade_api.proto.grpc.tradeapi.v1.orders import orders_service_pb2 as _orders_module
from finam_trade_api.proto.grpc.tradeapi.v1 import trade_pb2
from google.type.decimal_pb2 import Decimal
from typing import Optional, Callable
import threading
import logging
import time
import os

logger = logging.getLogger("finam_compat")

# === Proto re-exports (compatibility with old import paths) ===
# Old: from FinamPy.grpc import marketdata_service_pb2 as md
# New: from finam_compat import md  (or use new path directly)
# orders_service_pb2 = ord_pb  # exported below

# Timeframe mapping (old FinamPy exposed this via enum, new SDK uses same proto)
_TF_MAP = {
    "M1": md.TimeFrame.TIME_FRAME_M1,
    "M5": md.TimeFrame.TIME_FRAME_M5,
    "M15": md.TimeFrame.TIME_FRAME_M15,
    "M30": md.TimeFrame.TIME_FRAME_M30,
    "H1": md.TimeFrame.TIME_FRAME_H1,
    "H2": md.TimeFrame.TIME_FRAME_H2,
    "H4": md.TimeFrame.TIME_FRAME_H4,
    "H8": md.TimeFrame.TIME_FRAME_H8,
    "D": md.TimeFrame.TIME_FRAME_D,
    "W": md.TimeFrame.TIME_FRAME_W,
    "MN": md.TimeFrame.TIME_FRAME_MN,
    "QR": md.TimeFrame.TIME_FRAME_QR,
}


class Event:
    """Simple event dispatcher mimicking FinamPy's Event."""
    def __init__(self):
        self._handlers: list[Callable] = []

    def subscribe(self, handler: Callable):
        self._handlers.append(handler)

    def unsubscribe_all(self):
        self._handlers.clear()

    def emit(self, *args, **kwargs):
        for h in self._handlers:
            try:
                h(*args, **kwargs)
            except Exception as e:
                logger.error("Event handler error: %s", e)


class FinamPyCompat:
    """
    Drop-in replacement for FinamPy.FinamPy using finam-trade-api SDK.
    Provides the same interface: events, subscription threads, call_function.
    """

    def __init__(self, token: str):
        self._token = token
        self._client: Optional[FinamClient] = None

        # Events (same names as FinamPy)
        self.on_new_bar = Event()
        self.on_quote = Event()
        self.on_order = Event()
        self.on_trade = Event()
        self.on_latest_trades = Event()
        self.on_order_book = Event()

        # Compatibility properties
        self._account_ids: list[str] = []

    @property
    def client(self) -> FinamClient:
        if self._client is None:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._client

    @property
    def account_ids(self) -> list[str]:
        return self._account_ids

    @property
    def jwt_token(self) -> Optional[str]:
        if self._client:
            try:
                return self._client.get_token()
            except Exception:
                return None
        return None

    @property
    def metadata(self) -> tuple:
        """Compatibility: new SDK injects auth automatically via interceptor.
        Return empty tuple so old code using metadata=(fp.metadata,) still works."""
        return ()

    # === Stub accessors (compatibility) ===
    # Old code used fp.marketdata_stub, fp.orders_stub, fp.accounts_stub
    # These are properties returning the new SDK's service stubs.

    @property
    def marketdata_stub(self):
        """Returns MarketDataServiceStub from new SDK.
        Note: new SDK uses .Bars(request=...) not .Bars.with_call(request=..., metadata=...).
        For compatibility, old code should use call_function() or we wrap with_call."""
        return _CompatStub(self.client.market_data, self.client.get_token)

    @property
    def orders_stub(self):
        return _CompatStub(self.client.orders, self.client.get_token)

    @property
    def accounts_stub(self):
        return _CompatStub(self.client.accounts, self.client.get_token)

    def connect(self) -> None:
        """Create FinamClient and discover account IDs."""
        self._client = FinamClient(self._token)
        # Fetch account IDs from environment (new SDK doesn't auto-discover)
        try:
            acc = os.environ.get("FINAM_ACCOUNT") or os.environ.get("FINAM_ACCOUNT_ID")
            if acc:
                self._account_ids = [acc]
            stock = os.environ.get("FINAM_STOCK_ACCOUNT")
            if stock and stock not in self._account_ids:
                self._account_ids.append(stock)
        except Exception:
            pass
        if not self._account_ids:
            logger.warning("No account IDs found in env. Set FINAM_ACCOUNT / FINAM_STOCK_ACCOUNT.")

    def call_function(self, stub_method, request, timeout=None):
        """Compatibility wrapper for fp.call_function(stub.Method, request).
        In new SDK, just call stub.Method(request) directly."""
        try:
            if timeout:
                return stub_method(request=request, timeout=timeout)
            return stub_method(request=request)
        except Exception as e:
            logger.error("call_function error: %s", e)
            return None

    # === Timeframe helper (compatibility with FinamPy.timeframe_to_finam_timeframe) ===

    def timeframe_to_finam_timeframe(self, tf_str: str):
        """Convert timeframe string to (finam_tf, tf_range, tf_name).
        Old FinamPy returned (finam_tf, range_in_seconds, name)."""
        tf = _TF_MAP.get(tf_str)
        if tf is None:
            # Try direct enum lookup
            tf = tf_str
        # Approximate ranges (seconds)
        _TF_SECONDS = {
            "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
            "H1": 3600, "H2": 7200, "H4": 14400, "H8": 28800,
            "D": 86400, "W": 604800, "MN": 2592000,
        }
        secs = _TF_SECONDS.get(tf_str, 60)
        return (tf, secs, tf_str)

    # === Subscription threads (blocking, with auto-reconnect) ===
    # Each runs in a daemon thread, iterates over streaming RPC responses,
    # and dispatches to the corresponding Event.

    def subscribe_bars_thread(self, symbol: str, timeframe) -> None:
        """Blocking: iterates SubscribeBars stream, emits on_new_bar."""
        req = md.SubscribeBarsRequest(symbol=symbol, timeframe=timeframe)
        while True:
            try:
                stream = self.client.market_data.SubscribeBars(req)
                for response in stream:
                    self.on_new_bar.emit(response, timeframe)
            except Exception as e:
                logger.warning("SubscribeBars stream error: %s - reconnecting", e)
                time.sleep(3)

    def subscribe_quote_thread(self, symbols) -> None:
        """Blocking: iterates SubscribeQuote stream, emits on_quote."""
        if isinstance(symbols, (tuple, list)):
            symbols_field = list(symbols)
        else:
            symbols_field = [symbols]
        req = md.SubscribeQuoteRequest(symbols=symbols_field)
        while True:
            try:
                stream = self.client.market_data.SubscribeQuote(req, metadata=[("authorization", self.client.get_token())])
                for response in stream:
                    self.on_quote.emit(response)
            except Exception as e:
                logger.warning("SubscribeQuote stream error: %s - reconnecting", e)
                time.sleep(3)

    def subscribe_orders_thread(self, account_id: str = "") -> None:
        """Blocking: iterates SubscribeOrders stream, emits on_order."""
        req = ord_pb.SubscribeOrdersRequest(account_id=account_id)
        while True:
            try:
                stream = self.client.orders.SubscribeOrders(req)
                for response in stream:
                    for o in response.orders:
                        self.on_order.emit(o)
            except Exception as e:
                logger.warning("SubscribeOrders stream error: %s - reconnecting", e)
                time.sleep(3)

    def subscribe_trades_thread(self, account_id: str = "") -> None:
        """Blocking: iterates SubscribeTrades stream, emits on_trade."""
        req = ord_pb.SubscribeTradesRequest(account_id=account_id)
        while True:
            try:
                stream = self.client.orders.SubscribeTrades(req)
                for response in stream:
                    for t in response.trades:
                        self.on_trade.emit(t)
            except Exception as e:
                logger.warning("SubscribeTrades stream error: %s - reconnecting", e)
                time.sleep(3)

    def subscribe_latest_trades_thread(self, symbol: str) -> None:
        """Blocking: iterates SubscribeLatestTrades stream, emits on_latest_trades."""
        req = md.SubscribeLatestTradesRequest(symbol=symbol)
        while True:
            try:
                stream = self.client.market_data.SubscribeLatestTrades(req, metadata=[("authorization", self.client.get_token())])
                for response in stream:
                    self.on_latest_trades.emit(response)
            except Exception as e:
                logger.warning("SubscribeLatestTrades stream error: %s - reconnecting", e)
                time.sleep(3)

    def subscribe_order_book_thread(self, symbol: str) -> None:
        """Blocking: iterates SubscribeOrderBook stream, emits on_order_book."""
        req = md.SubscribeOrderBookRequest(symbol=symbol)
        while True:
            try:
                stream = self.client.market_data.SubscribeOrderBook(req)
                for response in stream:
                    self.on_order_book.emit(response)
            except Exception as e:
                logger.warning("SubscribeOrderBook stream error: %s - reconnecting", e)
                time.sleep(3)

    def subscribe_orders_trades(self, orders: bool = True, trades: bool = True,
                                 account_id: str = "") -> None:
        """Store params for subscribe_orders_trades_thread."""
        self._ot_params = {"orders": orders, "trades": trades, "account_id": account_id}

    def subscribe_orders_trades_thread(self) -> None:
        """Blocking: iterates SubscribeOrderTrade stream, emits on_trade + on_order."""
        params = getattr(self, "_ot_params", {"orders": True, "trades": True, "account_id": ""})
        req = ord_pb.OrderTradeRequest(
            account_id=params.get("account_id", ""),
        )
        while True:
            try:
                stream = self.client.orders.SubscribeOrderTrade(req)
                for response in stream:
                    if params.get("trades"):
                        for t in response.trades:
                            self.on_trade.emit(t)
                    if params.get("orders"):
                        for o in response.orders:
                            self.on_order.emit(o)
            except Exception as e:
                logger.warning("SubscribeOrderTrade stream error: %s - reconnecting", e)
                time.sleep(3)

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def close_channel(self) -> None:
        self.close()


class _CompatStub:
    """Wrapper that adds .with_call(request=..., metadata=...) syntax to new SDK stubs.

    Old FinamPy code used:
        fp.orders_stub.PlaceOrder.with_call(request=req, metadata=(fp.metadata,))
        resp, _ = fp.orders_stub.CancelOrder.with_call(request=req, timeout=5, metadata=(fp.metadata,))

    New SDK uses:
        client.orders.PlaceOrder(request=req)

    This wrapper makes both styles work by intercepting .with_call().
    """

    def __init__(self, real_stub, jwt_provider=None):
        self._stub = real_stub
        self._jwt_provider = jwt_provider

    def __getattr__(self, name):
        """Return a callable that supports both direct call and .with_call()."""
        attr = getattr(self._stub, name)

        # If it's a callable (RPC method), wrap it
        if callable(attr):
            return _CompatRpcMethod(attr, self._jwt_provider)
        return attr


class _CompatRpcMethod:
    """Wraps a single gRPC unary method to support .with_call() syntax."""

    def __init__(self, rpc_method, jwt_provider=None):
        self._method = rpc_method
        self._jwt_provider = jwt_provider

    def __call__(self, *args, **kwargs):
        """Direct call: stub.Method(request=req) or stub.Method(req)."""
        return self.with_call(*args, **kwargs)[0]

    def with_call(self, *args, **kwargs):
        """Compatibility: stub.Method.with_call(request=req, metadata=..., timeout=...).

        Returns (response, None) to match old gRPC API.
        New SDK returns just the response, so we add None as call_metadata."""
        timeout = kwargs.pop("timeout", None)
        kwargs.pop("metadata", None)  # старый код передаёт (fp.metadata,) — заменяем нашим auth
        # gRPC auth: Finam требует authorization: <jwt> — берём из замыкания клиента
        metadata = [("authorization", self._jwt_provider())] if self._jwt_provider else None
        if timeout is not None:
            response = self._method(*args, timeout=timeout, metadata=metadata, **kwargs)
        else:
            response = self._method(*args, metadata=metadata, **kwargs)
        return response, None


# === Module-level proto re-exports ===
# Allows: from finam_compat import md, ord_pb, accts_pb, side
# (For code that imported from finam_compat instead of new proto paths)
orders_service_pb2 = ord_pb
accounts_service_pb2 = accts_pb
side_pb2 = side
marketdata_service_pb2 = md
trade_pb2 = trade_pb2
