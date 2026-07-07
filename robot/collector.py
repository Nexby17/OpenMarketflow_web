"""Order Flow Data Collector — records Trades + OrderBook to CSV files.

Runs alongside DataProvider, subscribes to SubscribeLatestTrades + SubscribeOrderBook,
writes timestamped CSV files for backtesting.

Files per day:
  data/of_trades_SIU6_YYYYMMDD.csv   — timestamp,price,size,side
  data/of_orderbook_SIU6_YYYYMMDD.csv — timestamp,bids,asks

Usage: python3 collector.py [--symbol SiU6] [--outdir ../backtest/data]

Run as systemd service or standalone.
"""
import sys, os
sys.path.insert(0, os.getcwd())

import argparse
import csv
import logging
import signal as sig_module
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from FinamPy import FinamPy

log = logging.getLogger("collector")


def _to_float(val) -> float:
    """Safely convert Decimal/string/None to float."""
    if val is None or val == "":
        return 0.0
    if hasattr(val, "value"):
        s = val.value
        return float(s) if s else 0.0
    return float(val)

MSK = timezone(timedelta(hours=3))

# --- Args ---
parser = argparse.ArgumentParser(description="Order Flow Data Collector")
parser.add_argument("--symbol", default="SiU6", help="Finam symbol (default: SiU6)")
parser.add_argument("--outdir", default="../backtest/data", help="Output directory")
parser.add_argument("--flush-interval", type=int, default=5, help="Flush CSV every N seconds")
args, _ = parser.parse_known_args()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

SYMBOL = args.symbol
OUTDIR = Path(args.outdir)
OUTDIR.mkdir(parents=True, exist_ok=True)

# --- State ---
fp: FinamPy | None = None
_running = True
_last_trade_ts: float = 0.0
_last_ob_ts: float = 0.0
_last_reconnect_ts: float = 0.0
STALE_LIMIT = 60  # seconds before reconnect

# Per-day file handles
_trade_files: dict[str, csv.writer] = {}
_ob_files: dict[str, csv.writer] = {}
_trade_handles: dict[str, object] = {}
_ob_handles: dict[str, object] = {}
_lock = threading.Lock()

# Stats
_trades_count = 0
_ob_count = 0
_last_flush = time.time()


def _day_str() -> str:
    """Current trading day string (MSK date)."""
    return datetime.now(MSK).strftime("%Y%m%d")


def _get_trade_writer(day: str):
    """Get or create CSV writer for trades on given day."""
    if day not in _trade_files:
        path = OUTDIR / f"of_trades_{SYMBOL}_{day}.csv"
        f = open(path, "a", newline="", buffering=1)  # Line-buffered
        writer = csv.writer(f)
        # Write header if file is new
        if os.path.getsize(path) == 0:
            writer.writerow(["timestamp", "price", "size", "side"])
        _trade_files[day] = writer
        _trade_handles[day] = f
        log.info(f"Trade log: {path}")
    return _trade_files[day]


def _get_ob_writer(day: str):
    """Get or create CSV writer for orderbook on given day."""
    if day not in _ob_files:
        path = OUTDIR / f"of_orderbook_{SYMBOL}_{day}.csv"
        f = open(path, "a", newline="", buffering=1)
        writer = csv.writer(f)
        if os.path.getsize(path) == 0:
            writer.writerow(["timestamp", "side", "price", "size", "action"])
        _ob_files[day] = writer
        _ob_handles[day] = f
        log.info(f"OB log: {path}")
    return _ob_files[day]


def _flush_all():
    """Flush all file handles."""
    with _lock:
        for f in _trade_handles.values():
            f.flush()
        for f in _ob_handles.values():
            f.flush()


def _rotate_day():
    """Close old day files, reset writers."""
    with _lock:
        # Close all current files
        for day, f in list(_trade_handles.items()):
            f.close()
            log.info(f"Closed trade file: of_trades_{SYMBOL}_{day}.csv ({_trades_count} total trades)")
        for day, f in list(_ob_handles.items()):
            f.close()
            log.info(f"Closed OB file: of_orderbook_{SYMBOL}_{day}.csv ({_ob_count} total updates)")
        _trade_files.clear()
        _trade_handles.clear()
        _ob_files.clear()
        _ob_handles.clear()


# ========== Callbacks ==========

def _on_latest_trades(event):
    """Write trades to CSV."""
    global _trades_count, _last_trade_ts
    _last_trade_ts = time.time()
    try:
        day = _day_str()
        writer = _get_trade_writer(day)
        with _lock:
            for trade in event.trades:
                price = _to_float(trade.price)
                size = int(_to_float(trade.size))
                side = int(trade.side)  # 1=BUY, 2=SELL

                ts = time.time()
                if hasattr(trade.timestamp, "seconds"):
                    ts = trade.timestamp.seconds

                writer.writerow([f"{ts:.3f}", f"{price:.0f}", size, side])
                _trades_count += 1
    except Exception as e:
        log.error(f"Trades callback error: {e}")


