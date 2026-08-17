#!/usr/bin/env python3
"""
Standalone launcher for the Blackboard Scraper Telegram Bot Daemon.
Usage:
  python3 telegram_bot.py
"""

import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from telegram.bot import run_bot

if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\n👋 Telegram Bot stopped.")
