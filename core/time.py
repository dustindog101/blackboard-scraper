"""Date parsing and timezone-aware formatting shared by Blackboard surfaces."""
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def local_zone():
    from core.config import load_config
    name = os.environ.get('BB_TZ') or load_config().get('timezone') or 'America/New_York'
    return ZoneInfo(name)


def parse_datetime(value):
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
        return dt.replace(tzinfo=local_zone()) if dt.tzinfo is None else dt
    except ValueError:
        pass
    zone = timezone.utc if text.endswith(' UTC') else local_zone()
    text = re.sub(r'\s*(?:\([A-Z0-9_/-]+\)|UTC|EDT|EST)\s*$', '', text).strip()
    for fmt in ('%Y-%m-%d %H:%M', '%m/%d/%y, %I:%M %p', '%m/%d/%Y, %I:%M %p',
                '%m/%d/%y', '%m/%d/%Y', '%B %d, %Y at %I:%M %p', '%b %d, %Y %I:%M %p'):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=zone)
        except ValueError:
            continue
    return None


def due_at(value):
    dt = parse_datetime(value)
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if dt else None


def format_local(value):
    dt = parse_datetime(value)
    return dt.astimezone(local_zone()).strftime('%-m/%-d/%y, %-I:%M %p %Z') if dt else (value or 'TBD')


def add_due_fields(data):
    """Normalize dated items including browser fallback output at the export boundary."""
    if isinstance(data, list):
        return [add_due_fields(item) for item in data]
    if not isinstance(data, dict):
        return data
    result = {key: add_due_fields(value) for key, value in data.items()}
    for key in ('due_at', 'raw_due', 'due_date', 'dueDate', 'due'):
        if isinstance(data.get(key), str):
            result['due_at'] = due_at(data[key])
            if result['due_at']:
                break
    return result
