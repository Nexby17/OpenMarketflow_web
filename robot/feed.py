"""gRPC feed — real-time market data subscriptions via FinamPy."""
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

from finam_compat import FinamPyCompat as FinamPy
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


class QuoteFilter:
    """Filters anomalous quotes (spikes, stale data)."""
    def __init__(self, max_change_pct: float = 0.005):
        self._last_valid: float = 0
        self._max_change_pct = max_change_pct  # 0.5% max change per tick

    def set_baseline(self, price: float):
        """Set initial baseline from MOEX or other trusted source."""
        if price > 0:
            self._last_valid = price

    def filter(self, quote: Quote) -> Optional[Quote]:
        """Return quote if valid, None if spike."""
        if quote.last <= 0:
            return None
        if self._last_valid <= 0:
            self._last_valid = quote.last
            return quote
        change = abs(quote.last - self._last_valid) / self._last_valid
        if change > self._max_change_pct:
            return None  # Spike — ignore
        self._last_valid = quote.last
        return quote


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


@dataclass
class OBLevel:
    price: float
    buy_size: float  # bid volume
    sell_size: float  # ask (offer) volume


@dataclass
class OrderBookUpdate:
    symbol: str
    rows: list[OBLevel]  # aggregated levels
    timestamp: datetime


class Feed:
    """Manages gRPC subscriptions to Finam Trade API."""

    def __init__(self):
        self._fp: Optional[FinamPy] = None
        self._running = False
        self._threads: list[threading.Thread] = []

        # Callbacks
        self.on_quote: Callable[[Quote], None] = lambda q: None
        self.on_orderbook: Callable[[OrderBookUpdate], None] = lambda ob: None
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

        # Quote filter
        self._quote_filter = QuoteFilter(max_change_pct=0.002)

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
        self._fp.connect()  # now reads FINAM_API_KEY via config.py
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
                    # Use best available price from order book (more current than last trade)
                    if bid > 0 and ask > 0:
                        last = (bid + ask) / 2
                    elif bid > 0:
                        last = bid
                    elif ask > 0:
                        last = ask
                    ts = datetime.fromtimestamp(
                        q.timestamp.seconds + q.timestamp.nanos / 1e9, MSK
                    )
                    quote = Quote(bid=bid, ask=ask, last=last, timestamp=ts)
                    # Filter spikes
                    quote = self._quote_filter.filter(quote)
                    if quote is None:
                        return
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

        # --- Order Book (Level 2) ---
        # Local order book state: aggregate incremental updates
        self._ob_bids: dict[float, float] = {}  # price → size
        self._ob_asks: dict[float, float] = {}  # price → size
        # ACTION constants from Finam protobuf
        ACTION_UNKNOWN = 0
        ACTION_ADD = 1
        ACTION_UPDATE = 2
        ACTION_DELETE = 3

        def _on_ob(ob_response):
            if not self._running:
                return
            try:
                ob_list = ob_response.order_book
                if not ob_list:
                    return
                total_rows = 0
                for ob in ob_list:
                    total_rows += len(ob.rows)
                    for r in ob.rows:
                        p = r.price.value if hasattr(r.price, 'value') else str(r.price)
                        price = round(float(p), 2) if p else 0
                        if price <= 0:
                            continue
                        b = r.buy_size.value if hasattr(r.buy_size, 'value') else str(r.buy_size or '')
                        buy = float(b) if b else 0
                        s = r.sell_size.value if hasattr(r.sell_size, 'value') else str(r.sell_size or '')
                        sell = float(s) if s else 0
                        action = r.action

                        # Update local book
                        if action == ACTION_DELETE:
                            self._ob_bids.pop(price, None)
                            self._ob_asks.pop(price, None)
                        else:
                            # ADD or UPDATE
                            if buy > 0:
                                self._ob_bids[price] = buy
                            elif price in self._ob_bids:
                                self._ob_bids.pop(price, None)
                            if sell > 0:
                                self._ob_asks[price] = sell
                            elif price in self._ob_asks:
                                self._ob_asks.pop(price, None)

                # Build snapshot from local book
                if self._ob_bids or self._ob_asks:
                    rows = []
                    for p, sz in self._ob_bids.items():
                        rows.append(OBLevel(price=p, buy_size=sz, sell_size=0))
                    for p, sz in self._ob_asks.items():
                        rows.append(OBLevel(price=p, buy_size=0, sell_size=sz))
                    self.on_orderbook(OrderBookUpdate(
                        symbol=symbol,
                        rows=rows,
                        timestamp=datetime.now(),
                    ))
                    if not hasattr(self, '_ob_log_ts') or (datetime.now() - self._ob_log_ts).seconds >= 30:
                        self._ob_log_ts = datetime.now()
                        log.info(f"OB snapshot: {len(self._ob_bids)} bids, {len(self._ob_asks)} asks")
            except Exception as e:
                log.error(f"OrderBook parse error: {e}")

        self._fp.on_order_book.subscribe(_on_ob)
        ob_thread = threading.Thread(
            target=self._fp.subscribe_order_book_thread,
            args=(symbol,),
            daemon=True,
            name="feed-orderbook",
        )
        ob_thread.start()
        self._threads.append(ob_thread)
        log.info("Subscribed to order book")

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
