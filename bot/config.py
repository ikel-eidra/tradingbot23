"""Configuration management for TradingBot23.

Loads settings from environment variables (.env file) with sensible defaults.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# When frozen by PyInstaller, __file__ resolves inside the temp extraction dir.
# The .env the setup wizard writes lives next to the EXE instead.
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


# --- API Keys ---
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# --- Trading Mode ---
# "live" = real orders, "paper" = simulated, "backtest" = historical
TRADING_MODE = os.getenv("TRADING_MODE", "paper")

# --- Capital & Position Sizing ---
CAPITAL_USD = float(os.getenv("CAPITAL_USD", "10000"))
PER_TRADE_PCT = float(os.getenv("PER_TRADE_PCT", "0.20"))  # 20% of current balance per coin
MONTHLY_CONTRIBUTION_USD = float(os.getenv("MONTHLY_CONTRIBUTION_USD", "0"))
MONTHLY_CONTRIBUTION_DAY = int(os.getenv("MONTHLY_CONTRIBUTION_DAY", "1"))

# --- Strategy Parameters ---
# NET take profit target (after fees). Default = 1% net profit per trade.
NET_TP_PCT = float(os.getenv("NET_TP_PCT", "0.01"))
# NET stop loss (after fees). Default = 1.5% net loss tolerance.
NET_SL_PCT = float(os.getenv("NET_SL_PCT", "0.015"))
# Binance spot trading fee per side (0.1% standard, 0.075% with BNB discount).
FEE_PCT = float(os.getenv("FEE_PCT", "0.001"))

# Gross TP must cover net target + fees on both sides (buy + sell).
# Example: NET 1% + 0.1% buy fee + 0.1% sell fee = 1.2% gross TP.
TP_PCT = NET_TP_PCT + (2 * FEE_PCT)
# Gross SL: net loss tolerance MINUS the fee drag (you lose less in price
# terms because fees already eat 0.2% on top).
SL_PCT = max(NET_SL_PCT - (2 * FEE_PCT), 0.001)

# Allow manual override of gross TP/SL via env var if user wants explicit control.
_TP_OVERRIDE = os.getenv("TP_PCT")
if _TP_OVERRIDE is not None:
    TP_PCT = float(_TP_OVERRIDE)
_SL_OVERRIDE = os.getenv("SL_PCT")
if _SL_OVERRIDE is not None:
    SL_PCT = float(_SL_OVERRIDE)

MAX_HOLD_DAYS = int(os.getenv("MAX_HOLD_DAYS", "3"))  # Auto-close after 3 days
DIP_THRESHOLD_PCT = float(os.getenv("DIP_THRESHOLD_PCT", "0.02"))  # -2% dip to enter

# --- Win-rate enhancers ---
# Once unrealized PNL crosses +BREAK_EVEN_TRIGGER_PCT, slide SL up to
# entry+fees. Converts many losers into break-evens. Set to 0 to disable.
BREAK_EVEN_TRIGGER_PCT = float(os.getenv("BREAK_EVEN_TRIGGER_PCT", "0.005"))

# After a STOP-LOSS hit on a coin, refuse new entries on that coin for
# this many hours. Prevents stacking losses on a coin in a clear downtrend.
# Set to 0 to disable.
LOSS_COOLDOWN_HOURS = float(os.getenv("LOSS_COOLDOWN_HOURS", "24"))

# After a TP hit on a coin, wait this many hours before re-entering.
# Prevents the bot from scalping the same coin in a loop.
# Set to 0 to disable (re-enter immediately).
TP_COOLDOWN_HOURS = float(os.getenv("TP_COOLDOWN_HOURS", "1"))

# Skip new entries when BTC's 1h change is below this (negative) value.
# Avoids buying alts during broad market dumps. Set to None to disable.
_BTC_FILTER = os.getenv("BTC_REGIME_FILTER_PCT", "-0.015")
BTC_REGIME_FILTER_PCT = float(_BTC_FILTER) if _BTC_FILTER and _BTC_FILTER.lower() != "none" else None

# --- Coin Selection ---
TOP_N_COINS = int(os.getenv("TOP_N_COINS", "50"))
TOP_N_LOSERS = int(os.getenv("TOP_N_LOSERS", "5"))
MIN_VOLUME_USD = float(os.getenv("MIN_VOLUME_USD", "50000000"))  # $50M
SNAPSHOT_DAY = int(os.getenv("SNAPSHOT_DAY", "1"))  # Day of month

# --- Bot Operation ---
CHECK_INTERVAL_HOURS = float(os.getenv("CHECK_INTERVAL_HOURS", "1"))
POSITION_CHECK_MINS = float(os.getenv("POSITION_CHECK_MINS", "5"))  # how often to check TP/SL

# --- Engine selection ---
# "spot"    = Binance spot trading (default, no leverage)
# "futures" = Binance USDT-M Perpetual Futures (paper-only initially)
ENGINE = os.getenv("ENGINE", "futures")

# --- Futures settings (only used when ENGINE=futures) ---
LEVERAGE = int(os.getenv("LEVERAGE", "1"))  # 1x — same risk as spot, lower fees
MAX_LEVERAGE = 5  # Hard cap for safety
FUTURES_FEE_PCT = float(os.getenv("FUTURES_FEE_PCT", "0.0006"))  # 0.06% taker (Binance USDT-M)
# Average daily funding cost as % of notional. Binance posts every 8h.
# Historical average is ~0.01% per 8h = 0.03% per day. Conservative default.
FUNDING_RATE_DAILY = float(os.getenv("FUNDING_RATE_DAILY", "0.0003"))
# Net targets when running futures — usually smaller because leverage amplifies them.
# At 2x leverage, a 0.5% net price move = ~1% net PNL on margin.
FUTURES_NET_TP_PCT = float(os.getenv("FUTURES_NET_TP_PCT", "0.01"))   # 1% net — matches backtest
FUTURES_NET_SL_PCT = float(os.getenv("FUTURES_NET_SL_PCT", "0.015"))  # 1.5% net — reference only when SL disabled
# At 1x leverage, top-50 coins historically rebound — hold until TP or expiry, no SL.
# Set to "true" only if you want hard stop-losses re-enabled.
FUTURES_USE_SL = os.getenv("FUTURES_USE_SL", "false").lower() == "true"
# 5-minute dip threshold for futures (smaller than spot's 24h threshold)
FUTURES_DIP_THRESHOLD_PCT = float(os.getenv("FUTURES_DIP_THRESHOLD_PCT", "0.005"))  # -0.5% in 5m

# --- Crash Detection ---
# BTC 24h drop below this triggers crash mode: blocks entries + arms emergency SL.
# -8% based on historical crashes (May 2021: -30%, Nov 2022: -16%, Aug 2024: -9%)
CRASH_BTC_TRIGGER_PCT = float(os.getenv("CRASH_BTC_TRIGGER_PCT", "-0.08"))
# BTC 24h must recover above this before normal trading resumes (hysteresis gap).
CRASH_BTC_RECOVERY_PCT = float(os.getenv("CRASH_BTC_RECOVERY_PCT", "-0.05"))
# Emergency SL is set this far below current price when crash mode activates.
# 3% below current = protects most remaining margin while allowing small bounces.
CRASH_SL_PCT = float(os.getenv("CRASH_SL_PCT", "0.03"))

# --- Stablecoins to exclude ---
STABLECOIN_SYMBOLS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "USDP", "PYUSD",
    "USDE", "USD1", "USDD", "FRAX", "LUSD", "SUSD", "GUSD", "CUSD",
    "USDS", "USDX", "CUSDC", "ALUSD", "DOLA", "BEAN", "USDJ", "HUSD",
}

# --- Paths ---
# Can be overridden per-user via env vars (multi-user Docker setup).
DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data")))
LOG_DIR = Path(os.getenv("LOG_DIR", str(PROJECT_ROOT / "logs")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# --- Binance ---
# Use testnet for paper trading
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "false").lower() == "true"

# --- Telegram alerts ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def validate():
    """Validate config and warn on unfavorable risk/reward setups."""
    import logging
    log = logging.getLogger(__name__)

    errors = []
    if TRADING_MODE == "live":
        if not BINANCE_API_KEY:
            errors.append("BINANCE_API_KEY is required for live trading")
        if not BINANCE_API_SECRET:
            errors.append("BINANCE_API_SECRET is required for live trading")
    if errors:
        raise ValueError("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))

    # --- Risk/reward sanity checks ---
    # Required win rate to break even: SL / (TP + SL)
    if NET_TP_PCT > 0 and NET_SL_PCT > 0:
        rr_ratio = NET_SL_PCT / NET_TP_PCT  # risk units per reward unit
        breakeven_winrate = NET_SL_PCT / (NET_TP_PCT + NET_SL_PCT) * 100

        log.info(
            "Strategy parameters: NET TP %.3f%% / NET SL %.3f%% / "
            "Gross TP %.3f%% / Gross SL %.3f%% / Fee %.3f%% per side",
            NET_TP_PCT * 100, NET_SL_PCT * 100,
            TP_PCT * 100, SL_PCT * 100, FEE_PCT * 100,
        )
        log.info(
            "Risk:Reward = %.2f:1 | Required win rate to break even: %.1f%%",
            rr_ratio, breakeven_winrate,
        )

        if breakeven_winrate >= 90:
            log.warning(
                "⚠️  EXTREME RISK: Break-even win rate is %.1f%%. "
                "This is rarely sustainable in live markets. "
                "Consider tightening SL or raising NET_TP_PCT.",
                breakeven_winrate,
            )
        elif breakeven_winrate >= 80:
            log.warning(
                "⚠️  HIGH RISK: Break-even win rate is %.1f%%. "
                "Backtest carefully before going live.",
                breakeven_winrate,
            )

    # Warn if gross TP is smaller than the spread/fee buffer is realistic for
    if TP_PCT < 2 * FEE_PCT:
        log.warning(
            "⚠️  Gross TP (%.3f%%) is smaller than round-trip fees (%.3f%%). "
            "Profitable trades are mathematically impossible — adjust NET_TP_PCT.",
            TP_PCT * 100, 2 * FEE_PCT * 100,
        )
