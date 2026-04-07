"""Main entry point and CLI for TradingBot23.

Usage:
    python -m bot.main --mode paper              # Paper trading (default)
    python -m bot.main --mode live                # Live trading on Binance
    python -m bot.main --mode backtest --start 2025-04-01 --end 2026-03-31
    python -m bot.main --mode paper --force-snapshot
"""

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from bot import config
from bot.modules.backtester import Backtester
from bot.modules.data_fetcher import DataFetcher
from bot.modules.futures_trader import FuturesTrader
from bot.modules.strategy import Strategy
from bot.modules.trader import Trader

# Graceful shutdown
_running = True


def _signal_handler(sig, frame):
    global _running
    _running = False
    print("\nShutting down gracefully...")


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def setup_logging():
    """Configure logging to console and file."""
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_file = config.LOG_DIR / f"bot_{datetime.now().strftime('%Y%m%d')}.log"

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file),
    ]

    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format=log_format,
        handlers=handlers,
    )


def run_trading(force_snapshot: bool = False):
    """Run the trading bot in live or paper mode."""
    setup_logging()
    logger = logging.getLogger(__name__)

    # Validate config
    try:
        config.validate()
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    logger.info("Starting TradingBot23 in %s mode", config.TRADING_MODE.upper())
    logger.info("Capital: $%.2f | TP: %.1f%% | SL: %.1f%% | Max Hold: %d days",
                config.CAPITAL_USD, config.TP_PCT * 100, config.SL_PCT * 100, config.MAX_HOLD_DAYS)
    logger.info("Position size: %.0f%% of portfolio (compounding)", config.PER_TRADE_PCT * 100)

    fetcher = DataFetcher()
    if config.ENGINE == "futures":
        logger.info("Engine: FUTURES (paper-only, %dx leverage)", config.LEVERAGE)
        trader = FuturesTrader()
    else:
        logger.info("Engine: SPOT")
        trader = Trader()
    strategy = Strategy(fetcher=fetcher, trader=trader)

    if force_snapshot:
        logger.info("Forcing new monthly snapshot...")
        strategy.refresh_basket()

    interval_seconds = config.CHECK_INTERVAL_HOURS * 3600

    while _running:
        try:
            summary = strategy.run_cycle()
            status = strategy.get_status()
            logger.info(
                "Status | Portfolio: $%.2f | Cash: $%.2f | Open: %d | Basket: %s",
                status["portfolio_value"],
                status["cash_balance"],
                status["open_positions"],
                status["basket"],
            )
        except Exception:
            logger.exception("Error in trading cycle")

        # Wait for next cycle
        logger.info("Next check in %.1f hours...", config.CHECK_INTERVAL_HOURS)
        for _ in range(int(interval_seconds)):
            if not _running:
                break
            time.sleep(1)

    # Final status
    logger.info("Bot stopped. Final stats:")
    stats = strategy.trader.get_stats()
    portfolio = strategy.trader.get_portfolio_value()
    logger.info("Portfolio: $%.2f | Trades: %d | Win rate: %.1f%% | PNL: %+.2f%%",
                portfolio, stats.get("total_trades", 0),
                stats.get("win_rate", 0), stats.get("total_pnl", 0))


def run_backtest(start_str: str, end_str: str):
    """Run historical backtest."""
    setup_logging()
    logger = logging.getLogger(__name__)

    start = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    logger.info("Running backtest from %s to %s", start.date(), end.date())
    logger.info("Capital: $%.2f | TP: %.1f%% | SL: %.1f%% | Position: %.0f%% (compounding)",
                config.CAPITAL_USD, config.TP_PCT * 100, config.SL_PCT * 100, config.PER_TRADE_PCT * 100)

    backtester = Backtester()
    result = backtester.run(start, end)

    print(result.summary())

    # Save detailed results
    results_file = config.DATA_DIR / f"backtest_{start_str}_{end_str}.txt"
    results_file.write_text(result.summary())

    # Save trade log
    trades_file = config.DATA_DIR / f"backtest_trades_{start_str}_{end_str}.csv"
    with open(trades_file, "w") as f:
        f.write("symbol,entry_date,entry_price,exit_date,exit_price,pnl_pct,pnl_usd,reason\n")
        for t in result.trades:
            f.write(
                f"{t.symbol},{t.entry_date},{t.entry_price:.4f},"
                f"{t.exit_date},{t.exit_price:.4f},"
                f"{t.pnl_pct:+.2f},{t.pnl_usd:+.2f},{t.exit_reason}\n"
            )
    logger.info("Results saved to %s and %s", results_file, trades_file)


def main():
    parser = argparse.ArgumentParser(
        description="TradingBot23 - Top 10 Losers Mean-Reversion Spot Trading Bot",
    )
    parser.add_argument(
        "--mode", choices=["live", "paper", "backtest"], default="paper",
        help="Trading mode (default: paper)",
    )
    parser.add_argument(
        "--start", type=str, default="2025-04-01",
        help="Backtest start date YYYY-MM-DD (default: 2025-04-01)",
    )
    parser.add_argument(
        "--end", type=str, default="2026-03-31",
        help="Backtest end date YYYY-MM-DD (default: 2026-03-31)",
    )
    parser.add_argument(
        "--engine", choices=["spot", "futures"], default=None,
        help="Trading engine: spot (default) or futures (paper-only)",
    )
    parser.add_argument(
        "--force-snapshot", action="store_true",
        help="Force a new monthly snapshot regardless of date",
    )

    args = parser.parse_args()

    # Override config trading mode from CLI
    config.TRADING_MODE = args.mode
    if args.engine:
        config.ENGINE = args.engine

    if args.mode == "backtest":
        run_backtest(args.start, args.end)
    else:
        run_trading(force_snapshot=args.force_snapshot)


if __name__ == "__main__":
    main()
