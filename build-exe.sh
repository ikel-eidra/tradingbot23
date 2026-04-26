#!/usr/bin/env bash
# ============================================
# Build TradingBot23.exe
#
# Run this ON WINDOWS (Git Bash / PowerShell)
# or on Linux to get a Linux binary.
#
# The .exe your friend double-clicks:
#   1. Pops up a setup wizard for API keys
#   2. Starts paper trading
# ============================================

set -e

echo "Installing build dependencies..."
pip install pyinstaller

echo ""
echo "Installing bot dependencies..."
pip install -r requirements.txt

echo ""
echo "Building executable..."
pyinstaller tradingbot23.spec --clean

echo ""
echo "====================================="
echo "  Build complete!"
echo "====================================="
echo ""
echo "  Output: dist/TradingBot23.exe"
echo ""
echo "  To share with your friend:"
echo "    1. Copy dist/TradingBot23.exe to a folder"
echo "    2. Send the folder (or zip it)"
echo "    3. Friend double-clicks TradingBot23.exe"
echo "    4. Setup wizard asks for API keys"
echo "    5. Bot starts paper trading!"
echo ""
