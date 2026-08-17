import os
from typing import Any, Dict
from core.config import load_config


def get_telegram_config() -> Dict[str, Any]:
    """
    Loads Telegram configuration from config.json (telegram block)
    with environment variable fallbacks.
    """
    base_config = load_config()
    tg_config = base_config.get("telegram", {})

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or tg_config.get("bot_token")
    admin_chat_id_raw = os.getenv("TELEGRAM_ADMIN_CHAT_ID") or tg_config.get("admin_chat_id")

    admin_chat_id = None
    if admin_chat_id_raw:
        try:
            admin_chat_id = int(admin_chat_id_raw)
        except ValueError:
            admin_chat_id = str(admin_chat_id_raw)

    enabled_env = os.getenv("TELEGRAM_NOTIFY_ENABLED")
    if enabled_env is not None:
        enabled = enabled_env.lower() in ("true", "1", "yes")
    else:
        enabled = tg_config.get("enabled", False)

    # Must have both token and admin chat ID to be active
    is_active = bool(enabled and bot_token and admin_chat_id)

    allowed_chats = tg_config.get("allowed_chat_ids", [])
    if admin_chat_id and admin_chat_id not in allowed_chats:
        allowed_chats.append(admin_chat_id)

    return {
        "enabled": is_active,
        "bot_token": bot_token,
        "admin_chat_id": admin_chat_id,
        "allowed_chat_ids": allowed_chats,
        "parse_mode": tg_config.get("parse_mode", "HTML"),
        "silent_notifications": tg_config.get("silent_notifications", False),
        "notifications": tg_config.get("notifications", {
            "daily_briefing": {"enabled": True},
            "urgent_due_alerts": {"enabled": True, "threshold_hours": 36},
            "grade_updates": {"enabled": True},
            "announcements": {"enabled": True},
        }),
    }
