"""Live dashboard for TradingBot23."""

import logging
import threading
import time
import tkinter as tk
from collections import defaultdict
from datetime import datetime, timezone
from tkinter import messagebox, ttk

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import csv as _csv

from bot import config
import rebound_duration_analysis as rebound_analysis
from bot.modules import accounting
from bot.modules import ohverlay_notifier as ov
from bot.modules import telegram_notifier as tg
from bot.modules.event_ledger import EventLedger, append_event
from bot.modules.futures_trader import _history_csv, recent_reset_sessions
from bot.modules.p2p_arbitrage import (
    P2PDepthSweep,
    P2PJournal,
    P2PPaperArb,
    P2PRealismSettings,
    P2PRoute,
    P2PRouteSettings,
    apply_realism_to_sweep,
    build_depth_sweep,
    build_p2p_routes,
    build_p2p_hold_entry,
)
from bot.modules.p2p_monitor import P2PMonitor, P2PSnapshot

logger = logging.getLogger(__name__)


class ToolTip:
    """Small hover tooltip for tkinter/ttk controls."""

    def __init__(self, widget, text: str, delay_ms: int = 550, wraplength: int = 420):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.wraplength = wraplength
        self._after_id = None
        self._window = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self):
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _show(self):
        self._after_id = None
        if self._window or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 18
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        except tk.TclError:
            return

        self._window = tk.Toplevel(self.widget)
        self._window.wm_overrideredirect(True)
        self._window.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self._window,
            text=self.text,
            justify="left",
            bg="#21262d",
            fg="#f0f6fc",
            relief="solid",
            bd=1,
            padx=9,
            pady=7,
            wraplength=self.wraplength,
            font=("Consolas", 9),
        )
        label.pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._window:
            try:
                self._window.destroy()
            except tk.TclError:
                pass
            self._window = None


SETTINGS_HELP = {
    "Capital (USD)": "Starting paper capital for futures. Increasing adds free paper cash. Decreasing withdraws from free cash only; open trades keep their original margin.",
    "Leverage": "Leverage used for new futures paper trades only. Higher leverage magnifies both profit and drawdown. Existing trades keep their entry leverage.",
    "TP target (% net)": "Net take-profit target on margin after estimated fees and funding. The app calculates the required price move for each new trade.",
    "Stop Loss": "Optional hard stop-loss. When disabled, positions rely on TP, max-hold expiry, and cross-margin liquidation tracking.",
    "Crash protection": "Block entries pauses new futures longs when BTC is dumping. Emergency SL force-closes existing positions during crash mode, so use it only when you intentionally want an anti-crash stop.",
    "BTC filter (% 1h)": "Market-regime gate for long entries. Example: -1.5 blocks new trades when BTC is down 1.5% or worse over roughly 1 hour. Blank or 'none' disables it.",
    "Max hold (days)": "Maximum age of a futures paper position before it is closed at market in the simulation.",
    "Per trade (% of portfolio)": "Margin size for each new trade as a percent of current paper portfolio value. This is margin, not leveraged notional.",
    "Max open trades": "Maximum simultaneous futures paper positions. Lowering this does not force-close existing trades; it only blocks new entries until open count drops.",
    "Pre-trade wave check": "Before opening a futures trade, inspect the coin's recent 15m candles to avoid entries still making fresh lows.",
    "Min pre-trade score": "Minimum 0-100 wave score required before entry. Higher is stricter and opens fewer trades.",
    "Monthly contribution ($)": "Paper cash to add once per month for contribution/compounding simulation.",
    "Contribution day": "Calendar day of month when the paper contribution is applied. If the month is shorter, the app uses the last valid day.",
    "Auto-start futures": "When enabled, futures scanning resumes automatically on app launch after settings are confirmed.",
    "Ohverlay alerts": "Send short local bubble notifications to Ohverlay v4 via localhost webhook. Requires Ohverlay's Webhook Server to be enabled.",
}

RISK_HELP = {
    "Profile": "Local data profile name. Different profiles use separate data folders after restart, useful for beta testers.",
    "Max daily loss $": "Risk guard. Zero disables it. If today's closed futures P&L falls below this loss, new entries are blocked.",
    "Max open notional $": "Risk guard. Zero disables it. Blocks new entries once leveraged open notional reaches this dollar limit.",
    "Max loss streak": "Risk guard. Zero disables it. Blocks new entries after this many consecutive losing closed futures trades.",
    "P2P max route PHP": "Per-cycle cap for P2P paper/live-assist route sizing. Zero means use the capital value from the P2P tab.",
    "P2P delay min": "Assumed manual P2P settlement time. It adds a small realism haircut and also acts as Auto Cycle cooldown.",
    "P2P cancel %": "Simulated failed/cancelled P2P order rate. It deducts a small expected cost from paper route profit.",
    "P2P decay %": "Spread-fade haircut. Example: 0.05% on 500,000 PHP deducts 250 PHP from expected route profit.",
}

P2P_FIELD_HELP = {
    "Capital PHP": "Paper capital or route size basis for USDT/PHP P2P calculations. Paper mode compounds realized profit into this account.",
    "Min net %": "Minimum net profit percent required after buffer, transfer fee, delay, cancel, and decay haircuts.",
    "Xfer fee USDT": "Simulated crypto transfer/network fee when buying and selling across different marketplaces.",
    "Buffer PHP": "Fixed peso safety cost deducted from every route. Use this for hidden fees, payment friction, or manual execution error.",
    "Delay min": "Assumed time to complete the manual P2P cycle. It adds realism drag and sets Auto Cycle cooldown.",
    "Cancel %": "Expected cancelled/failed order rate. Higher values make paper results more conservative.",
    "Decay %": "Spread-fade haircut. It models the chance that the buy/sell spread shrinks while you execute manually.",
}

