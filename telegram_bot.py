#!/usr/bin/env python3
"""
Standalone launcher for the Blackboard Scraper Telegram Bot Daemon.
Usage:
  python3 telegram_bot.py            # Run in foreground
  python3 telegram_bot.py --daemon   # Run in background detached
  python3 telegram_bot.py --status   # Check running status and memory
  python3 telegram_bot.py --stop     # Gracefully stop daemon
  python3 telegram_bot.py --restart  # Restart daemon
"""

import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from telegram.bot import run_bot
from telegram.daemon import start_bot_daemon, stop_bot_daemon, restart_bot_daemon, get_bot_status
from core.session import quick_check_session_http
from core.config import load_courses


def main():
    if "--daemon" in sys.argv or "-d" in sys.argv:
        start_bot_daemon()
    elif "--stop" in sys.argv:
        stop_bot_daemon()
    elif "--restart" in sys.argv:
        restart_bot_daemon()
    elif "--status" in sys.argv:
        status = get_bot_status()
        if status["running"]:
            is_valid, _ = quick_check_session_http()
            sess_str = "✅ ACTIVE" if is_valid else "❌ EXPIRED"
            courses = load_courses()
            print("\n🤖 Telegram Bot Daemon Status:")
            print(f"  • State:       🟢 RUNNING (PID: {status['pid']})")
            print(f"  • Memory:      {status['memory_mb']} MB (RSS)")
            print(f"  • Session:     {sess_str}")
            print(f"  • Courses:     {len(courses)} configured")
            print(f"  • Log File:    {status['log_file']}\n")
        else:
            print("\n🤖 Telegram Bot Daemon: 🔴 STOPPED\n   Run `python3 telegram_bot.py --daemon` to start.\n")
    else:
        try:
            run_bot()
        except KeyboardInterrupt:
            print("\n👋 Telegram Bot stopped.")


if __name__ == "__main__":
    main()
