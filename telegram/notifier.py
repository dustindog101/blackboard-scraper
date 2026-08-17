import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.parse
import urllib.request

from telegram.config import get_telegram_config
from telegram.formatter import (
    format_announcement_alert,
    format_daily_briefing,
    format_grade_alert,
    format_urgent_due_alert,
)

logger = logging.getLogger("blackboard.telegram.notifier")


class TelegramNotifier:
    """
    Outbound Telegram notification client and interactive response poller.
    Uses standard library urllib (zero required external packages).
    """

    def __init__(self):
        self.config = get_telegram_config()
        self.bot_token = self.config.get("bot_token")
        self.admin_chat_id = self.config.get("admin_chat_id")
        self.enabled = self.config.get("enabled", False)
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else ""
        self.state_file = Path(__file__).parent.parent / ".session" / "telegram_state.json"

    def send_raw_message(
        self,
        text: str,
        chat_id: Optional[Any] = None,
        parse_mode: str = "HTML",
        silent: bool = False,
    ) -> bool:
        """Send message via Telegram HTTP API with retry on rate limits."""
        target_chat = chat_id or self.admin_chat_id
        if not self.enabled and not (self.bot_token and target_chat):
            return False

        url = f"{self.api_base}/sendMessage"
        payload = {
            "chat_id": target_chat,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": silent,
            "disable_web_page_preview": True,
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status == 200
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry_after = int(e.headers.get("Retry-After", 5))
                    time.sleep(retry_after)
                    continue
                logger.debug(f"Telegram API Error {e.code}: {e.read().decode('utf-8')}")
                return False
            except Exception as e:
                logger.debug(f"Failed to send Telegram message: {e}")
                time.sleep(1)
        return False

    def poll_for_passcode(self, timeout_sec: int = 120, stop_event: Optional[Any] = None) -> Optional[str]:
        """
        Polls Telegram getUpdates for an incoming 6-digit Duo passcode from admin_chat_id.
        Safely returns None if disabled, on network error, or if timeout expires.
        """
        if not self.enabled or not self.bot_token or not self.admin_chat_id:
            return None

        # Fetch latest update_id offset to avoid reading old messages
        offset = 0
        try:
            url = f"{self.api_base}/getUpdates?limit=1&offset=-1"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("ok") and data.get("result"):
                    offset = data["result"][-1]["update_id"] + 1
        except Exception:
            pass

        start_time = time.time()
        while time.time() - start_time < timeout_sec:
            if stop_event and stop_event.is_set():
                return None

            try:
                poll_url = f"{self.api_base}/getUpdates?offset={offset}&timeout=2"
                with urllib.request.urlopen(poll_url, timeout=6) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("ok") and data.get("result"):
                        for upd in data["result"]:
                            offset = max(offset, upd["update_id"] + 1)
                            msg = upd.get("message", {})
                            from_id = msg.get("chat", {}).get("id") or msg.get("from", {}).get("id")

                            # Check if message is from configured admin
                            if str(from_id) == str(self.admin_chat_id):
                                text = (msg.get("text") or "").strip()
                                # Look for 6-digit passcode pattern
                                match = re.search(r"\b(\d{6})\b", text)
                                if match:
                                    return match.group(1)
            except Exception:
                pass

            time.sleep(1.0)
        return None

    def send_chunks(self, chunks: List[str], chat_id: Optional[Any] = None) -> bool:
        """Send sequence of message chunks with slight spacing."""
        success = True
        for chunk in chunks:
            if not self.send_raw_message(chunk, chat_id=chat_id):
                success = False
            time.sleep(0.3)
        return success

    def notify_briefing(self, briefing_data: Dict[str, Any], chat_id: Optional[Any] = None) -> bool:
        """Send formatted daily briefing to Telegram."""
        chunks = format_daily_briefing(briefing_data)
        return self.send_chunks(chunks, chat_id=chat_id)

    def process_and_notify_diffs(self, current_data: Dict[str, Any], chat_id: Optional[Any] = None):
        """
        Diffs current scraped state against cached state to trigger push alerts
        for newly posted grades and announcements.
        """
        if not self.enabled:
            return

        state: Dict[str, Any] = {}
        if self.state_file.exists():
            try:
                state = json.loads(self.state_file.read_text())
            except Exception:
                state = {}

        seen_grades = set(state.get("grades", []))
        seen_announcements = set(state.get("announcements", []))

        new_grades = []
        new_announcements = []

        courses = current_data.get("courses", {})
        for cid, cdata in courses.items():
            if not isinstance(cdata, dict):
                continue
            cname = cdata.get("course_name", cid)

            for g in cdata.get("grades", []):
                gid = f"{cid}:{g.get('name')}:{g.get('grade')}"
                if gid not in seen_grades and g.get("grade") and g["grade"] not in ("Not graded", "-- %", ""):
                    seen_grades.add(gid)
                    new_grades.append((g, cname))

            for a in cdata.get("announcements", []):
                aid = f"{cid}:{a.get('title')}:{a.get('meta')}"
                if aid not in seen_announcements:
                    seen_announcements.add(aid)
                    new_announcements.append((a, cname))

        # Deliver push alerts
        for g, cname in new_grades:
            self.send_raw_message(format_grade_alert(g, cname), chat_id=chat_id)
            time.sleep(0.3)

        for a, cname in new_announcements:
            self.send_raw_message(format_announcement_alert(a, cname), chat_id=chat_id)
            time.sleep(0.3)

        # Persist updated cache
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({
            "grades": list(seen_grades),
            "announcements": list(seen_announcements),
            "last_updated": time.time(),
        }, indent=2))
