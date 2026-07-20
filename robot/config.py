"""Robot configuration."""
import os

# Finam credentials
# Finam gRPC access token (FinamPy uses this)
FINAM_TOKEN = os.environ.get("FINAM_API_KEY", "")
FINAM_API_KEY = os.environ.get("FINAM_API_KEY", "")
FINAM_ACCOUNT_ID = os.environ.get("FINAM_ACCOUNT_ID", "1225953")

# Trading (override via env vars for multi-instance)
SYMBOL = os.environ.get("ROBOT_SYMBOL", "SiU6@RTSX")
TICKER = os.environ.get("ROBOT_TICKER", "SiU6")
TIMEFRAME = os.environ.get("ROBOT_TIMEFRAME", "M1")

# gRPC reconnect
GRPC_RECONNECT_SEC = 5

# Warmup candles (REST)
WARMUP_BARS = 120
