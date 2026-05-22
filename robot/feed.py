"""gRPC feed — real-time market data subscriptions via FinamPy."""
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

from FinamPy import FinamPy
from google.type.decimal_pb2 import Decimal

import config

log = logging.getLogger("feed")

MSK = timezone(timedelta(hours=3))


@dataclass
class Quote:
    bid: float
    ask: float
    last: float
    timestamp: datetime


@dataclass
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: datetime


@dataclass
class OrderEvent:
    order_id: str
    client_order_id: str
    side: int  # 1=BUY, 2=SELL
    status: int
    executed_quantity: float
    price: float
    timestamp: datetime


@dataclass
class TradeEvent:
    trade_id: str
    order_id: str
    side: int
    quantity: float
    price: float
    timestamp: datetime


class Feed:
    """Manages gRPC subscriptions to Finam Trade API."""

    def __init__(self):
        self._fp: Optional[FinamPy] = None
        self._running = False
        self._threads: list[threading.Thread] = []

        # Callbacks
        self.on_quote: Callable[[Quote], None] = lambda q: None
        self.on_bar: Callable[[Bar], None] = lambda b: None
        self.on_order: Callable[[OrderEvent], None] = lambda e: None
        self.on_trade: Callable[[TradeEvent], None] = lambda e: None

        # Latest state
        self._latest_quote: Optional[Quote] = None
        self._latest_bar: Optional[Bar] = None
        self._lock = threading.Lock()

        # Stale detection
        self._last_quote_ts: float = 0  # time.time() of last quote
        self._last_bar_ts: float = 0
        self._stale_timeout = 60  # seconds without data → reconnect
        self._watchdog_thread: Optional[threading.Thread] = None
        self._on_stale = None  # set by Robot

    @property
    def latest_quote(self) -> Optional[Quote]:
        with self._lock:
            return self._latest_quote

    @property
    def latest_bar(self) -> Optional[Bar]:
        with self._lock:
            return self._latest_bar

    @property
    def connected(self) -> bool:
        return self._fp is not None

    def connect(self):
        """Initialize gRPC connection."""
        log.info("Connecting to Finam gRPC...")
        self._fp = FinamPy(config.FINAM_TOKEN)
        log.info(f"Connected. Accounts: {self._fp.account_ids}")

    def disconnect(self):
        """Stop subscriptions and disconnect."""
        self._running = False
        if self._fp:
            try:
                self._fp.close_channel()
            except Exception as e:
                log.warning(f"Disconnect error: {e}")
            self._fp = None
        log.info("Disconnected")

    def subscribe_all(self):
        """Subscribe to quotes, bars, orders, trades."""
        if not self._fp:
            raise RuntimeError("Not connected. Call connect() first.")

        self._running = True
        symbol = config.SYMBOL
        account_id = config.FINAM_ACCOUNT_ID

        # --- Quotes ---
        def _on_quote(quote_response):
            if not self._running:
                return
            try:
                if quote_response.quote:
                    q = quote_response.quote[0]
                    def _safe_float(v, default=0):
                        try:
                            s = str(v.value) if v else ''
                            return float(s) if s else default
                        except (ValueError, TypeError):
                            return default

                    bid = _safe_float(q.bid)
                    ask = _safe_float(q.ask)
                    last = _safe_float(q.last)
                    # Fallback: use mid if last is 0
                    if last == 0 and bid > 0 and ask > 0:
                        last = (bid + ask) / 2
                    ts = datetime.fromtimestamp(
                        q.timestamp.seconds + q.timestamp.nanos / 1e9, MSK
                    )
                    quote = Quote(bid=bid, ask=ask, last=last, timestamp=ts)
                    with self._lock:
                        self._latest_quote = quote
                        self._last_quote_ts = time.time()
                    self.on_quote(quote)
            except Exception as e:
                log.error(f"Quote callback error: {e}")

        self._fp.on_quote.subscribe(_on_quote)
        t = threading.Thread(
            target=self._fp.subscribe_quote_thread,
            args=((symbol,),),
            daemon=True,
            name="feed-quotes",
        )
        t.start()
        self._threads.append(t)
        log.info(f"Subscribed to quotes: {symbol}")

        # --- Bars (M1) ---
        finam_tf, _, _ = self._fp.timeframe_to_finam_timeframe(config.TIMEFRAME)
        last_bar_ref = {"bar": None, "dt": None}

        def _on_bar(bars_response, finam_timeframe):
            if not self._running:
                return
            try:
                for bar in bars_response.bars:
                    dt_bar = datetime.fromtimestamp(
                        bar.timestamp.seconds, MSK
                    )
                    # Emit when a NEW bar appears (previous bar closed)
                    if last_bar_ref["dt"] is not None and last_bar_ref["dt"] < dt_bar:
                        lb = last_bar_ref["bar"]
                        if lb:
                            closed_bar = Bar(
                                open=float(lb.open.value),
                                high=float(lb.high.value),
                                low=float(lb.low.value),
                                close=float(lb.close.value),
                                volume=float(lb.volume.value),
                                timestamp=last_bar_ref["dt"],
                            )
                            with self._lock:
                                self._latest_bar = closed_bar
                                self._last_bar_ts = time.time()
                            self.on_bar(closed_bar)
                    last_bar_ref["bar"] = bar
                    last_bar_ref["dt"] = dt_bar
            except Exception as e:
                log.error(f"Bar callback error: {e}")

        self._fp.on_new_bar.subscribe(_on_bar)
        t = threading.Thread(
            target=self._fp.subscribe_bars_thread,
            args=(symbol, finam_tf),
            daemon=True,
            name="feed-bars",
        )
        t.start()
        self._threads.append(t)
        log.info(f"Subscribed to bars: {symbol} {config.TIMEFRAME}")

        # --- Own orders ---
        def _on_order(order):
            if not self._running:
                return
            try:
                # order is OrderState from subscribe_orders
                oid = order.order_id
                client_oid = order.client_order_id
                side = order.side
                status = order.status
                exec_qty = float(order.executed_quantity.value) if order.executed_quantity else 0
                price = float(order.executed_price.value) if order.executed_price else 0
                ts = datetime.now(MSK)
                evt = OrderEvent(
                    order_id=oid,
                    client_order_id=client_oid,
                    side=side,
                    status=status,
                    executed_quantity=exec_qty,
                    price=price,
                    timestamp=ts,
                )
                self.on_order(evt)
            except Exception as e:
                log.error(f"Order callback error: {e}")

        self._fp.on_order.subscribe(_on_order)
        t = threading.Thread(
            target=self._fp.subscribe_orders_thread,
            daemon=True,
            name="feed-orders",
        )
        t.start()
        self._threads.append(t)
        log.info("Subscribed to own orders")

        # --- Own trades ---
        def _on_trade(trade):
            if not self._running:
                return
            try:
                tid = trade.trade_id
                oid = trade.order_id
                side = trade.side
                qty = float(trade.quantity.value) if trade.quantity else 0
                price = float(trade.price.value) if trade.price else 0
                ts = datetime.now(MSK)
                evt = TradeEvent(
                    trade_id=tid,
                    order_id=oid,
                    side=side,
                    quantity=qty,
                    price=price,
                    timestamp=ts,
                )
                self.on_trade(evt)
            except Exception as e:
                log.error(f"Trade callback error: {e}")

        self._fp.on_trade.subscribe(_on_trade)
        t = threading.Thread(
            target=self._fp.subscribe_trades_thread,
            daemon=True,
            name="feed-trades",
        )
        t.start()
        self._threads.append(t)
        log.info("Subscribed to own trades")

        # --- Watchdog ---
        self._last_quote_ts = time.time()
        self._last_bar_ts = time.time()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="feed-watchdog",
        )
        self._watchdog_thread.start()
        log.info("Watchdog started (60s stale timeout)")

    def _watchdog_loop(self):
        """Check if data is flowing. Reconnect if stale."""
        while self._running:
            time.sleep(15)
            if not self._running:
                return

            now = time.time()
            quote_age = now - self._last_quote_ts
            bar_age = now - self._last_bar_ts

            # Both stale = streams died (e.g. after clearing)
            if quote_age > self._stale_timeout and bar_age > self._stale_timeout:
                log.warning(f"Streams stale: quotes {quote_age:.0f}s, bars {bar_age:.0f}s — reconnecting")
                if self._on_stale:
                    self._on_stale()
                return  # Exit watchdog; Robot handles reconnect

    def wait(self):
        """Block until disconnect."""
        try:
            while self._running:
                for t in self._threads:
                    t.join(timeout=1)
        except KeyboardInterrupt:
            log.info("Interrupted")
            self.disconnect()