P2P_SUMMARY_HELP = {
    "BEST BUY USDT": "Lowest seller price available to buy USDT using PHP from the loaded P2P listings.",
    "BEST SELL USDT": "Highest buyer price available to sell USDT for PHP from the loaded P2P listings.",
    "TOP NET PROFIT": "Best single route profit after configured costs and realism haircuts.",
    "DEPTH SWEEP": "Estimated result if the app tries to use the full configured capital across available order depth.",
    "PAPER BAL": "Total P2P paper equity: free PHP cash plus any open hold inventory at cost.",
    "CYCLES": "Completed paper buy+sell cycles and realized PHP profit from those cycles.",
    "HOLD USDT": "Paper USDT inventory currently held from Paper Buy Hold.",
    "HOLD P&L": "Estimated mark-to-market profit/loss if the held USDT were sold into current buyer listings.",
}


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
        self.event_ledger = EventLedger()
        self._p2p_refreshing = False
        self._p2p_last_snapshot: P2PSnapshot | None = None
        self._p2p_routes: list[P2PRoute] = []
        self._p2p_sweep: P2PDepthSweep | None = None
        self._p2p_next_refresh_ts = 0.0
        self._p2p_settings_cache = P2PRouteSettings()
        self._p2p_last_alert_key = ""
        self._p2p_last_alert_ts = 0.0
        self._p2p_last_logged_route_key = ""
        self._p2p_last_auto_cycle_key = ""
        self._p2p_last_auto_cycle_ts = 0.0
        self._p2p_last_saved_controls: dict[str, str] = {}
        self._telegram_commands = None
        self._risk_kill_switch = False
        self._decision_log: list[dict[str, str]] = []
        self._rebound_study_running = False
        self._risk_panes = None

        logger.info(
            "Dashboard startup | auto_start=%s settings_confirmed=%s paused=%s data_dir=%s",
            config.AUTO_START_FUTURES,
            self._settings_confirmed,
            self._paused,
            config.DATA_DIR,
        )

        # Equity history: list of (datetime, portfolio_value)
        self._equity_history: list[tuple[datetime, float]] = []
        self._equity_lock = threading.Lock()

        self.root = tk.Tk()
        self.root.title("TradingBot23")
        self.root.geometry("1280x840")
        self.root.minsize(1100, 720)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._start_telegram_dashboard()
        self._start_trading_loop()
        self._schedule_refresh()

    # ── UI construction ────────────────────────────────────────────────────────

    def _tip(self, widget, text: str):
        tip = ToolTip(widget, text)
        try:
            widget._tradingbot_tooltip = tip
        except Exception:
            pass
        return widget

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
        self._tip(self.run_btn, "Run one futures scan immediately: check exits first, then scan for eligible entries if risk guards allow.")
        self._tip(self.pause_btn, "Pause blocks new futures cycles. Resume allows the loop to scan again if Settings and Risk checks pass.")
        tg_menu_btn = ttk.Button(ctrl, text="Telegram Menu", style="Btn.TButton",
                                 command=self._send_telegram_menu)
        tg_menu_btn.pack(side="left", padx=(0, 12))
        self._tip(tg_menu_btn, "Send the Telegram dashboard/menu buttons to your configured bot chat.")

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
        stats_help = {
            "PORTFOLIO": "Estimated futures paper equity: free cash plus current marked value of open positions.",
            "CASH": "Free paper cash/margin available for new futures trades.",
            "PNL": "Portfolio performance versus contributed futures paper capital, including unrealized open P&L.",
            "OPEN": "Number of currently open futures paper positions.",
            "TRADES": "Closed futures paper trades in the active session/history.",
            "WIN RATE": "Percent of closed futures trades with positive net P&L. Open losing trades are not counted until closed.",
        }

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
            label = ttk.Label(card, text=label_text, foreground="#8b949e", background="#161b22",
                              font=("Consolas", 8))
            label.pack(anchor="w")
            value = ttk.Label(card, textvariable=var, style=sty, background="#161b22")
            value.pack(anchor="w")
            help_text = stats_help.get(label_text, "")
            self._tip(card, help_text)
            self._tip(label, help_text)
            self._tip(value, help_text)

        # ── Notebook ──
        nb = ttk.Notebook(self.root)
        self.notebook = nb
        nb.pack(fill="both", expand=True, padx=0, pady=0)

        open_tab     = tk.Frame(nb, bg="#0d1117")
        risk_tab     = tk.Frame(nb, bg="#0d1117")
        charts_tab   = tk.Frame(nb, bg="#0d1117")
        history_tab  = tk.Frame(nb, bg="#0d1117")
        p2p_tab      = tk.Frame(nb, bg="#0d1117")
        p2p_history_tab = tk.Frame(nb, bg="#0d1117")
        ledger_tab   = tk.Frame(nb, bg="#0d1117")
        settings_tab = tk.Frame(nb, bg="#0d1117")
        self.settings_tab = settings_tab
        nb.add(open_tab,     text="  Open  ")
        nb.add(risk_tab,     text="  Risk  ")
        nb.add(charts_tab,   text="  Charts  ")
        nb.add(history_tab,  text="  History  ")
        nb.add(p2p_tab,      text="  P2P Arb  ")
        nb.add(p2p_history_tab, text="  P2P History  ")
        nb.add(ledger_tab,   text="  Ledger  ")
        nb.add(settings_tab, text="  Settings  ")
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_open_tab(open_tab)
        self._build_risk_tab(risk_tab)
        self._build_charts_tab(charts_tab)
        self._build_history_tab(history_tab)
        self._build_p2p_tab(p2p_tab)
        self._build_p2p_history_tab(p2p_history_tab)
        self._build_ledger_tab(ledger_tab)
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
            control_callback=self._telegram_control,
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

        scan_settings = self._p2p_capped_route_settings(settings)
        routes = build_p2p_routes([snapshot], settings=scan_settings)
        raw_sweep = build_depth_sweep([snapshot], settings=scan_settings)
        sweep = self._apply_p2p_realism(raw_sweep) if raw_sweep else None
        state = self.p2p_paper.state(settings.capital_php)
        return self._format_p2p_dashboard_text(snapshot, routes, sweep, state, settings) + stale_note

    def _telegram_info_text(self) -> str:
        return (
            "<b>TradingBot23 Live Assist Scope</b>\n"
            "Futures: paper portfolio, open positions, trade stats, and daily summary alerts.\n"
            "P2P Paper Sim: uses live Binance listings, updates paper ledger, and can send Telegram paper-event alerts.\n"
            "P2P Live Assist: scan, spread score, Telegram alert, and watchlist log only.\n\n"
            "Manual in real P2P: choose counterparty, send fiat, confirm payment, verify receipt, release crypto, and handle disputes.\n"
            "Commands: /dashboard, /p2p, /today, /export, /info"
            + ("\nRemote pause/resume is enabled." if config.TELEGRAM_ALLOW_CONTROL else "\nRemote pause/resume is disabled by default.")
        )

    def _telegram_control(self, action: str) -> str:
        action = (action or "").lower()
        if action == "today":
            return self._telegram_today_text()
        if action in {"pause", "resume"} and not config.TELEGRAM_ALLOW_CONTROL:
            self._record_decision(
                "telegram",
                action,
                "BLOCK",
                0,
                "remote futures controls disabled",
            )
            return (
                "<b>Remote futures controls are disabled.</b>\n"
                "Telegram can still show dashboard, P2P, today, info, and export snapshots."
            )
        if action == "pause":
            self._paused = True
            self._record_decision("telegram", "pause", "BLOCK", 0, "pause requested from Telegram")
            try:
                self.root.after(0, lambda: self.pause_btn.config(text="Resume"))
            except tk.TclError:
                pass
            return "⏸️ <b>TradingBot23 paused.</b>\nExisting paper positions remain in the local ledger."
        if action == "resume":
            allowed, reasons = self._risk_allows_trading()
            if not allowed:
                self._paused = False
                self._force_event.set()
                self._record_decision("telegram", "resume", "BLOCK", 0, "; ".join(reasons))
                try:
                    self.root.after(0, lambda: self.pause_btn.config(text="Pause"))
                except tk.TclError:
                    pass
                return "⛔ <b>Resume blocked by risk guard.</b>\n" + tg.escape_html("; ".join(reasons))
            self._paused = False
            self._force_event.set()
            self._record_decision("telegram", "resume", "RUN", 100, "resume requested from Telegram")
            try:
                self.root.after(0, lambda: self.pause_btn.config(text="Pause"))
            except tk.TclError:
                pass
            return "▶️ <b>TradingBot23 resumed.</b>\nA futures paper cycle was requested."
        if action == "export":
            out = self._write_ops_report()
            self._record_decision("telegram", "export", "UPDATED", 80, out.name)
            return f"📄 <b>Operations report exported.</b>\n{tg.escape_html(str(out))}"
        return "Unknown control action."

    def _telegram_today_text(self) -> str:
        snap = self._risk_snapshot()
        p2p_state = self.p2p_paper.state(self._p2p_starting_capital_php())
        allowed, reasons = self._risk_allows_trading()
        return "\n".join([
            "📅 <b>TradingBot23 Today</b>",
            f"Risk: <b>{'OK' if allowed else 'BLOCKED'}</b>",
            f"Futures daily closed P&L: <b>${snap['daily_pnl']:+,.2f}</b>",
            f"Portfolio: <b>${snap['portfolio']:,.2f}</b> | Cash: <b>${snap['cash']:,.2f}</b>",
            f"Open futures notional: <b>${snap['open_notional']:,.2f}</b>",
            f"P2P paper: <b>{self._num(p2p_state.get('balance_php')):,.0f} PHP</b> "
            f"({self._num(p2p_state.get('realized_profit_php')):+,.0f} realized)",
            f"Risk note: {tg.escape_html('; '.join(reasons) if reasons else 'limits clear')}",
        ])

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

    # ── Professional risk cockpit ─────────────────────────────────────────────

    def _build_risk_tab(self, parent):
        body = tk.Frame(parent, bg="#0d1117", padx=15, pady=12)
        body.pack(fill="both", expand=True)

        top = tk.Frame(body, bg="#0d1117")
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="PROFESSIONAL CONTROL CENTER", style="Header.TLabel",
                  background="#0d1117").pack(side="left")
        risk_refresh_btn = ttk.Button(top, text="Refresh", style="Btn.TButton",
                                      command=self._refresh_risk_tab)
        risk_refresh_btn.pack(side="right")
        self._tip(risk_refresh_btn, "Refresh the risk metrics, control state, and decision log.")
        export_btn = ttk.Button(top, text="Export Ops Report", style="Btn.TButton",
                                command=self._export_ops_report)
        export_btn.pack(side="right", padx=(0, 6))
        self._tip(export_btn, "Write a local operations report with futures, P2P, risk, and decision-log snapshots.")
        rebound_btn = ttk.Button(top, text="Run 5Y Rebound", style="Btn.TButton",
                                 command=self._run_rebound_study)
        rebound_btn.pack(side="right", padx=(0, 6))
        self._tip(rebound_btn, "Run the historical rebound-duration study for the top-50 universe in the background.")

        self._risk_summary_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self._risk_summary_var,
                  foreground="#8b949e", background="#0d1117",
                  font=("Consolas", 9)).pack(fill="x", anchor="w", pady=(0, 8))

        controls = tk.Frame(body, bg="#0d1117")
        controls.pack(fill="x", pady=(0, 10))

        self._risk_profile = tk.StringVar(value=config.BOT_PROFILE)
        self._risk_max_daily_loss = tk.StringVar(value=str(config.RISK_MAX_DAILY_LOSS_USD))
        self._risk_max_exposure = tk.StringVar(value=str(config.RISK_MAX_OPEN_EXPOSURE_USD))
        self._risk_max_loss_streak = tk.StringVar(value=str(config.RISK_MAX_LOSS_STREAK))
        self._risk_p2p_max_route = tk.StringVar(value=str(config.P2P_MAX_ROUTE_PHP))
        self._p2p_delay_mins = tk.StringVar(value=str(config.P2P_SETTLEMENT_DELAY_MINS))
        self._p2p_cancel_rate = tk.StringVar(value=str(config.P2P_CANCEL_RATE_PCT))
        self._p2p_spread_decay = tk.StringVar(value=str(config.P2P_SPREAD_DECAY_PCT))

        def entry(parent_frame, var, width):
            return tk.Entry(
                parent_frame,
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
            )

        for label_text, var, width in [
            ("Profile", self._risk_profile, 12),
            ("Max daily loss $", self._risk_max_daily_loss, 8),
            ("Max open notional $", self._risk_max_exposure, 9),
            ("Max loss streak", self._risk_max_loss_streak, 5),
            ("P2P max route PHP", self._risk_p2p_max_route, 9),
            ("P2P delay min", self._p2p_delay_mins, 6),
            ("P2P cancel %", self._p2p_cancel_rate, 6),
            ("P2P decay %", self._p2p_spread_decay, 6),
        ]:
            group = tk.Frame(controls, bg="#0d1117")
            group.pack(side="left", padx=(0, 10))
            label = ttk.Label(group, text=label_text, foreground="#8b949e", background="#0d1117",
                              font=("Consolas", 8))
            label.pack(anchor="w")
            input_widget = entry(group, var, width)
            input_widget.pack(anchor="w")
            help_text = RISK_HELP.get(label_text, "")
            self._tip(label, help_text)
            self._tip(input_widget, help_text)

        btns = tk.Frame(body, bg="#0d1117")
        btns.pack(fill="x", pady=(0, 10))
        apply_risk_btn = ttk.Button(btns, text="Apply Risk", style="Btn.TButton",
                                    command=self._apply_risk_settings)
        apply_risk_btn.pack(side="left", padx=(0, 6))
        self._tip(apply_risk_btn, "Save and apply risk settings. Profile changes require restart because they change the data folder.")
        kill_btn = ttk.Button(btns, text="Kill Switch", style="Btn.TButton",
                              command=self._risk_kill)
        kill_btn.pack(side="left", padx=(0, 6))
        self._tip(kill_btn, "Immediately block new futures entries. Existing positions are still monitored for exits.")
        clear_kill_btn = ttk.Button(btns, text="Clear Kill", style="Btn.TButton",
                                    command=self._risk_clear_kill)
        clear_kill_btn.pack(side="left", padx=(0, 6))
        self._tip(clear_kill_btn, "Clear the manual kill switch. Other risk guards may still block trading.")
        resume_allowed_btn = ttk.Button(btns, text="Resume If Allowed", style="Btn.TButton",
                                        command=self._risk_resume)
        resume_allowed_btn.pack(side="left", padx=(0, 6))
        self._tip(resume_allowed_btn, "Resume futures scanning only if all configured risk guards pass.")
        self._risk_action_var = tk.StringVar(value="")
        ttk.Label(btns, textvariable=self._risk_action_var,
                  foreground="#3fb950", background="#0d1117",
                  font=("Consolas", 9)).pack(side="left", padx=10)

        panes = tk.PanedWindow(body, orient=tk.VERTICAL, sashwidth=4, bg="#0d1117")
        self._risk_panes = panes
        panes.pack(fill="both", expand=True)

        risk_frame = tk.Frame(panes, bg="#0d1117")
        panes.add(risk_frame, minsize=155)
        ttk.Label(risk_frame, text="RISK, PROFILE, AND FORWARD TEST", style="Header.TLabel",
                  background="#0d1117").pack(anchor="w", pady=(0, 3))
        cols = ("metric", "value", "limit", "status")
        risk_table = tk.Frame(risk_frame, bg="#0d1117")
        risk_table.pack(fill="both", expand=True)
        self.risk_tree = ttk.Treeview(risk_table, columns=cols, show="headings", height=7)
        for col, heading, width in [
            ("metric", "METRIC", 210),
            ("value", "VALUE", 220),
            ("limit", "LIMIT", 180),
            ("status", "STATUS", 180),
        ]:
            self.risk_tree.heading(col, text=heading)
            self.risk_tree.column(col, width=width, anchor="center")
        self.risk_tree.tag_configure("ok", foreground="#3fb950")
        self.risk_tree.tag_configure("warn", foreground="#e3b341")
        self.risk_tree.tag_configure("block", foreground="#f85149")
        risk_vsb = ttk.Scrollbar(risk_table, orient="vertical", command=self.risk_tree.yview)
        self.risk_tree.configure(yscrollcommand=risk_vsb.set)
        self.risk_tree.pack(side="left", fill="both", expand=True)
        risk_vsb.pack(side="right", fill="y")

        decision_frame = tk.Frame(panes, bg="#0d1117")
        panes.add(decision_frame, minsize=115)
        ttk.Label(decision_frame, text="STRATEGY CONFIDENCE / DECISION LOG", style="Header.TLabel",
                  background="#0d1117").pack(anchor="w", pady=(8, 3))
        dcols = ("time", "domain", "action", "decision", "score", "reason")
        decision_table = tk.Frame(decision_frame, bg="#0d1117")
        decision_table.pack(fill="both", expand=True)
        self.decision_tree = ttk.Treeview(decision_table, columns=dcols, show="headings", height=5)
        for col, heading, width in [
            ("time", "TIME", 125),
            ("domain", "DOMAIN", 85),
            ("action", "ACTION", 125),
            ("decision", "DECISION", 95),
            ("score", "SCORE", 65),
            ("reason", "REASON", 520),
        ]:
            self.decision_tree.heading(col, text=heading)
            self.decision_tree.column(col, width=width, anchor="center")
        self.decision_tree.tag_configure("go", foreground="#3fb950")
        self.decision_tree.tag_configure("wait", foreground="#e3b341")
        self.decision_tree.tag_configure("block", foreground="#f85149")
        decision_vsb = ttk.Scrollbar(decision_table, orient="vertical", command=self.decision_tree.yview)
        self.decision_tree.configure(yscrollcommand=decision_vsb.set)
        self.decision_tree.pack(side="left", fill="both", expand=True)
        decision_vsb.pack(side="right", fill="y")
        self._refresh_risk_tab()
        self.root.after(150, self._fit_risk_panes)

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
            colors = {"tp_hit":"#3fb950","sl_hit":"#f85149","crash_sl":"#f0883e",
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
        ttk.Button(ctrl, text="Reset Futures Paper", style="Btn.TButton",
                   command=self._reset_futures_paper).pack(side="left", padx=(6, 0))
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

    def _futures_history_session_note(self) -> str:
        sessions = recent_reset_sessions(limit=1)
        if not sessions:
            return ""
        last = sessions[-1]
        reset_time = last.get("reset_time", "")[:16].replace("T", " ")
        archived_trades = int(self._num(last.get("archived_closed_trades")))
        archived_open = int(self._num(last.get("archived_open_positions")))
        return (
            f"  |  Current session since {reset_time} UTC"
            f"  |  archived {archived_trades} trades/{archived_open} open"
        )

    def _refresh_history(self):
        for item in self.hist_tree.get_children():
            self.hist_tree.delete(item)

        path = _history_csv()
        if not path.exists():
            note = self._futures_history_session_note()
            self._hist_summary_var.set("No current-session trade history yet." + note)
            return

        rows = []
        with open(path, "r", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))

        total = len(rows)
        wins  = sum(1 for r in rows if float(r.get("pnl_pct", 0)) > 0)
        total_pnl = sum(float(r.get("pnl_usd", 0)) for r in rows)
        wr = (wins / total * 100) if total else 0
        self._hist_summary_var.set(
            f"  {total} trades  |  Win rate: {wr:.1f}%  |  Total P&L: ${total_pnl:+.2f}"
            f"{self._futures_history_session_note()}")

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

    def _reset_futures_paper(self):
        open_count = len(self.trader.get_open_positions())
        closed_count = len(self.trader.get_trade_history())
        msg = (
            "Archive the current futures paper session and start a fresh one?\n\n"
            f"Closed trades to archive: {closed_count}\n"
            f"Open positions to archive/clear: {open_count}\n\n"
            "P2P paper data will not be changed. Futures will be paused after reset."
        )
        if not messagebox.askyesno("Reset Futures Paper", msg, parent=self.root):
            return

        self._paused = True
        self.pause_btn.config(text="Resume")
        self._force_event.clear()
        self._write_env({"AUTO_START_FUTURES": "false"})
        config.AUTO_START_FUTURES = False
        try:
            summary = self.trader.reset_paper_account(reason="dashboard_reset")
        except Exception as exc:
            logger.exception("Failed to reset futures paper account")
            self.status_var.set(f"Futures reset failed: {exc}")
            return

        with self._equity_lock:
            self._equity_history.clear()
        self._last_cycle_time = None
        self._last_cycle_summary = {}
        self._next_cycle_ts = 0.0
        self._record_decision(
            "futures",
            "paper_reset",
            "UPDATED",
            100,
            f"archived to {summary['session_id']}",
        )
        self.status_var.set(
            f"Futures paper reset. Archived to {summary['session_id']}; trading is paused."
        )
        self._refresh_history()
        self._update_stats()
        self._update_positions()
        self._update_closed()
        if hasattr(self, "ledger_tree"):
            self._refresh_event_ledger()

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

    # ── Event ledger tab ─────────────────────────────────────────────────────

    def _build_ledger_tab(self, parent):
        body = tk.Frame(parent, bg="#0d1117", padx=15, pady=12)
        body.pack(fill="both", expand=True)

        top = tk.Frame(body, bg="#0d1117")
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="UNIFIED EVENT LEDGER", style="Header.TLabel",
                  background="#0d1117").pack(side="left")
        ttk.Button(top, text="Refresh", style="Btn.TButton",
                   command=self._refresh_event_ledger).pack(side="right")

        self._ledger_summary_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self._ledger_summary_var,
                  foreground="#8b949e", background="#0d1117",
                  font=("Consolas", 9)).pack(fill="x", anchor="w", pady=(0, 8))

        cols = ("time", "domain", "type", "status", "amount", "pnl", "balance", "symbol", "details")
        self.ledger_tree = ttk.Treeview(body, columns=cols, show="headings", height=22)
        for col, heading, width in [
            ("time", "TIME", 135),
            ("domain", "DOMAIN", 80),
            ("type", "TYPE", 120),
            ("status", "STATUS", 100),
            ("amount", "AMOUNT", 110),
            ("pnl", "P&L", 95),
            ("balance", "BALANCE", 115),
            ("symbol", "SYMBOL/ROUTE", 115),
            ("details", "DETAILS", 420),
        ]:
            self.ledger_tree.heading(col, text=heading)
            self.ledger_tree.column(col, width=width, anchor="center")
        self.ledger_tree.tag_configure("profit", foreground="#3fb950")
        self.ledger_tree.tag_configure("loss", foreground="#f85149")
        self.ledger_tree.tag_configure("system", foreground="#e3b341")
        vsb = ttk.Scrollbar(body, orient="vertical", command=self.ledger_tree.yview)
        self.ledger_tree.configure(yscrollcommand=vsb.set)
        self.ledger_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._refresh_event_ledger()

    # ── P2P arbitrage tab ────────────────────────────────────────────────────

    def _build_p2p_tab(self, parent):
        body = tk.Frame(parent, bg="#0d1117", padx=15, pady=8)
        body.pack(fill="both", expand=True)

        top = tk.Frame(body, bg="#0d1117")
        top.pack(fill="x", pady=(0, 4))

        ttk.Label(top, text="USDT/PHP P2P ASSIST", style="Header.TLabel",
                  background="#0d1117").pack(side="left")
        p2p_refresh_btn = ttk.Button(top, text="Refresh Prices", style="Btn.TButton",
                                     command=self._refresh_p2p)
        p2p_refresh_btn.pack(side="right")
        self._tip(p2p_refresh_btn, "Reload live Binance P2P USDT/PHP listings and recalculate route estimates.")
        p2p_guide_btn = ttk.Button(top, text="P2P Guide", style="Btn.TButton",
                                   command=self._show_p2p_guide)
        p2p_guide_btn.pack(side="right", padx=(0, 6))
        self._tip(p2p_guide_btn, "Open a longer explanation of P2P modes, buttons, and automation scope.")

        controls = tk.Frame(body, bg="#0d1117")
        controls.pack(fill="x", pady=(0, 4))

        saved_p2p_state = self.p2p_paper.state()
        saved_p2p_capital = self._num(
            saved_p2p_state.get("starting_php"),
            config.P2P_DEFAULT_CAPITAL_PHP,
        )
        self._p2p_capital_php = tk.StringVar(value=f"{saved_p2p_capital:.0f}")
        self._p2p_min_profit_pct = tk.StringVar(value=str(config.P2P_MIN_NET_PCT))
        self._p2p_transfer_fee_usdt = tk.StringVar(value=str(config.P2P_TRANSFER_FEE_USDT))
        self._p2p_buffer_php = tk.StringVar(value=str(config.P2P_BUFFER_PHP))
        if not hasattr(self, "_p2p_delay_mins"):
            self._p2p_delay_mins = tk.StringVar(value=str(config.P2P_SETTLEMENT_DELAY_MINS))
        if not hasattr(self, "_p2p_cancel_rate"):
            self._p2p_cancel_rate = tk.StringVar(value=str(config.P2P_CANCEL_RATE_PCT))
        if not hasattr(self, "_p2p_spread_decay"):
            self._p2p_spread_decay = tk.StringVar(value=str(config.P2P_SPREAD_DECAY_PCT))
        self._p2p_auto_alert = tk.BooleanVar(value=config.P2P_TG_ALERTS)
        self._p2p_auto_log = tk.BooleanVar(value=config.P2P_AUTO_WATCH_LOG)
        self._p2p_auto_paper = tk.BooleanVar(value=config.P2P_AUTO_CYCLE)
        self._p2p_auto_hold_sell = tk.BooleanVar(value=config.P2P_AUTO_HOLD_SELL)
        self._p2p_mode = tk.StringVar(value=config.P2P_MODE)

        for label_text, var, width in [
            ("Capital PHP", self._p2p_capital_php, 10),
            ("Min net %", self._p2p_min_profit_pct, 6),
            ("Xfer fee USDT", self._p2p_transfer_fee_usdt, 6),
            ("Buffer PHP", self._p2p_buffer_php, 7),
            ("Delay min", self._p2p_delay_mins, 6),
            ("Cancel %", self._p2p_cancel_rate, 6),
            ("Decay %", self._p2p_spread_decay, 6),
        ]:
            group = tk.Frame(controls, bg="#0d1117")
            group.pack(side="left", padx=(0, 8))
            label = ttk.Label(group, text=label_text, foreground="#8b949e", background="#0d1117",
                              font=("Consolas", 8))
            label.pack(anchor="w")
            entry_widget = tk.Entry(
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
            )
            entry_widget.pack(anchor="w")
            help_text = P2P_FIELD_HELP.get(label_text, "")
            self._tip(label, help_text)
            self._tip(entry_widget, help_text)

        mode_group = tk.Frame(controls, bg="#0d1117")
        mode_group.pack(side="left", padx=(0, 10), pady=(10, 0))
        for value, text in [("paper", "Paper Sim (Live Data)"), ("live", "Live Assist")]:
            rb = tk.Radiobutton(
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
            )
            rb.pack(side="left", padx=(0, 8))
            self._tip(
                rb,
                "Paper Sim uses live listings but writes only paper transactions."
                if value == "paper"
                else "Live Assist scans, scores, logs, and alerts only. Fiat payment and release remain manual.",
            )

        recalc_btn = ttk.Button(controls, text="Recalculate Only", style="Btn.TButton",
                                command=self._recalculate_p2p_routes)
        recalc_btn.pack(side="left", padx=(0, 10), pady=(10, 0))
        self._tip(recalc_btn, "Recalculate route estimates from the current live listings. It does not create a paper transaction.")

        action_bar = tk.Frame(body, bg="#0d1117")
        action_bar.pack(fill="x", pady=(0, 4))

        self._p2p_paper_actions = tk.Frame(action_bar, bg="#0d1117")
        paper_cycle_btn = ttk.Button(self._p2p_paper_actions, text="Paper Buy+Sell", style="Btn.TButton",
                                     command=self._paper_cycle_now)
        paper_cycle_btn.pack(side="left", padx=(0, 6))
        self._tip(paper_cycle_btn, "Instant paper cycle: buy paper USDT and sell it immediately if the current route passes filters.")
        paper_hold_btn = ttk.Button(self._p2p_paper_actions, text="Paper Buy Hold", style="Btn.TButton",
                                    command=self._paper_hold_buy_now)
        paper_hold_btn.pack(side="left", padx=(0, 6))
        self._tip(paper_hold_btn, "Open a paper USDT inventory hold, then wait for a better sell price before closing.")
        check_hold_btn = ttk.Button(self._p2p_paper_actions, text="Check/Sell Hold", style="Btn.TButton",
                                    command=self._check_p2p_hold_sell)
        check_hold_btn.pack(side="left", padx=(0, 6))
        self._tip(check_hold_btn, "Check the open paper hold against current buyer listings. It sells only when target profit is met.")
        reset_paper_btn = ttk.Button(self._p2p_paper_actions, text="Reset Paper", style="Btn.TButton",
                                     command=self._reset_p2p_paper)
        reset_paper_btn.pack(side="left", padx=(0, 6))
        self._tip(reset_paper_btn, "Archive the current P2P paper session/history and start a fresh paper balance.")
        send_tg_btn = ttk.Button(self._p2p_paper_actions, text="Send TG", style="Btn.TButton",
                                 command=self._send_p2p_live_snapshot)
        send_tg_btn.pack(side="left", padx=(0, 12))
        self._tip(send_tg_btn, "Send the current P2P snapshot and paper summary to Telegram.")
        self._p2p_paper_alert = self._p2p_checkbutton(
            self._p2p_paper_actions,
            text="TG Alerts",
            variable=self._p2p_auto_alert,
        )
        self._p2p_paper_alert.pack(side="left", padx=(0, 10))
        self._tip(self._p2p_paper_alert, "Send Telegram alerts for P2P paper events and snapshots.")
        self._p2p_paper_auto = self._p2p_checkbutton(
            self._p2p_paper_actions,
            text="Auto Cycle",
            variable=self._p2p_auto_paper,
        )
        self._p2p_paper_auto.pack(side="left", padx=(0, 10))
        self._tip(self._p2p_paper_auto, "On refresh, auto-run Paper Buy+Sell only when a profitable route passes filters, no hold is open, and cooldown is satisfied.")
        self._p2p_hold_auto = self._p2p_checkbutton(
            self._p2p_paper_actions,
            text="Auto Hold Sell",
            variable=self._p2p_auto_hold_sell,
        )
        self._p2p_hold_auto.pack(side="left")
        self._tip(self._p2p_hold_auto, "On refresh, automatically check the open hold and sell only when target profit is met.")

        self._p2p_live_actions = tk.Frame(action_bar, bg="#0d1117")
        live_snapshot_btn = ttk.Button(self._p2p_live_actions, text="Send Live Snapshot", style="Btn.TButton",
                                       command=self._send_p2p_live_snapshot)
        live_snapshot_btn.pack(side="left", padx=(0, 6))
        self._tip(live_snapshot_btn, "Send the current live-assist route snapshot to Telegram. No trade is executed.")
        live_log_btn = ttk.Button(self._p2p_live_actions, text="Log Watch Route", style="Btn.TButton",
                                  command=self._log_top_p2p_route)
        live_log_btn.pack(side="left", padx=(0, 12))
        self._tip(live_log_btn, "Write the current best watch route to the local P2P journal for later review.")
        self._p2p_live_alert = self._p2p_checkbutton(
            self._p2p_live_actions,
            text="Telegram Alerts",
            variable=self._p2p_auto_alert,
        )
        self._p2p_live_alert.pack(side="left", padx=(0, 10))
        self._tip(self._p2p_live_alert, "Send Telegram alerts for live-assist opportunities. No automatic P2P trading is performed.")
        self._p2p_live_log = self._p2p_checkbutton(
            self._p2p_live_actions,
            text="Auto Watch Log",
            variable=self._p2p_auto_log,
        )
        self._p2p_live_log.pack(side="left")
        self._tip(self._p2p_live_log, "Automatically log the best live-assist watch route on refresh when it changes.")

        self._p2p_status_var = tk.StringVar(value="Open this tab or press Refresh to load prices.")
        ttk.Label(body, textvariable=self._p2p_status_var, foreground="#8b949e",
                  background="#0d1117", font=("Consolas", 9)).pack(fill="x", anchor="w")
        self._sync_p2p_mode_controls()

        summary = tk.Frame(body, bg="#0d1117")
        summary.pack(fill="x", pady=(4, 6))

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
            card.pack(side="left", padx=(0, 5), ipadx=7, ipady=4)
            label = ttk.Label(card, text=label_text, foreground="#8b949e", background="#161b22",
                              font=("Consolas", 8))
            label.pack(anchor="w")
            value = ttk.Label(card, textvariable=var, foreground="#58a6ff", background="#161b22",
                              font=("Consolas", 12, "bold"))
            value.pack(anchor="w")
            help_text = P2P_SUMMARY_HELP.get(label_text, "")
            self._tip(card, help_text)
            self._tip(label, help_text)
            self._tip(value, help_text)
        self._refresh_p2p_paper_summary()

        route_section = tk.Frame(body, bg="#0d1117")
        route_section.pack(fill="x", expand=False, pady=(0, 4))
        ttk.Label(route_section, text="ROUTE CALCULATOR", style="Header.TLabel",
                  background="#0d1117").pack(anchor="w", pady=(0, 3))

        route_cols = ("route", "size", "buy", "sell", "profit", "pct", "grade", "warnings")
        self.p2p_route_tree = ttk.Treeview(route_section, columns=route_cols, show="headings", height=2)
        for col, heading, width in [
            ("route", "ROUTE", 115),
            ("size", "SIZE PHP", 95),
            ("buy", "BUY", 135),
            ("sell", "SELL", 135),
            ("profit", "NET PHP", 95),
            ("pct", "NET %", 65),
            ("grade", "GRADE", 65),
            ("warnings", "WARNINGS", 200),
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
        ad_tables.configure(height=300)
        ad_tables.pack_propagate(False)
        buy_parent = tk.Frame(ad_tables, bg="#0d1117")
        sell_parent = tk.Frame(ad_tables, bg="#0d1117")
        buy_parent.pack(side="left", fill="both", expand=True, padx=(0, 6))
        sell_parent.pack(side="left", fill="both", expand=True, padx=(6, 0))

        self.p2p_buy_tree = self._build_p2p_table(
            buy_parent,
            "BUY USDT WITH PHP (LOWEST SELLER PRICES)",
            height=12,
        )
        self.p2p_sell_tree = self._build_p2p_table(
            sell_parent,
            "SELL USDT FOR PHP (HIGHEST BUYER PRICES)",
            height=12,
        )
        self._refresh_p2p_journal()
        self._refresh_p2p_transactions()

    def _show_p2p_guide(self):
        text = (
            "Inputs\n"
            "Capital PHP: paper bankroll used for route sizing.\n"
            "Min net %: minimum net profit after paper buffers before a route can execute.\n"
            "Xfer fee USDT: simulated crypto transfer/network cost deducted from the route.\n"
            "Buffer PHP: extra peso cost buffer deducted from expected profit.\n"
            "Delay min: settlement-delay assumption; longer delay increases spread-decay risk.\n"
            "Cancel %: simulated failed/cancelled order rate haircut.\n"
            "Decay %: spread haircut while manual P2P settlement is happening.\n\n"
            "Modes\n"
            "Paper Sim (Live Data): reads live listings and can write paper transactions.\n"
            "Live Assist: reads live listings only; it does not change paper balance.\n\n"
            "Buttons\n"
            "Refresh Prices: fetch latest P2P listings.\n"
            "Recalculate Only: rebuild routes without recording a paper trade.\n"
            "Paper Buy+Sell: simulate one complete buy-low/sell-high cycle if a route passes filters.\n"
            "Paper Buy Hold: buy paper USDT inventory and wait for a better sell price.\n"
            "Check/Sell Hold: sell the open paper hold only if the current sell route meets target.\n"
            "Reset Paper: reset P2P paper balance/history only; futures is not touched.\n"
            "Send TG: send the current P2P dashboard snapshot to Telegram.\n\n"
            "Checkboxes\n"
            "TG Alerts: send paper/live-assist alerts to Telegram.\n"
            "Auto Cycle: on refresh, auto-run Paper Buy+Sell only when a profitable route passes filters. "
            "It compounds free cash, waits for the settlement-delay window, and pauses while a Hold is open.\n"
            "Auto Hold Sell: on refresh, auto-check the open hold and sell only when target is met."
        )
        messagebox.showinfo("P2P Arb Controls", text, parent=self.root)

    def _build_p2p_history_tab(self, parent):
        body = tk.Frame(parent, bg="#0d1117", padx=15, pady=12)
        body.pack(fill="both", expand=True)

        top = tk.Frame(body, bg="#0d1117")
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="P2P PAPER HISTORY", style="Header.TLabel",
                  background="#0d1117").pack(side="left")
        ttk.Button(top, text="Refresh", style="Btn.TButton",
                   command=self._refresh_p2p_history_tab).pack(side="right")

        self._p2p_history_summary_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self._p2p_history_summary_var,
                  foreground="#8b949e", background="#0d1117",
                  font=("Consolas", 9)).pack(fill="x", anchor="w", pady=(0, 8))

        tx = tk.Frame(body, bg="#0d1117")
        tx.pack(fill="both", expand=True, pady=(0, 10))
        ttk.Label(tx, text="P2P PAPER TRANSACTION HISTORY", style="Header.TLabel",
                  background="#0d1117").pack(anchor="w", pady=(0, 3))
        tx_cols = ("time", "type", "status", "php", "usdt", "profit", "balance", "notes")
        self.p2p_history_tx_tree = ttk.Treeview(tx, columns=tx_cols, show="headings", height=13)
        for col, heading, width in [
            ("time", "TIME", 135),
            ("type", "TYPE", 110),
            ("status", "STATUS", 90),
            ("php", "PHP", 105),
            ("usdt", "USDT", 105),
            ("profit", "P&L PHP", 105),
            ("balance", "BALANCE", 110),
            ("notes", "NOTES", 430),
        ]:
            self.p2p_history_tx_tree.heading(col, text=heading)
            self.p2p_history_tx_tree.column(col, width=width, anchor="center")
        self.p2p_history_tx_tree.tag_configure("profit", foreground="#3fb950")
        self.p2p_history_tx_tree.tag_configure("loss", foreground="#f85149")
        tx_vsb = ttk.Scrollbar(tx, orient="vertical", command=self.p2p_history_tx_tree.yview)
        self.p2p_history_tx_tree.configure(yscrollcommand=tx_vsb.set)
        self.p2p_history_tx_tree.pack(side="left", fill="both", expand=True)
        tx_vsb.pack(side="right", fill="y")

        journal = tk.Frame(body, bg="#0d1117")
        journal.pack(fill="both", expand=True)
        ttk.Label(journal, text="P2P WATCH ROUTE JOURNAL", style="Header.TLabel",
                  background="#0d1117").pack(anchor="w", pady=(0, 3))
        journal_cols = ("time", "status", "route", "size", "profit", "notes")
        self.p2p_history_journal_tree = ttk.Treeview(
            journal, columns=journal_cols, show="headings", height=7,
        )
        for col, heading, width in [
            ("time", "TIME", 135),
            ("status", "STATUS", 95),
            ("route", "ROUTE", 145),
            ("size", "SIZE PHP", 105),
            ("profit", "EXP PHP", 105),
            ("notes", "NOTES", 520),
        ]:
            self.p2p_history_journal_tree.heading(col, text=heading)
            self.p2p_history_journal_tree.column(col, width=width, anchor="center")
        self.p2p_history_journal_tree.pack(fill="both", expand=True)
        self._refresh_p2p_history_tab()

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

    @staticmethod
    def _env_bool(value: bool) -> str:
        return "true" if value else "false"

    @staticmethod
    def _env_number(value: float) -> str:
        if abs(value - round(value)) < 0.0000001:
            return str(int(round(value)))
        return f"{value:.8f}".rstrip("0").rstrip(".")

    def _p2p_control_updates(self, settings: P2PRouteSettings | None = None) -> dict[str, str] | None:
        try:
            capital = settings.capital_php if settings else float(self._p2p_capital_php.get())
            min_pct = settings.min_profit_pct if settings else float(self._p2p_min_profit_pct.get())
            transfer_fee = (
                settings.cross_exchange_transfer_fee_usdt
                if settings
                else float(self._p2p_transfer_fee_usdt.get())
            )
            buffer_php = settings.local_buffer_php if settings else float(self._p2p_buffer_php.get())
            delay = float(self._p2p_delay_mins.get())
            cancel = float(self._p2p_cancel_rate.get())
            decay = float(self._p2p_spread_decay.get())
        except (AttributeError, TypeError, ValueError):
            return None

        if min(capital, min_pct, transfer_fee, buffer_php, delay, cancel, decay) < 0:
            return None

        mode = self._p2p_current_mode()
        if mode not in {"paper", "live"}:
            mode = "paper"

        return {
            "P2P_DEFAULT_CAPITAL_PHP": self._env_number(capital),
            "P2P_MIN_NET_PCT": self._env_number(min_pct),
            "P2P_TRANSFER_FEE_USDT": self._env_number(transfer_fee),
            "P2P_BUFFER_PHP": self._env_number(buffer_php),
            "P2P_SETTLEMENT_DELAY_MINS": self._env_number(delay),
            "P2P_CANCEL_RATE_PCT": self._env_number(cancel),
            "P2P_SPREAD_DECAY_PCT": self._env_number(decay),
            "P2P_MODE": mode,
            "P2P_TG_ALERTS": self._env_bool(self._p2p_auto_alert.get()),
            "P2P_AUTO_CYCLE": self._env_bool(self._p2p_auto_paper.get()),
            "P2P_AUTO_HOLD_SELL": self._env_bool(self._p2p_auto_hold_sell.get()),
            "P2P_AUTO_WATCH_LOG": self._env_bool(self._p2p_auto_log.get()),
        }

    def _persist_p2p_controls(self, settings: P2PRouteSettings | None = None) -> None:
        updates = self._p2p_control_updates(settings)
        if not updates or updates == self._p2p_last_saved_controls:
            return
        self._write_env(updates)
        self._p2p_last_saved_controls = dict(updates)

        config.P2P_DEFAULT_CAPITAL_PHP = float(updates["P2P_DEFAULT_CAPITAL_PHP"])
        config.P2P_MIN_NET_PCT = float(updates["P2P_MIN_NET_PCT"])
        config.P2P_TRANSFER_FEE_USDT = float(updates["P2P_TRANSFER_FEE_USDT"])
        config.P2P_BUFFER_PHP = float(updates["P2P_BUFFER_PHP"])
        config.P2P_SETTLEMENT_DELAY_MINS = float(updates["P2P_SETTLEMENT_DELAY_MINS"])
        config.P2P_CANCEL_RATE_PCT = float(updates["P2P_CANCEL_RATE_PCT"])
        config.P2P_SPREAD_DECAY_PCT = float(updates["P2P_SPREAD_DECAY_PCT"])
        config.P2P_MODE = updates["P2P_MODE"]
        config.P2P_TG_ALERTS = updates["P2P_TG_ALERTS"] == "true"
        config.P2P_AUTO_CYCLE = updates["P2P_AUTO_CYCLE"] == "true"
        config.P2P_AUTO_HOLD_SELL = updates["P2P_AUTO_HOLD_SELL"] == "true"
        config.P2P_AUTO_WATCH_LOG = updates["P2P_AUTO_WATCH_LOG"] == "true"

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
                    "Paper Sim uses live Binance P2P listings. Buy+Sell closes now; Buy Hold waits for target."
                )
        self._persist_p2p_controls()

    def _build_p2p_table(self, parent, title: str, height: int):
        section = tk.Frame(parent, bg="#0d1117")
        section.pack(fill="both", expand=True, pady=(0, 2))

        ttk.Label(section, text=title, style="Header.TLabel",
                  background="#0d1117").pack(anchor="w", pady=(0, 3))

        table_frame = tk.Frame(section, bg="#0d1117")
        table_frame.pack(fill="both", expand=True)

        cols = ("price", "limits", "available", "methods", "advertiser", "finish", "orders")
        tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=height)
        for col, heading, width, stretch in [
            ("price", "PRICE PHP", 82, False),
            ("limits", "LIMIT PHP", 135, False),
            ("available", "AVAIL USDT", 105, False),
            ("methods", "PAYMENT", 150, True),
            ("advertiser", "ADVERTISER", 145, True),
            ("finish", "FINISH", 68, False),
            ("orders", "ORDERS", 68, False),
        ]:
            tree.heading(col, text=heading)
            tree.column(col, width=width, minwidth=55, anchor="center", stretch=stretch)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
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
        self._recalculate_p2p_routes(update_status=False, run_automation=True)

    def _recalculate_p2p_routes(self, update_status: bool = True, run_automation: bool = False):
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

        scan_settings = self._p2p_capped_route_settings(settings)
        self._p2p_routes = build_p2p_routes([self._p2p_last_snapshot], settings=scan_settings)
        raw_sweep = build_depth_sweep([self._p2p_last_snapshot], settings=scan_settings)
        self._p2p_sweep = self._apply_p2p_realism(raw_sweep) if raw_sweep else None
        self._fill_p2p_route_tree(self._p2p_routes)
        self._update_p2p_sweep_summary()
        self._refresh_p2p_paper_summary()

        decision_reason = "no route meets current filters"
        decision = "SKIP"
        score = 0.0
        if self._p2p_sweep:
            decision_reason = (
                f"sweep {self._p2p_sweep.profit_php:+,.0f} PHP "
                f"({self._p2p_sweep.profit_pct:+.3f}%) grade {self._p2p_sweep.grade}"
            )
            decision = "RUN" if self._p2p_sweep.profit_pct >= settings.min_profit_pct and self._p2p_sweep.profit_php > 0 else "SKIP"
            score = max(0.0, min(100.0, self._p2p_sweep.profit_pct / max(settings.min_profit_pct, 0.001) * 60))
        self._record_decision("p2p", "recalculate", decision, score, decision_reason)

        if self._p2p_routes:
            top = self._p2p_routes[0]
            self._p2p_top_profit_var.set(f"{top.profit_php:+,.0f} PHP")
            self._p2p_top_route_var.set(f"{top.route_label} {top.grade}")
            if update_status:
                self._p2p_status_var.set(
                    self._p2p_capital_status(
                        capital_delta,
                        f"Recalculated {len(self._p2p_routes)} route(s) for "
                        f"{settings.capital_php:,.0f} PHP capital. No paper transaction was recorded.",
                    )
                )
        else:
            self._p2p_top_profit_var.set("--")
            self._p2p_top_route_var.set("--")
            if update_status:
                self._p2p_status_var.set(
                    self._p2p_capital_status(
                        capital_delta,
                        "No route meets the current profit filters. No paper transaction was recorded.",
                    )
                )

        if not run_automation:
            return

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
        self._persist_p2p_controls(settings)
        return settings

    def _p2p_starting_capital_php(self) -> float:
        if hasattr(self, "_p2p_capital_php"):
            return self._num(self._p2p_capital_php.get(), 500_000)
        return 500_000.0

    @staticmethod
    def _p2p_capped_route_settings(settings: P2PRouteSettings) -> P2PRouteSettings:
        route_cap = config.P2P_MAX_ROUTE_PHP
        capital_php = settings.capital_php
        if route_cap > 0:
            capital_php = min(capital_php, route_cap)
        return P2PRouteSettings(
            capital_php=capital_php,
            min_profit_php=settings.min_profit_php,
            min_profit_pct=settings.min_profit_pct,
            min_completion_rate=settings.min_completion_rate,
            min_orders=settings.min_orders,
            cross_exchange_transfer_fee_usdt=settings.cross_exchange_transfer_fee_usdt,
            local_buffer_php=settings.local_buffer_php,
            allowed_methods=settings.allowed_methods,
        )

    def _p2p_realism_settings(self) -> P2PRealismSettings:
        try:
            delay = float(self._p2p_delay_mins.get()) if hasattr(self, "_p2p_delay_mins") else config.P2P_SETTLEMENT_DELAY_MINS
            cancel = float(self._p2p_cancel_rate.get()) if hasattr(self, "_p2p_cancel_rate") else config.P2P_CANCEL_RATE_PCT
            decay = float(self._p2p_spread_decay.get()) if hasattr(self, "_p2p_spread_decay") else config.P2P_SPREAD_DECAY_PCT
        except Exception:
            delay = config.P2P_SETTLEMENT_DELAY_MINS
            cancel = config.P2P_CANCEL_RATE_PCT
            decay = config.P2P_SPREAD_DECAY_PCT
        return P2PRealismSettings(
            settlement_delay_mins=max(0.0, delay),
            cancel_rate_pct=max(0.0, cancel),
            spread_decay_pct=max(0.0, decay),
        )

    def _apply_p2p_realism(self, sweep: P2PDepthSweep) -> P2PDepthSweep:
        return apply_realism_to_sweep(sweep, realism=self._p2p_realism_settings())

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
        self.p2p_route_tree.yview_moveto(0)

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

    def _send_p2p_paper_event(self, title: str, lines: list[str]):
        if not hasattr(self, "_p2p_auto_alert") or not self._p2p_auto_alert.get():
            return
        text = "\n".join([
            f"🧾 <b>TradingBot23 P2P Paper - {tg.escape_html(title)}</b>",
            *lines,
        ])
        if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
            tg.send_dashboard_text(text)
        ov.send(text, title=f"TradingBot23 P2P Paper - {title}", source="tradingbot23-p2p")

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
        hold_settings = self._p2p_capped_route_settings(hold_settings)
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
            self._send_p2p_paper_event(
                "Paper Hold Opened",
                [
                    f"Bought: <b>{entry.buy_usdt:,.2f} USDT</b>",
                    f"Cost: <b>{entry.cost_php:,.0f} PHP</b> @ {entry.avg_buy_price:,.2f}",
                    f"Target: <b>+{settings.min_profit_pct:.3f}% net</b>",
                    f"Warnings: {tg.escape_html(warnings)}",
                ],
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
            realism=self._p2p_realism_settings(),
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
            self._send_p2p_paper_event(
                "Paper Hold Sold",
                [
                    f"Sold: <b>{evaluation.sold_usdt:,.2f} USDT</b>",
                    f"Avg sell: <b>{evaluation.avg_sell_price:,.2f} PHP</b>",
                    f"Net P&L: <b>{evaluation.profit_php:+,.0f} PHP ({evaluation.profit_pct:+.3f}%)</b>",
                ],
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
        if state.get("hold_position"):
            if not auto:
                self._p2p_status_var.set(
                    "Paper Buy+Sell skipped: a P2P hold is open. Sell or reset the hold first."
                )
            self._record_decision("p2p", "paper_cycle", "SKIP", 0, "hold position open")
            self._refresh_p2p_paper_summary(state)
            return
        paper_capital = self._num(state.get("cash_php", state.get("balance_php")), settings.capital_php)
        if paper_capital <= 0:
            if not auto:
                self._p2p_status_var.set("Paper cycle skipped: no free PHP paper cash.")
            return
        if auto:
            cooldown = max(60.0, self._p2p_realism_settings().settlement_delay_mins * 60)
            now = time.time()
            if now - self._p2p_last_auto_cycle_ts < cooldown:
                return
            min_auto_size = max(5_000.0, settings.capital_php * 0.05)
            if paper_capital < min_auto_size:
                self._record_decision(
                    "p2p",
                    "paper_cycle",
                    "SKIP",
                    0,
                    f"free cash {paper_capital:,.0f} below auto minimum {min_auto_size:,.0f}",
                )
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
        paper_settings = self._p2p_capped_route_settings(paper_settings)
        raw_paper_sweep = build_depth_sweep([self._p2p_last_snapshot], settings=paper_settings)
        paper_sweep = self._apply_p2p_realism(raw_paper_sweep) if raw_paper_sweep else None
        if not paper_sweep:
            self._p2p_status_var.set("Paper cycle skipped: not enough depth.")
            self._record_decision("p2p", "paper_cycle", "SKIP", 0, "not enough depth")
            return
        if auto:
            auto_key = self._p2p_sweep_listing_key(paper_sweep)
            if auto_key == self._p2p_last_auto_cycle_key:
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
            if auto:
                self._p2p_last_auto_cycle_key = auto_key
                self._p2p_last_auto_cycle_ts = time.time()
            self._record_decision(
                "p2p",
                "paper_cycle",
                "FILLED",
                100,
                f"{cycle.profit_php:+,.0f} PHP ({cycle.profit_pct:+.3f}%) realized paper",
            )
            self._append_p2p_lifecycle_events(cycle, paper_sweep, "AUTO" if auto else "MANUAL")
            warnings = "; ".join(cycle.warnings) if cycle.warnings else "ok"
            self._send_p2p_paper_event(
                "Paper Buy+Sell Filled",
                [
                    f"Size: <b>{cycle.size_php:,.0f} PHP</b>",
                    f"Avg buy/sell: {cycle.avg_buy_price:,.2f} / {cycle.avg_sell_price:,.2f}",
                    f"Net P&L: <b>{cycle.profit_php:+,.0f} PHP ({cycle.profit_pct:+.3f}%)</b>",
                    f"Balance: <b>{cycle.balance_after_php:,.0f} PHP</b>",
                    f"Warnings: {tg.escape_html(warnings)}",
                ],
            )
        elif not auto:
            self._record_decision(
                "p2p",
                "paper_cycle",
                "SKIP",
                0,
                f"below profit filter after realism: {paper_sweep.profit_php:+,.0f} PHP",
            )
            self._p2p_status_var.set("Paper cycle skipped: sweep is below profit filter.")

    def _append_p2p_lifecycle_events(self, cycle, sweep: P2PDepthSweep, source: str):
        steps = [
            ("ROUTE_SELECTED", "PLANNED", f"{source} paper route selected"),
            ("BUY_ORDER", "OPENED", f"{len(sweep.buy_lots)} buy lot(s)"),
            ("FIAT_PAYMENT", "CONFIRMED", "paper fiat payment confirmed"),
            ("SELL_ORDER", "OPENED", f"{len(sweep.sell_lots)} sell lot(s)"),
            ("CRYPTO_RELEASE", "CONFIRMED", "paper crypto release confirmed"),
            ("PAPER_CYCLE", "COMPLETED", "; ".join(cycle.warnings) if cycle.warnings else "ok"),
        ]
        for event_type, status, details in steps:
            try:
                self.event_ledger.append(
                    domain="p2p_lifecycle",
                    event_type=event_type,
                    status=status,
                    amount=cycle.size_php,
                    currency="PHP",
                    pnl=cycle.profit_php if status == "COMPLETED" else 0.0,
                    balance=cycle.balance_after_php if status == "COMPLETED" else 0.0,
                    symbol_or_route="USDT/PHP",
                    details=details,
                    timestamp=cycle.timestamp,
                )
            except Exception:
                pass
        self._refresh_event_ledger()

    @staticmethod
    def _p2p_sweep_listing_key(sweep: P2PDepthSweep) -> str:
        def lot_key(lot):
            return (
                lot.side,
                lot.marketplace,
                lot.advertiser,
                f"{lot.price:.4f}",
            )

        buy_key = tuple(lot_key(lot) for lot in sweep.buy_lots)
        sell_key = tuple(lot_key(lot) for lot in sweep.sell_lots)
        return repr((buy_key, sell_key, f"{sweep.avg_buy_price:.4f}", f"{sweep.avg_sell_price:.4f}"))

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
        if hasattr(self, "p2p_journal_tree"):
            self._fill_p2p_journal_tree(self.p2p_journal_tree, self.p2p_journal.recent(limit=8), 48)
        if hasattr(self, "p2p_history_journal_tree"):
            self._fill_p2p_journal_tree(
                self.p2p_history_journal_tree,
                self.p2p_journal.recent(limit=100),
                78,
            )

    def _refresh_p2p_transactions(self, state: dict | None = None):
        rows = self._p2p_transaction_rows(state=state, limit=200)
        if hasattr(self, "p2p_tx_tree"):
            self._fill_p2p_transactions_tree(self.p2p_tx_tree, rows[-14:], 42)
        if hasattr(self, "p2p_history_tx_tree"):
            self._fill_p2p_transactions_tree(self.p2p_history_tx_tree, rows, 78)
        self._update_p2p_history_summary(state)

    def _refresh_p2p_history_tab(self):
        self._refresh_p2p_journal()
        self._refresh_p2p_transactions()

    def _fill_p2p_journal_tree(self, tree, rows, note_limit: int):
        for item in tree.get_children():
            tree.delete(item)

        for row in reversed(rows):
            ts = row.get("timestamp", "")
            time_text = ts[:16].replace("T", " ")
            tree.insert("", "end", values=(
                time_text,
                row.get("status", ""),
                row.get("route", ""),
                f"{self._num(row.get('size_php')):,.0f}",
                f"{self._num(row.get('expected_profit_php')):+,.0f}",
                self._clip_text(row.get("notes", "") or row.get("warnings", ""), note_limit),
            ))

    def _fill_p2p_transactions_tree(self, tree, rows, note_limit: int):
        for item in tree.get_children():
            tree.delete(item)

        for row in reversed(rows):
            ts = row.get("timestamp", "")
            time_text = ts[:16].replace("T", " ")
            profit = self._num(row.get("profit_php"), 0.0)
            tags = ("profit",) if profit > 0 else ("loss",) if profit < 0 else ()
            tree.insert("", "end", tags=tags, values=(
                time_text,
                str(row.get("type", "")).replace("_", " "),
                row.get("status", ""),
                f"{self._num(row.get('amount_php')):,.0f}",
                f"{self._num(row.get('usdt')):,.2f}" if self._num(row.get("usdt")) else "--",
                f"{profit:+,.0f}" if profit else "--",
                f"{self._num(row.get('balance_after_php')):,.0f}",
                self._clip_text(row.get("notes", ""), note_limit),
            ))

    def _p2p_transaction_rows(self, state: dict | None = None, limit: int = 200) -> list[dict]:
        if state is not None:
            return list(state.get("transactions", []))[-limit:]
        starting_php = 500_000
        if hasattr(self, "_p2p_capital_php"):
            starting_php = self._num(self._p2p_capital_php.get(), 500_000)
        return self.p2p_paper.recent_transactions(limit=limit, starting_php=starting_php)

    def _update_p2p_history_summary(self, state: dict | None = None):
        if not hasattr(self, "_p2p_history_summary_var"):
            return
        starting_php = 500_000
        if hasattr(self, "_p2p_capital_php"):
            starting_php = self._num(self._p2p_capital_php.get(), 500_000)
        state = state or self.p2p_paper.state(starting_php)
        tx_count = len(state.get("transactions", []))
        self._p2p_history_summary_var.set(
            f"Paper balance {self._num(state.get('balance_php')):,.0f} PHP  |  "
            f"Cash {self._num(state.get('cash_php')):,.0f} PHP  |  "
            f"Realized {self._num(state.get('realized_profit_php')):+,.0f} PHP  |  "
            f"{tx_count} transaction(s)  |  CSV: {self.p2p_paper.transaction_path}"
        )

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
        tree.yview_moveto(0)

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
            label_widget = ttk.Label(grid, text=label, foreground="#8b949e", background="#0d1117",
                                     font=("Consolas", 9), width=22)
            label_widget.grid(row=r, column=0, sticky="w", pady=4)
            w = widget_factory(grid)
            w.grid(row=r, column=1, sticky="w", padx=8, pady=4)
            help_text = SETTINGS_HELP.get(label, "")
            self._tip(label_widget, help_text)
            self._tip(w, help_text)
            return w

        # Capital
        self._s_capital = tk.StringVar(value=str(int(config.CAPITAL_USD)))
        row("Capital (USD)", lambda p: field(p, self._s_capital, 10), 0)

        # Leverage
        self._s_leverage = tk.IntVar(value=config.LEVERAGE)
        lev_frame = tk.Frame(grid, bg="#0d1117")
        lev_frame.grid(row=1, column=1, sticky="w", padx=8, pady=4)
        lev_label = ttk.Label(grid, text="Leverage", foreground="#8b949e", background="#0d1117",
                              font=("Consolas", 9), width=22)
        lev_label.grid(row=1, column=0, sticky="w", pady=4)
        lev_spin = tk.Spinbox(
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
        )
        lev_spin.pack(side="left")
        ttk.Label(lev_frame, text="x", foreground="#8b949e", background="#0d1117",
                  font=("Consolas", 9)).pack(side="left", padx=(6, 0))
        self._tip(lev_label, SETTINGS_HELP["Leverage"])
        self._tip(lev_spin, SETTINGS_HELP["Leverage"])

        # TP %
        self._s_tp = tk.StringVar(value=str(round(config.FUTURES_NET_TP_PCT * 100, 2)))
        row("TP target (% net)", lambda p: field(p, self._s_tp, 8), 2)

        # SL enable + %
        self._s_sl_enabled = tk.BooleanVar(value=config.FUTURES_USE_SL)
        self._s_sl = tk.StringVar(value=str(round(config.FUTURES_NET_SL_PCT * 100, 2)))
        sl_frame = tk.Frame(grid, bg="#0d1117")
        sl_frame.grid(row=3, column=1, sticky="w", padx=8, pady=4)
        sl_label = ttk.Label(grid, text="Stop Loss", foreground="#8b949e", background="#0d1117",
                             font=("Consolas", 9), width=22)
        sl_label.grid(row=3, column=0, sticky="w", pady=4)
        sl_check = ttk.Checkbutton(sl_frame, text="Enable", variable=self._s_sl_enabled)
        sl_check.pack(side="left")
        sl_input = field(sl_frame, self._s_sl, 8)
        sl_input.pack(side="left", padx=8)
        ttk.Label(sl_frame, text="% net", foreground="#8b949e", background="#0d1117",
                  font=("Consolas",9)).pack(side="left")
        self._tip(sl_label, SETTINGS_HELP["Stop Loss"])
        self._tip(sl_check, SETTINGS_HELP["Stop Loss"])
        self._tip(sl_input, SETTINGS_HELP["Stop Loss"])

        # Crash protection
        self._s_crash_entry_guard = tk.BooleanVar(value=config.CRASH_ENTRY_GUARD_ENABLED)
        self._s_crash_emergency_sl = tk.BooleanVar(value=config.CRASH_EMERGENCY_SL_ENABLED)
        crash_frame = tk.Frame(grid, bg="#0d1117")
        crash_frame.grid(row=4, column=1, sticky="w", padx=8, pady=4)
        crash_label = ttk.Label(grid, text="Crash protection", foreground="#8b949e", background="#0d1117",
                                font=("Consolas", 9), width=22)
        crash_label.grid(row=4, column=0, sticky="w", pady=4)
        crash_entry_check = ttk.Checkbutton(
            crash_frame,
            text="Block entries",
            variable=self._s_crash_entry_guard,
        )
        crash_entry_check.pack(side="left")
        crash_sl_check = ttk.Checkbutton(
            crash_frame,
            text="Emergency SL",
            variable=self._s_crash_emergency_sl,
        )
        crash_sl_check.pack(side="left", padx=(12, 0))
        self._tip(crash_label, SETTINGS_HELP["Crash protection"])
        self._tip(crash_entry_check, "Pauses new futures entries while BTC 24h change is below the crash threshold. Existing positions are not closed by this setting.")
        self._tip(crash_sl_check, "Optional anti-crash stop. When enabled, crash mode sets an emergency SL on existing open positions; this behaves like an SL.")

        # BTC market-regime filter
        btc_filter_value = (
            ""
            if config.BTC_REGIME_FILTER_PCT is None
            else str(round(config.BTC_REGIME_FILTER_PCT * 100, 2))
        )
        self._s_btc_regime_filter = tk.StringVar(value=btc_filter_value)
        row("BTC filter (% 1h)", lambda p: field(p, self._s_btc_regime_filter, 8), 5)

        # Max hold days
        self._s_hold = tk.StringVar(value=str(config.MAX_HOLD_DAYS))
        row("Max hold (days)", lambda p: field(p, self._s_hold, 8), 6)

        # Per trade %
        self._s_per_trade = tk.StringVar(value=str(round(config.PER_TRADE_PCT * 100, 0)))
        row("Per trade (% of portfolio)", lambda p: field(p, self._s_per_trade, 8), 7)

        # Maximum simultaneous futures trades
        self._s_max_open_trades = tk.IntVar(value=config.MAX_OPEN_TRADES)
        max_open_frame = tk.Frame(grid, bg="#0d1117")
        max_open_frame.grid(row=8, column=1, sticky="w", padx=8, pady=4)
        max_open_label = ttk.Label(grid, text="Max open trades", foreground="#8b949e", background="#0d1117",
                                   font=("Consolas", 9), width=22)
        max_open_label.grid(row=8, column=0, sticky="w", pady=4)
        max_open_spin = tk.Spinbox(
            max_open_frame,
            from_=1,
            to=config.MAX_OPEN_TRADES_CAP,
            textvariable=self._s_max_open_trades,
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
        )
        max_open_spin.pack(side="left")
        ttk.Label(max_open_frame, text=f"1-{config.MAX_OPEN_TRADES_CAP}",
                  foreground="#8b949e", background="#0d1117",
                  font=("Consolas", 9)).pack(side="left", padx=(6, 0))
        self._tip(max_open_label, SETTINGS_HELP["Max open trades"])
        self._tip(max_open_spin, SETTINGS_HELP["Max open trades"])

        # Pre-trade wave analysis
        self._s_pretrade_enabled = tk.BooleanVar(value=config.PRE_TRADE_ANALYSIS_ENABLED)
        pretrade_frame = tk.Frame(grid, bg="#0d1117")
        pretrade_frame.grid(row=9, column=1, sticky="w", padx=8, pady=4)
        pretrade_label = ttk.Label(grid, text="Pre-trade wave check", foreground="#8b949e", background="#0d1117",
                                   font=("Consolas", 9), width=22)
        pretrade_label.grid(row=9, column=0, sticky="w", pady=4)
        pretrade_check = ttk.Checkbutton(pretrade_frame, text="Enable", variable=self._s_pretrade_enabled)
        pretrade_check.pack(side="left")
        self._tip(pretrade_label, SETTINGS_HELP["Pre-trade wave check"])
        self._tip(pretrade_check, SETTINGS_HELP["Pre-trade wave check"])

        self._s_pretrade_score = tk.StringVar(value=str(round(config.PRE_TRADE_MIN_SCORE, 0)))
        row("Min pre-trade score", lambda p: field(p, self._s_pretrade_score, 8), 10)

        # Monthly contribution
        self._s_monthly_contribution = tk.StringVar(value=str(round(config.MONTHLY_CONTRIBUTION_USD, 2)))
        row("Monthly contribution ($)", lambda p: field(p, self._s_monthly_contribution, 8), 11)

        self._s_monthly_day = tk.StringVar(value=str(config.MONTHLY_CONTRIBUTION_DAY))
        row("Contribution day", lambda p: field(p, self._s_monthly_day, 8), 12)

        self._s_auto_start = tk.BooleanVar(value=config.AUTO_START_FUTURES)
        auto_frame = tk.Frame(grid, bg="#0d1117")
        auto_frame.grid(row=13, column=1, sticky="w", padx=8, pady=4)
        auto_label = ttk.Label(grid, text="Auto-start futures", foreground="#8b949e", background="#0d1117",
                               font=("Consolas", 9), width=22)
        auto_label.grid(row=13, column=0, sticky="w", pady=4)
        auto_check = ttk.Checkbutton(auto_frame, text="Enable on launch", variable=self._s_auto_start)
        auto_check.pack(side="left")
        self._tip(auto_label, SETTINGS_HELP["Auto-start futures"])
        self._tip(auto_check, SETTINGS_HELP["Auto-start futures"])

        self._s_ohverlay_enabled = tk.BooleanVar(value=config.OHVERLAY_ENABLED)
        ohverlay_frame = tk.Frame(grid, bg="#0d1117")
        ohverlay_frame.grid(row=14, column=1, sticky="w", padx=8, pady=4)
        ohverlay_label = ttk.Label(grid, text="Ohverlay alerts", foreground="#8b949e", background="#0d1117",
                                   font=("Consolas", 9), width=22)
        ohverlay_label.grid(row=14, column=0, sticky="w", pady=4)
        ohverlay_check = ttk.Checkbutton(ohverlay_frame, text="Enable bubbles", variable=self._s_ohverlay_enabled)
        ohverlay_check.pack(side="left")
        ohverlay_test = ttk.Button(ohverlay_frame, text="Test", style="Btn.TButton",
                                   command=self._send_ohverlay_test)
        ohverlay_test.pack(side="left", padx=(8, 0))
        self._tip(ohverlay_label, SETTINGS_HELP["Ohverlay alerts"])
        self._tip(ohverlay_check, SETTINGS_HELP["Ohverlay alerts"])
        self._tip(ohverlay_test, "Send a test bubble to Ohverlay's local webhook using the current checkbox value.")

        # Apply button
        setup_msg = "" if self._settings_confirmed else "First run: review these values, then click Apply Settings."
        self._s_status = tk.StringVar(value=setup_msg)
        bf = tk.Frame(settings_panel, bg="#0d1117")
        bf.pack(fill="x", pady=12)
        apply_settings_btn = ttk.Button(bf, text="Apply Settings", style="Btn.TButton",
                                        command=self._apply_settings)
        apply_settings_btn.pack(side="left")
        self._tip(apply_settings_btn, "Save settings to .env and apply them to new futures trades. Open trades keep original leverage, margin, TP, and SL.")
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

    def _send_ohverlay_test(self):
        previous = config.OHVERLAY_ENABLED
        config.OHVERLAY_ENABLED = self._s_ohverlay_enabled.get()
        try:
            ov.send_event(
                "TradingBot23 Test",
                [
                    "Ohverlay bubble notifications are connected.",
                    "Enable Ohverlay's Webhook Server if no bubble appears.",
                ],
                source="tradingbot23-test",
            )
            self._s_status.set(
                "Ohverlay test sent." if config.OHVERLAY_ENABLED
                else "Ohverlay test skipped: checkbox is OFF."
            )
        finally:
            config.OHVERLAY_ENABLED = previous

    def _apply_settings(self):
        try:
            capital   = float(self._s_capital.get())
            leverage  = int(self._s_leverage.get())
            tp_pct    = float(self._s_tp.get()) / 100
            sl_on     = self._s_sl_enabled.get()
            sl_pct    = float(self._s_sl.get()) / 100
            crash_entry_guard = self._s_crash_entry_guard.get()
            crash_emergency_sl = self._s_crash_emergency_sl.get()
            btc_filter_raw = self._s_btc_regime_filter.get().strip()
            btc_regime_filter = (
                None
                if btc_filter_raw.lower() in {"", "none", "off"}
                else float(btc_filter_raw) / 100
            )
            hold_days = int(self._s_hold.get())
            per_trade = float(self._s_per_trade.get()) / 100
            max_open_trades = int(self._s_max_open_trades.get())
            pretrade_enabled = self._s_pretrade_enabled.get()
            pretrade_score = float(self._s_pretrade_score.get())
            monthly_contribution = float(self._s_monthly_contribution.get())
            monthly_day = int(self._s_monthly_day.get())
            auto_start = self._s_auto_start.get()
            ohverlay_enabled = self._s_ohverlay_enabled.get()
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
        if not 1 <= max_open_trades <= config.MAX_OPEN_TRADES_CAP:
            self._s_status.set(f"Error: max open trades must be 1-{config.MAX_OPEN_TRADES_CAP}")
            return
        if not 0 <= pretrade_score <= 100:
            self._s_status.set("Error: min pre-trade score must be 0-100")
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
            "CRASH_ENTRY_GUARD_ENABLED": "true" if crash_entry_guard else "false",
            "CRASH_EMERGENCY_SL_ENABLED": "true" if crash_emergency_sl else "false",
            "BTC_REGIME_FILTER_PCT": "none" if btc_regime_filter is None else btc_regime_filter,
            "MAX_HOLD_DAYS":      hold_days,
            "PER_TRADE_PCT":      per_trade,
            "MAX_OPEN_TRADES":    max_open_trades,
            "PRE_TRADE_ANALYSIS_ENABLED": "true" if pretrade_enabled else "false",
            "PRE_TRADE_MIN_SCORE": pretrade_score,
            "MONTHLY_CONTRIBUTION_USD": monthly_contribution,
            "MONTHLY_CONTRIBUTION_DAY": monthly_day,
            "AUTO_START_FUTURES": "true" if auto_start else "false",
            "OHVERLAY_ENABLED": "true" if ohverlay_enabled else "false",
            "SETTINGS_CONFIRMED": "true",
        })

        # Hot-apply to config (new trades pick these up immediately)
        config.CAPITAL_USD        = capital
        config.LEVERAGE           = leverage
        config.FUTURES_NET_TP_PCT = tp_pct
        config.FUTURES_NET_SL_PCT = sl_pct
        config.FUTURES_USE_SL     = sl_on
        config.CRASH_ENTRY_GUARD_ENABLED = crash_entry_guard
        config.CRASH_EMERGENCY_SL_ENABLED = crash_emergency_sl
        config.BTC_REGIME_FILTER_PCT = btc_regime_filter
        config.MAX_HOLD_DAYS      = hold_days
        config.PER_TRADE_PCT      = per_trade
        old_max_open_trades = config.MAX_OPEN_TRADES
        config.MAX_OPEN_TRADES    = max_open_trades
        config.TOP_N_LOSERS       = max(config.TOP_N_LOSERS, config.MAX_OPEN_TRADES)
        config.PRE_TRADE_ANALYSIS_ENABLED = pretrade_enabled
        config.PRE_TRADE_MIN_SCORE = pretrade_score
        config.MONTHLY_CONTRIBUTION_USD = monthly_contribution
        config.MONTHLY_CONTRIBUTION_DAY = monthly_day
        config.AUTO_START_FUTURES = auto_start
        config.OHVERLAY_ENABLED = ohverlay_enabled
        config.SETTINGS_CONFIRMED = True
        self._settings_confirmed = True
        if max_open_trades != old_max_open_trades:
            self.strategy.basket = []
            self.strategy.basket_month = None
            self.strategy.basket_year = None
        applied_capital_delta = self.trader.sync_starting_capital(capital)
        self.trader.leverage      = leverage
        self.engine_var.set(self._new_trade_setting_text())

        capital_note = ""
        if abs(applied_capital_delta) >= 0.01:
            sign = "+" if applied_capital_delta > 0 else "-"
            capital_note = f"  |  Cash {sign}${abs(applied_capital_delta):,.2f}"
        self._s_status.set(
            f"Applied!  Leverage: {leverage}x  |  Max open: {max_open_trades}  |  "
            f"Pretrade: {'ON' if pretrade_enabled else 'OFF'} {pretrade_score:.0f}  |  "
            f"TP: {tp_pct*100:.2f}%  |  SL: {'ON' if sl_on else 'OFF'}  |  "
            f"Crash: {'GUARD' if crash_entry_guard else 'OFF'}/"
            f"{'SL' if crash_emergency_sl else 'NO-SL'}  |  "
            f"BTC: {'OFF' if btc_regime_filter is None else f'{btc_regime_filter*100:+.2f}%'}  |  "
            f"Add ${monthly_contribution:.2f}/mo  |  Ohverlay: {'ON' if ohverlay_enabled else 'OFF'}"
            f"{capital_note}")
        self._refresh_summary()
        self._update_contribution_schedule()

    @staticmethod
    def _new_trade_setting_text() -> str:
        return f"   NEW TRADES: FUTURES {config.LEVERAGE}x / MAX {config.MAX_OPEN_TRADES}"

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

    def _risk_snapshot(self) -> dict:
        open_positions = list(self.trader.get_open_positions())
        closed = list(self.trader.get_trade_history())
        portfolio = self.trader.get_portfolio_value()
        cash = self.trader.cash_balance
        open_notional = sum(self._num(getattr(pos, "notional", 0.0)) for pos in open_positions)
        open_margin = sum(self._num(getattr(pos, "margin_used", 0.0)) for pos in open_positions)
        today_key = datetime.now(timezone.utc).date()
        daily_pnl = 0.0
        for pos in closed:
            exit_time = getattr(pos, "exit_time", None)
            if exit_time and exit_time.astimezone(timezone.utc).date() == today_key:
                daily_pnl += self._num(getattr(pos, "pnl_usd", 0.0))

        sorted_closed = sorted(
            [p for p in closed if getattr(p, "exit_time", None)],
            key=lambda p: p.exit_time,
            reverse=True,
        )
        loss_streak = 0
        for pos in sorted_closed:
            if self._num(getattr(pos, "pnl_usd", 0.0)) < 0:
                loss_streak += 1
            else:
                break

        try:
            p2p_state = self.p2p_paper.state(self._p2p_starting_capital_php())
        except Exception:
            p2p_state = {}
        backtests = sorted(config.DATA_DIR.glob("backtest*.txt")) if config.DATA_DIR.exists() else []
        try:
            rebound = rebound_analysis.latest_study_summary(config.DATA_DIR)
        except Exception:
            logger.debug("Could not load rebound study summary", exc_info=True)
            rebound = {}
        return {
            "portfolio": portfolio,
            "cash": cash,
            "open_count": len(open_positions),
            "open_notional": open_notional,
            "open_margin": open_margin,
            "daily_pnl": daily_pnl,
            "loss_streak": loss_streak,
            "total_trades": len(closed),
            "p2p_balance": self._num(p2p_state.get("balance_php")),
            "p2p_realized": self._num(p2p_state.get("realized_profit_php")),
            "profile": config.BOT_PROFILE,
            "data_dir": str(config.DATA_DIR),
            "backtest_count": len(backtests),
            "latest_backtest": backtests[-1].name if backtests else "none",
            "rebound": rebound,
        }

    def _risk_allows_trading(self) -> tuple[bool, list[str]]:
        snap = self._risk_snapshot()
        reasons: list[str] = []
        if self._risk_kill_switch:
            reasons.append("kill switch active")
        if config.RISK_MAX_DAILY_LOSS_USD > 0 and snap["daily_pnl"] <= -config.RISK_MAX_DAILY_LOSS_USD:
            reasons.append(f"daily loss {snap['daily_pnl']:+.2f} <= -{config.RISK_MAX_DAILY_LOSS_USD:.2f}")
        if config.RISK_MAX_OPEN_EXPOSURE_USD > 0 and snap["open_notional"] >= config.RISK_MAX_OPEN_EXPOSURE_USD:
            reasons.append(
                f"open notional {snap['open_notional']:.2f} >= {config.RISK_MAX_OPEN_EXPOSURE_USD:.2f}"
            )
        if config.RISK_MAX_LOSS_STREAK > 0 and snap["loss_streak"] >= config.RISK_MAX_LOSS_STREAK:
            reasons.append(f"loss streak {snap['loss_streak']} >= {config.RISK_MAX_LOSS_STREAK}")
        return not reasons, reasons

    def _apply_risk_settings(self):
        try:
            profile = self._risk_profile.get().strip() or "default"
            daily_loss = max(0.0, float(self._risk_max_daily_loss.get()))
            exposure = max(0.0, float(self._risk_max_exposure.get()))
            loss_streak = max(0, int(self._risk_max_loss_streak.get()))
            p2p_max_route = max(0.0, float(self._risk_p2p_max_route.get()))
            p2p_delay = max(0.0, float(self._p2p_delay_mins.get()))
            p2p_cancel = max(0.0, float(self._p2p_cancel_rate.get()))
            p2p_decay = max(0.0, float(self._p2p_spread_decay.get()))
        except ValueError as exc:
            self._risk_action_var.set(f"Error: {exc}")
            return

        self._write_env({
            "BOT_PROFILE": profile,
            "RISK_MAX_DAILY_LOSS_USD": daily_loss,
            "RISK_MAX_OPEN_EXPOSURE_USD": exposure,
            "RISK_MAX_LOSS_STREAK": loss_streak,
            "P2P_MAX_ROUTE_PHP": p2p_max_route,
            "P2P_SETTLEMENT_DELAY_MINS": p2p_delay,
            "P2P_CANCEL_RATE_PCT": p2p_cancel,
            "P2P_SPREAD_DECAY_PCT": p2p_decay,
        })
        old_profile = config.BOT_PROFILE
        config.BOT_PROFILE = profile
        config.RISK_MAX_DAILY_LOSS_USD = daily_loss
        config.RISK_MAX_OPEN_EXPOSURE_USD = exposure
        config.RISK_MAX_LOSS_STREAK = loss_streak
        config.P2P_MAX_ROUTE_PHP = p2p_max_route
        config.P2P_SETTLEMENT_DELAY_MINS = p2p_delay
        config.P2P_CANCEL_RATE_PCT = p2p_cancel
        config.P2P_SPREAD_DECAY_PCT = p2p_decay
        note = " Applied."
        if profile != old_profile:
            note += " Restart to switch data folder."
        self._risk_action_var.set(note.strip())
        self._record_decision("system", "risk_settings", "UPDATED", 80, "risk limits saved")
        self._refresh_risk_tab()

    def _risk_kill(self):
        self._risk_kill_switch = True
        self._paused = False
        self.pause_btn.config(text="Pause")
        self._force_event.set()
        self.status_var.set("Risk kill switch active. New entries are blocked; exits are still monitored.")
        self._risk_action_var.set("Kill switch active. Monitoring exits only.")
        self._record_decision("risk", "kill_switch", "BLOCK", 0, "manual kill switch; exits still monitored")
        try:
            append_event(
                domain="system",
                event_type="KILL_SWITCH",
                status="ACTIVE",
                amount=0,
                currency="",
                pnl=0,
                balance=self.trader.get_portfolio_value(),
                symbol_or_route="risk",
                details="manual dashboard kill switch",
            )
        except Exception:
            pass
        self._refresh_risk_tab()

    def _risk_clear_kill(self):
        self._risk_kill_switch = False
        self._risk_action_var.set("Kill switch cleared. Press Resume If Allowed if futures are paused.")
        self._record_decision("risk", "kill_switch", "UPDATED", 80, "manual kill switch cleared")
        self._refresh_risk_tab()

    def _risk_resume(self):
        allowed, reasons = self._risk_allows_trading()
        if not allowed:
            self._paused = False
            self.pause_btn.config(text="Pause")
            self._force_event.set()
            self._risk_action_var.set("Blocked: " + "; ".join(reasons))
            self._record_decision("risk", "resume", "BLOCK", 0, "; ".join(reasons))
            self._refresh_risk_tab()
            return
        self._paused = False
        self.pause_btn.config(text="Pause")
        self._force_event.set()
        self._risk_action_var.set("Resumed.")
        self._record_decision("risk", "resume", "RUN", 100, "risk checks passed")
        self._refresh_risk_tab()

    def _refresh_risk_tab(self):
        if not hasattr(self, "risk_tree"):
            return
        snap = self._risk_snapshot()
        allowed, reasons = self._risk_allows_trading()
        state_text = "OK" if allowed else "BLOCKED: " + "; ".join(reasons)
        self._risk_summary_var.set(
            f"Profile {snap['profile']}  |  Data {snap['data_dir']}  |  "
            f"Futures {'paused' if self._paused else 'running'}  |  Risk {state_text}"
        )
        for item in self.risk_tree.get_children():
            self.risk_tree.delete(item)

        def row(metric, value, limit="", status="OK", tag="ok"):
            self.risk_tree.insert("", "end", values=(metric, value, limit, status), tags=(tag,))

        row("Bot profile", snap["profile"], "restart required after change", "ACTIVE", "ok")
        row("Data folder", self._clip_text(snap["data_dir"], 64), "", "LOCAL", "ok")
        entry_tag = "ok" if allowed and not self._paused else "block" if not allowed else "warn"
        entry_status = "READY" if allowed and not self._paused else "BLOCKED" if not allowed else "PAUSED"
        entry_value = "entries allowed" if allowed and not self._paused else "; ".join(reasons) if reasons else "press Resume or Run Now"
        entry_limit = "risk guard + pause state"
        row("New futures entries", entry_value, entry_limit, entry_status, entry_tag)
        row(
            "Daily futures P&L",
            f"${snap['daily_pnl']:+,.2f}",
            f"-${config.RISK_MAX_DAILY_LOSS_USD:,.2f}" if config.RISK_MAX_DAILY_LOSS_USD else "disabled",
            "BLOCK" if config.RISK_MAX_DAILY_LOSS_USD and snap["daily_pnl"] <= -config.RISK_MAX_DAILY_LOSS_USD else "OK",
            "block" if config.RISK_MAX_DAILY_LOSS_USD and snap["daily_pnl"] <= -config.RISK_MAX_DAILY_LOSS_USD else "ok",
        )
        row(
            "Open futures notional",
            f"${snap['open_notional']:,.2f} ({snap['open_count']} positions)",
            f"${config.RISK_MAX_OPEN_EXPOSURE_USD:,.2f}" if config.RISK_MAX_OPEN_EXPOSURE_USD else "disabled",
            "BLOCK" if config.RISK_MAX_OPEN_EXPOSURE_USD and snap["open_notional"] >= config.RISK_MAX_OPEN_EXPOSURE_USD else "OK",
            "block" if config.RISK_MAX_OPEN_EXPOSURE_USD and snap["open_notional"] >= config.RISK_MAX_OPEN_EXPOSURE_USD else "ok",
        )
        row(
            "Loss streak",
            str(snap["loss_streak"]),
            str(config.RISK_MAX_LOSS_STREAK) if config.RISK_MAX_LOSS_STREAK else "disabled",
            "BLOCK" if config.RISK_MAX_LOSS_STREAK and snap["loss_streak"] >= config.RISK_MAX_LOSS_STREAK else "OK",
            "block" if config.RISK_MAX_LOSS_STREAK and snap["loss_streak"] >= config.RISK_MAX_LOSS_STREAK else "ok",
        )
        row("Open margin", f"${snap['open_margin']:,.2f}", "", "TRACK", "ok")
        row("P2P paper equity", f"{snap['p2p_balance']:,.0f} PHP", "", f"{snap['p2p_realized']:+,.0f} realized", "ok")
        row(
            "P2P max route",
            f"{config.P2P_MAX_ROUTE_PHP:,.0f} PHP" if config.P2P_MAX_ROUTE_PHP else "input capital",
            "per cycle cap",
            "ACTIVE" if config.P2P_MAX_ROUTE_PHP else "OFF",
            "ok",
        )
        row(
            "P2P realism",
            f"{config.P2P_SETTLEMENT_DELAY_MINS:.0f}m delay | {config.P2P_CANCEL_RATE_PCT:.1f}% cancel | {config.P2P_SPREAD_DECAY_PCT:.3f}% decay",
            "paper buffer",
            "ACTIVE",
            "warn",
        )
        rebound = snap.get("rebound") or {}
        if rebound:
            row(
                "5Y rebound study",
                f"hit {rebound.get('hit_rate_pct')}% | med {rebound.get('median_days')}d | p90 {rebound.get('p90_days')}d",
                f"dip<=-2% -> TP {rebound.get('target_pct')}%",
                f"{rebound.get('events')} events",
                "ok",
            )
            coverage_total = int(self._num(rebound.get("coverage_total")))
            coverage_ok = int(self._num(rebound.get("coverage_ok")))
            row(
                "Rebound coverage",
                f"{coverage_ok}/{coverage_total} symbols",
                self._clip_text(rebound.get("generated_name", ""), 42),
                "CURRENT TOP-50 BIAS",
                "warn",
            )
        else:
            row("5Y rebound study", "not run yet", "Run 5Y Rebound", "MISSING", "warn")
        row("Backtest files", f"{snap['backtest_count']} | latest {snap['latest_backtest']}", "", "FORWARD TESTING", "ok")
        self._refresh_decision_tree()

    def _fit_risk_panes(self):
        panes = getattr(self, "_risk_panes", None)
        if panes is None:
            return
        try:
            height = panes.winfo_height()
            if height <= 1:
                self.root.after(150, self._fit_risk_panes)
                return
            first_pane_height = max(135, height - 175)
            panes.sash_place(0, 0, first_pane_height)
        except tk.TclError:
            return

    def _run_rebound_study(self):
        if self._rebound_study_running:
            self._risk_action_var.set("5Y rebound study already running...")
            return
        self._rebound_study_running = True
        self._risk_action_var.set("Running 5Y rebound study in background...")
        self._record_decision("research", "5y_rebound", "RUN", 80, "background study started")

        def worker():
            try:
                rebound_analysis.main()
                summary = rebound_analysis.latest_study_summary(config.DATA_DIR)
                if summary:
                    status = (
                        f"5Y rebound updated: hit {summary.get('hit_rate_pct')}%, "
                        f"p90 {summary.get('p90_days')}d"
                    )
                else:
                    status = "5Y rebound study finished; summary not found."
                self._record_decision("research", "5y_rebound", "UPDATED", 90, status)
            except Exception as exc:
                logger.exception("5Y rebound study failed")
                status = f"5Y rebound study failed: {exc}"
                self._record_decision("research", "5y_rebound", "BLOCK", 0, status)
            finally:
                self._rebound_study_running = False
                try:
                    self.root.after(0, lambda: self._risk_action_var.set(status))
                    self.root.after(0, self._refresh_risk_tab)
                except tk.TclError:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _record_decision(
        self,
        domain: str,
        action: str,
        decision: str,
        score: float,
        reason: str,
        details: str = "",
    ):
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "domain": domain,
            "action": action,
            "decision": decision,
            "score": f"{score:.0f}",
            "reason": reason,
            "details": details,
        }
        self._decision_log.append(row)
        self._decision_log = self._decision_log[-300:]
        if hasattr(self, "decision_tree"):
            try:
                self.root.after(0, self._refresh_decision_tree)
            except tk.TclError:
                pass

    def _refresh_decision_tree(self):
        if not hasattr(self, "decision_tree"):
            return
        for item in self.decision_tree.get_children():
            self.decision_tree.delete(item)
        for row in reversed(self._decision_log[-80:]):
            decision = row.get("decision", "")
            tag = "block" if decision in {"BLOCK", "SKIP"} else "go" if decision in {"RUN", "FILLED", "UPDATED"} else "wait"
            self.decision_tree.insert("", "end", tags=(tag,), values=(
                row.get("timestamp", "")[:16].replace("T", " "),
                row.get("domain", ""),
                row.get("action", ""),
                decision,
                row.get("score", ""),
                self._clip_text(row.get("reason", ""), 80),
            ))

    def _refresh_event_ledger(self):
        if not hasattr(self, "ledger_tree"):
            return
        rows = self.event_ledger.recent(limit=300)
        for item in self.ledger_tree.get_children():
            self.ledger_tree.delete(item)
        for row in reversed(rows):
            pnl = self._num(row.get("pnl"))
            domain = row.get("domain", "")
            tag = "system" if domain == "system" else "profit" if pnl > 0 else "loss" if pnl < 0 else ""
            currency = row.get("currency", "")
            self.ledger_tree.insert("", "end", tags=(tag,), values=(
                row.get("timestamp", "")[:16].replace("T", " "),
                domain,
                row.get("event_type", ""),
                row.get("status", ""),
                f"{self._num(row.get('amount')):,.2f} {currency}".strip(),
                f"{pnl:+,.2f}" if pnl else "--",
                f"{self._num(row.get('balance')):,.2f}" if self._num(row.get("balance")) else "--",
                row.get("symbol_or_route", ""),
                self._clip_text(row.get("details", ""), 72),
            ))
        self._ledger_summary_var.set(f"{len(rows)} recent event(s)  |  CSV: {self.event_ledger.path}")

    def _export_ops_report(self):
        out = self._write_ops_report()
        if hasattr(self, "_risk_action_var"):
            self._risk_action_var.set(f"Report exported: {out.name}")
        return out

    def _write_ops_report(self):
        snap = self._risk_snapshot()
        stats = self.trader.get_stats()
        allowed, reasons = self._risk_allows_trading()
        p2p_state = self.p2p_paper.state(self._p2p_starting_capital_php())
        ledger_rows = self.event_ledger.recent(limit=25)
        decisions = self._decision_log[-25:]
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            "TradingBot23 Operations Report",
            f"Generated: {generated}",
            "",
            f"Profile: {snap['profile']}",
            f"Data folder: {snap['data_dir']}",
            f"Risk state: {'OK' if allowed else 'BLOCKED - ' + '; '.join(reasons)}",
            "",
            "Futures",
            f"  Portfolio: ${snap['portfolio']:,.2f}",
            f"  Cash: ${snap['cash']:,.2f}",
            f"  Daily closed P&L: ${snap['daily_pnl']:+,.2f}",
            f"  Open positions: {snap['open_count']}",
            f"  Open notional: ${snap['open_notional']:,.2f}",
            f"  Trades: {stats.get('total_trades', 0)} | Win rate: {stats.get('win_rate', 0):.1f}%",
        ]
        rebound = snap.get("rebound") or {}
        if rebound:
            lines.extend([
                f"  5Y rebound: hit {rebound.get('hit_rate_pct')}% | "
                f"median {rebound.get('median_days')}d | p90 {rebound.get('p90_days')}d",
                f"  Rebound report: {rebound.get('report_path')}",
            ])
        lines.extend([
            "",
            "P2P Paper",
            f"  Balance: {self._num(p2p_state.get('balance_php')):,.0f} PHP",
            f"  Cash: {self._num(p2p_state.get('cash_php')):,.0f} PHP",
            f"  Realized: {self._num(p2p_state.get('realized_profit_php')):+,.0f} PHP",
            f"  Transactions: {len(p2p_state.get('transactions', []))}",
            "",
            "Recent Decisions",
        ])
        for row in decisions:
            lines.append(
                f"  {row['timestamp'][:16]} {row['domain']} {row['action']} "
                f"{row['decision']} score={row['score']} {row['reason']}"
            )
        lines.append("")
        lines.append("Recent Ledger Events")
        for row in ledger_rows:
            lines.append(
                f"  {row.get('timestamp','')[:16]} {row.get('domain','')} "
                f"{row.get('event_type','')} {row.get('status','')} "
                f"pnl={row.get('pnl','')} {row.get('details','')}"
            )
        out = config.DATA_DIR / f"ops_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return out

    def _on_tab_changed(self, event):
        nb  = event.widget
        tab = nb.tab(nb.select(), "text").strip()
        if tab == "Risk":
            self._refresh_risk_tab()
            self.root.after(50, self._fit_risk_panes)
        elif tab == "Charts":
            self._draw_charts()
        elif tab == "History":
            self._refresh_history()
        elif tab == "P2P Arb" and self._p2p_last_snapshot is None:
            self._refresh_p2p()
        elif tab == "P2P History":
            self._refresh_p2p_history_tab()
        elif tab == "Ledger":
            self._refresh_event_ledger()

    # ── Button handlers ────────────────────────────────────────────────────────

    def _require_settings_confirmation(self, message):
        self.notebook.select(self.settings_tab)
        self.status_var.set(message)

    def _on_run_now(self):
        if not self._settings_confirmed:
            self._require_settings_confirmation("Review Settings and click Apply Settings before running futures.")
            return
        allowed, reasons = self._risk_allows_trading()
        if not allowed:
            self.status_var.set("Run blocked by risk guard: " + "; ".join(reasons))
            self._record_decision("risk", "run_now", "BLOCK", 0, "; ".join(reasons))
            return
        if self._paused:
            self._paused = False
            self.pause_btn.config(text="Pause")
        self._force_event.set()
        self._record_decision("futures", "run_now", "RUN", 100, "manual run requested")
        self.status_var.set("Running cycle now...")

    def _on_pause_resume(self):
        if not self._settings_confirmed:
            self._require_settings_confirmation("Review Settings and click Apply Settings before starting futures.")
            return
        if self._paused:
            allowed, reasons = self._risk_allows_trading()
            if not allowed:
                self._paused = False
                self.pause_btn.config(text="Pause")
                self._force_event.set()
                self.status_var.set("Resume blocked by risk guard: " + "; ".join(reasons))
                self._record_decision("risk", "resume", "BLOCK", 0, "; ".join(reasons))
                return
        self._paused = not self._paused
        if self._paused:
            self.pause_btn.config(text="Resume")
            self.status_var.set("Paused")
            self._record_decision("futures", "pause", "BLOCK", 0, "manual pause")
        else:
            self.pause_btn.config(text="Pause")
            self._force_event.set()
            self._record_decision("futures", "resume", "RUN", 100, "manual resume")
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
                allowed, reasons = self._risk_allows_trading()
                if not allowed:
                    closed = self.trader.check_positions()
                    summary = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "basket_refreshed": False,
                        "cash_contributed": 0.0,
                        "dips_found": 0,
                        "positions_opened": 0,
                        "positions_closed": len(closed),
                        "slots_filled": 0,
                        "risk_blocked": True,
                        "risk_reasons": reasons,
                    }
                    self._record_decision("risk", "cycle", "BLOCK", 0, "; ".join(reasons))
                else:
                    summary = self.strategy.run_cycle()
                    self._record_decision(
                        "futures",
                        "cycle",
                        "RUN",
                        80,
                        f"dips {summary.get('dips_found', 0)} opened {summary.get('positions_opened', 0)} "
                        f"filled {summary.get('slots_filled', 0)} closed {summary.get('positions_closed', 0)} "
                        f"prewait {summary.get('pre_trade_wait', 0)}",
                    )
                    for row in summary.get("pre_trade_decisions", [])[-12:]:
                        self._record_decision(
                            "futures",
                            f"pretrade {row.get('symbol', '')}",
                            row.get("decision", "WAIT"),
                            self._num(row.get("score")),
                            row.get("reason", ""),
                        )
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
            if hasattr(self, "notebook"):
                tab = self.notebook.tab(self.notebook.select(), "text").strip()
                if tab == "Risk":
                    self._refresh_risk_tab()
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
        allowed, reasons = self._risk_allows_trading()
        if not allowed:
            self.status_var.set("Risk guard active; monitoring exits only: " + "; ".join(reasons))
            return
        if self._last_cycle_time is None:
            self.status_var.set("Waiting for first cycle...")
            return
        s         = self._last_cycle_summary
        last      = self._last_cycle_time.strftime("%H:%M:%S")
        secs_left = max(0, int(self._next_cycle_ts - time.time()))
        m, sec    = divmod(secs_left, 60)
        countdown = f"{m:02d}:{sec:02d}"
        if s.get("market_regime_blocked"):
            self.status_var.set(
                f"Market regime guard active; monitoring exits only: "
                f"{s.get('market_regime_reason', '')}  |  Next scan: {countdown}"
            )
            return
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
            pos.tp_price = self.trader._net_tp_price(pos, now)
            leverage = getattr(pos, "leverage", 1)
            if current and pos.entry_price > 0:
                price_chg = (current - pos.entry_price) / pos.entry_price
                lev_pnl   = price_chg * leverage * 100
                pnl_str = f"{lev_pnl:+.2f}% ({price_chg*100:+.2f}% price)"
            else:
                pnl_str = "--"
            risk_price = self._num(getattr(pos, "liquidation_price", 0.0), 0.0)
            risk_str = f"${risk_price:.4f}" if 0 < risk_price < pos.entry_price else "No near liq"
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
