"""Robot configuration."""
import os

# Finam credentials
FINAM_TOKEN = os.environ.get("FINAM_TOKEN", "")
FINAM_API_KEY = os.environ.get("FINAM_API_KEY", "")
FINAM_ACCOUNT_ID = os.environ.get("FINAM_ACCOUNT_ID", "1225953")

# Trading
SYMBOL = "SiM6@RTSX"
TICKER = "SiM6"
TIMEFRAME = "M1"

# gRPC reconnect
GRPC_RECONNECT_SEC = 5

# Warmup candles (REST)
WARMUP_BARS = 120
