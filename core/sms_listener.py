import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("blackboard.sms_listener")

# Ranked regex patterns for Duo / UMBC SMS passcodes
HIGH_PRIORITY_PATTERNS = [
    re.compile(r"UMBC\s+SMS\s+passcode.*?:\s*([0-9]{6,8})(?![0-9])", re.IGNORECASE),
    re.compile(r"Duo\s+passcode.*?:\s*([0-9]{6,8})(?![0-9])", re.IGNORECASE),
    re.compile(r"passcode\s+([0-9]{6,8})(?![0-9])\s+to\s+log\s+in", re.IGNORECASE),
    re.compile(r"UMBC.*?code.*?:\s*([0-9]{6,8})(?![0-9])", re.IGNORECASE),
]

MEDIUM_PRIORITY_PATTERNS = [
    re.compile(r"(?:passcode|verification code|security code|login code|auth code|pin)[:\s]+([0-9]{6,8})(?![0-9])", re.IGNORECASE),
    re.compile(r"(?:code|passcode)\s+is[:\s]+([0-9]{6,8})(?![0-9])", re.IGNORECASE),
    re.compile(r"\b(\d{7})\b"),  # UMBC 7-digit SMS code
    re.compile(r"\b(\d{6})\b"),  # Standard 6-digit code
]

PASSCODE_REGEXES = HIGH_PRIORITY_PATTERNS + MEDIUM_PRIORITY_PATTERNS

COCOA_EPOCH_OFFSET = 978307200


def normalize_passcode(value: str) -> Optional[str]:
    """Validate intentional passcode entry without accepting partial numbers."""
    value = str(value).strip()
    return value if re.fullmatch(r"[0-9]{6,8}", value) else None


def extract_duo_passcode(text: str) -> Optional[str]:
    """Only auto-submit messages that identify Duo/UMBC and a code."""
    if not isinstance(text, str) or not re.search(r"\b(?:UMBC|Duo)\b", text, re.I):
        return None
    if not re.search(r"\b(?:passcode|code)\b", text, re.I):
        return None
    for pattern in PASSCODE_REGEXES:
        match = pattern.search(text)
        if match:
            return normalize_passcode(match.group(1))
    return None


def _messages_connection():
    db_path = Path.home() / "Library" / "Messages" / "chat.db"
    return sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=2.0)


def _search_imsg():
    imsg_bin = "/opt/homebrew/bin/imsg"
    if not os.path.exists(imsg_bin):
        return None
    return subprocess.run(
        [imsg_bin, "search", "--query", "passcode", "--limit", "20", "--json"],
        capture_output=True, text=True, timeout=3,
    )


def check_sms_access() -> Tuple[bool, str]:
    """Probe once so permission failures do not look like delayed SMS delivery."""
    if sys.platform != "darwin":
        return False, "Automatic Messages capture is available only on macOS."
    try:
        with closing(_messages_connection()) as conn:
            conn.execute("SELECT MAX(ROWID) FROM message").fetchone()
        return True, ""
    except (sqlite3.Error, OSError):
        # imsg may have its own macOS permission, independently of Python.
        try:
            proc = _search_imsg()
            if proc is not None and proc.returncode == 0:
                return True, ""
        except (OSError, subprocess.SubprocessError):
            pass
    return False, (
        "Cannot read macOS Messages. Grant Full Disk Access to the app running bb "
        "in System Settings > Privacy & Security > Full Disk Access, then restart it. "
        "You can enter the passcode manually or use 'bb login --manual'."
    )


def ensure_messages_sync() -> bool:
    """Start Messages in the background so incoming phone SMS can sync."""
    if sys.platform != "darwin":
        return False
    try:
        running = subprocess.run(["/usr/bin/pgrep", "-x", "Messages"], capture_output=True, timeout=2)
        if running.returncode == 0:
            return True
        launched = subprocess.run(
            ["/usr/bin/open", "-g", "-j", "-a", "Messages"], capture_output=True, timeout=3,
        )
        return launched.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def get_current_max_rowid() -> int:
    """Snapshot Messages immediately before requesting a new SMS."""
    if sys.platform != "darwin":
        return 0
    try:
        with closing(_messages_connection()) as conn:
            row = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()
        return row[0] if row and row[0] is not None else 0
    except (sqlite3.Error, OSError):
        return 0


def _fresh_message(unix_ts: float, after: Optional[float], has_rowid: bool = False) -> bool:
    # ROWID remains the primary clock: tolerate carrier clock drift, but not an
    # arbitrarily old message synced after the baseline. Time-only fallback is
    # deliberately tighter because it cannot establish message arrival order.
    tolerance = 300.0 if has_rowid else 15.0
    return (after is None or unix_ts >= after - tolerance) and unix_ts <= time.time() + 300.0


