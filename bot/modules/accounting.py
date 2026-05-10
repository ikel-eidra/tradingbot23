"""Paper-account contribution tracking."""

import json
from datetime import datetime, timezone

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
    if now.day != day:
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
