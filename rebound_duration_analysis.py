"""Measure top-50 crypto dip rebound durations over the last five years.

The study uses the same top-50 universe source as the app: CoinGecko
market-cap page 1, with stablecoins filtered by the app config. Historical
OHLC comes from Binance USDT pairs, futures first and spot as fallback.
"""

from __future__ import annotations

import csv
import json
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median

import requests
from dotenv import dotenv_values

from bot import config
from bot.modules.data_fetcher import DataFetcher


YEARS = 5
MAX_LOOKAHEAD_DAYS = 90
DIP_THRESHOLDS_PCT = [-2.0, -5.0, -10.0]


def output_dir() -> Path:
    return config.DATA_DIR / "analysis"


def load_runtime_config() -> Path:
    """Prefer the packaged app .env so research matches the running app."""
    env_path = Path("dist/.env") if Path("dist/.env").exists() else Path(".env")
    values = dotenv_values(env_path)
    if env_path.parent.name == "dist":
        config.PROJECT_ROOT = env_path.parent
        config.DATA_DIR = env_path.parent / "data"
        config.LOG_DIR = env_path.parent / "logs"
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)

    def as_float(key: str, current: float) -> float:
        try:
            return float(values.get(key, current))
        except (TypeError, ValueError):
            return current

    def as_int(key: str, current: int) -> int:
        try:
            return int(float(values.get(key, current)))
        except (TypeError, ValueError):
            return current

    config.TOP_N_COINS = as_int("TOP_N_COINS", config.TOP_N_COINS)
    config.LEVERAGE = as_int("LEVERAGE", config.LEVERAGE)
    config.FUTURES_NET_TP_PCT = as_float("FUTURES_NET_TP_PCT", config.FUTURES_NET_TP_PCT)
    config.FUTURES_FEE_PCT = as_float("FUTURES_FEE_PCT", config.FUTURES_FEE_PCT)
    config.FUNDING_RATE_DAILY = as_float("FUNDING_RATE_DAILY", config.FUNDING_RATE_DAILY)
    return env_path


def month_add(year: int, month: int, delta: int) -> tuple[int, int]:
    absolute = year * 12 + (month - 1) + delta
    return absolute // 12, (absolute % 12) + 1


