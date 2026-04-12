#!/usr/bin/env bash
# ============================================
# TradingBot23 — Add a User
# Creates a per-user config + isolated data/logs
# ============================================

set -e

echo ""
echo "====================================="
echo "  TradingBot23 — Add User"
echo "====================================="
echo ""

read -rp "Username (no spaces, e.g. ikel, marco): " username

if [[ -z "$username" ]]; then
    echo "Error: username cannot be empty"
    exit 1
fi

# Sanitize — lowercase, alphanumeric + underscore only
username=$(echo "$username" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_]//g')

USER_DIR="users/${username}"
ENV_FILE="${USER_DIR}/.env"

if [ -f "$ENV_FILE" ]; then
    read -rp "${username} already exists. Overwrite config? (y/N): " overwrite
    if [[ ! "$overwrite" =~ ^[Yy]$ ]]; then
        echo "Keeping existing config. Done."
        exit 0
    fi
fi

mkdir -p "${USER_DIR}/data" "${USER_DIR}/logs"

echo ""
echo "Setting up ${username}..."
echo ""
echo "You need 2 API keys (both are FREE):"
echo ""
echo "  1. Binance API key    — https://www.binance.com/en/my/settings/api-management"
echo "     (Create key with 'Read Only' — no trading permission needed for paper mode)"
echo ""
echo "  2. CoinMarketCap key  — https://pro.coinmarketcap.com/signup"
echo "     (Free tier = 10,000 calls/month, more than enough)"
echo ""
echo "-------------------------------------"

read -rp "Paste ${username}'s BINANCE API KEY: " binance_key
read -rp "Paste ${username}'s BINANCE API SECRET: " binance_secret
read -rp "Paste ${username}'s COINMARKETCAP API KEY: " cmc_key

echo ""
echo "-------------------------------------"
echo "Choose engine:"
echo "  1) Spot (default — no leverage, simpler)"
echo "  2) Futures (paper-only, 2x leverage)"
read -rp "Engine [1]: " engine_choice

if [[ "$engine_choice" == "2" ]]; then
    engine="futures"
else
    engine="spot"
fi

read -rp "Starting capital in USD [10000]: " capital
capital=${capital:-10000}

# Write per-user .env
cat > "$ENV_FILE" << ENVEOF
# TradingBot23 config for: ${username}
BINANCE_API_KEY=${binance_key}
BINANCE_API_SECRET=${binance_secret}
CMC_API_KEY=${cmc_key}

TRADING_MODE=paper
ENGINE=${engine}
CAPITAL_USD=${capital}

# Strategy defaults
NET_TP_PCT=0.01
NET_SL_PCT=0.015
FEE_PCT=0.001
MAX_HOLD_DAYS=3
DIP_THRESHOLD_PCT=0.02
PER_TRADE_PCT=0.20
CHECK_INTERVAL_HOURS=1

# Futures settings (only if ENGINE=futures)
LEVERAGE=2
FUTURES_NET_TP_PCT=0.005
FUTURES_NET_SL_PCT=0.0075
FUTURES_DIP_THRESHOLD_PCT=0.005

LOG_LEVEL=INFO
ENVEOF

chmod 600 "$ENV_FILE"

echo ""
echo "====================================="
echo "  User '${username}' created!"
echo "====================================="
echo ""
echo "  Config:  ${ENV_FILE}"
echo "  Data:    ${USER_DIR}/data/"
echo "  Logs:    ${USER_DIR}/logs/"
echo ""
echo "To start this user's bot:"
echo ""
echo "  docker compose --profile ${username} up -d"
echo "  docker compose --profile ${username} logs -f"
echo ""
echo "To start ALL users:"
echo ""
echo "  docker compose --profile '*' up -d"
echo ""
