"""EXE entry point for TradingBot23.

When double-clicked:
  1. If no .env found -> shows setup wizard (GUI)
  2. Loads config from .env
  3. Opens the live dashboard with paper trading running
"""

import sys
import os
from pathlib import Path

if getattr(sys, "frozen", False):
    os.chdir(Path(sys.executable).parent)
    os.environ.setdefault("DOTENV_PATH", str(Path(sys.executable).parent / ".env"))

from bot.setup_wizard import ensure_setup

if not ensure_setup():
    print("Setup cancelled. Exiting.")
    input("Press Enter to close...")
    sys.exit(0)

import logging
from bot import config
from bot.modules.data_fetcher import DataFetcher
from bot.modules.futures_trader import FuturesTrader
from bot.modules.strategy import Strategy
from bot.modules.trader import Trader

log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
log_file = config.LOG_DIR / "bot_gui.log"
logging.basicConfig(level=logging.INFO, format=log_format,
                    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_file)])

try:
    config.validate()
except ValueError as e:
    print(f"Config error: {e}")
    input("Press Enter to close...")
    sys.exit(1)

fetcher = DataFetcher()
if config.ENGINE == "futures":
    trader = FuturesTrader()
else:
    trader = Trader()
strategy = Strategy(fetcher=fetcher, trader=trader)

from bot.dashboard import Dashboard

if __name__ == "__main__":
    try:
        dashboard = Dashboard(strategy)
        dashboard.run()
    except KeyboardInterrupt:
        print("\nBot stopped.")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        input("Press Enter to close...")
