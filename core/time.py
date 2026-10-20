"""Date parsing and local-time formatting shared by every Blackboard command (coding standards, section 3)."""
import os
import re
from datetime import datetime, timezone, tzinfo
from typing import Optional

_LEGACY_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%m/%d/%y, %I:%M %p",
    "%m/%d/%Y, %I:%M %p",
    "%m/%d/%y",
    "%m/%d/%Y",
    "%B %d, %Y at %I:%M %p",
    "%b %d, %Y %I:%M %p",
)


def local_zone() -> tzinfo:
    """Zone used for display: BB_TZ env, then config.json `timezone`, then America/New_York, then system."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from core.config import load_config

    name = os.environ.get("BB_TZ") or load_config().get("timezone") or "America/New_York"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now().astimezone().tzinfo or timezone.utc


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp or a legacy Blackboard date string. Returns an aware datetime or None."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.upper() in ("TBD", "--", "UNKNOWN DATE"):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.replace(tzinfo=local_zone()) if dt.tzinfo is None else dt
    except ValueError:
        pass
    zone: tzinfo = timezone.utc if text.endswith(" UTC") else local_zone()
    text = re.sub(r"\s*(?:\([A-Z0-9_/-]+\)|UTC|EDT|EST)\s*$", "", text).strip()
    for fmt in _LEGACY_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=zone)
        except ValueError:
            continue
    return None


def to_iso(value: Optional[str]) -> Optional[str]:
    """ISO-8601 UTC timestamp (`due_at`) for any parseable date string, else None."""
    dt = parse_datetime(value)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def format_local(dt: Optional[datetime], *, with_zone: bool = True) -> str:
    """`Tue Oct 21 11:59 PM EDT` (year added only when it is not the current one). No platform-specific strftime flags."""
    if dt is None:
        return "TBD"
    dt = dt.astimezone(local_zone())
    hour = dt.hour % 12 or 12
    text = f"{dt:%a %b} {dt.day}"
    if dt.year != datetime.now(dt.tzinfo).year:
        text += f" {dt.year}"
    text += f" {hour}:{dt:%M %p}"
    return f"{text} {dt:%Z}" if with_zone else text


def format_clock(dt: datetime) -> str:
    """`11:59 PM` in local time."""
    dt = dt.astimezone(local_zone())
    return f"{dt.hour % 12 or 12}:{dt:%M %p}"
