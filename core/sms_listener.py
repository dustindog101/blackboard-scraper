import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("blackboard.sms_listener")

# Known UMBC Duo SMS shortcodes & senders
KNOWN_SENDERS = ["386732", "386767", "DUO", "duo"]
# Regex patterns for Duo SMS passcodes
PASSCODE_REGEXES = [
    re.compile(r"UMBC\s+SMS\s+passcode.*?:\s*(\d{6,7})", re.IGNORECASE),
    re.compile(r"Duo\s+passcode.*?:\s*(\d{6,7})", re.IGNORECASE),
    re.compile(r"passcode\s+(\d{6,7})\s+to\s+log\s+in", re.IGNORECASE),
    re.compile(r"UMBC.*?code.*?:\s*(\d{6,7})", re.IGNORECASE),
    re.compile(r"\b(\d{7})\b"),  # UMBC 7-digit SMS codes
    re.compile(r"\b(\d{6})\b"),  # Standard 6-digit codes
]

# macOS Cocoa timestamp epoch offset (seconds between 1970-01-01 and 2001-01-01)
COCOA_EPOCH_OFFSET = 978307200


def get_latest_duo_sms_sqlite(after_unix_timestamp: Optional[float] = None) -> Optional[Tuple[str, float, str]]:
    """
    Directly query macOS Messages SQLite database (~2-5ms) for incoming 2FA passcodes.
    Returns: Tuple[passcode, unix_timestamp, sender] or None
    """
    db_path = Path.home() / "Library" / "Messages" / "chat.db"
    if not db_path.exists():
        return None

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        cursor = conn.cursor()

        query = """
        SELECT 
            m.ROWID,
            m.text,
            m.date,
            h.id as sender
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.text IS NOT NULL AND (m.text LIKE '%UMBC%' OR m.text LIKE '%passcode%' OR m.text LIKE '%Duo%')
        ORDER BY m.date DESC
        LIMIT 10
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        conn.close()

        for rowid, text, apple_date, sender in rows:
            if not text:
                continue

            # Convert Apple nanoseconds timestamp to standard Unix timestamp (seconds)
            unix_ts = (apple_date / 1_000_000_000) + COCOA_EPOCH_OFFSET

            # If filtering by start timestamp, skip older messages (allow 5s clock skew)
            if after_unix_timestamp and unix_ts < (after_unix_timestamp - 5.0):
                continue

            for pattern in PASSCODE_REGEXES:
                match = pattern.search(text)
                if match:
                    code = match.group(1)
                    return code, unix_ts, sender or "UMBC Duo"

    except Exception as e:
        logger.debug(f"SQLite SMS query note: {e}")

    return None


def get_latest_duo_sms_imsg(after_unix_timestamp: Optional[float] = None) -> Optional[Tuple[str, float, str]]:
    """
    Fallback method using `imsg` CLI tool to query recent messages.
    """
    imsg_bin = "/opt/homebrew/bin/imsg"
    if not os.path.exists(imsg_bin):
        return None

    try:
        proc = subprocess.run(
            [imsg_bin, "search", "--query", "UMBC SMS passcode", "--limit", "3", "--json"],
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
                    sender = data.get("sender", "386732")

                    # Parse ISO timestamp
                    if created_at_str:
                        dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                        unix_ts = dt.timestamp()
                        if after_unix_timestamp and unix_ts < (after_unix_timestamp - 5.0):
                            continue
                    else:
                        unix_ts = time.time()

                    for pattern in PASSCODE_REGEXES:
                        match = pattern.search(text)
                        if match:
                            return match.group(1), unix_ts, sender

                except Exception:
                    continue
    except Exception as e:
        logger.debug(f"imsg fallback search error: {e}")

    return None


def wait_for_duo_sms_passcode(
    after_unix_timestamp: float,
    timeout_seconds: int = 35,
    poll_interval: float = 0.8,
) -> Optional[str]:
    """
    Polls for a newly arrived Duo SMS message on macOS Messages.
    Returns the extracted 6 or 7 digit passcode string.
    """
    print(f"⏳ Listening for incoming Duo SMS passcode on macOS Messages (timeout: {timeout_seconds}s)...")
    start_time = time.time()

    while time.time() - start_time < timeout_seconds:
        # 1. Try high-speed direct SQLite query (<5ms)
        res = get_latest_duo_sms_sqlite(after_unix_timestamp)
        if res:
            code, ts, sender = res
            elapsed = round(time.time() - start_time, 1)
            print(f"📩 \033[32mCaptured SMS Passcode: [{code}]\033[0m from {sender} (received in {elapsed}s)!")
            return code

        # 2. Try imsg CLI fallback
        res_imsg = get_latest_duo_sms_imsg(after_unix_timestamp)
        if res_imsg:
            code, ts, sender = res_imsg
            elapsed = round(time.time() - start_time, 1)
            print(f"📩 \033[32mCaptured SMS Passcode via imsg: [{code}]\033[0m from {sender} (received in {elapsed}s)!")
            return code

        time.sleep(poll_interval)

    print(f"⚠️  Timed out waiting for Duo SMS passcode after {timeout_seconds}s.")
    return None
