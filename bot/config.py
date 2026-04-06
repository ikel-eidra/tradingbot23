"""Configuration management for TradingBot23.

Loads settings from environment variables (.env file) with sensible defaults.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


# --- API Keys ---
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
CMC_API_KEY = os.getenv("CMC_API_KEY", "")

# --- Trading Mode ---
# "live" = real orders, "paper" = simulated, "backtest" = historical
TRADING_MODE = os.getenv("TRADING_MODE", "paper")

# --- Capital & Position Sizing ---
CAPITAL_USD = float(os.getenv("CAPITAL_USD", "10000"))
PER_TRADE_PCT = float(os.getenv("PER_TRADE_PCT", "0.20"))  # 20% of current balance per coin

# --- Strategy Parameters ---
TP_PCT = float(os.getenv("TP_PCT", "0.02"))           # +2% take profit
SL_PCT = float(os.getenv("SL_PCT", "0.015"))          # -1.5% stop loss
MAX_HOLD_DAYS = int(os.getenv("MAX_HOLD_DAYS", "3"))  # Auto-close after 3 days
DIP_THRESHOLD_PCT = float(os.getenv("DIP_THRESHOLD_PCT", "0.02"))  # -2% dip to enter

# --- Coin Selection ---
TOP_N_COINS = int(os.getenv("TOP_N_COINS", "50"))
TOP_N_LOSERS = int(os.getenv("TOP_N_LOSERS", "10"))
MIN_VOLUME_USD = float(os.getenv("MIN_VOLUME_USD", "50000000"))  # $50M
SNAPSHOT_DAY = int(os.getenv("SNAPSHOT_DAY", "1"))  # Day of month

# --- Bot Operation ---
CHECK_INTERVAL_HOURS = float(os.getenv("CHECK_INTERVAL_HOURS", "1"))

# --- Stablecoins to exclude ---
STABLECOIN_SYMBOLS = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "USDP", "PYUSD"}

# --- Paths ---
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# --- Binance ---
# Use testnet for paper trading
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "false").lower() == "true"

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def validate():
    """Validate that required configuration is present."""
    errors = []
    if TRADING_MODE == "live":
        if not BINANCE_API_KEY:
            errors.append("BINANCE_API_KEY is required for live trading")
        if not BINANCE_API_SECRET:
            errors.append("BINANCE_API_SECRET is required for live trading")
    if not CMC_API_KEY:
        errors.append("CMC_API_KEY is required to fetch market data")
    if errors:
        raise ValueError("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))
