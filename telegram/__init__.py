"""
Telegram Integration & Bot Control Layer for Blackboard Ultra Scraper.
Decoupled, modular add-on for push alerts and interactive chat bot control.
"""

from telegram.config import get_telegram_config
from telegram.formatter import (
    escape_html,
    chunk_message,
    format_daily_briefing,
    format_urgent_due_alert,
    format_grade_alert,
    format_announcement_alert,
)
from telegram.notifier import TelegramNotifier

__all__ = [
    "get_telegram_config",
    "escape_html",
    "chunk_message",
    "format_daily_briefing",
    "format_urgent_due_alert",
    "format_grade_alert",
    "format_announcement_alert",
    "TelegramNotifier",
]
