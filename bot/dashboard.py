"""Live dashboard for TradingBot23."""

import logging
import threading
import time
import tkinter as tk
from collections import defaultdict
from datetime import datetime, timezone
from tkinter import ttk

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import csv as _csv

from bot import config
from bot.modules import accounting
from bot.modules import telegram_notifier as tg
from bot.modules.futures_trader import _history_csv
from bot.modules.p2p_arbitrage import (
    P2PDepthSweep,
    P2PJournal,
    P2PPaperArb,
    P2PRoute,
    P2PRouteSettings,
    build_depth_sweep,
    build_p2p_routes,
    build_p2p_hold_entry,
)
from bot.modules.p2p_monitor import P2PMonitor, P2PSnapshot

logger = logging.getLogger(__name__)


class Dashboard:
    REFRESH_MS = 2000
    P2P_REFRESH_SECS = 60
    P2P_ALERT_COOLDOWN_SECS = 300

    def __init__(self, strategy):
        self.strategy = strategy
        self.trader   = strategy.trader
        self._running      = True
        self._settings_confirmed = config.SETTINGS_CONFIRMED
        self._paused       = (not config.AUTO_START_FUTURES) or (not self._settings_confirmed)
        self._force_event  = threading.Event()
        self._cycle_thread = None

        self._last_cycle_time:    datetime | None = None
        self._last_cycle_summary: dict = {}
        self._next_cycle_ts:      float = 0.0
        self.p2p_monitor = P2PMonitor(asset="USDT", fiat="PHP")
        self.p2p_journal = P2PJournal()
        self.p2p_paper = P2PPaperArb()
        self._p2p_refreshing = False
        self._p2p_last_snapshot: P2PSnapshot | None = None
        self._p2p_routes: list[P2PRoute] = []
        self._p2p_sweep: P2PDepthSweep | None = None
        self._p2p_next_refresh_ts = 0.0
        self._p2p_settings_cache = P2PRouteSettings()
        self._p2p_last_alert_key = ""
        self._p2p_last_alert_ts = 0.0
        self._p2p_last_logged_route_key = ""
        self._telegram_commands = None

        # Equity history: list of (datetime, portfolio_value)
        self._equity_history: list[tuple[datetime, float]] = []
        self._equity_lock = threading.Lock()

        self.root = tk.Tk()
        self.root.title("TradingBot23")
        self.root.geometry("1080x780")
        self.root.minsize(980, 680)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._start_telegram_dashboard()
        self._start_trading_loop()
        self._schedule_refresh()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        self.root.configure(bg="#0d1117")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background="#0d1117", foreground="#c9d1d9", font=("Consolas", 10))
        style.configure("Header.TLabel", font=("Consolas", 11, "bold"),
                        foreground="#c9d1d9", background="#0d1117")
        style.configure("Big.TLabel",   font=("Consolas", 22, "bold"),
                        foreground="#58a6ff", background="#0d1117")
        style.configure("Mode.TLabel",  font=("Consolas", 14, "bold"), background="#0d1117")
        style.configure("Treeview", background="#161b22", foreground="#c9d1d9",
                        fieldbackground="#161b22", font=("Consolas", 9), rowheight=22)
        style.configure("Treeview.Heading", background="#21262d", foreground="#8b949e",
                        font=("Consolas", 9, "bold"))
        style.map("Treeview", background=[("selected", "#1f6feb")])
        style.configure("Btn.TButton", font=("Consolas", 9, "bold"), padding=4)
        style.configure("TNotebook",         background="#0d1117", borderwidth=0)
        style.configure("TNotebook.Tab",     background="#161b22", foreground="#8b949e",
                        padding=[12, 4], font=("Consolas", 9, "bold"))
        style.map("TNotebook.Tab",
                  background=[("selected", "#0d1117")],
                  foreground=[("selected", "#58a6ff")])
        style.configure(
            "Settings.TEntry",
            fieldbackground="#f0f6fc",
            foreground="#0d1117",
            insertcolor="#0d1117",
            bordercolor="#8b949e",
            lightcolor="#f0f6fc",
            darkcolor="#8b949e",
        )
        style.map(
            "Settings.TEntry",
            fieldbackground=[("disabled", "#30363d"), ("readonly", "#f0f6fc"), ("focus", "#ffffff")],
            foreground=[("disabled", "#8b949e"), ("readonly", "#0d1117"), ("focus", "#0d1117")],
        )
        style.configure(
            "Settings.TCombobox",
            fieldbackground="#f0f6fc",
            background="#f0f6fc",
            foreground="#0d1117",
            arrowcolor="#0d1117",
            bordercolor="#8b949e",
            selectbackground="#c9d1d9",
            selectforeground="#0d1117",
        )
        style.map(
            "Settings.TCombobox",
            fieldbackground=[("readonly", "#f0f6fc"), ("focus", "#ffffff")],
            foreground=[("readonly", "#0d1117"), ("focus", "#0d1117")],
            background=[("readonly", "#f0f6fc"), ("focus", "#ffffff")],
        )
        self.root.option_add("*TCombobox*Listbox.background", "#f0f6fc")
        self.root.option_add("*TCombobox*Listbox.foreground", "#0d1117")
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#58a6ff")
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#0d1117")

        # ── Top bar ──
        top = tk.Frame(self.root, bg="#0d1117", pady=8, padx=15)
        top.pack(fill="x")

        mode_text  = "FUTURES PAPER"
        mode_color = "#3fb950"
        self.mode_label = ttk.Label(top, text=f"  {mode_text}  ",
                                    style="Mode.TLabel", foreground=mode_color)
        self.mode_label.pack(side="left")

        self.engine_var = tk.StringVar(value=self._new_trade_setting_text())
        ttk.Label(top, textvariable=self.engine_var, style="Header.TLabel",
                  foreground="#8b949e").pack(side="left")

        self.clock_label = ttk.Label(top, text="", style="Header.TLabel", foreground="#8b949e")
        self.clock_label.pack(side="right")

        # ── Control bar ──
        ctrl = tk.Frame(self.root, bg="#161b22", padx=15, pady=6)
        ctrl.pack(fill="x")

        self.run_btn   = ttk.Button(ctrl, text="Run Now",    style="Btn.TButton", command=self._on_run_now)
        pause_text = "Resume" if self._paused else "Pause"
        self.pause_btn = ttk.Button(ctrl, text=pause_text,    style="Btn.TButton", command=self._on_pause_resume)
        self.run_btn.pack(side="left", padx=(0, 6))
        self.pause_btn.pack(side="left", padx=(0, 12))
        ttk.Button(ctrl, text="Telegram Menu", style="Btn.TButton",
                   command=self._send_telegram_menu).pack(side="left", padx=(0, 12))

        if not self._settings_confirmed:
            initial_status = "First run: review Settings and click Apply Settings before trading."
        elif self._paused:
            initial_status = "Paused on launch. Press Resume to start futures paper trading."
        else:
            initial_status = "Starting up..."
        self.status_var = tk.StringVar(value=initial_status)
        ttk.Label(ctrl, textvariable=self.status_var, foreground="#8b949e",
                  background="#161b22", font=("Consolas", 9)).pack(side="left")

        # ── Stats cards ──
        stats_frame = tk.Frame(self.root, bg="#0d1117", padx=15, pady=4)
        stats_frame.pack(fill="x")

        self.portfolio_var = tk.StringVar(value="$0.00")
        self.cash_var      = tk.StringVar(value="$0.00")
        self.pnl_var       = tk.StringVar(value="+0.00%")
        self.trades_var    = tk.StringVar(value="0")
        self.winrate_var   = tk.StringVar(value="0.0%")
        self.open_var      = tk.StringVar(value="0")

        for label_text, var, sty in [
            ("PORTFOLIO", self.portfolio_var, "Big.TLabel"),
            ("CASH",      self.cash_var,      "Header.TLabel"),
            ("PNL",       self.pnl_var,       "Header.TLabel"),
            ("OPEN",      self.open_var,       "Header.TLabel"),
            ("TRADES",    self.trades_var,     "Header.TLabel"),
            ("WIN RATE",  self.winrate_var,    "Header.TLabel"),
        ]:
            card = tk.Frame(stats_frame, bg="#161b22",
                            highlightbackground="#30363d", highlightthickness=1)
            card.pack(side="left", padx=4, ipadx=12, ipady=6, fill="y")
            ttk.Label(card, text=label_text, foreground="#8b949e", background="#161b22",
                      font=("Consolas", 8)).pack(anchor="w")
            ttk.Label(card, textvariable=var, style=sty, background="#161b22").pack(anchor="w")

        # ── Notebook ──
        nb = ttk.Notebook(self.root)
        self.notebook = nb
        nb.pack(fill="both", expand=True, padx=0, pady=0)

        open_tab     = tk.Frame(nb, bg="#0d1117")
        charts_tab   = tk.Frame(nb, bg="#0d1117")
        history_tab  = tk.Frame(nb, bg="#0d1117")
        p2p_tab      = tk.Frame(nb, bg="#0d1117")
        settings_tab = tk.Frame(nb, bg="#0d1117")
        self.settings_tab = settings_tab
        nb.add(open_tab,     text="  Open  ")
        nb.add(charts_tab,   text="  Charts  ")
        nb.add(history_tab,  text="  History  ")
        nb.add(p2p_tab,      text="  P2P Arb  ")
        nb.add(settings_tab, text="  Settings  ")
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_open_tab(open_tab)
        self._build_charts_tab(charts_tab)
        self._build_history_tab(history_tab)
        self._build_p2p_tab(p2p_tab)
        self._build_settings_tab(settings_tab)
        if not self._settings_confirmed:
            nb.select(settings_tab)
            self.status_var.set("First run: review Settings and click Apply Settings before trading.")

        # ── Bottom bar ──
        basket_frame = tk.Frame(self.root, bg="#161b22", padx=15, pady=5)
        basket_frame.pack(fill="x", side="bottom")
        self.basket_var = tk.StringVar(value="Basket: loading...")
        ttk.Label(basket_frame, textvariable=self.basket_var, foreground="#8b949e",
                  background="#161b22", font=("Consolas", 9)).pack(side="left", anchor="w")
        ttk.Label(basket_frame, text="FutolTech  |  Futol Ethical Technology Ecosystems",
                  foreground="#388bfd", background="#161b22",
                  font=("Consolas", 8, "bold")).pack(side="right", anchor="e")

    def _start_telegram_dashboard(self):
        self._telegram_commands = tg.TelegramDashboardPoller(
            futures_callback=self._telegram_futures_snapshot,
            p2p_callback=self._telegram_p2p_snapshot,
            info_callback=self._telegram_info_text,
        )
        self._telegram_commands.start()

    def _send_telegram_menu(self):
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
            self.status_var.set("Telegram dashboard needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.")
            return
        tg.send_dashboard_menu("TradingBot23 dashboard controls:")
        self.status_var.set("Telegram dashboard menu sent.")

    def _telegram_futures_snapshot(self) -> str:
        portfolio = self.trader.get_portfolio_value()
        cash = self.trader.cash_balance
        stats = self.trader.get_stats()
        initial = (
            self.trader.get_contributed_capital()
            if hasattr(self.trader, "get_contributed_capital")
            else config.CAPITAL_USD
        )
        pnl_pct = ((portfolio - initial) / initial) * 100 if initial > 0 else 0.0
        open_positions = list(self.trader.get_open_positions())
        total_pnl = sum(p.pnl_usd for p in self.trader.get_trade_history())
        last_scan = (
            self._last_cycle_time.strftime("%Y-%m-%d %H:%M:%S UTC")
            if self._last_cycle_time
            else "not run yet"
        )
        mode = "paused" if self._paused else "running"
        lines = [
            "📊 <b>TradingBot23 Futures Paper</b>",
            f"Mode: <b>{mode}</b>  |  New trades: <b>{config.LEVERAGE}x cross</b>",
            f"Portfolio: <b>${portfolio:,.2f}</b>  |  Cash: <b>${cash:,.2f}</b>",
            f"PNL: <b>{pnl_pct:+.2f}%</b>  |  Realized: <b>${total_pnl:+,.2f}</b>",
            f"Open: <b>{len(open_positions)}</b>  |  Trades: <b>{stats.get('total_trades', 0)}</b>  |  Win: <b>{stats.get('win_rate', 0):.1f}%</b>",
            f"Last scan: {last_scan}",
        ]
        if open_positions:
            lines.append("")
            lines.append("<b>Open positions</b>")
            for pos in open_positions[:6]:
                current = pos.last_known_price or pos.entry_price
                lines.append(
                    f"{tg.escape_html(pos.symbol)} {pos.leverage}x | "
                    f"${pos.margin_used:,.2f} | now ${current:,.4f} | "
                    f"PNL {pos.pnl_pct:+.2f}% | liq ${pos.liquidation_price:,.4f}"
                )
            if len(open_positions) > 6:
                lines.append(f"+{len(open_positions) - 6} more open position(s)")
        return "\n".join(lines)

    def _telegram_p2p_snapshot(self) -> str:
        settings = self._p2p_settings_cache
        stale_note = ""
        try:
            snapshot = self.p2p_monitor.fetch_snapshot(rows=20)
        except Exception as exc:
            if not self._p2p_last_snapshot:
                raise
            snapshot = self._p2p_last_snapshot
            stale_note = f"\nUsing last UI snapshot; live refresh failed: {tg.escape_html(exc)}"

        routes = build_p2p_routes([snapshot], settings=settings)
        sweep = build_depth_sweep([snapshot], settings=settings)
        state = self.p2p_paper.state(settings.capital_php)
        return self._format_p2p_dashboard_text(snapshot, routes, sweep, state, settings) + stale_note

    def _telegram_info_text(self) -> str:
        return (
            "<b>TradingBot23 Live Assist Scope</b>\n"
            "Futures: paper portfolio, open positions, trade stats, and daily summary alerts.\n"
            "P2P: live USDT/PHP scan, spread scoring, Telegram alerts, watchlist log, paper ledger, and hold-exit checks.\n\n"
            "Manual in real P2P: choose counterparty, send fiat, confirm payment, verify receipt, release crypto, and handle disputes.\n"
            "Commands: /dashboard, /p2p, /info"
        )

    def _format_p2p_dashboard_text(
        self,
        snapshot: P2PSnapshot,
        routes: list[P2PRoute],
        sweep: P2PDepthSweep | None,
        state: dict,
        settings: P2PRouteSettings,
    ) -> str:
        best_buy = snapshot.best_buy
        best_sell = snapshot.best_sell
        as_of = snapshot.as_of.strftime("%Y-%m-%d %H:%M:%S UTC") if snapshot.as_of else "unknown"
        balance = self._num(state.get("balance_php"))
        cash = self._num(state.get("cash_php"), balance)
        realized = self._num(state.get("realized_profit_php"))
        hold = state.get("hold_position") or {}
        lines = [
            "📊 <b>TradingBot23 P2P Arb</b>",
            f"USDT/PHP updated: {as_of}",
            f"Capital: <b>{settings.capital_php:,.0f} PHP</b>  |  Min net: <b>{settings.min_profit_pct:.3f}%</b>",
            f"Paper balance: <b>{balance:,.0f} PHP</b>  |  Cash: <b>{cash:,.0f} PHP</b>  |  Realized: <b>{realized:+,.0f} PHP</b>",
        ]
        if best_buy and best_sell:
            lines.append(
                f"Best buy: <b>{best_buy.price:,.2f}</b> | "
                f"Best sell: <b>{best_sell.price:,.2f}</b> | "
                f"Raw spread: <b>{(snapshot.spread or 0):+.2f}</b> PHP"
            )
        if sweep:
            warnings = "; ".join(sweep.warnings) if sweep.warnings else "ok"
            lines.extend([
                "",
                "<b>Depth sweep</b>",
                f"Size: {sweep.size_php:,.0f} PHP",
                f"Avg buy/sell: {sweep.avg_buy_price:,.2f} / {sweep.avg_sell_price:,.2f}",
                f"Net: <b>{sweep.profit_php:+,.0f} PHP ({sweep.profit_pct:+.3f}%)</b> | Grade {sweep.grade}",
                f"Warnings: {tg.escape_html(warnings)}",
            ])
        elif routes:
            top = routes[0]
            warnings = "; ".join(top.warnings) if top.warnings else "ok"
            lines.extend([
                "",
                "<b>Top route</b>",
                f"{tg.escape_html(top.route_label)} | {top.size_php:,.0f} PHP",
                f"Net: <b>{top.profit_php:+,.0f} PHP ({top.profit_pct:+.3f}%)</b> | Grade {top.grade}",
                f"Warnings: {tg.escape_html(warnings)}",
            ])
        else:
            lines.append("No P2P route meets current capacity filters.")
        if hold:
            last_profit = self._num(hold.get("last_profit_php"))
            last_pct = self._num(hold.get("last_profit_pct"))
            lines.extend([
                "",
                "<b>Open P2P hold</b>",
                f"{self._num(hold.get('usdt')):,.2f} USDT @ {self._num(hold.get('avg_buy_price')):,.2f}",
                f"Marked P&L: {last_profit:+,.0f} PHP ({last_pct:+.3f}%)",
            ])
        return "\n".join(lines)

    def _build_open_tab(self, parent):
        # Open positions
        pl = tk.Frame(parent, bg="#0d1117")
        pl.pack(fill="x", padx=15, pady=(10, 2))
        ttk.Label(pl, text="OPEN POSITIONS", style="Header.TLabel").pack(anchor="w")

        pf = tk.Frame(parent, bg="#0d1117")
        pf.pack(fill="both", expand=True, padx=15)

        pos_cols = ("symbol","amount","lev","entry","current","pnl","trigger","tp","sl","age")
        self.pos_tree = ttk.Treeview(pf, columns=pos_cols, show="headings", height=5)
        pnl_heading = "P&L % (leveraged)"
        risk_heading = "CROSS LIQ"
        for col, heading, width in [
            ("symbol","SYMBOL",70),("amount","AMOUNT $",85),("lev","ENTRY LEV",70),
            ("entry","ENTRY",90),("current","CURRENT",90),
            ("pnl",pnl_heading,160),("trigger","24H TRIGGER",90),
            ("tp","TP",90),("sl",risk_heading,105),("age","AGE",55),
        ]:
            self.pos_tree.heading(col, text=heading)
            self.pos_tree.column(col, width=width, anchor="center")
        self.pos_tree.pack(fill="both", expand=True)

        # Closed trades
        cl = tk.Frame(parent, bg="#0d1117")
        cl.pack(fill="x", padx=15, pady=(10, 2))
        ttk.Label(cl, text="RECENT CLOSED TRADES", style="Header.TLabel").pack(anchor="w")

        cf = tk.Frame(parent, bg="#0d1117")
        cf.pack(fill="both", expand=True, padx=15, pady=(0, 8))

        closed_cols = ("symbol","amount","lev","entry","exit","pnl","pnl_usd","trigger","reason","time")
        self.closed_tree = ttk.Treeview(cf, columns=closed_cols, show="headings", height=5)
        for col, heading, width in [
            ("symbol","SYMBOL",65),("amount","AMOUNT $",80),("lev","ENTRY LEV",70),
            ("entry","ENTRY",85),("exit","EXIT",85),
            ("pnl","P&L %",65),("pnl_usd","P&L $",75),
            ("trigger","24H TRIGGER",90),("reason","REASON",75),("time","CLOSED",95),
        ]:
            self.closed_tree.heading(col, text=heading)
            self.closed_tree.column(col, width=width, anchor="center")
        self.closed_tree.pack(fill="both", expand=True)

    def _build_charts_tab(self, parent):
        ctrl = tk.Frame(parent, bg="#0d1117", pady=8)
        ctrl.pack(fill="x", padx=15)
        ttk.Button(ctrl, text="Refresh Charts", style="Btn.TButton",
                   command=self._draw_charts).pack(side="left")
        ttk.Label(ctrl, text="  Updates automatically when you switch to this tab.",
                  foreground="#8b949e", background="#0d1117",
                  font=("Consolas", 9)).pack(side="left")

        self._chart_frame = tk.Frame(parent, bg="#0d1117")
        self._chart_frame.pack(fill="both", expand=True)
        self._canvas_widget = None

        # Placeholder until first draw
        ttk.Label(self._chart_frame,
                  text="Switch to Charts tab after the bot runs a few trades.",
                  foreground="#8b949e", background="#0d1117",
                  font=("Consolas", 10)).pack(expand=True)

    # ── Chart rendering ────────────────────────────────────────────────────────

    def _draw_charts(self):
        # Clear old chart
        for w in self._chart_frame.winfo_children():
            w.destroy()

        closed = self.trader.get_trade_history()
        with self._equity_lock:
            eq_hist = list(self._equity_history)

        plt.style.use("dark_background")
        fig = plt.figure(figsize=(11, 7), facecolor="#0d1117")
        gs  = gridspec.GridSpec(2, 3, figure=fig, hspace=0.5, wspace=0.4)

        # ── 1. Equity curve ──
        ax1 = fig.add_subplot(gs[0, :])
        ax1.set_facecolor("#161b22")
        if len(eq_hist) >= 2:
            xs = [e[0] for e in eq_hist]
            ys = [e[1] for e in eq_hist]
            ax1.plot(xs, ys, color="#58a6ff", linewidth=2)
            ax1.fill_between(xs, config.CAPITAL_USD, ys,
                             where=[v >= config.CAPITAL_USD for v in ys],
                             alpha=0.15, color="#3fb950")
            ax1.fill_between(xs, config.CAPITAL_USD, ys,
                             where=[v < config.CAPITAL_USD for v in ys],
                             alpha=0.15, color="#f85149")
            ax1.axhline(config.CAPITAL_USD, color="#8b949e",
                        linestyle="--", linewidth=0.8, alpha=0.6)
            final  = ys[-1]
            change = (final - config.CAPITAL_USD) / config.CAPITAL_USD * 100
            ax1.set_title(f"Equity Curve  |  ${config.CAPITAL_USD:.0f} -> ${final:.2f} ({change:+.2f}%)",
                          color="#c9d1d9", fontsize=10)
        else:
            ax1.set_title("Equity Curve  (collecting data...)", color="#c9d1d9", fontsize=10)
            ax1.text(0.5, 0.5, "Not enough data yet", transform=ax1.transAxes,
                     ha="center", va="center", color="#8b949e", fontsize=11)
        ax1.tick_params(colors="#8b949e", labelsize=7)
        ax1.set_ylabel("Portfolio $", color="#8b949e", fontsize=8)
        for sp in ax1.spines.values(): sp.set_edgecolor("#30363d")

        # ── 2. P&L distribution ──
        ax2 = fig.add_subplot(gs[1, 0])
        ax2.set_facecolor("#161b22")
        if closed:
            pnls = [t.pnl_pct for t in closed]
            ax2.hist(pnls, bins=max(10, len(pnls)//3), color="#58a6ff",
                     alpha=0.75, edgecolor="#0d1117", linewidth=0.3)
            ax2.axvline(0, color="#f85149", linewidth=1.2, linestyle="--")
            ax2.axvline(np.mean(pnls), color="#3fb950", linewidth=1.2, linestyle="--",
                        label=f"Mean {np.mean(pnls):+.2f}%")
            ax2.legend(fontsize=7, labelcolor="#c9d1d9", facecolor="#21262d")
        ax2.set_title("Trade Returns", color="#c9d1d9", fontsize=9)
        ax2.set_xlabel("P&L %", color="#8b949e", fontsize=8)
        ax2.tick_params(colors="#8b949e", labelsize=7)
        for sp in ax2.spines.values(): sp.set_edgecolor("#30363d")

        # ── 3. Exit breakdown pie ──
        ax3 = fig.add_subplot(gs[1, 1])
        ax3.set_facecolor("#161b22")
        if closed:
            reasons = defaultdict(int)
            for t in closed:
                r = t.status.value if hasattr(t.status, "value") else str(t.status)
                reasons[r] += 1
            labels = list(reasons.keys())
            sizes  = list(reasons.values())
            colors = {"tp_hit":"#3fb950","sl_hit":"#f85149",
                      "expired":"#e3b341","liquidated":"#ff6b6b",
                      "month_end":"#8b949e"}
            clrs = [colors.get(l, "#58a6ff") for l in labels]
            wedges, texts, autotexts = ax3.pie(
                sizes, labels=labels, autopct="%1.0f%%",
                colors=clrs, startangle=90,
                textprops={"color":"#c9d1d9","fontsize":7},
            )
            for at in autotexts:
                at.set_color("#0d1117"); at.set_fontsize(7); at.set_fontweight("bold")
        else:
            ax3.text(0.5, 0.5, "No trades yet", transform=ax3.transAxes,
                     ha="center", va="center", color="#8b949e")
        ax3.set_title("Exit Breakdown", color="#c9d1d9", fontsize=9)

        # ── 4. Cumulative P&L per trade ──
        ax4 = fig.add_subplot(gs[1, 2])
        ax4.set_facecolor("#161b22")
        if closed:
            cum = np.cumsum([t.pnl_usd for t in closed])
            colors_bar = ["#3fb950" if v >= 0 else "#f85149" for v in cum]
            ax4.bar(range(len(cum)), cum, color=colors_bar, alpha=0.8, width=0.8)
            ax4.axhline(0, color="#8b949e", linewidth=0.8)
            ax4.set_title(f"Cumulative P&L  ({cum[-1]:+.2f} USD)", color="#c9d1d9", fontsize=9)
            ax4.set_xlabel("Trade #", color="#8b949e", fontsize=8)
            ax4.set_ylabel("USD", color="#8b949e", fontsize=8)
        else:
            ax4.set_title("Cumulative P&L", color="#c9d1d9", fontsize=9)
            ax4.text(0.5, 0.5, "No trades yet", transform=ax4.transAxes,
                     ha="center", va="center", color="#8b949e")
        ax4.tick_params(colors="#8b949e", labelsize=7)
        for sp in ax4.spines.values(): sp.set_edgecolor("#30363d")

        # Footer stats
        if closed:
            wins = [t for t in closed if t.pnl_pct > 0]
            wr   = len(wins) / len(closed) * 100
            ev   = np.mean([t.pnl_pct for t in closed])
            fig.text(0.01, 0.002,
                f"Trades: {len(closed)}  |  Win rate: {wr:.1f}%  |  "
                f"Avg P&L: {ev:+.3f}%  |  "
                f"Best: {max(t.pnl_pct for t in closed):+.2f}%  |  "
                f"Worst: {min(t.pnl_pct for t in closed):+.2f}%",
                color="#8b949e", fontsize=7.5)

        fig.patch.set_facecolor("#0d1117")

        canvas = FigureCanvasTkAgg(fig, master=self._chart_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)
        self._canvas_widget = canvas
        plt.close(fig)

    # ── History tab ───────────────────────────────────────────────────────────

    def _build_history_tab(self, parent):
        ctrl = tk.Frame(parent, bg="#0d1117", pady=8)
        ctrl.pack(fill="x", padx=15)
        ttk.Button(ctrl, text="Refresh", style="Btn.TButton",
                   command=self._refresh_history).pack(side="left")
        ttk.Button(ctrl, text="Export Report", style="Btn.TButton",
                   command=self._export_history_report).pack(side="left", padx=(6, 0))
        self._hist_summary_var = tk.StringVar(value="")
        ttk.Label(ctrl, textvariable=self._hist_summary_var, foreground="#8b949e",
                  background="#0d1117", font=("Consolas", 9)).pack(side="left", padx=12)

        hf = tk.Frame(parent, bg="#0d1117")
        hf.pack(fill="both", expand=True, padx=15, pady=(0, 8))

        hist_cols = ("date","symbol","engine","entry","exit","amount","lev","pnl_pct","pnl_usd","reason","trigger")
        self.hist_tree = ttk.Treeview(hf, columns=hist_cols, show="headings")
        for col, heading, width in [
            ("date","CLOSED",110),("symbol","SYMBOL",65),("engine","ENGINE",60),
            ("entry","ENTRY",85),("exit","EXIT",85),("amount","AMOUNT $",80),
            ("lev","ENTRY LEV",70),("pnl_pct","P&L %",65),("pnl_usd","P&L $",75),
            ("reason","REASON",75),("trigger","24H TRIG",75),
        ]:
            self.hist_tree.heading(col, text=heading)
            self.hist_tree.column(col, width=width, anchor="center")

        vsb = ttk.Scrollbar(hf, orient="vertical", command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=vsb.set)
        self.hist_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.hist_tree.tag_configure("win",  foreground="#3fb950")
        self.hist_tree.tag_configure("loss", foreground="#f85149")

    def _refresh_history(self):
        for item in self.hist_tree.get_children():
            self.hist_tree.delete(item)

        path = _history_csv()
        if not path.exists():
            self._hist_summary_var.set("No trade history yet.")
            return

        rows = []
        with open(path, "r", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))

        total = len(rows)
        wins  = sum(1 for r in rows if float(r.get("pnl_pct", 0)) > 0)
        total_pnl = sum(float(r.get("pnl_usd", 0)) for r in rows)
        wr = (wins / total * 100) if total else 0
        self._hist_summary_var.set(
            f"  {total} trades  |  Win rate: {wr:.1f}%  |  Total P&L: ${total_pnl:+.2f}")

        for r in reversed(rows):
            pnl = float(r.get("pnl_pct", 0))
            tag = "win" if pnl > 0 else "loss"
            close_t = r.get("close_time", "")[:16].replace("T", " ")
            self.hist_tree.insert("", "end", tags=(tag,), values=(
                close_t,
                r.get("symbol",""),
                r.get("engine",""),
                f"${float(r.get('entry_price',0)):.4f}",
                f"${float(r.get('exit_price',0)):.4f}" if r.get("exit_price") else "--",
                f"${float(r.get('amount_usd',0)):.2f}",
                f"{r.get('leverage','1')}x",
                f"{pnl:+.2f}%",
                f"${float(r.get('pnl_usd',0)):+.2f}",
                r.get("reason",""),
                f"{float(r.get('entry_change_24h',0)):+.2f}%",
            ))

    def _export_history_report(self):
        path = _history_csv()
        if not path.exists():
            self._hist_summary_var.set("No trade history to export.")
            return

        with open(path, "r", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        if not rows:
            self._hist_summary_var.set("No trade history to export.")
            return

        total = len(rows)
        wins = [r for r in rows if self._num(r.get("pnl_pct")) > 0]
        losses = [r for r in rows if self._num(r.get("pnl_pct")) <= 0]
        total_pnl_usd = sum(self._num(r.get("pnl_usd")) for r in rows)
        avg_pnl_pct = sum(self._num(r.get("pnl_pct")) for r in rows) / total
        best = max(rows, key=lambda r: self._num(r.get("pnl_pct")))
        worst = min(rows, key=lambda r: self._num(r.get("pnl_pct")))
        contributed_capital = (
            self.trader.get_contributed_capital()
            if hasattr(self.trader, "get_contributed_capital")
            else config.CAPITAL_USD
        )
        portfolio = self.trader.get_portfolio_value()

        by_engine = defaultdict(list)
        by_reason = defaultdict(list)
        by_leverage = defaultdict(list)
        for row in rows:
            by_engine[row.get("engine", "unknown")].append(row)
            by_reason[row.get("reason", "unknown")].append(row)
            by_leverage[row.get("leverage", "1")].append(row)

        def section(title, groups):
            lines = [title]
            for key in sorted(groups):
                group = groups[key]
                group_wins = sum(1 for r in group if self._num(r.get("pnl_pct")) > 0)
                group_pnl = sum(self._num(r.get("pnl_usd")) for r in group)
                win_rate = group_wins / len(group) * 100 if group else 0
                lines.append(
                    f"  {key}: {len(group)} trades | win rate {win_rate:.1f}% | P&L ${group_pnl:+.2f}"
                )
            return "\n".join(lines)

        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        report = "\n\n".join([
            "TradingBot23 Performance Report",
            f"Generated: {generated}",
            (
                f"Trades: {total}\n"
                f"Wins: {len(wins)} | Losses/breakeven: {len(losses)} | "
                f"Win rate: {(len(wins) / total * 100):.1f}%\n"
                f"Contributed capital: ${contributed_capital:,.2f}\n"
                f"Current portfolio: ${portfolio:,.2f}\n"
                f"Total P&L: ${total_pnl_usd:+.2f}\n"
                f"Average P&L per trade: {avg_pnl_pct:+.2f}%\n"
                f"Best trade: {best.get('symbol', '')} {self._num(best.get('pnl_pct')):+.2f}% "
                f"(${self._num(best.get('pnl_usd')):+.2f})\n"
                f"Worst trade: {worst.get('symbol', '')} {self._num(worst.get('pnl_pct')):+.2f}% "
                f"(${self._num(worst.get('pnl_usd')):+.2f})"
            ),
            section("By Engine", by_engine),
            section("By Entry Leverage", by_leverage),
            section("By Exit Reason", by_reason),
            "Note: This report summarizes local paper futures history from trade_history.csv.",
        ])

        out = config.DATA_DIR / f"performance_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        out.write_text(report, encoding="utf-8")
        self._hist_summary_var.set(f"Report exported: {out.name}")

    # ── P2P arbitrage tab ────────────────────────────────────────────────────

    def _build_p2p_tab(self, parent):
        body = tk.Frame(parent, bg="#0d1117", padx=15, pady=12)
        body.pack(fill="both", expand=True)

        top = tk.Frame(body, bg="#0d1117")
        top.pack(fill="x", pady=(0, 8))

        ttk.Label(top, text="USDT/PHP P2P ASSIST", style="Header.TLabel",
                  background="#0d1117").pack(side="left")
        ttk.Button(top, text="Refresh Prices", style="Btn.TButton",
                   command=self._refresh_p2p).pack(side="right")

        controls = tk.Frame(body, bg="#0d1117")
        controls.pack(fill="x", pady=(0, 8))

        saved_p2p_state = self.p2p_paper.state()
        saved_p2p_capital = self._num(saved_p2p_state.get("starting_php"), 500_000)
        self._p2p_capital_php = tk.StringVar(value=f"{saved_p2p_capital:.0f}")
        self._p2p_min_profit_pct = tk.StringVar(value="0.10")
        self._p2p_transfer_fee_usdt = tk.StringVar(value="1.0")
        self._p2p_buffer_php = tk.StringVar(value="0")
        self._p2p_auto_alert = tk.BooleanVar(value=True)
        self._p2p_auto_log = tk.BooleanVar(value=False)
        self._p2p_auto_paper = tk.BooleanVar(value=False)
        self._p2p_auto_hold_sell = tk.BooleanVar(value=True)
        self._p2p_mode = tk.StringVar(value="paper")

        for label_text, var, width in [
            ("Capital PHP", self._p2p_capital_php, 10),
            ("Min net %", self._p2p_min_profit_pct, 6),
            ("Xfer fee USDT", self._p2p_transfer_fee_usdt, 6),
            ("Buffer PHP", self._p2p_buffer_php, 7),
        ]:
            group = tk.Frame(controls, bg="#0d1117")
            group.pack(side="left", padx=(0, 12))
            ttk.Label(group, text=label_text, foreground="#8b949e", background="#0d1117",
                      font=("Consolas", 8)).pack(anchor="w")
            tk.Entry(
                group,
                textvariable=var,
                width=width,
                font=("Consolas", 9),
                bg="#f0f6fc",
                fg="#0d1117",
                insertbackground="#0d1117",
                selectbackground="#58a6ff",
                selectforeground="#0d1117",
                relief="solid",
                bd=1,
                highlightthickness=1,
                highlightbackground="#8b949e",
                highlightcolor="#58a6ff",
            ).pack(anchor="w")

        mode_group = tk.Frame(controls, bg="#0d1117")
        mode_group.pack(side="left", padx=(0, 12), pady=(13, 0))
        for value, text in [("paper", "Paper Sim"), ("live", "Live Assist")]:
            tk.Radiobutton(
                mode_group,
                text=text,
                value=value,
                variable=self._p2p_mode,
                command=self._sync_p2p_mode_controls,
                bg="#0d1117",
                fg="#c9d1d9",
                selectcolor="#161b22",
                activebackground="#0d1117",
                activeforeground="#58a6ff",
                font=("Consolas", 9),
            ).pack(side="left", padx=(0, 8))

        ttk.Button(controls, text="Recalculate", style="Btn.TButton",
                   command=self._recalculate_p2p_routes).pack(side="left", padx=(0, 12), pady=(13, 0))

        action_bar = tk.Frame(body, bg="#0d1117")
        action_bar.pack(fill="x", pady=(0, 8))

        self._p2p_paper_actions = tk.Frame(action_bar, bg="#0d1117")
        ttk.Button(self._p2p_paper_actions, text="Run Instant Paper Cycle", style="Btn.TButton",
                   command=self._paper_cycle_now).pack(side="left", padx=(0, 6))
        ttk.Button(self._p2p_paper_actions, text="Open Paper Hold", style="Btn.TButton",
                   command=self._paper_hold_buy_now).pack(side="left", padx=(0, 6))
        ttk.Button(self._p2p_paper_actions, text="Check / Sell Hold", style="Btn.TButton",
                   command=self._check_p2p_hold_sell).pack(side="left", padx=(0, 6))
        ttk.Button(self._p2p_paper_actions, text="Reset Paper", style="Btn.TButton",
                   command=self._reset_p2p_paper).pack(side="left", padx=(0, 12))
        self._p2p_paper_auto = self._p2p_checkbutton(
            self._p2p_paper_actions,
            text="Auto Cycle",
            variable=self._p2p_auto_paper,
        )
        self._p2p_paper_auto.pack(side="left", padx=(0, 10))
        self._p2p_hold_auto = self._p2p_checkbutton(
            self._p2p_paper_actions,
            text="Auto Hold Sell",
            variable=self._p2p_auto_hold_sell,
        )
        self._p2p_hold_auto.pack(side="left")

        self._p2p_live_actions = tk.Frame(action_bar, bg="#0d1117")
        ttk.Button(self._p2p_live_actions, text="Send Live Snapshot", style="Btn.TButton",
                   command=self._send_p2p_live_snapshot).pack(side="left", padx=(0, 6))
        ttk.Button(self._p2p_live_actions, text="Log Watch Route", style="Btn.TButton",
                   command=self._log_top_p2p_route).pack(side="left", padx=(0, 12))
        self._p2p_live_alert = self._p2p_checkbutton(
            self._p2p_live_actions,
            text="Telegram Alerts",
            variable=self._p2p_auto_alert,
        )
        self._p2p_live_alert.pack(side="left", padx=(0, 10))
        self._p2p_live_log = self._p2p_checkbutton(
            self._p2p_live_actions,
            text="Auto Watch Log",
            variable=self._p2p_auto_log,
        )
        self._p2p_live_log.pack(side="left")

        self._p2p_status_var = tk.StringVar(value="Open this tab or press Refresh to load prices.")
        ttk.Label(body, textvariable=self._p2p_status_var, foreground="#8b949e",
                  background="#0d1117", font=("Consolas", 9)).pack(fill="x", anchor="w")
        self._sync_p2p_mode_controls()

        summary = tk.Frame(body, bg="#0d1117")
        summary.pack(fill="x", pady=(8, 10))

        self._p2p_buy_var = tk.StringVar(value="--")
        self._p2p_sell_var = tk.StringVar(value="--")
        self._p2p_top_profit_var = tk.StringVar(value="--")
        self._p2p_top_route_var = tk.StringVar(value="--")
        self._p2p_sweep_profit_var = tk.StringVar(value="--")
        self._p2p_paper_balance_var = tk.StringVar(value="--")
        self._p2p_paper_cycles_var = tk.StringVar(value="--")
        self._p2p_hold_usdt_var = tk.StringVar(value="--")
        self._p2p_hold_pnl_var = tk.StringVar(value="--")

        for label_text, var in [
            ("BEST BUY USDT", self._p2p_buy_var),
            ("BEST SELL USDT", self._p2p_sell_var),
            ("TOP NET PROFIT", self._p2p_top_profit_var),
            ("DEPTH SWEEP", self._p2p_sweep_profit_var),
            ("PAPER BAL", self._p2p_paper_balance_var),
            ("CYCLES", self._p2p_paper_cycles_var),
            ("HOLD USDT", self._p2p_hold_usdt_var),
            ("HOLD P&L", self._p2p_hold_pnl_var),
        ]:
            card = tk.Frame(summary, bg="#161b22",
                            highlightbackground="#30363d", highlightthickness=1)
            card.pack(side="left", padx=(0, 6), ipadx=8, ipady=6)
            ttk.Label(card, text=label_text, foreground="#8b949e", background="#161b22",
                      font=("Consolas", 8)).pack(anchor="w")
            ttk.Label(card, textvariable=var, foreground="#58a6ff", background="#161b22",
                      font=("Consolas", 14, "bold")).pack(anchor="w")
        self._refresh_p2p_paper_summary()

        route_section = tk.Frame(body, bg="#0d1117")
        route_section.pack(fill="x", expand=False, pady=(0, 6))
        ttk.Label(route_section, text="ROUTE CALCULATOR", style="Header.TLabel",
                  background="#0d1117").pack(anchor="w", pady=(0, 3))

        route_cols = ("route", "size", "buy", "sell", "profit", "pct", "grade", "warnings")
        self.p2p_route_tree = ttk.Treeview(route_section, columns=route_cols, show="headings", height=3)
        for col, heading, width in [
            ("route", "ROUTE", 115),
            ("size", "SIZE PHP", 95),
            ("buy", "BUY", 135),
            ("sell", "SELL", 135),
            ("profit", "NET PHP", 95),
            ("pct", "NET %", 65),
            ("grade", "GRADE", 65),
            ("warnings", "WARNINGS", 220),
        ]:
            self.p2p_route_tree.heading(col, text=heading)
            self.p2p_route_tree.column(col, width=width, anchor="center")
        self.p2p_route_tree.tag_configure("A", foreground="#3fb950")
        self.p2p_route_tree.tag_configure("B", foreground="#58a6ff")
        self.p2p_route_tree.tag_configure("C", foreground="#e3b341")
        self.p2p_route_tree.tag_configure("REVIEW", foreground="#f85149")
        self.p2p_route_tree.tag_configure("WATCH", foreground="#8b949e")
        self.p2p_route_tree.tag_configure("SKIP", foreground="#f85149")
        self.p2p_route_tree.pack(fill="both", expand=True)

        ad_tables = tk.Frame(body, bg="#0d1117")
        ad_tables.pack(fill="both", expand=True)
        buy_parent = tk.Frame(ad_tables, bg="#0d1117")
        sell_parent = tk.Frame(ad_tables, bg="#0d1117")
        buy_parent.pack(side="left", fill="both", expand=True, padx=(0, 6))
        sell_parent.pack(side="left", fill="both", expand=True, padx=(6, 0))

        self.p2p_buy_tree = self._build_p2p_table(
            buy_parent,
            "BUY USDT WITH PHP (LOWEST SELLER PRICES)",
            height=8,
        )
        self.p2p_sell_tree = self._build_p2p_table(
            sell_parent,
            "SELL USDT FOR PHP (HIGHEST BUYER PRICES)",
            height=8,
        )

        journal = tk.Frame(body, bg="#0d1117")
        journal.pack(fill="x", expand=False, pady=(0, 4))
        ttk.Label(journal, text="RECENT P2P CYCLE JOURNAL", style="Header.TLabel",
                  background="#0d1117").pack(anchor="w", pady=(0, 3))
        journal_cols = ("time", "status", "route", "size", "profit", "notes")
        self.p2p_journal_tree = ttk.Treeview(journal, columns=journal_cols, show="headings", height=3)
        for col, heading, width in [
            ("time", "TIME", 120),
            ("status", "STATUS", 85),
            ("route", "ROUTE", 120),
            ("size", "SIZE PHP", 90),
            ("profit", "EXP PHP", 90),
            ("notes", "NOTES", 320),
        ]:
            self.p2p_journal_tree.heading(col, text=heading)
            self.p2p_journal_tree.column(col, width=width, anchor="center")
        self.p2p_journal_tree.pack(fill="both", expand=True)
        self._refresh_p2p_journal()

        tx = tk.Frame(body, bg="#0d1117")
        tx.pack(fill="both", expand=True, pady=(0, 4))
        ttk.Label(tx, text="P2P PAPER TRANSACTION HISTORY", style="Header.TLabel",
                  background="#0d1117").pack(anchor="w", pady=(0, 3))
        tx_cols = ("time", "type", "status", "php", "usdt", "profit", "balance", "notes")
        self.p2p_tx_tree = ttk.Treeview(tx, columns=tx_cols, show="headings", height=5)
        for col, heading, width in [
            ("time", "TIME", 120),
            ("type", "TYPE", 95),
            ("status", "STATUS", 75),
            ("php", "PHP", 90),
            ("usdt", "USDT", 90),
            ("profit", "P&L PHP", 90),
            ("balance", "BALANCE", 95),
            ("notes", "NOTES", 300),
        ]:
            self.p2p_tx_tree.heading(col, text=heading)
            self.p2p_tx_tree.column(col, width=width, anchor="center")
        self.p2p_tx_tree.tag_configure("profit", foreground="#3fb950")
        self.p2p_tx_tree.tag_configure("loss", foreground="#f85149")
        self.p2p_tx_tree.pack(fill="both", expand=True)
        self._refresh_p2p_transactions()

    def _p2p_checkbutton(self, parent, text: str, variable: tk.BooleanVar):
        return tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            bg="#0d1117",
            fg="#c9d1d9",
            selectcolor="#161b22",
            activebackground="#0d1117",
            activeforeground="#58a6ff",
            font=("Consolas", 9),
        )

    def _p2p_current_mode(self) -> str:
        if not hasattr(self, "_p2p_mode"):
            return "paper"
        return self._p2p_mode.get() or "paper"

    def _sync_p2p_mode_controls(self):
        if not hasattr(self, "_p2p_paper_actions") or not hasattr(self, "_p2p_live_actions"):
            return
        self._p2p_paper_actions.pack_forget()
        self._p2p_live_actions.pack_forget()
        if self._p2p_current_mode() == "live":
            self._p2p_live_actions.pack(side="left", fill="x")
            if hasattr(self, "_p2p_status_var"):
                self._p2p_status_var.set(
                    "Live Assist: scan, alert, and log only. No P2P order, fiat, or release is automated."
                )
        else:
            self._p2p_paper_actions.pack(side="left", fill="x")
            if hasattr(self, "_p2p_status_var"):
                self._p2p_status_var.set(
                    "Paper Sim: Instant Cycle closes immediately; Paper Hold keeps USDT until target."
                )

    def _build_p2p_table(self, parent, title: str, height: int):
        section = tk.Frame(parent, bg="#0d1117")
        section.pack(fill="both", expand=True, pady=(0, 10))

        ttk.Label(section, text=title, style="Header.TLabel",
                  background="#0d1117").pack(anchor="w", pady=(0, 3))

        table_frame = tk.Frame(section, bg="#0d1117")
        table_frame.pack(fill="both", expand=True)

        cols = ("price", "limits", "available", "methods", "advertiser", "finish", "orders")
        tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=height)
        for col, heading, width in [
            ("price", "PRICE PHP", 75),
            ("limits", "LIMIT PHP", 125),
            ("available", "AVAIL USDT", 90),
            ("methods", "PAYMENT", 155),
            ("advertiser", "ADVERTISER", 115),
            ("finish", "FINISH", 60),
            ("orders", "ORDERS", 60),
        ]:
            tree.heading(col, text=heading)
            tree.column(col, width=width, minwidth=50, anchor="center", stretch=False)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(table_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        return tree

    def _refresh_p2p(self):
        if self._p2p_refreshing:
            return
        if not hasattr(self, "_p2p_status_var"):
            return

        self._p2p_refreshing = True
        self._p2p_next_refresh_ts = time.time() + self.P2P_REFRESH_SECS
        self._p2p_status_var.set("Loading Binance P2P USDT/PHP and recalculating routes...")

        worker = threading.Thread(target=self._load_p2p_snapshot, daemon=True)
        worker.start()

    def _load_p2p_snapshot(self):
        snapshot = None
        error = None
        try:
            snapshot = self.p2p_monitor.fetch_snapshot(rows=20)
        except Exception as exc:
            logger.exception("Failed to refresh P2P monitor")
            error = str(exc)

        def finish():
            self._p2p_refreshing = False
            if snapshot:
                self._apply_p2p_snapshot(snapshot)
            else:
                self._p2p_status_var.set(f"P2P load failed: {error or 'unknown error'}")

        try:
            self.root.after(0, finish)
        except tk.TclError:
            pass

    def _apply_p2p_snapshot(self, snapshot: P2PSnapshot):
        self._p2p_last_snapshot = snapshot

        buy = snapshot.best_buy
        sell = snapshot.best_sell

        self._p2p_buy_var.set(f"{buy.price:,.2f} PHP" if buy else "--")
        self._p2p_sell_var.set(f"{sell.price:,.2f} PHP" if sell else "--")

        self._fill_p2p_tree(self.p2p_buy_tree, snapshot.buy_ads)
        self._fill_p2p_tree(self.p2p_sell_tree, snapshot.sell_ads)

        self._p2p_status_var.set(
            f"USDT/PHP updated {snapshot.as_of.strftime('%H:%M:%S')} UTC  |  "
            "Routes are estimates only; manual fiat verification is still required."
        )
        self._recalculate_p2p_routes(update_status=False)

    def _recalculate_p2p_routes(self, update_status: bool = True):
        settings = self._p2p_route_settings()
        if not settings:
            return

        state, capital_delta = self.p2p_paper.sync_starting_capital(settings.capital_php)
        self._refresh_p2p_paper_summary(state)

        if not self._p2p_last_snapshot:
            if update_status and hasattr(self, "_p2p_status_var"):
                self._p2p_status_var.set(
                    self._p2p_capital_status(
                        capital_delta,
                        "Load P2P prices before recalculating routes.",
                    )
                )
            return

        self._p2p_routes = build_p2p_routes([self._p2p_last_snapshot], settings=settings)
        self._p2p_sweep = build_depth_sweep([self._p2p_last_snapshot], settings=settings)
        self._fill_p2p_route_tree(self._p2p_routes)
        self._update_p2p_sweep_summary()
        self._refresh_p2p_paper_summary()

        if self._p2p_routes:
            top = self._p2p_routes[0]
            self._p2p_top_profit_var.set(f"{top.profit_php:+,.0f} PHP")
            self._p2p_top_route_var.set(f"{top.route_label} {top.grade}")
            if update_status:
                self._p2p_status_var.set(
                    self._p2p_capital_status(
                        capital_delta,
                        f"Recalculated {len(self._p2p_routes)} route(s) for "
                        f"{settings.capital_php:,.0f} PHP capital.",
                    )
                )
        else:
            self._p2p_top_profit_var.set("--")
            self._p2p_top_route_var.set("--")
            if update_status:
                self._p2p_status_var.set(
                    self._p2p_capital_status(
                        capital_delta,
                        "No route meets the current profit filters.",
                    )
                )

        if self._p2p_current_mode() == "live":
            self._run_p2p_live_assist(settings)
        else:
            if self._p2p_auto_paper.get():
                self._execute_p2p_paper_cycle(auto=True)
            if self._p2p_auto_hold_sell.get():
                self._evaluate_p2p_hold(auto=True)

    def _p2p_route_settings(self) -> P2PRouteSettings | None:
        try:
            capital_php = float(self._p2p_capital_php.get())
            min_profit_pct = float(self._p2p_min_profit_pct.get())
            transfer_fee = float(self._p2p_transfer_fee_usdt.get())
            buffer_php = float(self._p2p_buffer_php.get())
        except ValueError as exc:
            self._p2p_status_var.set(f"P2P settings error: {exc}")
            return None

        if capital_php <= 0:
            self._p2p_status_var.set("P2P settings error: capital must be positive")
            return None
        if min_profit_pct < 0 or transfer_fee < 0 or buffer_php < 0:
            self._p2p_status_var.set("P2P settings error: min %, fee, and buffer cannot be negative")
            return None

        settings = P2PRouteSettings(
            capital_php=capital_php,
            min_profit_php=-1_000_000_000.0,
            min_profit_pct=min_profit_pct,
            cross_exchange_transfer_fee_usdt=transfer_fee,
            local_buffer_php=buffer_php,
        )
        self._p2p_settings_cache = settings
        return settings

    def _fill_p2p_route_tree(self, routes: list[P2PRoute]):
        for item in self.p2p_route_tree.get_children():
            self.p2p_route_tree.delete(item)

        if self._p2p_sweep:
            warnings = "; ".join(self._p2p_sweep.warnings) if self._p2p_sweep.warnings else "ok"
            self.p2p_route_tree.insert("", "end", tags=(self._p2p_sweep.grade,), values=(
                "DEPTH SWEEP",
                f"{self._p2p_sweep.size_php:,.0f}",
                f"avg {self._p2p_sweep.avg_buy_price:,.2f}",
                f"avg {self._p2p_sweep.avg_sell_price:,.2f}",
                f"{self._p2p_sweep.profit_php:+,.0f}",
                f"{self._p2p_sweep.profit_pct:+.3f}%",
                self._p2p_sweep.grade,
                self._clip_text(warnings, 34),
            ))

        for route in routes:
            warnings = "; ".join(route.warnings) if route.warnings else "ok"
            self.p2p_route_tree.insert("", "end", tags=(route.grade,), values=(
                route.route_label,
                f"{route.size_php:,.0f}",
                f"{route.buy_ad.marketplace} {route.buy_ad.price:,.2f}",
                f"{route.sell_ad.marketplace} {route.sell_ad.price:,.2f}",
                f"{route.profit_php:+,.0f}",
                f"{route.profit_pct:+.3f}%",
                route.grade,
                self._clip_text(warnings, 34),
            ))

    def _update_p2p_sweep_summary(self):
        if not self._p2p_sweep:
            self._p2p_sweep_profit_var.set("--")
            return
        sweep = self._p2p_sweep
        self._p2p_sweep_profit_var.set(f"{sweep.profit_php:+,.0f} PHP")

    def _run_p2p_live_assist(self, settings: P2PRouteSettings):
        if not self._p2p_last_snapshot:
            return
        if getattr(self, "_p2p_auto_log", None) and self._p2p_auto_log.get():
            self._auto_log_p2p_route(settings)
        if getattr(self, "_p2p_auto_alert", None) and self._p2p_auto_alert.get():
            self._maybe_send_p2p_assist_alert(settings)

    def _auto_log_p2p_route(self, settings: P2PRouteSettings):
        if not self._p2p_routes:
            return
        route = self._p2p_routes[0]
        if route.profit_php <= 0 or route.profit_pct < settings.min_profit_pct:
            return
        key = (
            f"{route.route_label}|{route.size_php:.0f}|"
            f"{route.buy_ad.price:.2f}|{route.sell_ad.price:.2f}|{route.profit_php:.0f}"
        )
        if key == self._p2p_last_logged_route_key:
            return
        self._p2p_last_logged_route_key = key
        self.p2p_journal.append(route, status="AUTO_WATCH", notes="live assist")
        self._refresh_p2p_journal()

    def _maybe_send_p2p_assist_alert(self, settings: P2PRouteSettings):
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
            return
        key = ""
        text = ""
        if self._p2p_sweep and self._p2p_sweep.profit_php > 0 and self._p2p_sweep.profit_pct >= settings.min_profit_pct:
            sweep = self._p2p_sweep
            key = (
                f"SWEEP|{sweep.size_php:.0f}|{sweep.avg_buy_price:.2f}|"
                f"{sweep.avg_sell_price:.2f}|{sweep.profit_php:.0f}"
            )
            warnings = "; ".join(sweep.warnings) if sweep.warnings else "ok"
            text = "\n".join([
                f"Depth sweep: <b>{sweep.size_php:,.0f} PHP</b>",
                f"Avg buy/sell: <b>{sweep.avg_buy_price:,.2f}</b> / <b>{sweep.avg_sell_price:,.2f}</b>",
                f"Net: <b>{sweep.profit_php:+,.0f} PHP ({sweep.profit_pct:+.3f}%)</b> | Grade {sweep.grade}",
                f"Warnings: {tg.escape_html(warnings)}",
                "Manual fiat/payment/release confirmation required.",
            ])
        elif self._p2p_routes:
            route = self._p2p_routes[0]
            if route.profit_php <= 0 or route.profit_pct < settings.min_profit_pct:
                return
            key = (
                f"ROUTE|{route.route_label}|{route.size_php:.0f}|"
                f"{route.buy_ad.price:.2f}|{route.sell_ad.price:.2f}|{route.profit_php:.0f}"
            )
            warnings = "; ".join(route.warnings) if route.warnings else "ok"
            text = "\n".join([
                f"Route: <b>{tg.escape_html(route.route_label)}</b>",
                f"Size: <b>{route.size_php:,.0f} PHP</b>",
                f"Buy/sell: <b>{route.buy_ad.price:,.2f}</b> / <b>{route.sell_ad.price:,.2f}</b>",
                f"Net: <b>{route.profit_php:+,.0f} PHP ({route.profit_pct:+.3f}%)</b> | Grade {route.grade}",
                f"Warnings: {tg.escape_html(warnings)}",
                "Manual fiat/payment/release confirmation required.",
            ])
        if not key:
            return
        now = time.time()
        if key == self._p2p_last_alert_key:
            return
        if now - self._p2p_last_alert_ts < self.P2P_ALERT_COOLDOWN_SECS:
            return
        self._p2p_last_alert_key = key
        self._p2p_last_alert_ts = now
        tg.alert_p2p_signal(text)

    def _send_p2p_live_snapshot(self):
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
            self._p2p_status_var.set("Telegram snapshot needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.")
            return
        self._p2p_status_var.set("Sending live P2P snapshot to Telegram...")

        def worker():
            try:
                text = self._telegram_p2p_snapshot()
                tg.send_dashboard_text(text)
                status = "Live P2P snapshot sent to Telegram."
            except Exception as exc:
                logger.exception("Failed to send P2P Telegram snapshot")
                status = f"Telegram P2P snapshot failed: {exc}"
            try:
                self.root.after(0, lambda: self._p2p_status_var.set(status))
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _paper_cycle_now(self):
        if not self._p2p_last_snapshot:
            self._refresh_p2p()
            self._p2p_status_var.set("Loading prices first; press Run Instant Paper Cycle again after refresh.")
            return
        self._recalculate_p2p_routes(update_status=False)
        self._execute_p2p_paper_cycle(auto=False)

    def _paper_hold_buy_now(self):
        if not self._p2p_last_snapshot:
            self._refresh_p2p()
            self._p2p_status_var.set("Loading prices first; press Open Paper Hold again after refresh.")
            return
        settings = self._p2p_route_settings()
        if not settings:
            return
        state, _ = self.p2p_paper.sync_starting_capital(settings.capital_php)
        if state.get("hold_position"):
            self._p2p_status_var.set("Hold buy skipped: one P2P hold is already open.")
            self._refresh_p2p_paper_summary(state)
            return
        cash_php = self._num(state.get("cash_php"), settings.capital_php)
        if cash_php <= 0:
            self._p2p_status_var.set("Hold buy skipped: no free PHP paper cash.")
            return

        hold_settings = P2PRouteSettings(
            capital_php=min(settings.capital_php, cash_php),
            min_profit_php=settings.min_profit_php,
            min_profit_pct=settings.min_profit_pct,
            min_completion_rate=settings.min_completion_rate,
            min_orders=settings.min_orders,
            cross_exchange_transfer_fee_usdt=settings.cross_exchange_transfer_fee_usdt,
            local_buffer_php=settings.local_buffer_php,
            allowed_methods=settings.allowed_methods,
        )
        entry = build_p2p_hold_entry([self._p2p_last_snapshot], settings=hold_settings)
        if not entry:
            self._p2p_status_var.set("Hold buy skipped: not enough buy-side depth.")
            return
        state, position = self.p2p_paper.open_hold(
            entry,
            target_profit_pct=settings.min_profit_pct,
            starting_php=settings.capital_php,
        )
        self._refresh_p2p_paper_summary(state)
        if position:
            warnings = "; ".join(entry.warnings) if entry.warnings else "ok"
            self._p2p_status_var.set(
                f"Paper hold opened: {entry.buy_usdt:,.2f} USDT @ "
                f"{entry.avg_buy_price:,.2f} | Target +{settings.min_profit_pct:.3f}% | {warnings}"
            )
        else:
            self._p2p_status_var.set("Hold buy skipped: insufficient free cash or open hold exists.")

    def _check_p2p_hold_sell(self):
        if not self._p2p_last_snapshot:
            self._refresh_p2p()
            self._p2p_status_var.set("Loading prices first; press Check / Sell Hold again after refresh.")
            return
        self._evaluate_p2p_hold(auto=False)

    def _evaluate_p2p_hold(self, auto: bool):
        if not self._p2p_last_snapshot:
            return
        settings = self._p2p_route_settings()
        if not settings:
            return
        state, _ = self.p2p_paper.sync_starting_capital(settings.capital_php)
        if not state.get("hold_position"):
            if not auto:
                self._p2p_status_var.set("No open P2P hold to evaluate.")
            self._refresh_p2p_paper_summary(state)
            return

        state, evaluation = self.p2p_paper.evaluate_hold_exit(
            [self._p2p_last_snapshot],
            settings=settings,
            starting_php=settings.capital_php,
        )
        self._refresh_p2p_paper_summary(state)
        if not evaluation:
            if not auto:
                self._p2p_status_var.set("Hold sell check skipped: not enough sell-side depth.")
            return
        if evaluation.exit_ready:
            source = "Auto hold" if auto else "Hold"
            self._p2p_status_var.set(
                f"{source} sold: {evaluation.profit_php:+,.0f} PHP "
                f"({evaluation.profit_pct:+.3f}%) @ {evaluation.avg_sell_price:,.2f}"
            )
        elif not auto:
            warnings = "; ".join(evaluation.warnings) if evaluation.warnings else "waiting"
            self._p2p_status_var.set(
                f"Hold not sold: {evaluation.profit_php:+,.0f} PHP "
                f"({evaluation.profit_pct:+.3f}%) vs target "
                f"+{evaluation.target_profit_pct:.3f}% | {warnings}"
            )

    def _execute_p2p_paper_cycle(self, auto: bool):
        if not self._p2p_last_snapshot or not self._p2p_sweep:
            self._p2p_status_var.set("No depth sweep available for paper cycle.")
            return
        settings = self._p2p_route_settings()
        if not settings:
            return
        state, _ = self.p2p_paper.sync_starting_capital(settings.capital_php)
        paper_capital = self._num(state.get("cash_php", state.get("balance_php")), settings.capital_php)
        if paper_capital <= 0:
            if not auto:
                self._p2p_status_var.set("Paper cycle skipped: no free PHP paper cash.")
            return
        paper_settings = P2PRouteSettings(
            capital_php=paper_capital,
            min_profit_php=settings.min_profit_php,
            min_profit_pct=settings.min_profit_pct,
            min_completion_rate=settings.min_completion_rate,
            min_orders=settings.min_orders,
            cross_exchange_transfer_fee_usdt=settings.cross_exchange_transfer_fee_usdt,
            local_buffer_php=settings.local_buffer_php,
            allowed_methods=settings.allowed_methods,
        )
        paper_sweep = build_depth_sweep([self._p2p_last_snapshot], settings=paper_settings)
        if not paper_sweep:
            self._p2p_status_var.set("Paper cycle skipped: not enough depth.")
            return
        state, cycle = self.p2p_paper.execute_if_profitable(
            paper_sweep,
            min_profit_pct=settings.min_profit_pct,
            starting_php=settings.capital_php,
        )
        self._refresh_p2p_paper_summary(state)
        if cycle:
            source = "Auto paper" if auto else "Paper"
            self._p2p_status_var.set(
                f"{source} cycle: {cycle.profit_php:+,.0f} PHP | "
                f"Balance {cycle.balance_after_php:,.0f} PHP"
            )
        elif not auto:
            self._p2p_status_var.set("Paper cycle skipped: sweep is below profit filter.")

    def _reset_p2p_paper(self):
        try:
            starting_php = float(self._p2p_capital_php.get())
        except ValueError as exc:
            self._p2p_status_var.set(f"P2P settings error: {exc}")
            return
        state = self.p2p_paper.reset(starting_php)
        self._refresh_p2p_paper_summary(state)
        self._p2p_status_var.set(f"P2P paper arb reset to {starting_php:,.0f} PHP.")

    def _refresh_p2p_paper_summary(self, state: dict | None = None):
        if not hasattr(self, "_p2p_paper_balance_var"):
            return
        starting_php = 500_000
        if hasattr(self, "_p2p_capital_php"):
            starting_php = self._num(self._p2p_capital_php.get(), 500_000)
        state = state or self.p2p_paper.state(starting_php)
        balance = self._num(state.get("balance_php"), 0)
        cash = self._num(state.get("cash_php"), balance)
        cycles = state.get("cycles", [])
        hold_trades = state.get("hold_trades", [])
        realized = self._num(state.get("realized_profit_php"), 0)
        hold = state.get("hold_position") or {}
        self._p2p_paper_balance_var.set(f"{balance:,.0f} PHP")
        self._p2p_paper_cycles_var.set(f"{len(cycles) + len(hold_trades)} ({realized:+,.0f})")
        if hold:
            usdt = self._num(hold.get("usdt"), 0)
            avg_buy = self._num(hold.get("avg_buy_price"), 0)
            last_profit = hold.get("last_profit_php")
            last_pct = hold.get("last_profit_pct")
            self._p2p_hold_usdt_var.set(f"{usdt:,.2f}")
            if last_profit is not None and last_pct is not None:
                self._p2p_hold_pnl_var.set(f"{self._num(last_profit):+,.0f} ({self._num(last_pct):+.3f}%)")
            else:
                self._p2p_hold_pnl_var.set(f"@ {avg_buy:,.2f}")
        else:
            self._p2p_hold_usdt_var.set("--")
            self._p2p_hold_pnl_var.set(f"Cash {cash:,.0f}")
        self._refresh_p2p_transactions(state)

    @staticmethod
    def _p2p_capital_status(delta: float, message: str) -> str:
        if abs(delta) < 0.01:
            return message
        return f"P2P paper capital adjusted {delta:+,.0f} PHP. {message}"

    def _log_top_p2p_route(self):
        if not self._p2p_routes:
            self._p2p_status_var.set("No P2P route to log yet.")
            return

        route = self._p2p_routes[0]
        if route.profit_php <= 0:
            self._p2p_status_var.set("Top P2P route is not profitable, not logged.")
            return
        path = self.p2p_journal.append(route, status="WATCHLIST")
        self._refresh_p2p_journal()
        self._p2p_status_var.set(f"Logged top P2P route to {path.name}.")

    def _refresh_p2p_journal(self):
        if not hasattr(self, "p2p_journal_tree"):
            return
        for item in self.p2p_journal_tree.get_children():
            self.p2p_journal_tree.delete(item)

        for row in reversed(self.p2p_journal.recent(limit=8)):
            ts = row.get("timestamp", "")
            time_text = ts[:16].replace("T", " ")
            self.p2p_journal_tree.insert("", "end", values=(
                time_text,
                row.get("status", ""),
                row.get("route", ""),
                f"{self._num(row.get('size_php')):,.0f}",
                f"{self._num(row.get('expected_profit_php')):+,.0f}",
                self._clip_text(row.get("notes", "") or row.get("warnings", ""), 48),
            ))

    def _refresh_p2p_transactions(self, state: dict | None = None):
        if not hasattr(self, "p2p_tx_tree"):
            return
        for item in self.p2p_tx_tree.get_children():
            self.p2p_tx_tree.delete(item)

        if state is None:
            starting_php = 500_000
            if hasattr(self, "_p2p_capital_php"):
                starting_php = self._num(self._p2p_capital_php.get(), 500_000)
            rows = self.p2p_paper.recent_transactions(limit=14, starting_php=starting_php)
        else:
            rows = list(state.get("transactions", []))[-14:]

        for row in reversed(rows):
            ts = row.get("timestamp", "")
            time_text = ts[:16].replace("T", " ")
            profit = self._num(row.get("profit_php"), 0.0)
            tags = ("profit",) if profit > 0 else ("loss",) if profit < 0 else ()
            self.p2p_tx_tree.insert("", "end", tags=tags, values=(
                time_text,
                str(row.get("type", "")).replace("_", " "),
                row.get("status", ""),
                f"{self._num(row.get('amount_php')):,.0f}",
                f"{self._num(row.get('usdt')):,.2f}" if self._num(row.get("usdt")) else "--",
                f"{profit:+,.0f}" if profit else "--",
                f"{self._num(row.get('balance_after_php')):,.0f}",
                self._clip_text(row.get("notes", ""), 42),
            ))

    def _fill_p2p_tree(self, tree, ads):
        for item in tree.get_children():
            tree.delete(item)

        for ad in ads:
            tree.insert("", "end", values=(
                f"{ad.price:,.2f}",
                f"{ad.min_limit:,.0f}-{ad.max_limit:,.0f}",
                f"{ad.available:,.2f}",
                self._clip_text(ad.methods_text, 34),
                self._clip_text(ad.advertiser, 22),
                self._format_rate(ad.completion_rate),
                str(ad.orders) if ad.orders is not None else "--",
            ))

    @staticmethod
    def _format_rate(rate: float | None) -> str:
        if rate is None:
            return "--"
        pct = rate * 100 if rate <= 1 else rate
        return f"{pct:.1f}%"

    @staticmethod
    def _clip_text(value: str, limit: int) -> str:
        text = str(value or "")
        return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."

    # ── Settings tab ──────────────────────────────────────────────────────────

    def _build_settings_tab(self, parent):
        body = tk.Frame(parent, bg="#0d1117", padx=15, pady=14)
        body.pack(fill="both", expand=True)

        settings_panel = tk.Frame(body, bg="#0d1117")
        settings_panel.pack(side="left", fill="y", anchor="nw")

        ttk.Label(settings_panel, text="TRADING PARAMETERS", style="Header.TLabel",
                  background="#0d1117").pack(anchor="w", pady=(0, 4))

        grid = tk.Frame(settings_panel, bg="#0d1117")
        grid.pack(fill="x")

        def field(parent, var, width):
            return tk.Entry(
                parent,
                textvariable=var,
                width=width,
                font=("Consolas", 10),
                bg="#f0f6fc",
                fg="#0d1117",
                insertbackground="#0d1117",
                selectbackground="#58a6ff",
                selectforeground="#0d1117",
                relief="solid",
                bd=1,
                highlightthickness=1,
                highlightbackground="#8b949e",
                highlightcolor="#58a6ff",
            )

        def row(label, widget_factory, r):
            ttk.Label(grid, text=label, foreground="#8b949e", background="#0d1117",
                      font=("Consolas", 9), width=22).grid(row=r, column=0, sticky="w", pady=4)
            w = widget_factory(grid)
            w.grid(row=r, column=1, sticky="w", padx=8, pady=4)
            return w

        # Capital
        self._s_capital = tk.StringVar(value=str(int(config.CAPITAL_USD)))
        row("Capital (USD)", lambda p: field(p, self._s_capital, 10), 0)

        # Leverage
        self._s_leverage = tk.IntVar(value=config.LEVERAGE)
        lev_frame = tk.Frame(grid, bg="#0d1117")
        lev_frame.grid(row=1, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(grid, text="Leverage", foreground="#8b949e", background="#0d1117",
                  font=("Consolas", 9), width=22).grid(row=1, column=0, sticky="w", pady=4)
        tk.Spinbox(
            lev_frame,
            from_=1,
            to=config.MAX_LEVERAGE,
            textvariable=self._s_leverage,
            width=5,
            font=("Consolas", 10),
            bg="#f0f6fc",
            fg="#0d1117",
            buttonbackground="#c9d1d9",
            insertbackground="#0d1117",
            selectbackground="#58a6ff",
            selectforeground="#0d1117",
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground="#8b949e",
            highlightcolor="#58a6ff",
        ).pack(side="left")
        ttk.Label(lev_frame, text="x", foreground="#8b949e", background="#0d1117",
                  font=("Consolas", 9)).pack(side="left", padx=(6, 0))

        # TP %
        self._s_tp = tk.StringVar(value=str(round(config.FUTURES_NET_TP_PCT * 100, 2)))
        row("TP target (% net)", lambda p: field(p, self._s_tp, 8), 2)

        # SL enable + %
        self._s_sl_enabled = tk.BooleanVar(value=config.FUTURES_USE_SL)
        self._s_sl = tk.StringVar(value=str(round(config.FUTURES_NET_SL_PCT * 100, 2)))
        sl_frame = tk.Frame(grid, bg="#0d1117")
        sl_frame.grid(row=3, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(grid, text="Stop Loss", foreground="#8b949e", background="#0d1117",
                  font=("Consolas", 9), width=22).grid(row=3, column=0, sticky="w", pady=4)
        ttk.Checkbutton(sl_frame, text="Enable", variable=self._s_sl_enabled).pack(side="left")
        field(sl_frame, self._s_sl, 8).pack(side="left", padx=8)
        ttk.Label(sl_frame, text="% net", foreground="#8b949e", background="#0d1117",
                  font=("Consolas",9)).pack(side="left")

        # Max hold days
        self._s_hold = tk.StringVar(value=str(config.MAX_HOLD_DAYS))
        row("Max hold (days)", lambda p: field(p, self._s_hold, 8), 4)

        # Per trade %
        self._s_per_trade = tk.StringVar(value=str(round(config.PER_TRADE_PCT * 100, 0)))
        row("Per trade (% of portfolio)", lambda p: field(p, self._s_per_trade, 8), 5)

        # Monthly contribution
        self._s_monthly_contribution = tk.StringVar(value=str(round(config.MONTHLY_CONTRIBUTION_USD, 2)))
        row("Monthly contribution ($)", lambda p: field(p, self._s_monthly_contribution, 8), 6)

        self._s_monthly_day = tk.StringVar(value=str(config.MONTHLY_CONTRIBUTION_DAY))
        row("Contribution day", lambda p: field(p, self._s_monthly_day, 8), 7)

        self._s_auto_start = tk.BooleanVar(value=config.AUTO_START_FUTURES)
        auto_frame = tk.Frame(grid, bg="#0d1117")
        auto_frame.grid(row=8, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(grid, text="Auto-start futures", foreground="#8b949e", background="#0d1117",
                  font=("Consolas", 9), width=22).grid(row=8, column=0, sticky="w", pady=4)
        ttk.Checkbutton(auto_frame, text="Enable on launch", variable=self._s_auto_start).pack(side="left")

        # Apply button
        setup_msg = "" if self._settings_confirmed else "First run: review these values, then click Apply Settings."
        self._s_status = tk.StringVar(value=setup_msg)
        bf = tk.Frame(settings_panel, bg="#0d1117")
        bf.pack(fill="x", pady=12)
        ttk.Button(bf, text="Apply Settings", style="Btn.TButton",
                   command=self._apply_settings).pack(side="left")
        ttk.Label(bf, textvariable=self._s_status, foreground="#3fb950",
                  background="#0d1117", font=("Consolas", 9)).pack(side="left", padx=12)

        ttk.Label(settings_panel,
                  text="Changes apply to new trades only. Open positions keep their original settings.",
                  foreground="#8b949e", background="#0d1117",
                  font=("Consolas", 8)).pack(anchor="w")

        schedule_panel = tk.Frame(body, bg="#0d1117")
        schedule_panel.pack(side="left", fill="both", expand=True, padx=(28, 0), anchor="n")
        ttk.Label(schedule_panel, text="12-MONTH CONTRIBUTION PLAN", style="Header.TLabel",
                  background="#0d1117").pack(anchor="w", pady=(0, 4))

        self._contrib_summary_var = tk.StringVar(value="")
        ttk.Label(schedule_panel, textvariable=self._contrib_summary_var,
                  foreground="#8b949e", background="#0d1117",
                  font=("Consolas", 9)).pack(anchor="w", pady=(0, 6))

        contrib_cols = ("month", "due", "amount", "status")
        self.contrib_tree = ttk.Treeview(
            schedule_panel, columns=contrib_cols, show="headings", height=12,
        )
        for col, heading, width in [
            ("month", "MONTH", 80),
            ("due", "DUE", 90),
            ("amount", "AMOUNT", 80),
            ("status", "STATUS", 90),
        ]:
            self.contrib_tree.heading(col, text=heading)
            self.contrib_tree.column(col, width=width, anchor="center")
        self.contrib_tree.tag_configure("paid", foreground="#3fb950")
        self.contrib_tree.tag_configure("due", foreground="#e3b341")
        self.contrib_tree.tag_configure("scheduled", foreground="#8b949e")
        self.contrib_tree.pack(fill="x", anchor="n")
        self._update_contribution_schedule()

    def _apply_settings(self):
        try:
            capital   = float(self._s_capital.get())
            leverage  = int(self._s_leverage.get())
            tp_pct    = float(self._s_tp.get()) / 100
            sl_on     = self._s_sl_enabled.get()
            sl_pct    = float(self._s_sl.get()) / 100
            hold_days = int(self._s_hold.get())
            per_trade = float(self._s_per_trade.get()) / 100
            monthly_contribution = float(self._s_monthly_contribution.get())
            monthly_day = int(self._s_monthly_day.get())
            auto_start = self._s_auto_start.get()
        except ValueError as e:
            self._s_status.set(f"Error: {e}")
            return

        if capital <= 0:
            self._s_status.set("Error: capital must be positive")
            return
        leverage = max(1, min(leverage, config.MAX_LEVERAGE))
        self._s_leverage.set(leverage)
        if monthly_contribution < 0:
            self._s_status.set("Error: monthly contribution cannot be negative")
            return
        if not 1 <= monthly_day <= 31:
            self._s_status.set("Error: contribution day must be 1-31")
            return
        try:
            capital_cash_delta = self.trader.starting_capital_delta(capital)
            if self.trader.cash_balance + capital_cash_delta < -0.005:
                self._s_status.set(
                    f"Error: capital decrease needs ${abs(capital_cash_delta):,.2f} free cash; "
                    f"available cash is ${self.trader.cash_balance:,.2f}."
                )
                return
        except ValueError as e:
            self._s_status.set(f"Error: {e}")
            return

        # Save to .env
        self._write_env({
            "CAPITAL_USD":        capital,
            "LEVERAGE":           leverage,
            "FUTURES_NET_TP_PCT": tp_pct,
            "FUTURES_NET_SL_PCT": sl_pct,
            "FUTURES_USE_SL":     "true" if sl_on else "false",
            "MAX_HOLD_DAYS":      hold_days,
            "PER_TRADE_PCT":      per_trade,
            "MONTHLY_CONTRIBUTION_USD": monthly_contribution,
            "MONTHLY_CONTRIBUTION_DAY": monthly_day,
            "AUTO_START_FUTURES": "true" if auto_start else "false",
            "SETTINGS_CONFIRMED": "true",
        })

        # Hot-apply to config (new trades pick these up immediately)
        config.CAPITAL_USD        = capital
        config.LEVERAGE           = leverage
        config.FUTURES_NET_TP_PCT = tp_pct
        config.FUTURES_NET_SL_PCT = sl_pct
        config.FUTURES_USE_SL     = sl_on
        config.MAX_HOLD_DAYS      = hold_days
        config.PER_TRADE_PCT      = per_trade
        config.MONTHLY_CONTRIBUTION_USD = monthly_contribution
        config.MONTHLY_CONTRIBUTION_DAY = monthly_day
        config.AUTO_START_FUTURES = auto_start
        config.SETTINGS_CONFIRMED = True
        self._settings_confirmed = True
        applied_capital_delta = self.trader.sync_starting_capital(capital)
        self.trader.leverage      = leverage
        self.engine_var.set(self._new_trade_setting_text())

        capital_note = ""
        if abs(applied_capital_delta) >= 0.01:
            sign = "+" if applied_capital_delta > 0 else "-"
            capital_note = f"  |  Cash {sign}${abs(applied_capital_delta):,.2f}"
        self._s_status.set(
            f"Applied!  Leverage: {leverage}x  |  TP: {tp_pct*100:.2f}%  |  "
            f"SL: {'ON' if sl_on else 'OFF'}  |  Add ${monthly_contribution:.2f}/mo"
            f"{capital_note}")
        self._refresh_summary()
        self._update_contribution_schedule()

    @staticmethod
    def _new_trade_setting_text() -> str:
        return f"   NEW TRADES: FUTURES {config.LEVERAGE}x"

    @staticmethod
    def _num(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _write_env(updates: dict) -> None:
        env_path = config.PROJECT_ROOT / ".env"
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        written = set()
        new_lines = []
        for line in lines:
            if "=" in line and not line.strip().startswith("#"):
                key = line.split("=")[0].strip()
                if key in updates:
                    new_lines.append(f"{key}={updates[key]}")
                    written.add(key)
                    continue
            new_lines.append(line)
        for key, val in updates.items():
            if key not in written:
                new_lines.append(f"{key}={val}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    def _on_tab_changed(self, event):
        nb  = event.widget
        tab = nb.tab(nb.select(), "text").strip()
        if tab == "Charts":
            self._draw_charts()
        elif tab == "History":
            self._refresh_history()
        elif tab == "P2P Arb" and self._p2p_last_snapshot is None:
            self._refresh_p2p()

    # ── Button handlers ────────────────────────────────────────────────────────

    def _require_settings_confirmation(self, message):
        self.notebook.select(self.settings_tab)
        self.status_var.set(message)

    def _on_run_now(self):
        if not self._settings_confirmed:
            self._require_settings_confirmation("Review Settings and click Apply Settings before running futures.")
            return
        if self._paused:
            self._paused = False
            self.pause_btn.config(text="Pause")
        self._force_event.set()
        self.status_var.set("Running cycle now...")

    def _on_pause_resume(self):
        if not self._settings_confirmed:
            self._require_settings_confirmation("Review Settings and click Apply Settings before starting futures.")
            return
        self._paused = not self._paused
        if self._paused:
            self.pause_btn.config(text="Resume")
            self.status_var.set("Paused")
        else:
            self.pause_btn.config(text="Pause")
            self._force_event.set()
            self.status_var.set("Resumed")

    # ── Trading loop ───────────────────────────────────────────────────────────

    def _start_trading_loop(self):
        self._cycle_thread = threading.Thread(target=self._trading_loop, daemon=True)
        self._cycle_thread.start()

    def _trading_loop(self):
        from bot.modules import telegram_notifier as tg
        now = datetime.now(timezone.utc)
        if self._settings_confirmed and self.strategy.should_refresh_basket(now):
            try:
                self.strategy.refresh_basket(now)
            except Exception:
                logger.exception("Failed to refresh basket")

        scan_interval    = config.POSITION_CHECK_MINS * 60
        last_summary_day = now.day  # send daily summary once per day

        while self._running:
            if self._paused:
                time.sleep(1)
                continue

            now_ts = time.time()
            force  = self._force_event.is_set()
            if force:
                self._force_event.clear()

            # Full cycle every 5 min: check positions + scan dips + fill empty slots
            try:
                summary = self.strategy.run_cycle()
                self._last_cycle_time    = datetime.now(timezone.utc)
                self._last_cycle_summary = summary
                self._next_cycle_ts      = now_ts + scan_interval
            except Exception:
                logger.exception("Error in trading cycle")

            # Record equity snapshot
            try:
                pv = self.trader.get_portfolio_value()
                with self._equity_lock:
                    self._equity_history.append((datetime.now(timezone.utc), pv))
                    if len(self._equity_history) > 5000:
                        self._equity_history = self._equity_history[-5000:]
            except Exception:
                pass

            # Daily summary alert at midnight UTC
            try:
                today = datetime.now(timezone.utc).day
                if today != last_summary_day:
                    last_summary_day = today
                    stats = self.trader.get_stats()
                    tg.alert_summary(
                        portfolio=self.trader.get_portfolio_value(),
                        cash=self.trader.cash_balance,
                        open_pos=len(list(self.trader.get_open_positions())),
                        total_trades=stats.get("total_trades", 0),
                        win_rate=stats.get("win_rate", 0),
                        total_pnl_usd=sum(
                            p.pnl_usd for p in self.trader.get_trade_history()
                        ),
                    )
            except Exception:
                pass

            self._force_event.wait(timeout=scan_interval)

    # ── UI refresh ─────────────────────────────────────────────────────────────

    def _schedule_refresh(self):
        if not self._running:
            return
        self._refresh_ui()
        self.root.after(self.REFRESH_MS, self._schedule_refresh)

    def _refresh_ui(self):
        try:
            self._update_clock()
            self._update_status()
            self._update_stats()
            self._update_positions()
            self._update_closed()
            self._update_basket()
            self._update_contribution_schedule()
            self._maybe_refresh_p2p_tab()
        except Exception:
            logger.debug("Dashboard refresh error", exc_info=True)

    def _maybe_refresh_p2p_tab(self):
        if self._p2p_refreshing:
            return
        if not hasattr(self, "notebook") or not hasattr(self, "_p2p_status_var"):
            return
        try:
            tab = self.notebook.tab(self.notebook.select(), "text").strip()
        except tk.TclError:
            return
        if tab == "P2P Arb" and time.time() >= self._p2p_next_refresh_ts:
            self._refresh_p2p()

    def _update_clock(self):
        self.clock_label.config(
            text=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))

    def _update_status(self):
        if not self._settings_confirmed:
            self.status_var.set("First run: review Settings and click Apply Settings before trading.")
            return
        if self._paused:
            self.status_var.set("Paused. Press Resume to start futures paper trading.")
            return
        if self._last_cycle_time is None:
            self.status_var.set("Waiting for first cycle...")
            return
        s         = self._last_cycle_summary
        last      = self._last_cycle_time.strftime("%H:%M:%S")
        secs_left = max(0, int(self._next_cycle_ts - time.time()))
        m, sec    = divmod(secs_left, 60)
        countdown = f"{m:02d}:{sec:02d}"
        self.status_var.set(
            f"Last: {last} UTC  |  Next scan: {countdown}  |  "
            f"Dips: {s.get('dips_found',0)}  "
            f"Opened: {s.get('positions_opened',0)}  "
            f"Filled: {s.get('slots_filled',0)}  "
            f"Closed: {s.get('positions_closed',0)}"
        )

    def _update_stats(self):
        portfolio      = self.trader.get_portfolio_value()
        cash           = self.trader.cash_balance
        stats          = self.trader.get_stats()
        initial        = (
            self.trader.get_contributed_capital()
            if hasattr(self.trader, "get_contributed_capital")
            else config.CAPITAL_USD
        )
        pnl_pct        = ((portfolio - initial) / initial) * 100 if initial > 0 else 0
        open_positions = list(self.trader.get_open_positions())  # snapshot

        self.portfolio_var.set(f"${portfolio:,.2f}")
        self.cash_var.set(f"${cash:,.2f}")
        self.pnl_var.set(f"{pnl_pct:+.2f}%")
        self.open_var.set(str(len(open_positions)))
        self.trades_var.set(str(stats.get("total_trades", 0)))
        self.winrate_var.set(f"{stats.get('win_rate', 0):.1f}%")

    def _update_positions(self):
        for item in self.pos_tree.get_children():
            self.pos_tree.delete(item)
        now            = datetime.now(timezone.utc)
        self.trader.refresh_cross_liquidation_prices()
        open_positions = list(self.trader.get_open_positions())  # snapshot — no race condition
        for pos in open_positions:
            age_h   = (now - pos.entry_time).total_seconds() / 3600
            current = pos.last_known_price or pos.entry_price
            leverage = getattr(pos, "leverage", 1)
            if current and pos.entry_price > 0:
                price_chg = (current - pos.entry_price) / pos.entry_price
                lev_pnl   = price_chg * leverage * 100
                pnl_str = f"{lev_pnl:+.2f}% ({price_chg*100:+.2f}% price)"
            else:
                pnl_str = "--"
            risk_price = self._num(getattr(pos, "liquidation_price", 0.0), 0.0)
            risk_str = f"${risk_price:.4f}" if risk_price > 0 else "No near liq"
            self.pos_tree.insert("", "end", values=(
                pos.symbol,
                f"${pos.amount_usd:.2f}",
                f"{leverage}x",
                f"${pos.entry_price:.4f}",
                f"${current:.4f}" if current else "--",
                pnl_str,
                f"{pos.entry_change_24h:+.2f}%",
                f"${pos.tp_price:.4f}",
                risk_str,
                f"{age_h:.1f}h",
            ))

    def _update_closed(self):
        for item in self.closed_tree.get_children():
            self.closed_tree.delete(item)
        closed = list(self.trader.get_trade_history())  # snapshot
        for pos in reversed(closed[-10:]):
            exit_t  = getattr(pos, "exit_time", None)
            t_str   = exit_t.strftime("%m/%d %H:%M") if exit_t else "--"
            reason  = getattr(pos, "status", "--")
            if hasattr(reason, "value"):
                reason = reason.value
            leverage = getattr(pos, "leverage", 1)
            self.closed_tree.insert("", "end", values=(
                pos.symbol,
                f"${pos.amount_usd:.2f}",
                f"{leverage}x",
                f"${pos.entry_price:.4f}",
                f"${pos.exit_price:.4f}" if pos.exit_price else "--",
                f"{pos.pnl_pct:+.2f}%",
                f"${pos.pnl_usd:+.2f}",
                f"{pos.entry_change_24h:+.2f}%",
                reason,
                t_str,
            ))

    def _update_basket(self):
        if self.strategy.basket:
            syms = [c["symbol"] for c in self.strategy.basket]
            ms   = (f" ({self.strategy.basket_year}-{self.strategy.basket_month:02d})"
                    if self.strategy.basket_month else "")
            self.basket_var.set(f"Basket{ms}: {', '.join(syms)}")
        else:
            self.basket_var.set("Basket: waiting for first cycle...")

    def _update_contribution_schedule(self):
        if not hasattr(self, "contrib_tree"):
            return

        for item in self.contrib_tree.get_children():
            self.contrib_tree.delete(item)

        rows = accounting.contribution_schedule(months=12)
        paid_count = 0
        due_count = 0
        total_planned = 0.0
        for row in rows:
            status = row["status"]
            amount = self._num(row["amount_usd"])
            paid_count += 1 if status == "paid" else 0
            due_count += 1 if status == "due" else 0
            total_planned += amount
            self.contrib_tree.insert("", "end", values=(
                row["month"],
                row["date"],
                f"${amount:,.2f}",
                status.upper(),
            ), tags=(status,))

        if rows:
            amount = self._num(rows[0]["amount_usd"])
            day = config.MONTHLY_CONTRIBUTION_DAY
            self._contrib_summary_var.set(
                f"${amount:,.2f}/month on day {day}  |  "
                f"{paid_count}/12 paid  |  {due_count} due  |  "
                f"12-mo plan ${total_planned:,.2f}"
            )

    def _on_close(self):
        self._running = False
        self._force_event.set()
        if self._telegram_commands:
            self._telegram_commands.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()
        self._running = False
        if self._telegram_commands:
            self._telegram_commands.stop()
