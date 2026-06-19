"""Config for Order Flow Robot."""
import os

# === Broker ===
SYMBOL = os.environ.get("ROBOT_SYMBOL", "SiU6")
TICKER = os.environ.get("ROBOT_TICKER", "SiU6")
ACCOUNT_ID = os.environ.get("FINAM_ACCOUNT", "1225953")
DP_URL = os.environ.get("DP_URL", "http://localhost:5060")

# === Strategy Defaults ===
# Override in of_config.json
LOTS = 1
MAX_PYRAMID_LEVELS = 5
MAX_AVERAGE_LEVELS = 100
STEP_AVERAGE = 50
STEP_PYRAMID = 35
SPREAD = 50
PARTIAL_TP = True
STOP_LOSS_MODE = "rub"       # rub / pct / pts
STOP_LOSS_VALUE = 7000
MARGIN_PER_LOT = 7000
MIN_PROFIT_PER_LOT = 30
MAX_HOLD_MINUTES = 999
TIMEFRAME = "M5"

# Signal params
ABSORPTION_THRESHOLD = 0.35
CVD_LOOKBACK = 10
OB_IMBALANCE_THRESHOLD = 0.50
SIGNAL_CONFIRM_COUNT = 1

# Protective filters
ENABLE_MAX_LEVELS = True
ENABLE_DAILY_STOP = True
ENABLE_OB_FILTER = True
OB_MIN_LOTS = 20
OB_SCAN_RADIUS = 50
