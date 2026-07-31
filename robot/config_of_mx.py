"""Config for Order Flow Robot — MXU6 (Индекс Мосбиржи)."""
import os

# === Broker ===
SYMBOL = os.environ.get("ROBOT_SYMBOL_MX", "MXU6@RTSX")
TICKER = os.environ.get("ROBOT_TICKER_MX", "MXU6")
ACCOUNT_ID = os.environ.get("FINAM_ACCOUNT", "1225953")
DP_URL = os.environ.get("DP_URL", "http://localhost:5060")

# === Strategy Defaults ===
# Override in of_config_mx.json
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
SIGNAL_CONFIRM_COUNT = 1    # signals for entry (1-3)
SIGNAL_CONFIRM_EXIT = 1     # signals for exit (1-3)

# CVD Acceleration + Aggression Ratio (replaces OB Imbalance)
CVD_ACCEL_PERIOD = 10         # bars for CVD acceleration calc (50 min)
CVD_ACCEL_THRESHOLD = 1000   # min CVD accel delta to signal
AGG_WINDOW = 3               # bars for rolling aggression calc
AGG_RATIO_THRESHOLD = 1.0    # min buy/sell ratio to confirm direction
USE_CVD_ACCEL = True          # enable CVD Acceleration signal
USE_AGG_RATIO = True         # use aggression ratio as filter
USE_OB_IMBALANCE = False      # disabled — replaced by CVD Accel
USE_CVD_TREND = False         # disabled — using CVD Accel instead
OB_IMBALANCE_THRESHOLD = 0.50 # legacy, kept for engine compat

# Protective filters
ENABLE_MAX_LEVELS = True
ENABLE_DAILY_STOP = True
ENABLE_OB_FILTER = True
OB_MIN_LOTS = 20
OB_SCAN_RADIUS = 50
