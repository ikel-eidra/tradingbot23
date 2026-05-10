"""Paper-account contribution tracking."""

import calendar
import json
from datetime import date, datetime, timezone

from bot import config


def _account_state_json():
    return config.DATA_DIR / "account_state.json"


def load_account_state() -> dict:
    path = _account_state_json()
    if not path.exists():
        return {
            "total_contributed_usd": 0.0,
            "last_monthly_contribution": None,
            "contributions": [],
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {
            "total_contributed_usd": 0.0,
            "last_monthly_contribution": None,
            "contributions": [],
        }

    data.setdefault("total_contributed_usd", 0.0)
    data.setdefault("last_monthly_contribution", None)
    data.setdefault("contributions", [])
    return data


def save_account_state(state: dict) -> None:
    path = _account_state_json()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def total_contributed_capital() -> float:
    state = load_account_state()
    return config.CAPITAL_USD + float(state.get("total_contributed_usd", 0.0))


def _scheduled_date(year: int, month: int, day: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(max(day, 1), last_day))


def _add_months(year: int, month: int, offset: int) -> tuple[int, int]:
    absolute = (year * 12) + (month - 1) + offset
    return absolute // 12, (absolute % 12) + 1


def contribution_schedule(months: int = 12, now: datetime | None = None) -> list[dict]:
    """Return the next N monthly paper-contribution markers."""
    now = now or datetime.now(timezone.utc)
    amount = float(getattr(config, "MONTHLY_CONTRIBUTION_USD", 0.0))
    day = int(getattr(config, "MONTHLY_CONTRIBUTION_DAY", 1))
    paid_months = {
        c.get("month")
        for c in load_account_state().get("contributions", [])
        if c.get("month")
    }

    rows = []
    today = now.date()
    for offset in range(months):
        year, month = _add_months(now.year, now.month, offset)
        due = _scheduled_date(year, month, day)
        month_key = f"{year:04d}-{month:02d}"
        if month_key in paid_months:
            status = "paid"
        elif due <= today:
            status = "due"
        else:
            status = "scheduled"
        rows.append({
            "month": month_key,
            "date": due.isoformat(),
            "amount_usd": amount,
            "status": status,
        })
    return rows


def apply_monthly_contribution(cash_balance: float, now: datetime | None = None) -> tuple[float, float]:
    """Add this month's paper contribution if it is due.

    Returns:
        (new_cash_balance, applied_amount)
    """
    amount = float(getattr(config, "MONTHLY_CONTRIBUTION_USD", 0.0))
    if amount <= 0:
        return cash_balance, 0.0

    now = now or datetime.now(timezone.utc)
    day = int(getattr(config, "MONTHLY_CONTRIBUTION_DAY", 1))
    if now.date() < _scheduled_date(now.year, now.month, day):
        return cash_balance, 0.0

    state = load_account_state()
    month_key = f"{now.year:04d}-{now.month:02d}"
    if state.get("last_monthly_contribution") == month_key:
        return cash_balance, 0.0

    state["last_monthly_contribution"] = month_key
    state["total_contributed_usd"] = float(state.get("total_contributed_usd", 0.0)) + amount
    state.setdefault("contributions", []).append({
        "time": now.isoformat(),
        "month": month_key,
        "amount_usd": amount,
    })
    save_account_state(state)
    return cash_balance + amount, amount
