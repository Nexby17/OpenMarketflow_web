"""Config for Arbitrage Robot — universal pair trading."""
import os

# === Broker ===
DP_URL = os.environ.get("DP_URL", "http://localhost:5060")
ACCOUNT_ID = os.environ.get("FINAM_ACCOUNT", "2049688")

# === Pair ===
# NOTE: stocks on Finam Trade API use @MISX (RTSX is futures-only) — fixed 2026-09-10
SYMBOL_A = os.environ.get("ARB_SYMBOL_A", "GAZP@MISX")   # spot (MISX)
TICKER_A = os.environ.get("ARB_TICKER_A", "GAZP")
SYMBOL_B = os.environ.get("ARB_SYMBOL_B", "GZZ6@RTSX")   # futures (RTSX) - GZZ6 декабрь-26, ликвиден
TICKER_B = os.environ.get("ARB_TICKER_B", "GZZ6")

# Board codes for FinamPy subscriptions
BOARD_A = os.environ.get("ARB_BOARD_A", "MISX")    # MISX for stocks
BOARD_B = os.environ.get("ARB_BOARD_B", "RTSSTD")  # RTSSTD for futures

# === Strategy Defaults (override in arb_config.json) ===
LOTS_A = 10         # lots for instrument A (GAZP: 10 lots × 10 shares = 100)
LOTS_B = 1          # lots for instrument B (GZM6: 1 contract × 100 shares = 100)
HEDGE_RATIO = 10.0  # price_A × ratio = comparable price_B (GAZP×10 = GZ)

# Z-score / deviation entry (Fix #1, 2026-09-10)
# Default mode: annualized deviation-from-fair % (see ArbParams docs)
ENTRY_MODE = "zscore"
DEV_ANN_HIGH = 2.5    # SHORT basis: futures expensive vs fair by > 2.5% annualized
DEV_ANN_LOW = -1.0    # LONG basis: futures cheap vs fair by > 1.0% annualized
DEV_LOOKBACK = 2500        # deviation history pushes (~5 trading days @ 60s)
DEV_PUSH_INTERVAL = 60.0   # seconds between deviation history pushes
ENTRY_Z = 2.0              # legacy raw-spread zscore (spread_rub mode only)
LOOKBACK = 50              # raw-spread history pushes (spread_rub mode only)

# Dividend calendar (Fix #2): ex-dividend dates before futures expiration.
# Empty by default — fill per-pair in arb_config.json via UI or manually:
# "dividends": [{"date": "2026-07-17", "amount": 12.5}, ...]
DIVIDENDS = []

# Min profit exit — type + value (single choice)
# type: "pts" | "rub" | "pct"
MIN_PROFIT_TYPE = "rub"
MIN_PROFIT_VALUE = 20

# Risk — type + value (single choice)
# type: "stop_loss_rub" | "time_stop_min" | "max_dd_pct" | "kill_switch"
RISK_TYPE = "stop_loss_rub"
RISK_VALUE = 5000

# Execution
LEG_A_TIMEOUT = 5          # seconds to wait for limit fill
MIN_FILL_RATIO = 0.5       # min fill fraction to proceed to market leg
SLIPPAGE_BPS = 5           # max slippage in bps for market leg

# Session filter
SESSION_START = "10:00"    # MSK
SESSION_END = "18:45"      # MSK
EVENING_START = "19:00"    # MSK (optional)
EVENING_END = "23:50"      # MSK
USE_EVENING = False

# Capital
CAPITAL = 1_000_000        # ₽ available for this robot

# Paper mode
PAPER = True

# API port
PORT = 5090
