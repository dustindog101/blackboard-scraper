import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("blackboard.sms_listener")

# Ranked regex patterns for Duo / UMBC SMS passcodes
# Top priority: explicit UMBC/Duo passcode phrasing
HIGH_PRIORITY_PATTERNS = [
    re.compile(r"UMBC\s+SMS\s+passcode.*?:\s*(\d{6,8})", re.IGNORECASE),
    re.compile(r"Duo\s+passcode.*?:\s*(\d{6,8})", re.IGNORECASE),
    re.compile(r"passcode\s+(\d{6,8})\s+to\s+log\s+in", re.IGNORECASE),
    re.compile(r"UMBC.*?code.*?:\s*(\d{6,8})", re.IGNORECASE),
]

# Medium priority: generic 2FA security codes
MEDIUM_PRIORITY_PATTERNS = [
    re.compile(r"(?:passcode|verification code|security code|login code|auth code|pin)[:\s]+(\d{6,8})", re.IGNORECASE),
    re.compile(r"(?:code|passcode)\s+is[:\s]+(\d{6,8})", re.IGNORECASE),
    re.compile(r"\b(\d{7})\b"),  # UMBC 7-digit SMS code
    re.compile(r"\b(\d{6})\b"),  # Standard 6-digit code
]

# macOS Cocoa timestamp epoch offset (seconds between 1970-01-01 and 2001-01-01)
COCOA_EPOCH_OFFSET = 978307200


def get_latest_duo_sms_sqlite(after_unix_timestamp: Optional[float] = None) -> Optional[Tuple[str, float, str, str]]:
    """
    Directly query macOS Messages SQLite database (~2-5ms) for incoming 2FA passcodes.
    Accepts ANY sender number (sender numbers dynamically change).
    Returns: Tuple[passcode, unix_timestamp, sender, raw_text] or None
    """
    db_path = Path.home() / "Library" / "Messages" / "chat.db"
    if not db_path.exists():
        return None

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        cursor = conn.cursor()

        # Query recent incoming messages regardless of sender
        query = """
        SELECT 
            m.ROWID,
            m.text,
            m.date,
            COALESCE(h.id, 'SMS') as sender,
            m.is_from_me
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.text IS NOT NULL AND m.is_from_me = 0
        ORDER BY m.date DESC
        LIMIT 20
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        conn.close()

        # Check high priority patterns first
        for rowid, text, apple_date, sender, is_from_me in rows:
            if not text:
                continue

            unix_ts = (apple_date / 1_000_000_000) + COCOA_EPOCH_OFFSET

            # Allow 4s clock skew buffer if start timestamp is provided
            if after_unix_timestamp and unix_ts < (after_unix_timestamp - 4.0):
                continue

            for pattern in HIGH_PRIORITY_PATTERNS:
                match = pattern.search(text)
                if match:
                    code = match.group(1)
                    return code, unix_ts, sender, text

        # Check medium priority patterns
        for rowid, text, apple_date, sender, is_from_me in rows:
            if not text:
                continue

            unix_ts = (apple_date / 1_000_000_000) + COCOA_EPOCH_OFFSET
            if after_unix_timestamp and unix_ts < (after_unix_timestamp - 4.0):
                continue

            for pattern in MEDIUM_PRIORITY_PATTERNS:
                match = pattern.search(text)
                if match:
                    code = match.group(1)
                    return code, unix_ts, sender, text

    except Exception as e:
        logger.debug(f"SQLite SMS query note: {e}")

    return None


def get_latest_duo_sms_imsg(after_unix_timestamp: Optional[float] = None) -> Optional[Tuple[str, float, str, str]]:
    """
    Fallback method using `imsg` CLI tool to search recent messages.
    """
    imsg_bin = "/opt/homebrew/bin/imsg"
    if not os.path.exists(imsg_bin):
        return None

    try:
        proc = subprocess.run(
            [imsg_bin, "search", "--query", "passcode", "--limit", "5", "--json"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode == 0 and proc.stdout:
            for line in proc.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    text = data.get("text", "")
                    created_at_str = data.get("created_at", "")
                    sender = data.get("sender", "SMS")

                    if created_at_str:
                        dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                        unix_ts = dt.timestamp()
                        if after_unix_timestamp and unix_ts < (after_unix_timestamp - 4.0):
                            continue
                    else:
                        unix_ts = time.time()

                    for pattern in HIGH_PRIORITY_PATTERNS + MEDIUM_PRIORITY_PATTERNS:
                        match = pattern.search(text)
                        if match:
                            return match.group(1), unix_ts, sender, text

                except Exception:
                    continue
    except Exception as e:
        logger.debug(f"imsg fallback search error: {e}")

    return None


def wait_for_duo_sms_passcode(
    after_unix_timestamp: float,
    timeout_seconds: int = 40,
    poll_interval: float = 0.6,
) -> Optional[str]:
    """
    Actively listens for a newly arrived Duo SMS message on macOS Messages.
    Accepts ANY incoming sender number and automatically extracts the 6 or 7 digit code.
    """
    print(f"⏳ Listening for incoming 2FA SMS on macOS Messages (dynamic sender, timeout: {timeout_seconds}s)...")
    start_time = time.time()

    while time.time() - start_time < timeout_seconds:
        # 1. High-speed direct SQLite query (<3ms)
        res = get_latest_duo_sms_sqlite(after_unix_timestamp)
        if res:
            code, ts, sender, raw_text = res
            elapsed = round(time.time() - start_time, 1)
            print(f"📩 \033[32mAuto-Detected SMS Code: [{code}]\033[0m (from sender '{sender}' in {elapsed}s)!")
            return code

        # 2. imsg CLI fallback
        res_imsg = get_latest_duo_sms_imsg(after_unix_timestamp)
        if res_imsg:
            code, ts, sender, raw_text = res_imsg
            elapsed = round(time.time() - start_time, 1)
            print(f"📩 \033[32mAuto-Detected SMS Code via imsg: [{code}]\033[0m (from sender '{sender}' in {elapsed}s)!")
            return code

        time.sleep(poll_interval)

    print(f"⚠️  Timed out waiting for Duo SMS passcode after {timeout_seconds}s.")
    return None
