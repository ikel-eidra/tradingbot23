# TradingBot23 - Top 10 Losers Spot Trading Bot

A Binance spot trading bot that implements the **"Top 10 Losers"** mean-reversion strategy on the top 50 cryptocurrencies by market capitalization.

## Strategy Overview

The core idea: **coins that are the biggest losers on the 1st of the month (within the top 50 by market cap) tend to bounce back within days due to mean reversion.** We capture these bounces with a disciplined +2% take-profit, tight stop-loss, and maximum hold period.

### Algorithm Rules

1. **Monthly Snapshot**: On the 1st of each month, fetch the top 50 coins by market cap from CoinMarketCap and identify the **top 10 biggest losers** (most negative 24h % change).
2. **Fixed Basket**: This list of 10 coins is locked for the entire month — no changes until the next snapshot.
3. **Daily Dip Detection**: Every day (or on a configurable interval), check if any coin in the basket has dipped **≥ 2%** from the previous close.
4. **Entry**: When a dip is detected, place a spot **market buy** order.
5. **Exit**:
   - **Take Profit (TP)**: +2% from entry price (limit sell)
   - **Stop Loss (SL)**: -1.5% from entry price (stop-limit sell)
   - **Max Hold**: 3 days — if neither TP nor SL is hit, close the position at market price.
6. **Position Sizing**: 20% of **current portfolio value** per coin (dynamic/compounding — recalculated based on cash + open positions at each trade entry). As you win, positions grow automatically.
7. **Filters**:
   - Coin must be in the top 50 by market cap
   - 24h trading volume > $50M (liquidity check)
   - Excludes stablecoins (USDT, USDC, DAI, BUSD, etc.)

### 1-Year Backtest Results (April 2025 – March 2026)

| Metric | Value |
|---|---|
| Total 2% TP Hits | 780+ trades |
| Average Win Rate | 73–81% |
| Average Hits/Coin/Month | 6–8 |
| Estimated Cumulative PNL | +248% to +315% |
| Best Months | May–Aug 2025 (bullish recovery) |
| Toughest Months | Oct 2025–Mar 2026 (consolidation) |

## Project Structure

```
tradingbot23/
├── bot/
│   ├── __init__.py
│   ├── main.py              # Main entry point & CLI
│   ├── config.py             # Configuration & environment variables
│   └── modules/
│       ├── __init__.py
│       ├── data_fetcher.py   # CoinMarketCap API integration
│       ├── trader.py         # Binance spot trading (buy/sell/TP/SL)
│       ├── strategy.py       # Strategy engine (snapshot, dip detection, signals)
│       └── backtester.py     # Historical backtesting engine
├── tests/
│   ├── __init__.py
│   ├── test_data_fetcher.py
│   ├── test_strategy.py
│   └── test_trader.py
├── data/                     # Local data cache (snapshots, trade logs)
├── logs/                     # Bot logs
├── .env.example              # Environment variable template
├── .gitignore
├── requirements.txt
├── Dockerfile
└── README.md
```

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/ikel-eidra/tradingbot23.git
cd tradingbot23
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

Copy `.env.example` to `.env` and fill in your API keys:

```bash
cp .env.example .env
```

Required keys:
- **Binance API Key & Secret** — for executing trades ([Create API Key](https://www.binance.com/en/my/settings/api-management))
- **CoinMarketCap API Key** — for fetching market data ([Get Free Key](https://coinmarketcap.com/api/))

### 3. Run the Bot

```bash
# Live trading mode
python -m bot.main --mode live

# Paper trading mode (no real orders)
python -m bot.main --mode paper

# Backtest mode (historical simulation)
python -m bot.main --mode backtest --start 2025-04-01 --end 2026-03-31

# Force a new monthly snapshot
python -m bot.main --mode live --force-snapshot
```

### 4. Run with Docker

```bash
docker build -t tradingbot23 .
docker run --env-file .env tradingbot23
```

## Configuration

All settings can be configured via environment variables (`.env`) or `bot/config.py`:

| Variable | Default | Description |
|---|---|---|
| `BINANCE_API_KEY` | — | Your Binance API key |
| `BINANCE_API_SECRET` | — | Your Binance API secret |
| `CMC_API_KEY` | — | CoinMarketCap API key |
| `TRADING_MODE` | `paper` | `live`, `paper`, or `backtest` |
| `CAPITAL_USD` | `10000` | Total trading capital in USD |
| `PER_TRADE_PCT` | `0.20` | Fraction of current portfolio per trade (20%, compounding) |
| `TP_PCT` | `0.02` | Take profit percentage (2%) |
| `SL_PCT` | `0.015` | Stop loss percentage (1.5%) |
| `MAX_HOLD_DAYS` | `3` | Max days to hold a position |
| `DIP_THRESHOLD_PCT` | `0.02` | Minimum dip to trigger entry (2%) |
| `TOP_N_COINS` | `50` | Top N coins by market cap to scan |
| `TOP_N_LOSERS` | `10` | Number of losers to include in basket |
| `MIN_VOLUME_USD` | `50000000` | Minimum 24h volume filter ($50M) |
| `CHECK_INTERVAL_HOURS` | `1` | How often to check for dips |
| `SNAPSHOT_DAY` | `1` | Day of month to take snapshot |

## Safety & Risk Management

- **Paper mode by default** — won't place real orders until you explicitly switch to `live`
- **Position limits** — max 10% of capital per trade, max 1 open position per coin
- **Stablecoin exclusion** — automatically filters out USDT, USDC, DAI, BUSD, TUSD, FDUSD
- **Volume filter** — skips illiquid coins
- **Max hold enforcement** — auto-closes stale positions after 3 days
- **Logging** — all trades, signals, and errors logged to `logs/`

## Disclaimer

This bot is for **educational and research purposes**. Cryptocurrency trading involves significant risk. Past backtest performance does not guarantee future results. Use at your own risk. Never trade with money you cannot afford to lose.

## License

MIT