def _on_order_book(event):
    """Write orderbook updates to CSV."""
    global _ob_count, _last_ob_ts
    _last_ob_ts = time.time()
    try:
        day = _day_str()
        writer = _get_ob_writer(day)
        ts = time.time()
        with _lock:
            for ob in event.order_book:
                for r in ob.rows:
                    price = _to_float(r.price)
                    buy = _to_float(r.buy_size)
                    sell = _to_float(r.sell_size)
                    action = int(r.action)

                    # Write bid rows and ask rows
                    if buy > 0:
                        writer.writerow([f"{ts:.3f}", "B", f"{price:.0f}", int(buy), action])
                        _ob_count += 1
                    if sell > 0:
                        writer.writerow([f"{ts:.3f}", "S", f"{price:.0f}", int(sell), action])
                        _ob_count += 1
    except Exception as e:
        log.error(f"OB callback error: {e}")


# ========== Connection ==========

def _reconnect():
    """Kill stale FinamPy and reconnect. Guard: max once per 60s."""
    global _last_reconnect_ts, fp
    now = time.time()
    if now - _last_reconnect_ts < STALE_LIMIT:
        return
    _last_reconnect_ts = now
    log.warning(f"[WATCHDOG] No data for {STALE_LIMIT}s — reconnecting FinamPy...")
    try:
        if fp is not None:
            try:
                fp.on_latest_trades.unsubscribe_all()
                fp.on_order_book.unsubscribe_all()
            except Exception:
                pass
            try:
                fp.close()
            except Exception:
                pass
        time.sleep(1)
        fp = None
        if connect_finam():
            _last_trade_ts = time.time()
            _last_ob_ts = time.time()
            log.info("[WATCHDOG] Reconnected OK")
        else:
            log.error("[WATCHDOG] Reconnect failed")
    except Exception as e:
        log.error(f"[WATCHDOG] Error: {e}")


def connect_finam() -> bool:
    """Connect FinamPy and subscribe to Trades + OrderBook."""
    global fp, _last_trade_ts, _last_ob_ts
    token = os.environ.get("FINAM_TOKEN")
    if not token:
        log.error("FINAM_TOKEN not set!")
        return False

    fp = FinamPy(token)
    _last_trade_ts = time.time()
    _last_ob_ts = time.time()
    log.info(f"FinamPy connected. Accounts: {fp.account_ids}")

    # Subscribe to trades
    try:
        fp.on_latest_trades.subscribe(_on_latest_trades)
        t_trades = threading.Thread(
            target=fp.subscribe_latest_trades_thread,
            args=(SYMBOL,),
            daemon=True,
            name="sub-trades",
        )
        t_trades.start()
        log.info(f"Subscribed to LatestTrades: {SYMBOL}")
    except AttributeError:
        log.error("subscribe_latest_trades_thread not available — check FinamPy version")

    # Subscribe to order book
    try:
        fp.on_order_book.subscribe(_on_order_book)
        t_ob = threading.Thread(
            target=fp.subscribe_order_book_thread,
            args=(SYMBOL,),
            daemon=True,
            name="sub-orderbook",
        )
        t_ob.start()
        log.info(f"Subscribed to OrderBook: {SYMBOL}")
    except AttributeError:
        log.error("subscribe_order_book_thread not available — check FinamPy version")

    return True


# ========== Main ==========

_last_day = ""

def main_loop():
    """Periodic flush + day rotation."""
    global _last_day, _last_flush
    log.info("Collector main loop started")

    while _running:
        try:
            now = time.time()

            # Flush every N seconds
            if now - _last_flush > args.flush_interval:
                _flush_all()
                _last_flush = now

            # Day rotation check (MSK midnight)
            today = _day_str()
            if today != _last_day:
                if _last_day:  # Don't rotate on first run
                    log.info(f"Day rotation: {_last_day} → {today}")
                    _rotate_day()
                _last_day = today

            # === WATCHDOG ===
            trade_gap = now - _last_trade_ts if _last_trade_ts > 0 else 0
            ob_gap = now - _last_ob_ts if _last_ob_ts > 0 else 0
            if trade_gap > STALE_LIMIT or ob_gap > STALE_LIMIT:
                log.warning(f"[WATCHDOG] trade_gap={trade_gap:.0f}s ob_gap={ob_gap:.0f}s")
                _reconnect()

            # Stats every 60 sec
            if int(now) % 60 == 0:
                log.info(f"Stats: trades={_trades_count} ob_updates={_ob_count} trade_gap={trade_gap:.0f}s ob_gap={ob_gap:.0f}s")

            time.sleep(1)

        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(5)

    # Final flush on shutdown
    _flush_all()
    log.info(f"Final: trades={_trades_count} ob_updates={_ob_count}")


def on_shutdown(signum, frame):
    global _running
    log.info(f"Signal {signum} — shutting down...")
    _running = False
    time.sleep(2)  # Let main_loop do final flush
    _rotate_day()
    if fp:
        try:
            fp.close_channel()
        except Exception:
            pass
    sys.exit(0)


sig_module.signal(sig_module.SIGTERM, on_shutdown)
sig_module.signal(sig_module.SIGINT, on_shutdown)


if __name__ == "__main__":
    log.info(f"=== Order Flow Data Collector ===")
    log.info(f"Symbol: {SYMBOL} | Output: {OUTDIR.absolute()}")

    if not connect_finam():
        log.error("Failed to connect FinamPy — exiting")
        sys.exit(1)

    _last_day = _day_str()
    log.info(f"Trading day: {_last_day}")
    log.info("Collecting data... Press Ctrl+C to stop.")

    try:
        main_loop()
    except KeyboardInterrupt:
        on_shutdown(None, None)
