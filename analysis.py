"""
Strategy analysis: Top-5 24h losers from Top-100, 1x futures, 20% per trade.
Runs a 12-month backtest using Binance public klines + CoinGecko coin list.
Saves charts to dist/data/analysis/
"""

import os
import time
import requests
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np

OUTPUT_DIR = "dist/data/analysis"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Strategy params (matching user's proposed setup) ──────────────────────────
TOP_N_COINS     = 100
TOP_N_LOSERS    = 5
PER_TRADE_PCT   = 0.20
DIP_THRESHOLD   = -2.0          # % daily change to trigger entry
FEE_PCT         = 0.0006        # 0.06% per side (1x futures)
GROSS_TP        = 0.0120        # 1.2% gross TP (≈1% net after 2×0.06% fees)
GROSS_SL        = 0.0130        # 1.3% gross SL
MAX_HOLD_DAYS   = 3
CAPITAL         = 500.0
BACKTEST_MONTHS = 12            # last 12 months

STABLES = {"USDT","USDC","DAI","BUSD","TUSD","FDUSD","USDP","PYUSD","USD1","USDS","USDE","FDUSD"}

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def cg_top_coins(limit=100):
    url = "https://api.coingecko.com/api/v3/coins/markets"
    coins, page = [], 1
    while len(coins) < limit:
        per = min(250, limit - len(coins))
        r = requests.get(url, params={
            "vs_currency":"usd","order":"market_cap_desc",
            "per_page":str(per),"page":str(page),"sparkline":"false"
        }, timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for e in batch:
            s = e.get("symbol","").upper()
            if s not in STABLES:
                coins.append(s)
        page += 1
        time.sleep(1.2)   # CoinGecko free rate limit
    return coins[:limit]

def binance_klines(symbol, start_dt, end_dt):
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": f"{symbol}USDT",
        "interval": "1d",
        "startTime": int(start_dt.timestamp() * 1000),
        "endTime":   int(end_dt.timestamp()   * 1000),
        "limit": 500,
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return []
        return [{
            "date":  datetime.fromtimestamp(k[0]/1000, tz=timezone.utc).date(),
            "open":  float(k[1]), "high": float(k[2]),
            "low":   float(k[3]), "close":float(k[4]),
        } for k in r.json()]
    except Exception:
        return []

def candle_on(candles, day):
    for c in candles:
        if c["date"] == day:
            return c
    return None

def prev_candle(candles, day):
    prev = None
    for c in candles:
        if c["date"] >= day:
            return prev
        prev = c
    return prev

# ── Main backtest ─────────────────────────────────────────────────────────────

def run_backtest(coins):
    now = datetime.now(timezone.utc)
    backtest_start = (now - timedelta(days=BACKTEST_MONTHS * 30)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)

    log(f"Fetching {BACKTEST_MONTHS}-month Binance klines for {len(coins)} coins…")
    fetch_from = backtest_start - timedelta(days=5)
    klines = {}
    failed = []
    for i, sym in enumerate(coins):
        data = binance_klines(sym, fetch_from, now)
        if data:
            klines[sym] = data
        else:
            failed.append(sym)
        if (i+1) % 10 == 0:
            log(f"  {i+1}/{len(coins)} fetched ({len(failed)} failed)")
        time.sleep(0.07)   # Binance 1200 req/min

    available = [s for s in coins if s in klines]
    log(f"Got data for {len(available)}/{len(coins)} coins. Failed: {failed[:10]}")

    # Month-by-month simulation
    capital    = CAPITAL
    equity     = [(backtest_start, capital)]
    all_trades = []
    monthly    = []   # list of dicts per month

    current = backtest_start
    while current <= now - timedelta(days=5):
        # Month window
        month_end_raw = (current.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        month_end = min(month_end_raw.replace(tzinfo=timezone.utc), now - timedelta(days=1))
        month_key = current.strftime("%Y-%m")

        # ── Pick basket: top-5 24h losers on day-1 of this month ──
        day1 = current.date()
        losers = []
        for sym in available:
            c  = candle_on(klines[sym], day1)
            pc = prev_candle(klines[sym], day1)
            if c and pc and pc["close"] > 0:
                chg = (c["close"] - pc["close"]) / pc["close"] * 100
                losers.append((sym, chg))
        losers.sort(key=lambda x: x[1])
        basket = [s for s,_ in losers[:TOP_N_LOSERS]]
        if not basket:
            log(f"  {month_key}: no basket data, skipping")
            current = (month_end + timedelta(days=1)).replace(day=1)
            continue

        log(f"  {month_key}: basket = {basket}")

        # ── Daily simulation ──
        dates = sorted({c["date"] for sym in basket
                        if sym in klines for c in klines[sym]
                        if current.date() <= c["date"] <= month_end.date()})

        cash        = capital
        open_pos    = []   # list of dicts
        month_trades= []

        for day in dates:
            # Check existing positions
            still_open = []
            for pos in open_pos:
                sym = pos["symbol"]
                c = candle_on(klines.get(sym,[]), day)
                if c is None:
                    still_open.append(pos)
                    continue
                held = (datetime.combine(day, datetime.min.time()).replace(tzinfo=timezone.utc) - pos["entry_date"]).days
                reason = None
                exit_p = None
                if c["high"] >= pos["tp"]:
                    reason, exit_p = "tp", pos["tp"]
                elif c["low"] <= pos["sl"]:
                    reason, exit_p = "sl", pos["sl"]
                elif held >= MAX_HOLD_DAYS:
                    reason, exit_p = "expired", c["close"]
                if reason:
                    fee  = exit_p * pos["qty"] * FEE_PCT
                    proc = exit_p * pos["qty"] - fee
                    cash += proc
                    entry_eff = pos["entry"] * (1 + FEE_PCT)
                    exit_eff  = exit_p       * (1 - FEE_PCT)
                    pnl_pct   = (exit_eff - entry_eff) / entry_eff * 100
                    pnl_usd   = (exit_eff - entry_eff) * pos["qty"]
                    t = {**pos, "exit_date": day, "exit_price": exit_p,
                         "reason": reason, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd}
                    month_trades.append(t)
                    all_trades.append(t)
                else:
                    still_open.append(pos)
            open_pos = still_open

            # New entries
            open_syms = {p["symbol"] for p in open_pos}
            for sym in basket:
                if sym in open_syms or len(open_pos) >= TOP_N_LOSERS:
                    continue
                c  = candle_on(klines.get(sym,[]), day)
                pc = prev_candle(klines.get(sym,[]), day)
                if not c or not pc or pc["close"] <= 0:
                    continue
                chg = (c["close"] - pc["close"]) / pc["close"] * 100
                if chg > DIP_THRESHOLD:
                    continue
                open_val = sum(p["qty"] * candle_on(klines.get(p["symbol"],[]),day or p["entry"])["close"]
                               if candle_on(klines.get(p["symbol"],[]),day) else p["qty"]*p["entry"]
                               for p in open_pos)
                portfolio  = cash + open_val
                trade_amt  = min(portfolio * PER_TRADE_PCT, cash)
                if trade_amt < 10:
                    continue
                ep  = c["close"]
                qty = trade_amt * (1 - FEE_PCT) / ep
                cash -= trade_amt
                open_pos.append({
                    "symbol": sym, "entry": ep, "qty": qty,
                    "tp": ep * (1 + GROSS_TP), "sl": ep * (1 - GROSS_SL),
                    "entry_date": datetime.combine(day, datetime.min.time()).replace(tzinfo=timezone.utc),
                    "amount_usd": trade_amt, "trigger_chg": chg,
                })

        # Close remaining at month end
        for pos in open_pos:
            last = dates[-1] if dates else month_end.date()
            c = candle_on(klines.get(pos["symbol"],[]), last)
            exit_p = c["close"] if c else pos["entry"]
            fee    = exit_p * pos["qty"] * FEE_PCT
            proc   = exit_p * pos["qty"] - fee
            cash  += proc
            entry_eff = pos["entry"] * (1 + FEE_PCT)
            exit_eff  = exit_p       * (1 - FEE_PCT)
            pnl_pct   = (exit_eff - entry_eff) / entry_eff * 100
            t = {**pos, "exit_date": last, "exit_price": exit_p,
                 "reason": "month_end", "pnl_pct": pnl_pct,
                 "pnl_usd": (exit_eff - entry_eff) * pos["qty"]}
            month_trades.append(t)
            all_trades.append(t)

        capital = cash
        equity.append((month_end, capital))
        wins = [t for t in month_trades if t["pnl_pct"] > 0]
        monthly.append({
            "month": month_key, "trades": len(month_trades),
            "wins": len(wins),
            "win_rate": len(wins)/len(month_trades)*100 if month_trades else 0,
            "pnl_pct": (capital - equity[-2][1]) / equity[-2][1] * 100 if len(equity)>=2 else 0,
            "basket": basket,
        })

        current = (month_end + timedelta(days=1)).replace(day=1)

    return all_trades, equity, monthly

# ── Charts ────────────────────────────────────────────────────────────────────

def make_charts(all_trades, equity, monthly):
    plt.style.use("dark_background")
    fig = plt.figure(figsize=(16, 12))
    fig.patch.set_facecolor("#0d1117")
    gs = gridspec.GridSpec(3, 2, figure=fig, hspace=0.45, wspace=0.35)

    col = {"tp":"#3fb950","sl":"#f85149","expired":"#e3b341","month_end":"#8b949e"}

    # ── 1. Equity curve ──
    ax1 = fig.add_subplot(gs[0, :])
    ax1.set_facecolor("#161b22")
    dates  = [e[0] for e in equity]
    values = [e[1] for e in equity]
    ax1.plot(dates, values, color="#58a6ff", linewidth=2)
    ax1.fill_between(dates, CAPITAL, values,
                     where=[v >= CAPITAL for v in values], alpha=0.15, color="#3fb950")
    ax1.fill_between(dates, CAPITAL, values,
                     where=[v < CAPITAL for v in values], alpha=0.15, color="#f85149")
    ax1.axhline(CAPITAL, color="#8b949e", linestyle="--", linewidth=0.8, alpha=0.6)
    ax1.set_title(f"Equity Curve  |  Start ${CAPITAL:.0f}  ->  End ${values[-1]:.2f}  "
                  f"({(values[-1]-CAPITAL)/CAPITAL*100:+.1f}%)",
                  color="#c9d1d9", fontsize=12, pad=8)
    ax1.set_ylabel("Portfolio USD", color="#8b949e")
    ax1.tick_params(colors="#8b949e")
    for spine in ax1.spines.values():
        spine.set_edgecolor("#30363d")

    # ── 2. Monthly win rate ──
    ax2 = fig.add_subplot(gs[1, 0])
    ax2.set_facecolor("#161b22")
    months   = [m["month"] for m in monthly]
    winrates = [m["win_rate"] for m in monthly]
    bars = ax2.bar(months, winrates, color=[
        "#3fb950" if w >= 60 else "#e3b341" if w >= 50 else "#f85149"
        for w in winrates], alpha=0.85)
    ax2.axhline(60, color="#8b949e", linestyle="--", linewidth=0.7, alpha=0.5)
    ax2.set_title("Monthly Win Rate %", color="#c9d1d9", fontsize=10)
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("%", color="#8b949e")
    ax2.tick_params(colors="#8b949e", axis="x", rotation=45, labelsize=7)
    ax2.tick_params(colors="#8b949e", axis="y")
    for spine in ax2.spines.values():
        spine.set_edgecolor("#30363d")

    # ── 3. Monthly P&L ──
    ax3 = fig.add_subplot(gs[1, 1])
    ax3.set_facecolor("#161b22")
    pnls = [m["pnl_pct"] for m in monthly]
    ax3.bar(months, pnls, color=["#3fb950" if p >= 0 else "#f85149" for p in pnls], alpha=0.85)
    ax3.axhline(0, color="#8b949e", linewidth=0.8)
    ax3.set_title("Monthly P&L %", color="#c9d1d9", fontsize=10)
    ax3.set_ylabel("%", color="#8b949e")
    ax3.tick_params(colors="#8b949e", axis="x", rotation=45, labelsize=7)
    ax3.tick_params(colors="#8b949e", axis="y")
    for spine in ax3.spines.values():
        spine.set_edgecolor("#30363d")

    # ── 4. Trade return distribution ──
    ax4 = fig.add_subplot(gs[2, 0])
    ax4.set_facecolor("#161b22")
    if all_trades:
        pnls_all = [t["pnl_pct"] for t in all_trades]
        ax4.hist(pnls_all, bins=30, color="#58a6ff", alpha=0.7, edgecolor="#30363d")
        ax4.axvline(0, color="#f85149", linewidth=1, linestyle="--")
        ax4.axvline(np.mean(pnls_all), color="#3fb950", linewidth=1.2, linestyle="--",
                    label=f"Mean {np.mean(pnls_all):+.2f}%")
        ax4.legend(fontsize=8, labelcolor="#c9d1d9", facecolor="#21262d")
    ax4.set_title("Trade Return Distribution", color="#c9d1d9", fontsize=10)
    ax4.set_xlabel("P&L %", color="#8b949e")
    ax4.tick_params(colors="#8b949e")
    for spine in ax4.spines.values():
        spine.set_edgecolor("#30363d")

    # ── 5. Exit reason breakdown ──
    ax5 = fig.add_subplot(gs[2, 1])
    ax5.set_facecolor("#161b22")
    if all_trades:
        reasons = defaultdict(int)
        for t in all_trades:
            reasons[t["reason"]] += 1
        labels = list(reasons.keys())
        sizes  = list(reasons.values())
        colors = [col.get(l, "#8b949e") for l in labels]
        wedges, texts, autotexts = ax5.pie(
            sizes, labels=labels, autopct="%1.1f%%",
            colors=colors, startangle=90,
            textprops={"color":"#c9d1d9","fontsize":9},
        )
        for at in autotexts:
            at.set_color("#0d1117")
            at.set_fontsize(8)
    ax5.set_title("Exit Reason Breakdown", color="#c9d1d9", fontsize=10)

    # ── Summary text ──
    if all_trades:
        wins     = [t for t in all_trades if t["pnl_pct"] > 0]
        tps      = [t for t in all_trades if t["reason"] == "tp"]
        sls      = [t for t in all_trades if t["reason"] == "sl"]
        avg_win  = np.mean([t["pnl_pct"] for t in wins]) if wins else 0
        avg_loss = np.mean([t["pnl_pct"] for t in all_trades if t["pnl_pct"] <= 0]) if all_trades else 0
        win_rate = len(wins) / len(all_trades) * 100
        ev       = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)
        fig.text(0.01, 0.01,
            f"Total trades: {len(all_trades)}  |  Win rate: {win_rate:.1f}%  |  "
            f"Avg win: {avg_win:+.2f}%  |  Avg loss: {avg_loss:+.2f}%  |  "
            f"Expected value/trade: {ev:+.3f}%  |  "
            f"TP hits: {len(tps)}  SL hits: {len(sls)}  |  "
            f"Final capital: ${equity[-1][1]:.2f}",
            color="#8b949e", fontsize=8, va="bottom",
        )

    path = f"{OUTPUT_DIR}/strategy_analysis.png"
    plt.savefig(path, dpi=130, bbox_inches="tight", facecolor="#0d1117")
    plt.close()
    log(f"Chart saved -> {path}")
    return path

# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log("Fetching top-100 coins from CoinGecko…")
    coins = cg_top_coins(100)
    log(f"Got {len(coins)} coins: {coins[:10]}…")

    all_trades, equity, monthly = run_backtest(coins)

    # Print summary
    print("\n" + "="*60)
    print(f"BACKTEST SUMMARY  ({BACKTEST_MONTHS} months)")
    print("="*60)
    if all_trades:
        wins     = [t for t in all_trades if t["pnl_pct"] > 0]
        win_rate = len(wins) / len(all_trades) * 100
        import numpy as np
        avg_win  = np.mean([t["pnl_pct"] for t in wins]) if wins else 0
        avg_loss = np.mean([t["pnl_pct"] for t in all_trades if t["pnl_pct"] <= 0]) or 0
        ev       = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)
        final    = equity[-1][1]
        print(f"Initial capital : ${CAPITAL:.2f}")
        print(f"Final capital   : ${final:.2f}  ({(final-CAPITAL)/CAPITAL*100:+.1f}%)")
        print(f"Total trades    : {len(all_trades)}")
        print(f"Win rate        : {win_rate:.1f}%")
        print(f"Avg win         : {avg_win:+.2f}%")
        print(f"Avg loss        : {avg_loss:+.2f}%")
        print(f"Expected value  : {ev:+.3f}% per trade")
        by_reason = defaultdict(list)
        for t in all_trades:
            by_reason[t["reason"]].append(t["pnl_pct"])
        for r, vals in sorted(by_reason.items()):
            rw = len([v for v in vals if v > 0])
            print(f"  {r:12s}: {len(vals):3d} trades  win {rw/len(vals)*100:.0f}%  avg {np.mean(vals):+.2f}%")
        print()
        for m in monthly:
            print(f"  {m['month']}  trades:{m['trades']:2d}  wins:{m['wins']:2d}  "
                  f"wr:{m['win_rate']:4.0f}%  pnl:{m['pnl_pct']:+5.1f}%  basket:{m['basket']}")
    print("="*60)

    path = make_charts(all_trades, equity, monthly)
    print(f"\nChart: {path}")
