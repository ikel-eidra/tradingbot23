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

# The app must prefer the .env beside the EXE/project over machine-wide
# variables so unrelated bots cannot hijack credentials such as Telegram tokens.
load_dotenv(PROJECT_ROOT / ".env", override=True)


# --- API Keys ---
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# --- Trading Mode ---
# "paper" = simulated futures trades, "backtest" = historical simulation.
# Live order execution is intentionally not implemented.
TRADING_MODE = os.getenv("TRADING_MODE", "paper")

# --- Capital & Position Sizing ---
CAPITAL_USD = float(os.getenv("CAPITAL_USD", "10000"))
PER_TRADE_PCT = float(os.getenv("PER_TRADE_PCT", "0.20"))  # 20% of current balance per coin
MONTHLY_CONTRIBUTION_USD = float(os.getenv("MONTHLY_CONTRIBUTION_USD", "0"))
MONTHLY_CONTRIBUTION_DAY = int(os.getenv("MONTHLY_CONTRIBUTION_DAY", "1"))

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

# --- Pre-trade wave analysis ---
# Before opening a futures paper trade, inspect Binance 15m candles over the
# last 24h. This blocks "falling knife" entries where the 24h dip is still
# making fresh lows instead of showing a usable bounce/rebound.
PRE_TRADE_ANALYSIS_ENABLED = os.getenv("PRE_TRADE_ANALYSIS_ENABLED", "true").lower() == "true"
PRE_TRADE_MIN_SCORE = float(os.getenv("PRE_TRADE_MIN_SCORE", "60"))
PRE_TRADE_MIN_REBOUND_PCT = float(os.getenv("PRE_TRADE_MIN_REBOUND_PCT", "0.35"))
PRE_TRADE_MAX_1H_DROP_PCT = float(os.getenv("PRE_TRADE_MAX_1H_DROP_PCT", "0.75"))
PRE_TRADE_MAX_4H_DROP_PCT = float(os.getenv("PRE_TRADE_MAX_4H_DROP_PCT", "2.50"))
PRE_TRADE_MIN_24H_RANGE_PCT = float(os.getenv("PRE_TRADE_MIN_24H_RANGE_PCT", "1.20"))
PRE_TRADE_KLINE_INTERVAL = os.getenv("PRE_TRADE_KLINE_INTERVAL", "15m")
PRE_TRADE_KLINE_LIMIT = int(os.getenv("PRE_TRADE_KLINE_LIMIT", "97"))
PRE_TRADE_BREAKDOWN_GUARD_ENABLED = os.getenv("PRE_TRADE_BREAKDOWN_GUARD_ENABLED", "true").lower() == "true"
PRE_TRADE_MAX_24H_DROP_PCT = float(os.getenv("PRE_TRADE_MAX_24H_DROP_PCT", "8.0"))
PRE_TRADE_MAX_LOWER_CLOSE_STREAK = int(os.getenv("PRE_TRADE_MAX_LOWER_CLOSE_STREAK", "5"))
PRE_TRADE_MAX_BELOW_SMA20_PCT = float(os.getenv("PRE_TRADE_MAX_BELOW_SMA20_PCT", "1.5"))
PRE_TRADE_MIN_BREAKDOWN_REBOUND_PCT = float(os.getenv("PRE_TRADE_MIN_BREAKDOWN_REBOUND_PCT", "1.0"))

# Uses the local futures paper ledger to quarantine symbols that already caused
# large realized losses. This keeps repeated "failing coin" entries out of the
# monthly basket/dip pool until the lookback window passes.
PAPER_SYMBOL_GUARD_ENABLED = os.getenv("PAPER_SYMBOL_GUARD_ENABLED", "true").lower() == "true"
PAPER_SYMBOL_GUARD_LOOKBACK_DAYS = float(os.getenv("PAPER_SYMBOL_GUARD_LOOKBACK_DAYS", "30"))
PAPER_SYMBOL_GUARD_MAX_REALIZED_LOSS_USD = float(os.getenv("PAPER_SYMBOL_GUARD_MAX_REALIZED_LOSS_USD", "50"))
PAPER_SYMBOL_GUARD_EXPIRED_LOSS_USD = float(os.getenv("PAPER_SYMBOL_GUARD_EXPIRED_LOSS_USD", "25"))
PAPER_SYMBOL_GUARD_BLOCK_LIQUIDATED = os.getenv("PAPER_SYMBOL_GUARD_BLOCK_LIQUIDATED", "true").lower() == "true"

