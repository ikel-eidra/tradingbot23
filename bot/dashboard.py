"""Live dashboard for TradingBot23.

A tkinter-based GUI that shows real-time bot status:
- Trading mode (PAPER/LIVE) with color indicator
- Portfolio value, cash balance, PNL
- Open positions table with live P&L
- Recent closed trades
- Strategy stats (win rate, total trades)

Runs the trading loop in a background thread and updates the UI every second.
"""

import logging
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from tkinter import ttk

from bot import config

logger = logging.getLogger(__name__)


class Dashboard:
    """Main dashboard window."""

    REFRESH_MS = 2000

    def __init__(self, strategy):
        self.strategy = strategy
        self.trader = strategy.trader
        self._running = True
        self._cycle_thread = None

        self.root = tk.Tk()
        self.root.title("TradingBot23")
        self.root.geometry("880x620")
        self.root.minsize(780, 520)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._start_trading_loop()
        self._schedule_refresh()

    def _build_ui(self):
        self.root.configure(bg="#0d1117")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background="#0d1117", foreground="#c9d1d9", font=("Consolas", 10))
        style.configure("Header.TLabel", font=("Consolas", 11, "bold"), foreground="#c9d1d9", background="#0d1117")
        style.configure("Big.TLabel", font=("Consolas", 22, "bold"), foreground="#58a6ff", background="#0d1117")
        style.configure("Mode.TLabel", font=("Consolas", 14, "bold"), background="#0d1117")
        style.configure("Treeview", background="#161b22", foreground="#c9d1d9",
                         fieldbackground="#161b22", font=("Consolas", 9), rowheight=22)
        style.configure("Treeview.Heading", background="#21262d", foreground="#8b949e",
                         font=("Consolas", 9, "bold"))
        style.map("Treeview", background=[("selected", "#1f6feb")])

        # --- Top bar: Mode + Engine ---
        top = tk.Frame(self.root, bg="#0d1117", pady=8, padx=15)
        top.pack(fill="x")

        is_paper = config.TRADING_MODE != "live"
        mode_text = "PAPER TRADING" if is_paper else "LIVE TRADING"
        mode_color = "#3fb950" if is_paper else "#f85149"
        self.mode_label = ttk.Label(top, text=f"  {mode_text}  ", style="Mode.TLabel",
                                     foreground=mode_color)
        self.mode_label.pack(side="left")

        engine_text = f"ENGINE: {config.ENGINE.upper()}"
        if config.ENGINE == "futures":
            engine_text += f" ({config.LEVERAGE}x)"
        ttk.Label(top, text=f"   {engine_text}", style="Header.TLabel",
                  foreground="#8b949e").pack(side="left")

        self.clock_label = ttk.Label(top, text="", style="Header.TLabel", foreground="#8b949e")
        self.clock_label.pack(side="right")

        # --- Stats row ---
        stats_frame = tk.Frame(self.root, bg="#0d1117", padx=15, pady=4)
        stats_frame.pack(fill="x")

        self.portfolio_var = tk.StringVar(value="$0.00")
        self.cash_var = tk.StringVar(value="$0.00")
        self.pnl_var = tk.StringVar(value="+0.00%")
        self.trades_var = tk.StringVar(value="0")
        self.winrate_var = tk.StringVar(value="0.0%")
        self.open_var = tk.StringVar(value="0")

        cards = [
            ("PORTFOLIO", self.portfolio_var, "Big.TLabel"),
            ("CASH", self.cash_var, "Header.TLabel"),
            ("PNL", self.pnl_var, "Header.TLabel"),
            ("OPEN", self.open_var, "Header.TLabel"),
            ("TRADES", self.trades_var, "Header.TLabel"),
            ("WIN RATE", self.winrate_var, "Header.TLabel"),
        ]

        for label_text, var, sty in cards:
            card = tk.Frame(stats_frame, bg="#161b22", padx=12, pady=6,
                            highlightbackground="#30363d", highlightthickness=1)
            card.pack(side="left", padx=4, fill="y")
            ttk.Label(card, text=label_text, foreground="#8b949e", background="#161b22",
                      font=("Consolas", 8)).pack(anchor="w")
            ttk.Label(card, textvariable=var, style=sty, background="#161b22").pack(anchor="w")

        # --- Open Positions table ---
        pos_label_frame = tk.Frame(self.root, bg="#0d1117", padx=15, pady=(12, 2))
        pos_label_frame.pack(fill="x")
        ttk.Label(pos_label_frame, text="OPEN POSITIONS", style="Header.TLabel").pack(anchor="w")

        pos_frame = tk.Frame(self.root, bg="#0d1117", padx=15)
        pos_frame.pack(fill="both", expand=True)

        pos_cols = ("symbol", "entry", "current", "pnl", "tp", "sl", "age")
        self.pos_tree = ttk.Treeview(pos_frame, columns=pos_cols, show="headings", height=5)
        for col, heading, width in [
            ("symbol", "SYMBOL", 80), ("entry", "ENTRY", 100), ("current", "CURRENT", 100),
            ("pnl", "P&L %", 80), ("tp", "TP", 100), ("sl", "SL", 100), ("age", "AGE", 70),
        ]:
            self.pos_tree.heading(col, text=heading)
            self.pos_tree.column(col, width=width, anchor="center")
        self.pos_tree.pack(fill="both", expand=True)

        # --- Recent Closed Trades ---
        closed_label_frame = tk.Frame(self.root, bg="#0d1117", padx=15, pady=(8, 2))
        closed_label_frame.pack(fill="x")
        ttk.Label(closed_label_frame, text="RECENT CLOSED TRADES", style="Header.TLabel").pack(anchor="w")

        closed_frame = tk.Frame(self.root, bg="#0d1117", padx=15, pady=(0, 10))
        closed_frame.pack(fill="both", expand=True)

        closed_cols = ("symbol", "entry", "exit", "pnl", "pnl_usd", "reason", "time")
        self.closed_tree = ttk.Treeview(closed_frame, columns=closed_cols, show="headings", height=4)
        for col, heading, width in [
            ("symbol", "SYMBOL", 70), ("entry", "ENTRY", 90), ("exit", "EXIT", 90),
            ("pnl", "P&L %", 70), ("pnl_usd", "P&L $", 80), ("reason", "REASON", 80),
            ("time", "CLOSED", 100),
        ]:
            self.closed_tree.heading(col, text=heading)
            self.closed_tree.column(col, width=width, anchor="center")
        self.closed_tree.pack(fill="both", expand=True)

        # --- Basket bar ---
        basket_frame = tk.Frame(self.root, bg="#161b22", padx=15, pady=5)
        basket_frame.pack(fill="x", side="bottom")
        self.basket_var = tk.StringVar(value="Basket: loading...")
        ttk.Label(basket_frame, textvariable=self.basket_var, foreground="#8b949e",
                  background="#161b22", font=("Consolas", 9)).pack(anchor="w")

    def _start_trading_loop(self):
        self._cycle_thread = threading.Thread(target=self._trading_loop, daemon=True)
        self._cycle_thread.start()

    def _trading_loop(self):
        now = datetime.now(timezone.utc)
        if self.strategy.should_refresh_basket(now):
            try:
                self.strategy.refresh_basket(now)
            except Exception:
                logger.exception("Failed to refresh basket")

        interval = config.CHECK_INTERVAL_HOURS * 3600
        while self._running:
            try:
                self.strategy.run_cycle()
            except Exception:
                logger.exception("Error in trading cycle")

            elapsed = 0
            while elapsed < interval and self._running:
                time.sleep(1)
                elapsed += 1

    def _schedule_refresh(self):
        if not self._running:
            return
        self._refresh_ui()
        self.root.after(self.REFRESH_MS, self._schedule_refresh)

    def _refresh_ui(self):
        try:
            self._update_stats()
            self._update_positions()
            self._update_closed()
            self._update_basket()
            self._update_clock()
        except Exception:
            logger.debug("Dashboard refresh error", exc_info=True)

    def _update_clock(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        self.clock_label.config(text=now)

    def _update_stats(self):
        portfolio = self.trader.get_portfolio_value()
        cash = self.trader.cash_balance
        stats = self.trader.get_stats()
        initial = config.CAPITAL_USD
        pnl_pct = ((portfolio - initial) / initial) * 100 if initial > 0 else 0

        self.portfolio_var.set(f"${portfolio:,.2f}")
        self.cash_var.set(f"${cash:,.2f}")

        pnl_str = f"{pnl_pct:+.2f}%"
        self.pnl_var.set(pnl_str)

        open_positions = self.trader.get_open_positions()
        self.open_var.set(str(len(open_positions)))
        self.trades_var.set(str(stats.get("total_trades", 0)))
        self.winrate_var.set(f"{stats.get('win_rate', 0):.1f}%")

    def _update_positions(self):
        for item in self.pos_tree.get_children():
            self.pos_tree.delete(item)

        positions = self.trader.get_open_positions()
        now = datetime.now(timezone.utc)

        for pos in positions:
            age_hours = (now - pos.entry_time).total_seconds() / 3600
            age_str = f"{age_hours:.1f}h"

            current = getattr(pos, "current_price", None)
            if current is None:
                try:
                    current = self.trader.get_current_price(pos.symbol)
                except Exception:
                    current = pos.entry_price

            if current and pos.entry_price > 0:
                pnl = ((current - pos.entry_price) / pos.entry_price) * 100
                pnl_str = f"{pnl:+.2f}%"
            else:
                pnl_str = "--"

            tp_price = getattr(pos, "tp_price", None) or pos.entry_price * (1 + config.TP_PCT)
            sl_price = getattr(pos, "sl_price", None) or pos.entry_price * (1 - config.SL_PCT)

            self.pos_tree.insert("", "end", values=(
                pos.symbol,
                f"${pos.entry_price:.4f}",
                f"${current:.4f}" if current else "--",
                pnl_str,
                f"${tp_price:.4f}",
                f"${sl_price:.4f}",
                age_str,
            ))

    def _update_closed(self):
        for item in self.closed_tree.get_children():
            self.closed_tree.delete(item)

        closed = self.trader.get_trade_history()
        recent = closed[-10:] if closed else []

        for pos in reversed(recent):
            exit_time = getattr(pos, "exit_time", None)
            time_str = exit_time.strftime("%m/%d %H:%M") if exit_time else "--"
            pnl_pct = getattr(pos, "pnl_pct", 0)
            pnl_usd = getattr(pos, "pnl_usd", 0)
            reason = getattr(pos, "status", "--")
            if hasattr(reason, "value"):
                reason = reason.value

            self.closed_tree.insert("", "end", values=(
                pos.symbol,
                f"${pos.entry_price:.4f}",
                f"${pos.exit_price:.4f}" if pos.exit_price else "--",
                f"{pnl_pct:+.2f}%",
                f"${pnl_usd:+.2f}",
                reason,
                time_str,
            ))

    def _update_basket(self):
        if self.strategy.basket:
            symbols = [c["symbol"] for c in self.strategy.basket]
            month_str = ""
            if self.strategy.basket_year and self.strategy.basket_month:
                month_str = f" ({self.strategy.basket_year}-{self.strategy.basket_month:02d})"
            self.basket_var.set(f"Basket{month_str}: {', '.join(symbols)}")
        else:
            self.basket_var.set("Basket: waiting for first cycle...")

    def _on_close(self):
        self._running = False
        self.root.destroy()

    def run(self):
        self.root.mainloop()
        self._running = False