def get_latest_duo_sms_sqlite(
    start_rowid: Optional[int] = None,
    after_unix_timestamp: Optional[float] = None,
    excluded_codes: Optional[set[str]] = None,
) -> Optional[Tuple[str, float, str, str]]:
    """Read incoming Duo/UMBC codes after the ROWID snapshot, newest first."""
    if sys.platform != "darwin":
        return None
    try:
        with closing(_messages_connection()) as conn:
            rows = conn.execute(
                """
                SELECT m.text, m.attributedBody, m.date, COALESCE(h.id, 'SMS')
                FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE m.is_from_me = 0 AND m.ROWID > ?
                ORDER BY m.ROWID DESC LIMIT 50
                """, (start_rowid if start_rowid is not None else 0,),
            ).fetchall()
        for raw_text, attr_blob, apple_date, sender in rows:
            text = raw_text or " ".join(
                chunk.decode("utf-8", errors="ignore")
                for chunk in re.findall(b"[\x20-\x7E]{3,}", attr_blob or b"")
            )
            if apple_date is None:
                continue
            unix_ts = apple_date / 1_000_000_000 + COCOA_EPOCH_OFFSET
            if not _fresh_message(unix_ts, after_unix_timestamp, bool(start_rowid)):
                continue
            code = extract_duo_passcode(text)
            if code and code not in (excluded_codes or ()):
                return code, unix_ts, sender, text
    except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
        logger.debug("SQLite SMS query unavailable: %s", type(exc).__name__)
    return None


def get_latest_duo_sms_imsg(
    after_unix_timestamp: Optional[float] = None,
    excluded_codes: Optional[set[str]] = None,
) -> Optional[Tuple[str, float, str, str]]:
    """Read dated, incoming Duo messages from imsg's JSON or JSONL output."""
    if sys.platform != "darwin":
        return None
    try:
        proc = _search_imsg()
        if proc is None or proc.returncode != 0 or not proc.stdout:
            return None
        try:
            data = json.loads(proc.stdout)
            rows = data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            rows = []
            for line in proc.stdout.splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        candidates = []
        for row in rows:
            if not isinstance(row, dict) or row.get("is_from_me") is not False:
                continue
            try:
                created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                if created.tzinfo is None:
                    continue
                unix_ts = created.timestamp()
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
            if not _fresh_message(unix_ts, after_unix_timestamp):
                continue
            text = row.get("text") or ""
            code = extract_duo_passcode(text)
            if code and code not in (excluded_codes or ()):
                candidates.append((code, unix_ts, row.get("sender", "SMS"), text))
        return max(candidates, key=lambda item: item[1]) if candidates else None
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("imsg SMS query unavailable: %s", type(exc).__name__)
    return None


def wait_for_duo_sms_passcode(
    start_rowid: Optional[int] = None,
    after_unix_timestamp: Optional[float] = None,
    timeout_seconds: int = 90,
    poll_interval: float = 0.5,
    excluded_codes: Optional[set[str]] = None,
    stop_event=None,
    access_checked: bool = False,
) -> Optional[str]:
    """Listen for a fresh Duo SMS; cancellation never logs the code or message."""
    if sys.platform != "darwin":
        return None
    if not access_checked:
        available, reason = check_sms_access()
        if not available:
            print(f"   ⚠️ SMS auto-capture unavailable: {reason}")
            return None
    started = time.monotonic()
    last_ping = started
    while time.monotonic() - started < timeout_seconds:
        if stop_event is not None and stop_event.is_set():
            return None
        result = get_latest_duo_sms_sqlite(start_rowid, after_unix_timestamp, excluded_codes)
        if result is None:
            result = get_latest_duo_sms_imsg(after_unix_timestamp, excluded_codes)
        if result is not None:
            if stop_event is not None and stop_event.is_set():
                return None
            print(f"   📩 Duo SMS auto-captured after {time.monotonic() - started:.1f}s.")
            return result[0]
        if time.monotonic() - last_ping >= 10.0:
            print(f"   ↳ Waiting for a new Duo SMS... ({time.monotonic() - started:.0f}s)")
            last_ping = time.monotonic()
        remaining = timeout_seconds - (time.monotonic() - started)
        delay = min(poll_interval, max(0, remaining))
        if stop_event is not None:
            if stop_event.wait(delay):
                return None
        else:
            time.sleep(delay)
    print(f"   ⚠️ No new Duo SMS detected within {timeout_seconds}s; manual entry remains available.")
    return None