# Skip new entries when BTC's 1h change is below this (negative) value.
# Avoids buying alts during broad market dumps. Set to None to disable.
_BTC_FILTER = os.getenv("BTC_REGIME_FILTER_PCT", "-0.015")
BTC_REGIME_FILTER_PCT = float(_BTC_FILTER) if _BTC_FILTER and _BTC_FILTER.lower() != "none" else None

# --- Coin Selection ---
TOP_N_COINS = int(os.getenv("TOP_N_COINS", "50"))
TOP_N_LOSERS = int(os.getenv("TOP_N_LOSERS", "5"))
MAX_OPEN_TRADES_CAP = 10
MAX_OPEN_TRADES = max(
    1,
    min(
        MAX_OPEN_TRADES_CAP,
        int(os.getenv("MAX_OPEN_TRADES", str(TOP_N_LOSERS))),
    ),
)
TOP_N_LOSERS = max(TOP_N_LOSERS, MAX_OPEN_TRADES)
MIN_VOLUME_USD = float(os.getenv("MIN_VOLUME_USD", "50000000"))  # $50M
SNAPSHOT_DAY = int(os.getenv("SNAPSHOT_DAY", "1"))  # Day of month

# --- Bot Operation ---
CHECK_INTERVAL_HOURS = float(os.getenv("CHECK_INTERVAL_HOURS", "1"))
POSITION_CHECK_MINS = float(os.getenv("POSITION_CHECK_MINS", "5"))  # how often to check TP/SL
AUTO_START_FUTURES = os.getenv("AUTO_START_FUTURES", "false").lower() == "true"
SETTINGS_CONFIRMED = os.getenv("SETTINGS_CONFIRMED", "false").lower() == "true"
BOT_PROFILE = os.getenv("BOT_PROFILE", "default").strip() or "default"

# --- Professional risk controls ---
# Zero disables each limit. These are enforced by the dashboard control loop.
RISK_MAX_DAILY_LOSS_USD = float(os.getenv("RISK_MAX_DAILY_LOSS_USD", "0"))
RISK_MAX_OPEN_EXPOSURE_USD = float(os.getenv("RISK_MAX_OPEN_EXPOSURE_USD", "0"))
RISK_MAX_LOSS_STREAK = int(os.getenv("RISK_MAX_LOSS_STREAK", "0"))

# --- P2P realism / sizing controls ---
# Paper P2P still uses live listings, then applies these conservative buffers.
P2P_MAX_ROUTE_PHP = float(os.getenv("P2P_MAX_ROUTE_PHP", "0"))
P2P_DEFAULT_CAPITAL_PHP = float(os.getenv("P2P_DEFAULT_CAPITAL_PHP", "500000"))
P2P_MIN_NET_PCT = float(os.getenv("P2P_MIN_NET_PCT", "0.10"))
P2P_TRANSFER_FEE_USDT = float(os.getenv("P2P_TRANSFER_FEE_USDT", "1.0"))
P2P_BUFFER_PHP = float(os.getenv("P2P_BUFFER_PHP", "0"))
P2P_SETTLEMENT_DELAY_MINS = float(os.getenv("P2P_SETTLEMENT_DELAY_MINS", "20"))
P2P_CANCEL_RATE_PCT = float(os.getenv("P2P_CANCEL_RATE_PCT", "2"))
P2P_SPREAD_DECAY_PCT = float(os.getenv("P2P_SPREAD_DECAY_PCT", "0.03"))
P2P_MODE = os.getenv("P2P_MODE", "paper").strip().lower()
if P2P_MODE not in {"paper", "live"}:
    P2P_MODE = "paper"
P2P_TG_ALERTS = os.getenv("P2P_TG_ALERTS", "true").lower() == "true"
P2P_AUTO_CYCLE = os.getenv("P2P_AUTO_CYCLE", "false").lower() == "true"
P2P_AUTO_HOLD_SELL = os.getenv("P2P_AUTO_HOLD_SELL", "true").lower() == "true"
P2P_AUTO_WATCH_LOG = os.getenv("P2P_AUTO_WATCH_LOG", "false").lower() == "true"

# --- Engine selection ---
# Futures-only. Spot trading is intentionally disabled; it needs two exchange
# cycles to complete a turnabout, while this app focuses on futures paper flow.
ENGINE = "futures"

# --- Futures settings ---
LEVERAGE = int(os.getenv("LEVERAGE", "1"))  # 1x default; paper cap below.
MAX_LEVERAGE = 20  # Paper-only cap for stress testing leverage behavior.
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
# 5-minute dip threshold for optional short-window futures entry checks.
FUTURES_DIP_THRESHOLD_PCT = float(os.getenv("FUTURES_DIP_THRESHOLD_PCT", "0.005"))  # -0.5% in 5m

