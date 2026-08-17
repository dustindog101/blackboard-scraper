#!/usr/bin/env python3
"""
Launcher for the Blackboard Ultra & Telegram Bot macOS Menubar App.
Usage:
  python3 menubar.py
"""

import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from ui.menubar import run_menubar

if __name__ == "__main__":
    try:
        run_menubar()
    except KeyboardInterrupt:
        print("\n👋 Menubar app closed.")
