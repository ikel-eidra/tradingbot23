# TradingBot23

Automated Binance spot trading bot implementing a mean-reversion strategy on the top 50 cryptocurrencies by market capitalization.

---

## Table of Contents

- [Overview](#overview)
- [Strategy](#strategy)
- [Architecture](#architecture)
- [Getting Started](#getting-started)
- [Configuration](#configuration)
- [Usage](#usage)
- [Backtesting](#backtesting)
- [Risk Management](#risk-management)
- [Testing](#testing)
- [Deployment](#deployment)
- [Disclaimer](#disclaimer)
- [License](#license)

---

## Overview

TradingBot23 identifies the **top 10 biggest daily losers** among the top 50 coins by market cap at the start of each month. It then monitors these coins for mean-reversion opportunities — buying on dips and exiting at a fixed +2% take-profit target, with a -1.5% stop-loss and 3-day maximum holding period.

The bot connects to **Binance** for order execution and **CoinMarketCap** for market data. It supports paper trading, live trading, and historical backtesting.

## Strategy

### Core Thesis

Large-cap cryptocurrencies that experience short-term sell-offs tend to revert toward their mean within 1–3 days. By systematically buying these dips and taking quick profits, the strategy captures small, high-probability gains that compound over time.

### Rules

| Parameter | Value | Description |
|---|---|---|
| Universe | Top 50 by market cap | Scanned via CoinMarketCap on snapshot day |
| Basket Size | 10 coins | Most negative 24h % change, locked for the month |
| Entry Signal | 24h change ≤ -2% | Checked hourly (configurable) |
| Take Profit | +2% from entry | Limit sell, placed as OCO order on Binance |
| Stop Loss | -1.5% from entry | Stop-limit sell, placed as OCO order on Binance |
| Max Hold | 3 days | Auto-close at market price if neither TP nor SL fills |
| Position Size | 20% of portfolio | Dynamic — recalculated from current portfolio value |
| Filters | Volume > $50M, no stablecoins | Ensures liquidity and excludes pegged assets |

### Execution Flow

```
1st of month ──► Fetch top 50 by market cap
                  │
                  ▼
              Rank by 24h % change (ascending)
                  │
                  ▼
              Select top 10 losers ──► Lock basket for 30 days
                  │
                  ▼
              Hourly cycle:
                ├── Check open positions (TP / SL / expiry)
                ├── Scan basket for -2% dips
                └── Open new positions on detected dips
```

### Position Sizing (Compounding)

Trade amounts are calculated as a percentage of the **current portfolio value** (cash + open position market value), not the initial deposit. This means:

- Winning streaks automatically increase position sizes.
- Losing streaks automatically reduce exposure.
- Capital efficiency improves as the portfolio grows.

## Architecture

```
tradingbot23/
├── bot/
│   ├── __init__.py
│   ├── main.py                  # CLI entry point
│   ├── config.py                # Environment-driven configuration
│   └── modules/
│       ├── __init__.py
│       ├── data_fetcher.py      # CoinMarketCap API client
│       ├── trader.py            # Binance order execution & position management
│       ├── strategy.py          # Monthly snapshot, dip detection, signal dispatch
│       └── backtester.py        # Historical simulation engine
├── tests/
│   ├── test_data_fetcher.py     # 4 tests — ranking, filtering, volume, limits
│   ├── test_strategy.py         # 5 tests — basket lifecycle, dip detection
│   └── test_trader.py           # 8 tests — paper trading, TP/SL/expiry, compounding
├── data/                        # Persisted monthly snapshots (JSON)
├── logs/                        # Daily log files
├── .env.example                 # Configuration template
├── .gitignore
├── requirements.txt
├── Dockerfile
└── README.md
```

### Module Responsibilities

| Module | Responsibility |
|---|---|
| `data_fetcher.py` | Fetches top coins from CoinMarketCap, ranks losers, persists snapshots to disk |
| `trader.py` | Manages Binance connection, places market buy + OCO sell orders, tracks positions, computes portfolio value |
| `strategy.py` | Orchestrates monthly basket refresh, hourly dip scans, and trade signal execution |
| `backtester.py` | Fetches Binance historical klines, simulates the strategy day-by-day, produces trade logs and equity curves |
| `config.py` | Loads all parameters from environment variables with defaults |
| `main.py` | Parses CLI arguments, initializes modules, runs the main trading loop |

## Getting Started

### Prerequisites

- Python 3.10+
- A [Binance](https://www.binance.com/) account with API access enabled
- A [CoinMarketCap](https://coinmarketcap.com/api/) API key (free tier is sufficient)

### Installation

```bash
git clone https://github.com/ikel-eidra/tradingbot23.git
cd tradingbot23
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### API Key Setup

```bash
cp .env.example .env
```

Edit `.env` and add your keys:

```
BINANCE_API_KEY=your_key_here
BINANCE_API_SECRET=your_secret_here
CMC_API_KEY=your_coinmarketcap_key_here
```

> **Security**: The `.env` file is gitignored. Never commit API keys to version control.

## Configuration

All parameters are configurable via environment variables. Defaults are production-ready for the base strategy.

| Variable | Default | Description |
|---|---|---|
| `BINANCE_API_KEY` | — | Binance API key (required for live/paper) |
| `BINANCE_API_SECRET` | — | Binance API secret (required for live/paper) |
| `CMC_API_KEY` | — | CoinMarketCap API key (required) |
| `TRADING_MODE` | `paper` | `paper`, `live`, or `backtest` |
| `CAPITAL_USD` | `10000` | Initial trading capital (USD) |
| `PER_TRADE_PCT` | `0.20` | Position size as fraction of current portfolio value |
| `TP_PCT` | `0.02` | Take profit threshold (2%) |
| `SL_PCT` | `0.015` | Stop loss threshold (1.5%) |
| `MAX_HOLD_DAYS` | `3` | Force-close positions after N days |
| `DIP_THRESHOLD_PCT` | `0.02` | Minimum 24h decline to trigger entry (2%) |
| `TOP_N_COINS` | `50` | Market cap universe size |
| `TOP_N_LOSERS` | `10` | Number of losers in monthly basket |
| `MIN_VOLUME_USD` | `50000000` | Minimum 24h volume ($50M) |
| `CHECK_INTERVAL_HOURS` | `1` | Dip scan frequency |
| `SNAPSHOT_DAY` | `1` | Day of month for basket refresh |
| `BINANCE_TESTNET` | `false` | Use Binance testnet for development |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

## Usage

### Paper Trading (default — no real money)

```bash
python -m bot.main --mode paper
```

### Live Trading

```bash
python -m bot.main --mode live
```

### Force a Fresh Monthly Snapshot

```bash
python -m bot.main --mode paper --force-snapshot
```

## Backtesting

Run a historical simulation using Binance kline data:

```bash
python -m bot.main --mode backtest --start 2025-04-01 --end 2026-03-31
```

Output:
- Summary printed to console (trades, win rate, PNL, drawdown)
- Detailed results saved to `data/backtest_<start>_<end>.txt`
- Trade-by-trade CSV saved to `data/backtest_trades_<start>_<end>.csv`

For accurate backtests, provide historical monthly snapshots via the `basket_override` parameter in `Backtester.run()`. Without overrides, the backtester uses a default basket of well-known large-cap coins.

## Risk Management

| Control | Implementation |
|---|---|
| Paper mode by default | No real orders until `--mode live` is explicitly set |
| OCO orders | TP and SL are placed together on Binance — if one fills, the other is automatically cancelled |
| One position per coin | Duplicate entries are blocked at the trader level |
| Cash safety check | Trade size is capped at available cash balance |
| Dynamic position sizing | Losses reduce exposure automatically (compounding works both ways) |
| Max hold enforcement | Stale positions are closed after the configured holding period |
| Stablecoin exclusion | USDT, USDC, DAI, BUSD, TUSD, FDUSD, USDP, PYUSD are filtered out |
| Volume filter | Coins below $50M 24h volume are excluded |
| Testnet support | Set `BINANCE_TESTNET=true` for development against the Binance testnet |
| Logging | All trades, signals, errors, and portfolio snapshots are logged to `logs/` |

## Testing

```bash
python -m unittest discover -s tests -v
```

The test suite covers:
- Data fetcher: loser ranking, volume filtering, count limits, positive-change exclusion
- Strategy: basket refresh logic, dip detection with price data, empty basket handling
- Trader: paper buy/sell, TP/SL/expiry triggers, duplicate position blocking, compounding

## Deployment

### Docker

```bash
docker build -t tradingbot23 .
docker run --env-file .env tradingbot23
```

### Systemd (Linux)

Create `/etc/systemd/system/tradingbot23.service`:

```ini
[Unit]
Description=TradingBot23
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/tradingbot23
ExecStart=/opt/tradingbot23/venv/bin/python -m bot.main --mode live
EnvironmentFile=/opt/tradingbot23/.env
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable tradingbot23
sudo systemctl start tradingbot23
```

## Disclaimer

This software is provided for **educational and research purposes only**. Cryptocurrency trading carries substantial financial risk. Past performance — whether from backtests or live results — does not guarantee future returns. The authors accept no liability for financial losses incurred through use of this software. Trade at your own risk and never allocate capital you cannot afford to lose.

## License

MIT
