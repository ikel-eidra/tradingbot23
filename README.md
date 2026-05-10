# TradingBot23

**Automated crypto futures mean-reversion bot with a paper trading GUI, persistent trade history, and Telegram alerts.**

Runs on Windows as a standalone EXE — no Python, no coding required for end users.

---

## What It Does

TradingBot23 identifies the **top 5 biggest losers** among the top 50 coins by market cap each month, then trades them using a mean-reversion strategy: large-cap coins that dip tend to bounce back. The bot buys the dip, waits for the bounce, and exits at a fixed profit target.

**Data sources (100% free, no API key required):**
- [CoinGecko](https://coingecko.com) — market cap rankings and 24h price changes
- [Binance public API](https://binance.com) — real-time prices for TP/SL monitoring

---

## Quick Start — Windows EXE (No Python needed)

1. Download `TradingBot23.exe` from [Releases](https://github.com/michaelfutol/tradingbot23/releases)
2. Extract and open the `.env` file with Notepad
3. Add your Binance API keys (read-only keys work for paper trading)
4. Double-click `TradingBot23.exe`

The bot runs in **paper trading mode only**. It does not place real orders.

---

## Dashboard

The app has a full GUI with four tabs:

| Tab | What you see |
|---|---|
| **Live** | Open positions with entry price, current price, entry leverage, P&L%, TP, liquidation price, age |
| **Charts** | Equity curve, trade return distribution, exit breakdown pie, cumulative P&L |
| **History** | Every trade ever made, loaded from disk, plus exportable performance reports |
| **Settings** | Change leverage, TP%, SL on/off, capital, monthly contribution, max hold days, and view the next 12 contribution markers |

---

## Strategy

### Core Thesis

Top-50 coins by market cap (BTC, ETH, SOL, BNB, etc.) have strong institutional backing and historically rebound from short-term dips within days. The strategy systematically buys these dips and exits at a small profit target.

### Rules

| Parameter | Value | Description |
|---|---|---|
| Universe | Top 50 by market cap | Via CoinGecko free API, no key needed |
| Basket | Top 5 worst 24h performers | Locked monthly, refreshed on the 1st |
| Entry signal | 24h change ≤ −2% **OR** slot empty | Scanned every 5 minutes |
| Take profit | +1% NET (after all fees) | Gross price target auto-computed |
| Stop loss | Disabled by default | At 1x leverage, top-50 coins rebound reliably |
| Liquidation guard | Always active | Refuses trades where SL would breach liquidation price |
| Max hold | 3 days | Auto-close at market if TP not reached |
| Position size | 20% of portfolio | Dynamic compounding — grows with your portfolio |
| Engine | Futures 1x (default) | One-cycle paper entries/exits with futures fee and liquidation modeling |
| Leverage | 1x–20x (paper configurable) | Higher settings are for paper stress testing only |

### Execution Flow

```
1st of month ──► CoinGecko: fetch top 50 by market cap
                      │
                      ▼
                 Rank by 24h % change (ascending)
                      │
                      ▼
                 Lock top 5 losers as monthly basket
                      │
                      ▼
              Every 5 minutes:
                ├── Check open positions (TP / liquidation / expiry)
                ├── Scan basket coins for −2% dip → open position
                └── Fill any empty slots with worst performers (always invested)
```

### Win-Rate Enhancers

- **Break-even SL trailing** — once a position moves +0.5% in your favor, the stop slides up to entry + fees. Even if price reverses, you exit at near-zero loss instead of the full stop.
- **Loss cooldown** — after a stop hit on a coin, that coin is blocked for 24 hours to avoid stacking losses on a falling knife.
- **TP cooldown** — after a TP hit on a coin, waits 1 hour before re-entering the same coin (prevents scalping the same coin in a loop).
- **Always invested** — if a basket slot is empty and you have free cash, the bot fills it with the current worst performer without waiting for a −2% dip signal.

---

## Architecture

```
tradingbot23/
├── bot/
│   ├── config.py                  # All settings loaded from .env
│   ├── dashboard.py               # Tkinter GUI — 4 tabs (Live, Charts, History, Settings)
│   ├── setup_wizard.py            # First-run GUI wizard
│   ├── main.py                    # Entry point
│   └── modules/
│       ├── data_fetcher.py        # CoinGecko API — rankings, 24h changes, snapshots
│       ├── futures_trader.py      # Paper futures engine with leverage, funding, liquidation
│       ├── strategy.py            # Basket logic, dip detection, fill_empty_slots
│       ├── telegram_notifier.py   # Trade alerts via Telegram bot
│       └── backtester.py          # Historical simulation using Binance klines
├── data/                          # Monthly snapshots (JSON) + trade_history.csv
├── logs/                          # Daily log files
├── .env.example                   # Configuration template
├── tradingbot23.spec              # PyInstaller spec for Windows EXE build
└── README.md
```

---

## Configuration

All settings live in `.env`. The Settings tab in the GUI lets you change most of these at runtime without restarting.

### Core Settings

| Variable | Default | Description |
|---|---|---|
| `BINANCE_API_KEY` | — | Binance API key (read-only for paper mode) |
| `BINANCE_API_SECRET` | — | Binance API secret |
| `TRADING_MODE` | `paper` | Futures are paper-only; no live execution path is implemented |
| `ENGINE` | `futures` | Futures-only; spot trading is intentionally disabled |
| `CAPITAL_USD` | `500` | Starting paper capital in USD |
| `LEVERAGE` | `1` | 1x–20x paper setting. 1x is the lowest-risk futures setting |
| `TOP_N_COINS` | `50` | Market cap universe (top 50 recommended) |
| `TOP_N_LOSERS` | `5` | Basket size (max simultaneous positions) |
| `PER_TRADE_PCT` | `0.20` | 20% of portfolio per trade |
| `MONTHLY_CONTRIBUTION_USD` | `0` | Paper cash added once per month; set `100` to simulate adding $100/month |
| `MONTHLY_CONTRIBUTION_DAY` | `1` | Day of month to apply the paper contribution |

### Profit / Risk Settings

| Variable | Default | Description |
|---|---|---|
| `FUTURES_NET_TP_PCT` | `0.01` | 1% net profit target after fees |
| `FUTURES_NET_SL_PCT` | `0.015` | 1.5% net SL reference (only used if SL enabled) |
| `FUTURES_USE_SL` | `false` | Enable hard stop-loss (disabled by default) |
| `MAX_HOLD_DAYS` | `3` | Auto-close after N days |
| `DIP_THRESHOLD_PCT` | `0.02` | Entry trigger: −2% 24h change |
| `BREAK_EVEN_TRIGGER_PCT` | `0.005` | Slide SL to break-even after +0.5% move |
| `LOSS_COOLDOWN_HOURS` | `24` | Hours to skip a coin after SL hit |
| `TP_COOLDOWN_HOURS` | `1` | Hours to skip a coin after TP hit |

### Telegram Alerts (Optional)

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather on Telegram |
| `TELEGRAM_CHAT_ID` | Your chat ID (get it from @userinfobot) |

**Setup:**
1. Message @BotFather → `/newbot` → copy the token
2. Message @userinfobot → copy your ID
3. Paste both into `.env` — alerts activate immediately on next restart

**You'll receive alerts for:**
- Every trade opened (coin, entry price, margin, leverage)
- Every trade closed (exit price, net P&L, portfolio value)
- Daily summary at midnight UTC

---

## Persistent Trade History

Every closed trade is appended to `data/trade_history.csv`. This file:
- Survives app restarts
- Is loaded on startup so stats (win rate, total P&L) are always correct
- Can be opened in Excel for analysis
- Is the data source for the History tab in the GUI
- Can be summarized from the History tab with **Export Report**

CSV columns: `open_time, close_time, symbol, engine, entry_price, exit_price, amount_usd, notional, leverage, pnl_pct, pnl_usd, funding_paid, reason, entry_change_24h`

Monthly paper contributions are tracked in `data/account_state.json` so restarts do not double-add the same month. The Settings tab shows a 12-month contribution schedule with paid/due/scheduled markers. Dashboard portfolio P&L uses total contributed capital, not only starting capital, so deposits are not counted as profit.

---

## Futures Engine

TradingBot23 is futures-only by design. Spot support was removed because spot/OCO execution needs separate exchange cycles to complete a turnabout, while this app focuses on one-cycle futures-style paper entries and exits.

| Control | Detail |
|---|---|
| Execution | Paper-only futures simulation |
| Leverage | 1x–20x (default 1x, paper-only) |
| Fees | 0.06% per side on notional |
| Funding cost | ~0.03%/day modeled |
| Liquidation | Tracked; unsafe entries are refused |
| Real orders | Not implemented |

---

## Building the EXE (Developers)

```bash
git clone https://github.com/ikel-eidra/tradingbot23.git
cd tradingbot23
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pyinstaller tradingbot23.spec --clean
```

The EXE appears in `dist/TradingBot23.exe`. Copy the entire `dist/` folder to share — the `.env` file must travel with the EXE.

---

## Risk Management

| Control | Detail |
|---|---|
| Paper mode only | No real orders are placed; live futures execution is not implemented |
| 1x leverage default | Lowest-risk futures setting with wide liquidation distance |
| Liquidation guard | At any leverage, refuses new trades where the SL would breach the liquidation price |
| One position per coin | Duplicate entries blocked at trader level |
| Cash safety check | Position size capped at available cash |
| Dynamic sizing | Losses reduce exposure automatically; gains increase it |
| Stablecoin filter | USDT, USDC, USDE, USD1, DAI, BUSD and others excluded from basket |
| Max hold enforcement | Stale positions auto-closed after configured holding period |
| TP/Loss cooldowns | Prevents re-entering a coin immediately after a win or loss |

---

## Backtest Results (12-month simulation, May 2025 – May 2026)

Backtested on real Binance kline data using the same strategy logic:

| Metric | Result |
|---|---|
| Starting capital | $10,000 |
| Final portfolio | ~$18,080 |
| Total return | **+80.8%** |
| Win rate | **77.3%** |
| Engine | Futures 1x |
| TP / SL | 1% net / 1.5% net |

> Past results do not guarantee future performance.

---

## Disclaimer

This software is provided for **educational and research purposes only**. Cryptocurrency trading carries substantial financial risk. The authors accept no liability for financial losses. Always start with paper trading. Never allocate capital you cannot afford to lose.

---

## License

MIT
