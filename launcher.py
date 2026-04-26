"""EXE entry point for TradingBot23.

When double-clicked:
  1. If no .env found → shows setup wizard (GUI or terminal)
  2. Loads config from .env
  3. Starts paper trading

This is the file PyInstaller bundles into tradingbot23.exe.
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

from bot.main import main

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBot stopped.")
    except Exception as e:
        print(f"\nError: {e}")
        input("Press Enter to close...")