def five_year_full_month_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return 60 complete months ending at the last completed month."""
    now = now or datetime.now(timezone.utc)
    first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = first_this_month - timedelta(days=1)
    start_year, start_month = month_add(end.year, end.month, -59)
    start = datetime(start_year, start_month, 1, tzinfo=timezone.utc)
    return start, end


def app_price_target_pct() -> float:
    leverage = max(1, min(config.LEVERAGE, config.MAX_LEVERAGE))
    fee_drag = 2 * config.FUTURES_FEE_PCT * leverage
    funding_drag = config.FUNDING_RATE_DAILY * leverage * 1.5
    gross_tp_move = (config.FUTURES_NET_TP_PCT + fee_drag + funding_drag) / leverage
    return gross_tp_move * 100


def app_price_target_pct_from_env(data_dir: Path | None = None) -> float:
    if data_dir is None:
        return app_price_target_pct()
    env_path = Path(data_dir).parent / ".env"
    if not env_path.exists():
        return app_price_target_pct()
    values = dotenv_values(env_path)

    def as_float(key: str, current: float) -> float:
        try:
            return float(values.get(key, current))
        except (TypeError, ValueError):
            return current

    def as_int(key: str, current: int) -> int:
        try:
            return int(float(values.get(key, current)))
        except (TypeError, ValueError):
            return current

    leverage = max(1, min(as_int("LEVERAGE", config.LEVERAGE), config.MAX_LEVERAGE))
    net_tp = as_float("FUTURES_NET_TP_PCT", config.FUTURES_NET_TP_PCT)
    fee_pct = as_float("FUTURES_FEE_PCT", config.FUTURES_FEE_PCT)
    funding_daily = as_float("FUNDING_RATE_DAILY", config.FUNDING_RATE_DAILY)
    fee_drag = 2 * fee_pct * leverage
    funding_drag = funding_daily * leverage * 1.5
    return ((net_tp + fee_drag + funding_drag) / leverage) * 100


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = (len(ordered) - 1) * pct
    lower = math.floor(idx)
    upper = math.ceil(idx)
    if lower == upper:
        return ordered[int(idx)]
    weight = idx - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def fetch_binance_klines(symbol: str, start: datetime, end: datetime) -> tuple[str, list[dict]]:
    endpoints = [
        ("futures", "https://fapi.binance.com/fapi/v1/klines"),
        ("spot", "https://api.binance.com/api/v3/klines"),
    ]
    pair = f"{symbol}USDT"
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    for source, url in endpoints:
        candles: list[dict] = []
        cursor = start_ms
        while cursor < end_ms:
            try:
                resp = requests.get(
                    url,
                    params={
                        "symbol": pair,
                        "interval": "1d",
                        "startTime": cursor,
                        "endTime": end_ms,
                        "limit": 1500,
                    },
                    timeout=20,
                )
                if resp.status_code == 429:
                    time.sleep(float(resp.headers.get("Retry-After", "2")))
                    continue
                if resp.status_code != 200:
                    candles = []
                    break
                batch = resp.json()
                if not batch:
                    break
            except (requests.RequestException, ValueError):
                candles = []
                break

            for kline in batch:
                candles.append({
                    "date": datetime.fromtimestamp(kline[0] / 1000, tz=timezone.utc).date(),
                    "open": float(kline[1]),
                    "high": float(kline[2]),
                    "low": float(kline[3]),
                    "close": float(kline[4]),
                    "volume": float(kline[5]),
                })

            next_cursor = int(batch[-1][0]) + 24 * 60 * 60 * 1000
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < 1500:
                break
            time.sleep(0.05)

        if candles:
            candles.sort(key=lambda c: c["date"])
            deduped = {c["date"]: c for c in candles}
            return source, [deduped[d] for d in sorted(deduped)]

    return "", []


def measure_rebounds(
    symbol: str,
    rank: int | None,
    source: str,
    candles: list[dict],
    start: datetime,
    end: datetime,
    target_pcts: list[float],
) -> list[dict]:
    rows: list[dict] = []
    start_day = start.date()
    end_day = end.date()
    candles = [c for c in candles if start_day <= c["date"] <= end_day]
    if len(candles) < 2:
        return rows

    for i in range(1, len(candles)):
        prev_close = candles[i - 1]["close"]
        entry = candles[i]["close"]
        if prev_close <= 0 or entry <= 0:
            continue
        change_pct = ((entry - prev_close) / prev_close) * 100
        for dip_threshold in DIP_THRESHOLDS_PCT:
            if change_pct > dip_threshold:
                continue
            for target_pct in target_pcts:
                target_price = entry * (1 + target_pct / 100)
                max_j = min(len(candles), i + MAX_LOOKAHEAD_DAYS + 1)
                hit_date = None
                for j in range(i + 1, max_j):
                    if candles[j]["high"] >= target_price:
                        hit_date = candles[j]["date"]
                        break
                duration_days = (hit_date - candles[i]["date"]).days if hit_date else ""
                rows.append({
                    "symbol": symbol,
                    "rank": rank if rank is not None else "",
                    "source": source,
                    "entry_date": candles[i]["date"].isoformat(),
                    "dip_threshold_pct": dip_threshold,
                    "daily_change_pct": round(change_pct, 6),
                    "entry_close": round(entry, 10),
                    "target_pct": round(target_pct, 6),
                    "target_price": round(target_price, 10),
                    "hit": 1 if hit_date else 0,
                    "hit_date": hit_date.isoformat() if hit_date else "",
                    "duration_days": duration_days,
                    "max_lookahead_days": MAX_LOOKAHEAD_DAYS,
                })
    return rows


def summarize(rows: list[dict], group_fields: tuple[str, ...]) -> list[dict]:
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[tuple(row[field] for field in group_fields)].append(row)

    summaries = []
    for key, group in sorted(grouped.items()):
        durations = [float(r["duration_days"]) for r in group if r["hit"]]
        total = len(group)
        hit_count = len(durations)
        out = {field: value for field, value in zip(group_fields, key)}
        out.update({
            "events": total,
            "hit_count": hit_count,
            "hit_rate_pct": round((hit_count / total * 100) if total else 0.0, 2),
            "median_days": round(median(durations), 2) if durations else "",
            "avg_days": round(mean(durations), 2) if durations else "",
            "p75_days": round(percentile(durations, 0.75), 2) if durations else "",
            "p90_days": round(percentile(durations, 0.90), 2) if durations else "",
            "within_1d_pct": round(sum(d <= 1 for d in durations) / total * 100, 2) if total else 0.0,
            "within_3d_pct": round(sum(d <= 3 for d in durations) / total * 100, 2) if total else 0.0,
            "within_7d_pct": round(sum(d <= 7 for d in durations) / total * 100, 2) if total else 0.0,
            "within_14d_pct": round(sum(d <= 14 for d in durations) / total * 100, 2) if total else 0.0,
            "within_30d_pct": round(sum(d <= 30 for d in durations) / total * 100, 2) if total else 0.0,
            "no_hit_90d_pct": round((total - hit_count) / total * 100, 2) if total else 0.0,
        })
        summaries.append(out)
    return summaries


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def latest_study_summary(data_dir: Path | None = None) -> dict:
    analysis_dir = (data_dir or config.DATA_DIR) / "analysis"
    summaries = sorted(analysis_dir.glob("rebound_duration_summary_5y_*.csv"))
    reports = sorted(analysis_dir.glob("rebound_duration_report_5y_*.txt"))
    coverage_files = sorted(analysis_dir.glob("rebound_duration_coverage_5y_*.csv"))
    if not summaries:
        return {}

    target = round(app_price_target_pct_from_env(data_dir), 6)
    with open(summaries[-1], "r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    primary_rows = [
        row for row in rows
        if abs(float(row.get("dip_threshold_pct", 0)) - -2.0) < 1e-9
    ]
    if not primary_rows:
        return {}
    primary = min(primary_rows, key=lambda row: abs(float(row.get("target_pct", 0)) - target))

    coverage_total = 0
    coverage_ok = 0
    if coverage_files:
        with open(coverage_files[-1], "r", newline="", encoding="utf-8") as handle:
            coverage_rows = list(csv.DictReader(handle))
        coverage_total = len(coverage_rows)
        coverage_ok = sum(1 for row in coverage_rows if row.get("source") != "missing")

    return {
        "summary_path": str(summaries[-1]),
        "report_path": str(reports[-1]) if reports else "",
        "coverage_path": str(coverage_files[-1]) if coverage_files else "",
        "generated_name": summaries[-1].name,
        "dip_threshold_pct": primary.get("dip_threshold_pct", ""),
        "target_pct": primary.get("target_pct", ""),
        "events": primary.get("events", ""),
        "hit_rate_pct": primary.get("hit_rate_pct", ""),
        "median_days": primary.get("median_days", ""),
        "p90_days": primary.get("p90_days", ""),
        "within_1d_pct": primary.get("within_1d_pct", ""),
        "within_3d_pct": primary.get("within_3d_pct", ""),
        "no_hit_90d_pct": primary.get("no_hit_90d_pct", ""),
        "coverage_ok": coverage_ok,
        "coverage_total": coverage_total,
    }


def main() -> None:
    env_path = load_runtime_config()
    out_dir = output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    start, end = five_year_full_month_window()

    configured_target = app_price_target_pct()
    target_pcts = sorted({0.0, round(configured_target, 6), 1.0, 2.0, 5.0})

    fetcher = DataFetcher()
    coins = fetcher.get_top_coins(limit=config.TOP_N_COINS)
    universe = [
        {
            "symbol": c["symbol"],
            "rank": c.get("cmc_rank"),
            "name": c.get("name", c["symbol"]),
        }
        for c in coins
        if c.get("symbol")
    ]

    print(f"Runtime config: {env_path}")
    print(f"Universe: {len(universe)} non-stable coins from CoinGecko top-{config.TOP_N_COINS} page")
    print(f"Window: {start.date()} to {end.date()} UTC ({YEARS} years, complete months)")
    print(f"Targets: {target_pcts} pct | app configured price target: {configured_target:.4f}%")

    all_rows: list[dict] = []
    coverage: list[dict] = []
    for index, coin in enumerate(universe, start=1):
        symbol = coin["symbol"]
        source, candles = fetch_binance_klines(symbol, start - timedelta(days=5), end)
        coverage.append({
            "symbol": symbol,
            "rank": coin.get("rank") or "",
            "name": coin.get("name", ""),
            "source": source or "missing",
            "candles": len(candles),
            "first_date": candles[0]["date"].isoformat() if candles else "",
            "last_date": candles[-1]["date"].isoformat() if candles else "",
        })
        if candles:
            rows = measure_rebounds(
                symbol,
                coin.get("rank"),
                source,
                candles,
                start,
                end,
                target_pcts,
            )
            all_rows.extend(rows)
        print(f"{index:02d}/{len(universe)} {symbol:<10} {source or 'missing':<8} candles={len(candles)}")
        time.sleep(0.05)

    overall = summarize(all_rows, ("dip_threshold_pct", "target_pct"))
    by_coin = summarize(all_rows, ("symbol", "rank", "dip_threshold_pct", "target_pct"))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    events_path = out_dir / f"rebound_duration_events_5y_{stamp}.csv"
    overall_path = out_dir / f"rebound_duration_summary_5y_{stamp}.csv"
    by_coin_path = out_dir / f"rebound_duration_by_coin_5y_{stamp}.csv"
    coverage_path = out_dir / f"rebound_duration_coverage_5y_{stamp}.csv"
    report_path = out_dir / f"rebound_duration_report_5y_{stamp}.txt"

    write_csv(events_path, all_rows)
    write_csv(overall_path, overall)
    write_csv(by_coin_path, by_coin)
    write_csv(coverage_path, coverage)

    preferred_target = min(target_pcts, key=lambda t: abs(t - configured_target))
    preferred = [
        row for row in overall
        if float(row["dip_threshold_pct"]) == -2.0 and float(row["target_pct"]) == preferred_target
    ]
    preferred_row = preferred[0] if preferred else {}
    missing = [c for c in coverage if c["source"] == "missing"]

    lines = [
        "TradingBot23 Top-50 Rebound Duration Study",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Runtime config: {env_path}",
        f"Window: {start.date()} to {end.date()} UTC ({YEARS} years, complete months)",
        f"Universe: current CoinGecko top-{config.TOP_N_COINS} page, stablecoins filtered",
        f"Universe count after app filters: {len(universe)}",
        f"Binance coverage: {len(universe) - len(missing)}/{len(universe)} symbols",
        f"Configured app price target: {configured_target:.4f}%",
        "",
        "Primary read: daily dips <= -2% reaching the configured app TP price move",
    ]
    if preferred_row:
        lines.extend([
            f"  Events: {preferred_row['events']}",
            f"  Hit rate within {MAX_LOOKAHEAD_DAYS}d: {preferred_row['hit_rate_pct']}%",
            f"  Median rebound: {preferred_row['median_days']} days",
            f"  P90 rebound: {preferred_row['p90_days']} days",
            f"  Within 1d: {preferred_row['within_1d_pct']}%",
            f"  Within 3d: {preferred_row['within_3d_pct']}%",
            f"  Within 7d: {preferred_row['within_7d_pct']}%",
            f"  No hit within {MAX_LOOKAHEAD_DAYS}d: {preferred_row['no_hit_90d_pct']}%",
        ])
    lines.extend([
        "",
        "Overall summary rows:",
    ])
    for row in overall:
        lines.append(
            f"  dip<={float(row['dip_threshold_pct']):+.0f}% target={float(row['target_pct']):.4f}% "
            f"events={row['events']} hit={row['hit_rate_pct']}% "
            f"median={row['median_days']}d p90={row['p90_days']}d "
            f"<=3d={row['within_3d_pct']}% no90={row['no_hit_90d_pct']}%"
        )
    if missing:
        lines.extend([
            "",
            "Missing Binance USDT coverage:",
            "  " + ", ".join(c["symbol"] for c in missing),
        ])
    lines.extend([
        "",
        "Limitations:",
        "  This is a current top-50-page study, not historical monthly market-cap membership.",
        "  Binance listing age creates shorter histories for newer coins.",
        "  Daily candles round rebound timing to days and cannot prove intraday sequence.",
        "",
        f"Events CSV: {events_path}",
        f"Overall CSV: {overall_path}",
        f"By-coin CSV: {by_coin_path}",
        f"Coverage CSV: {coverage_path}",
    ])
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