# --- Crash Detection ---
# BTC 24h drop below this triggers crash mode: blocks entries + arms emergency SL.
# 20x paper exposure needs an earlier guard than historical full-crash thresholds.
CRASH_BTC_TRIGGER_PCT = float(os.getenv("CRASH_BTC_TRIGGER_PCT", "-0.04"))
# BTC 24h must recover above this before normal trading resumes (hysteresis gap).
CRASH_BTC_RECOVERY_PCT = float(os.getenv("CRASH_BTC_RECOVERY_PCT", "-0.02"))
# Emergency SL is set this far below current price when crash mode activates.
# 1.5% below current protects 20x paper margin while allowing small bounces.
CRASH_SL_PCT = float(os.getenv("CRASH_SL_PCT", "0.015"))

# --- Stablecoins to exclude ---
STABLECOIN_SYMBOLS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "USDP", "PYUSD",
    "USDE", "USD1", "USDD", "FRAX", "LUSD", "SUSD", "GUSD", "CUSD",
    "USDS", "USDX", "CUSDC", "ALUSD", "DOLA", "BEAN", "USDJ", "HUSD",
    "USDG", "USD0", "USDF", "USDL", "USDR", "USR", "USYC", "RLUSD",
    "USDTB", "USDSB", "USDM", "EURC", "EURS", "AEUR", "EURI",
}
FUTURES_EXCLUDED_SYMBOLS = set(STABLECOIN_SYMBOLS)


def is_futures_excluded_symbol(symbol: str) -> bool:
    return (symbol or "").upper() in FUTURES_EXCLUDED_SYMBOLS

# --- Paths ---
# Can be overridden per-user via env vars (multi-user Docker setup). BOT_PROFILE
# keeps beta-testers from inheriting another user's paper trades when they want
# a clean local profile. The default profile preserves the existing data path.
_profile_slug = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in BOT_PROFILE)
_default_data_dir = PROJECT_ROOT / "data"
if "DATA_DIR" in os.environ:
    DATA_DIR = Path(os.getenv("DATA_DIR", str(_default_data_dir)))
elif _profile_slug and _profile_slug.lower() != "default":
    DATA_DIR = _default_data_dir / f"profile_{_profile_slug}"
else:
    DATA_DIR = _default_data_dir
LOG_DIR = Path(os.getenv("LOG_DIR", str(PROJECT_ROOT / "logs")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# --- Binance ---
# Use testnet for paper trading
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "false").lower() == "true"

# --- Telegram alerts ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ALLOW_CONTROL = os.getenv("TELEGRAM_ALLOW_CONTROL", "false").lower() == "true"

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def validate():
    """Validate config and warn on unfavorable risk/reward setups."""
    import logging
    log = logging.getLogger(__name__)

    errors = []
    if TRADING_MODE == "live":
        errors.append("Live trading is not implemented. Use TRADING_MODE=paper.")
    if errors:
        raise ValueError("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))

    # --- Risk/reward sanity checks ---
    # Required win rate to break even: SL / (TP + SL)
    if FUTURES_NET_TP_PCT > 0 and FUTURES_NET_SL_PCT > 0:
        rr_ratio = FUTURES_NET_SL_PCT / FUTURES_NET_TP_PCT
        breakeven_winrate = FUTURES_NET_SL_PCT / (FUTURES_NET_TP_PCT + FUTURES_NET_SL_PCT) * 100

        log.info(
            "Strategy parameters: NET TP %.3f%% / NET SL %.3f%% / "
            "Futures fee %.3f%% per side",
            FUTURES_NET_TP_PCT * 100, FUTURES_NET_SL_PCT * 100,
            FUTURES_FEE_PCT * 100,
        )
        log.info(
            "Risk:Reward = %.2f:1 | Required win rate to break even: %.1f%%",
            rr_ratio, breakeven_winrate,
        )

        if breakeven_winrate >= 90:
            log.warning(
                "⚠️  EXTREME RISK: Break-even win rate is %.1f%%. "
                "This is rarely sustainable. "
                "Consider tightening SL or raising FUTURES_NET_TP_PCT.",
                breakeven_winrate,
            )
        elif breakeven_winrate >= 80:
            log.warning(
                "⚠️  HIGH RISK: Break-even win rate is %.1f%%. "
                "Backtest carefully before increasing capital.",
                breakeven_winrate,
            )
