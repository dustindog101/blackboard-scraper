import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from core.config import SESSION_DIR

logger = logging.getLogger("blackboard.session_tracker")
TELEMETRY_FILE = SESSION_DIR / "session_telemetry.json"


def _format_seconds(seconds: float) -> str:
    """Format duration in seconds to human readable string (e.g. '8h 24m 10s')."""
    if seconds < 0:
        seconds = 0
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hrs > 0:
        return f"{hrs}h {mins}m {secs}s" if mins > 0 else f"{hrs}h {secs}s"
    elif mins > 0:
        return f"{mins}m {secs}s"
    else:
        return f"{secs}s"


class SessionTracker:
    """
    Tracks Blackboard session lifespan telemetry, records duration on expiry,
    computes rolling averages, and determines optimal auto-refresh intervals.
    """

    def __init__(self, telemetry_path: Path = TELEMETRY_FILE):
        self.path = telemetry_path
        self.data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        """Load session telemetry from JSON."""
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.debug(f"Telemetry load warning: {e}")
        return {
            "current_session": None,
            "history": [],
            "stats": {
                "total_recorded_sessions": 0,
                "average_lifespan_seconds": 0,
                "average_lifespan_human": "Unknown",
                "min_lifespan_seconds": 0,
                "max_lifespan_seconds": 0,
            },
        }

    def _save(self) -> None:
        """Persist telemetry data atomically."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug(f"Telemetry save error: {e}")

    def record_probe(self, is_valid: bool, user_data: Optional[Dict[str, Any]] = None) -> Tuple[bool, Optional[str]]:
        """
        Record a periodic session probe result.
        Returns: (state_changed, alert_message)
        """
        now = time.time()
        iso_now = datetime.now(timezone.utc).isoformat()
        curr = self.data.get("current_session")

        # --------------------------------------------------------------------
        # CASE 1: Session is ACTIVE
        # --------------------------------------------------------------------
        if is_valid:
            if not curr or curr.get("status") != "VALID":
                # New session registered
                student_id = (user_data or {}).get("studentId") or (user_data or {}).get("userName") or "Active"
                self.data["current_session"] = {
                    "session_id": f"sess_{int(now)}",
                    "login_time": now,
                    "login_time_iso": iso_now,
                    "last_seen_valid": now,
                    "last_seen_valid_iso": iso_now,
                    "status": "VALID",
                    "student_id": student_id,
                }
                self._save()
                return True, f"🟢 Blackboard session established for {student_id}."
            else:
                # Update last seen timestamp
                curr["last_seen_valid"] = now
                curr["last_seen_valid_iso"] = iso_now
                self._save()
                return False, None

        # --------------------------------------------------------------------
        # CASE 2: Session is EXPIRED / INVALID
        # --------------------------------------------------------------------
        else:
            if curr and curr.get("status") == "VALID":
                # Session just expired! Compute duration
                login_t = curr.get("login_time", now)
                last_seen_t = curr.get("last_seen_valid", now)
                duration = max(last_seen_t - login_t, now - login_t)

                record = {
                    "session_id": curr.get("session_id", f"sess_{int(now)}"),
                    "login_time_iso": curr.get("login_time_iso", iso_now),
                    "expired_time_iso": iso_now,
                    "duration_seconds": round(duration, 1),
                    "duration_human": _format_seconds(duration),
                    "student_id": curr.get("student_id", "Unknown"),
                    "status": "EXPIRED",
                }

                self.data["history"].append(record)
                # Keep last 50 session history records
                if len(self.data["history"]) > 50:
                    self.data["history"] = self.data["history"][-50:]

                # Update rolling statistics
                self._recalculate_stats()

                # Mark current session as expired
                curr["status"] = "EXPIRED"
                curr["expired_time"] = now
                curr["expired_time_iso"] = iso_now
                self._save()

                avg_str = self.data["stats"].get("average_lifespan_human", "Unknown")
                alert_msg = (
                    f"🔴 <b>Blackboard Session Expired</b>\n"
                    f"⏱️ <b>Lifespan:</b> {record['duration_human']}\n"
                    f"📊 <b>Historical Avg:</b> {avg_str}\n"
                    f"💡 <i>Run /login or use menubar to refresh session cookies.</i>"
                )
                return True, alert_msg

            return False, None

    def _recalculate_stats(self) -> None:
        """Calculate historical average, minimum, and maximum session lifespan."""
        history = self.data.get("history", [])
        if not history:
            return

        durations = [h["duration_seconds"] for h in history if h.get("duration_seconds", 0) > 60]
        if not durations:
            return

        avg_sec = sum(durations) / len(durations)
        min_sec = min(durations)
        max_sec = max(durations)

        # Recommended auto-refresh threshold (90% of minimum or avg minus 30 mins)
        rec_sec = max(avg_sec - 1800, avg_sec * 0.85)

        self.data["stats"] = {
            "total_recorded_sessions": len(history),
            "average_lifespan_seconds": round(avg_sec, 1),
            "average_lifespan_human": _format_seconds(avg_sec),
            "min_lifespan_seconds": round(min_sec, 1),
            "min_lifespan_human": _format_seconds(min_sec),
            "max_lifespan_seconds": round(max_sec, 1),
            "max_lifespan_human": _format_seconds(max_sec),
            "recommended_refresh_interval_seconds": round(rec_sec, 1),
            "recommended_refresh_interval_human": _format_seconds(rec_sec),
        }

    def get_current_duration(self) -> Optional[float]:
        """Get live elapsed seconds for the currently active session."""
        curr = self.data.get("current_session")
        if curr and curr.get("status") == "VALID":
            login_t = curr.get("login_time", time.time())
            return max(time.time() - login_t, 0)
        return None

    def get_telemetry_summary_dict(self) -> Dict[str, Any]:
        """Return structured telemetry dictionary for API and CLI."""
        curr = self.data.get("current_session")
        elapsed = self.get_current_duration()
        return {
            "is_active": (curr.get("status") == "VALID") if curr else False,
            "current_session_duration_seconds": elapsed,
            "current_session_duration_human": _format_seconds(elapsed) if elapsed is not None else None,
            "login_time_iso": curr.get("login_time_iso") if curr else None,
            "stats": self.data.get("stats", {}),
            "recent_history": self.data.get("history", [])[-5:],
        }

    def format_cli_summary(self) -> str:
        """Format telemetry into a clean CLI terminal view."""
        summary = self.get_telemetry_summary_dict()
        stats = summary["stats"]
        lines = [
            "\n📊 ═══════════════════════════════════════════════════════════════════════",
            "   BLACKBOARD SESSION LIFESPAN TELEMETRY & STATS",
            "═══════════════════════════════════════════════════════════════════════",
        ]

        if summary["is_active"]:
            lines.append(f"  🟢 Current Session Status: ACTIVE ({summary['current_session_duration_human']})")
            lines.append(f"     Started At:             {summary['login_time_iso']}")
        else:
            lines.append("  🔴 Current Session Status: EXPIRED / INACTIVE")

        lines.append("")
        lines.append("📈 Historical Lifespan Telemetry:")
        lines.append(f"  • Total Tracked Sessions:  {stats.get('total_recorded_sessions', 0)}")
        lines.append(f"  • Average Lifespan:        {stats.get('average_lifespan_human', 'N/A')}")
        lines.append(f"  • Shortest Observed:       {stats.get('min_lifespan_human', 'N/A')}")
        lines.append(f"  • Longest Observed:        {stats.get('max_lifespan_human', 'N/A')}")
        lines.append(f"  • Recommended Refresh At:  {stats.get('recommended_refresh_interval_human', 'N/A')}")

        if summary["recent_history"]:
            lines.append("\n🕒 Recent Session Log (Last 5):")
            for h in reversed(summary["recent_history"]):
                lines.append(f"  • {h.get('duration_human', 'Unknown')} (Ended: {h.get('expired_time_iso', '')[:19]})")

        lines.append("═══════════════════════════════════════════════════════════════════════\n")
        return "\n".join(lines)


# Global singleton instance
tracker = SessionTracker()
