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
from bot.modules.futures_trader import _history_csv

logger = logging.getLogger(__name__)


class Dashboard:
    REFRESH_MS = 2000

    def __init__(self, strategy):
        self.strategy = strategy
        self.trader   = strategy.trader
        self._running      = True
        self._paused       = False
        self._force_event  = threading.Event()
        self._cycle_thread = None

        self._last_cycle_time:    datetime | None = None
        self._last_cycle_summary: dict = {}
        self._next_cycle_ts:      float = 0.0

        # Equity history: list of (datetime, portfolio_value)
        self._equity_history: list[tuple[datetime, float]] = []
        self._equity_lock = threading.Lock()

        self.root = tk.Tk()
        self.root.title("TradingBot23")
        self.root.geometry("940x720")
        self.root.minsize(800, 580)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
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
        self.pause_btn = ttk.Button(ctrl, text="Pause",      style="Btn.TButton", command=self._on_pause_resume)
        self.run_btn.pack(side="left", padx=(0, 6))
        self.pause_btn.pack(side="left", padx=(0, 12))

        self.status_var = tk.StringVar(value="Starting up...")
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

        # ── Notebook (Live | Charts | History | Settings) ──
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=0, pady=0)

        open_tab     = tk.Frame(nb, bg="#0d1117")
        charts_tab   = tk.Frame(nb, bg="#0d1117")
        history_tab  = tk.Frame(nb, bg="#0d1117")
        settings_tab = tk.Frame(nb, bg="#0d1117")
        nb.add(open_tab,     text="  Open  ")
        nb.add(charts_tab,   text="  Charts  ")
        nb.add(history_tab,  text="  History  ")
        nb.add(settings_tab, text="  Settings  ")
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_open_tab(open_tab)
        self._build_charts_tab(charts_tab)
        self._build_history_tab(history_tab)
        self._build_settings_tab(settings_tab)

        # ── Bottom bar ──
        basket_frame = tk.Frame(self.root, bg="#161b22", padx=15, pady=5)
        basket_frame.pack(fill="x", side="bottom")
        self.basket_var = tk.StringVar(value="Basket: loading...")
        ttk.Label(basket_frame, textvariable=self.basket_var, foreground="#8b949e",
                  background="#161b22", font=("Consolas", 9)).pack(side="left", anchor="w")
        ttk.Label(basket_frame, text="FutolTech  |  Futol Ethical Technology Ecosystems",
                  foreground="#388bfd", background="#161b22",
                  font=("Consolas", 8, "bold")).pack(side="right", anchor="e")

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
        risk_heading = "LIQ"
        for col, heading, width in [
            ("symbol","SYMBOL",70),("amount","AMOUNT $",85),("lev","ENTRY LEV",70),
            ("entry","ENTRY",90),("current","CURRENT",90),
            ("pnl",pnl_heading,160),("trigger","24H TRIGGER",90),
            ("tp","TP",90),("sl",risk_heading,90),("age","AGE",55),
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

        def row(label, widget_factory, r):
            ttk.Label(grid, text=label, foreground="#8b949e", background="#0d1117",
                      font=("Consolas", 9), width=22).grid(row=r, column=0, sticky="w", pady=4)
            w = widget_factory(grid)
            w.grid(row=r, column=1, sticky="w", padx=8, pady=4)
            return w

        # Capital
        self._s_capital = tk.StringVar(value=str(int(config.CAPITAL_USD)))
        row("Capital (USD)", lambda p: ttk.Entry(p, textvariable=self._s_capital, width=10,
            font=("Consolas",10)), 0)

        # Leverage
        self._s_leverage = tk.IntVar(value=config.LEVERAGE)
        lf = tk.Frame(grid, bg="#0d1117")
        lf.grid(row=1, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(grid, text="Leverage", foreground="#8b949e", background="#0d1117",
                  font=("Consolas", 9), width=22).grid(row=1, column=0, sticky="w", pady=4)
        for lv in range(1, 6):
            ttk.Radiobutton(lf, text=f"{lv}x", variable=self._s_leverage, value=lv).pack(side="left", padx=4)

        # TP %
        self._s_tp = tk.StringVar(value=str(round(config.FUTURES_NET_TP_PCT * 100, 2)))
        row("TP target (% net)", lambda p: ttk.Entry(p, textvariable=self._s_tp, width=8,
            font=("Consolas",10)), 2)

        # SL enable + %
        self._s_sl_enabled = tk.BooleanVar(value=config.FUTURES_USE_SL)
        self._s_sl = tk.StringVar(value=str(round(config.FUTURES_NET_SL_PCT * 100, 2)))
        sl_frame = tk.Frame(grid, bg="#0d1117")
        sl_frame.grid(row=3, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(grid, text="Stop Loss", foreground="#8b949e", background="#0d1117",
                  font=("Consolas", 9), width=22).grid(row=3, column=0, sticky="w", pady=4)
        ttk.Checkbutton(sl_frame, text="Enable", variable=self._s_sl_enabled).pack(side="left")
        ttk.Entry(sl_frame, textvariable=self._s_sl, width=8,
                  font=("Consolas",10)).pack(side="left", padx=8)
        ttk.Label(sl_frame, text="% net", foreground="#8b949e", background="#0d1117",
                  font=("Consolas",9)).pack(side="left")

        # Max hold days
        self._s_hold = tk.StringVar(value=str(config.MAX_HOLD_DAYS))
        row("Max hold (days)", lambda p: ttk.Entry(p, textvariable=self._s_hold, width=8,
            font=("Consolas",10)), 4)

        # Per trade %
        self._s_per_trade = tk.StringVar(value=str(round(config.PER_TRADE_PCT * 100, 0)))
        row("Per trade (% of portfolio)", lambda p: ttk.Entry(p, textvariable=self._s_per_trade, width=8,
            font=("Consolas",10)), 5)

        # Monthly contribution
        self._s_monthly_contribution = tk.StringVar(value=str(round(config.MONTHLY_CONTRIBUTION_USD, 2)))
        row("Monthly contribution ($)", lambda p: ttk.Entry(
            p, textvariable=self._s_monthly_contribution, width=8, font=("Consolas",10)), 6)

        self._s_monthly_day = tk.StringVar(value=str(config.MONTHLY_CONTRIBUTION_DAY))
        row("Contribution day", lambda p: ttk.Entry(
            p, textvariable=self._s_monthly_day, width=8, font=("Consolas",10)), 7)

        # Apply button
        self._s_status = tk.StringVar(value="")
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
        except ValueError as e:
            self._s_status.set(f"Error: {e}")
            return

        leverage = max(1, min(leverage, config.MAX_LEVERAGE))
        if monthly_contribution < 0:
            self._s_status.set("Error: monthly contribution cannot be negative")
            return
        if not 1 <= monthly_day <= 31:
            self._s_status.set("Error: contribution day must be 1-31")
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
        self.trader.leverage      = leverage
        self.engine_var.set(self._new_trade_setting_text())

        self._s_status.set(
            f"Applied!  Leverage: {leverage}x  |  TP: {tp_pct*100:.2f}%  |  "
            f"SL: {'ON' if sl_on else 'OFF'}  |  Add ${monthly_contribution:.2f}/mo")
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

    # ── Button handlers ────────────────────────────────────────────────────────

    def _on_run_now(self):
        if self._paused:
            return
        self._force_event.set()
        self.status_var.set("Running cycle now...")

    def _on_pause_resume(self):
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
        if self.strategy.should_refresh_basket(now):
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
        except Exception:
            logger.debug("Dashboard refresh error", exc_info=True)

    def _update_clock(self):
        self.clock_label.config(
            text=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))

    def _update_status(self):
        if self._paused:
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
            risk_price = getattr(pos, "liquidation_price", None)
            if not risk_price:
                risk_price = getattr(pos, "sl_price", None)
            risk_str = f"${risk_price:.4f}" if risk_price else "--"
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
        self.root.destroy()

    def run(self):
        self.root.mainloop()
        self._running = False
