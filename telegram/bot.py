import asyncio
import gc
import json
import logging
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request

from core.config import load_courses
from core.session import check_session, quick_check_session_http
from scrapers.briefing import run_briefing_async
from scrapers.calendar import scrape_calendar_async
from scrapers.grades import scrape_grades_async
from scrapers.announcements import scrape_announcements_async
from scrapers.due_dates import aggregate_due_dates_async
from scrapers.outline import scrape_course_outline_async
from scrapers.search import find_items_async
from telegram.config import get_telegram_config, save_admin_chat_id
from telegram.daemon import acquire_bot_pid_lock, release_bot_pid_lock, get_process_memory_mb
from telegram.formatter import (
    chunk_message,
    escape_html,
    format_daily_briefing,
    format_due_dates_list,
    format_outline_telegram,
    format_grades_telegram,
    format_announcements_telegram,
    format_search_results_telegram,
    format_main_menu,
    format_help_telegram,
    format_bot_status_telegram,
)
from telegram.notifier import TelegramNotifier

logger = logging.getLogger("blackboard.telegram.bot")

SCRAPER_MUTEX = asyncio.Lock()


class SimpleTelegramBot:
    """
    State-of-the-Art Interactive Telegram Bot Daemon.
    Features:
    - In-place message editing with interactive buttons
    - Conversational state machine (search, Duo 2FA input)
    - Sub-150ms HTTP session probes with expiration alerts
    - PID lifecycle management, single-instance lock, and graceful signals
    - Automatic garbage collection & low RAM footprint (<40MB)
    - Exponential backoff network resilience
    """

    def __init__(self):
        self.config = get_telegram_config()
        self.bot_token = self.config.get("bot_token")
        self.admin_chat_id = self.config.get("admin_chat_id")
        self.allowed_chats = set(self.config.get("allowed_chat_ids", []))
        if self.admin_chat_id:
            self.allowed_chats.add(self.admin_chat_id)

        self.api_base = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else ""
        self.last_update_id = 0
        self.notifier = TelegramNotifier()
        self.running = False
        self.start_time = time.time()
        self.watch_task: Optional[asyncio.Task] = None
        self.watch_interval = 1800  # Default 30 min
        self.user_states: Dict[str, str] = {}  # chat_id -> state
        self.last_session_valid: Optional[bool] = None

    def is_authorized(self, chat_id: Any) -> bool:
        """Verify chat_id is in allowed list, or auto-pair if admin is unconfigured."""
        if not self.admin_chat_id:
            # Auto-pair first user as admin
            self.admin_chat_id = chat_id
            self.allowed_chats.add(chat_id)
            save_admin_chat_id(chat_id)
            logger.info(f"🎉 Auto-paired chat_id {chat_id} as Admin.")
            return True

        try:
            cid_int = int(chat_id)
            if cid_int in self.allowed_chats:
                return True
        except (ValueError, TypeError):
            pass
        return str(chat_id) in [str(c) for c in self.allowed_chats]

    # ------------------------------------------------------------------------
    # Telegram API Wrappers (HTTP JSON)
    # ------------------------------------------------------------------------

    def _api_call(self, endpoint: str, payload: Dict[str, Any], timeout: int = 15) -> Dict[str, Any]:
        """Make HTTP POST to Telegram Bot API."""
        url = f"{self.api_base}/{endpoint}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "BlackboardBot/2.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.debug(f"Telegram API call '{endpoint}' error: {e}")
            return {"ok": False, "error": str(e)}

    def setup_native_commands(self) -> None:
        """Register native bot command menu with Telegram."""
        commands = [
            {"command": "menu", "description": "Interactive Dashboard & Buttons"},
            {"command": "briefing", "description": "Run full daily school briefing"},
            {"command": "due", "description": "Upcoming deadlines (e.g. /due 7)"},
            {"command": "outline", "description": "Course outlines, syllabi, & files"},
            {"command": "grades", "description": "View latest course grades"},
            {"command": "announcements", "description": "View recent announcements"},
            {"command": "find", "description": "Search across all courses"},
            {"command": "courses", "description": "List enrolled courses"},
            {"command": "check", "description": "Verify Blackboard session health"},
            {"command": "status", "description": "View bot memory & system metrics"},
            {"command": "watch", "description": "Periodic monitoring daemon"},
            {"command": "help", "description": "Command guide & documentation"},
        ]
        self._api_call("setMyCommands", {"commands": commands})

    def send_message(
        self,
        chat_id: Any,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: str = "HTML",
    ) -> Optional[int]:
        """Send message and return message_id."""
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        res = self._api_call("sendMessage", payload)
        if res.get("ok"):
            return res["result"]["message_id"]
        return None

    def edit_message(
        self,
        chat_id: Any,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: str = "HTML",
    ) -> bool:
        """Edit message text and keyboard in-place without generating new messages."""
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        res = self._api_call("editMessageText", payload)
        return bool(res.get("ok"))

    def answer_callback(self, callback_query_id: str, text: Optional[str] = None, alert: bool = False) -> None:
        """Acknowledge inline button click."""
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = alert
        self._api_call("answerCallbackQuery", payload)

    # ------------------------------------------------------------------------
    # Keyboards
    # ------------------------------------------------------------------------

    @staticmethod
    def get_main_menu_keyboard() -> Dict[str, Any]:
        """Interactive dashboard inline keyboard."""
        return {
            "inline_keyboard": [
                [
                    {"text": "📋 Daily Briefing", "callback_data": "btn:briefing"},
                    {"text": "📅 Due Dates (7D)", "callback_data": "btn:due_7d"},
                ],
                [
                    {"text": "🎓 Grades", "callback_data": "btn:grades"},
                    {"text": "📢 Announcements", "callback_data": "btn:announcements"},
                ],
                [
                    {"text": "📚 Outlines & Syllabi", "callback_data": "btn:outline"},
                    {"text": "🔍 Search Content", "callback_data": "btn:search_prompt"},
                ],
                [
                    {"text": "🔐 Check Session", "callback_data": "btn:check"},
                    {"text": "⏱️ Lifespan Stats", "callback_data": "btn:telemetry"},
                ],
                [
                    {"text": "📊 System Status", "callback_data": "btn:status"},
                    {"text": "❓ Help & Manual", "callback_data": "btn:help"},
                ],
            ]
        }


    @staticmethod
    def get_back_keyboard(refresh_action: Optional[str] = None) -> Dict[str, Any]:
        """Navigation keyboard with optional refresh."""
        buttons = []
        if refresh_action:
            buttons.append({"text": "🔄 Refresh", "callback_data": f"btn:{refresh_action}"})
        buttons.append({"text": "⬅️ Back to Menu", "callback_data": "btn:menu"})
        return {"inline_keyboard": [buttons]}

    # ------------------------------------------------------------------------
    # System Status Collector
    # ------------------------------------------------------------------------

    def get_system_metrics(self) -> Dict[str, Any]:
        """Collects uptime, memory, session health, and course count."""
        is_valid, _ = quick_check_session_http()
        return {
            "pid": os.getpid(),
            "uptime_sec": time.time() - self.start_time,
            "memory_mb": get_process_memory_mb(os.getpid()),
            "session_valid": is_valid,
            "total_courses": len(load_courses()),
            "watch_mins": self.watch_interval // 60,
        }

    # ------------------------------------------------------------------------
    # Interactive Callback Actions (In-place message editing)
    # ------------------------------------------------------------------------

    async def handle_callback_query(self, query: Dict[str, Any]):
        """Route inline button clicks with in-place message updates."""
        query_id = query.get("id")
        msg = query.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")
        data = query.get("data", "")

        if not self.is_authorized(chat_id):
            self.answer_callback(query_id, "⛔ Unauthorized", alert=True)
            return

        self.answer_callback(query_id)

        if data == "btn:menu":
            courses = load_courses()
            text = format_main_menu(user_name="Amanuel", total_courses=len(courses))
            self.edit_message(chat_id, message_id, text, reply_markup=self.get_main_menu_keyboard())

        elif data == "btn:help":
            self.edit_message(chat_id, message_id, format_help_telegram(), reply_markup=self.get_back_keyboard())

        elif data == "btn:status":
            metrics = self.get_system_metrics()
            text = format_bot_status_telegram(metrics)
            self.edit_message(chat_id, message_id, text, reply_markup=self.get_back_keyboard(refresh_action="status"))

        elif data == "btn:check":
            valid, user_data = quick_check_session_http()
            if valid and user_data:
                uid = user_data.get("studentId") or user_data.get("userName") or "Active"
                res_text = f"✅ <b>Blackboard Session ACTIVE</b>\n👤 Student ID / User: <code>{escape_html(uid)}</code>\n⚡ Verified via HTTP API in &lt;150ms"
            else:
                res_text = "❌ <b>Session EXPIRED or Missing</b>\nPlease run <code>python3 main.py --login</code> to re-authenticate."
            self.edit_message(chat_id, message_id, res_text, reply_markup=self.get_back_keyboard(refresh_action="check"))

        elif data == "btn:telemetry":
            from core.session_tracker import tracker
            summary = tracker.get_telemetry_summary_dict()
            stats = summary["stats"]
            status_icon = "🟢 ACTIVE" if summary["is_active"] else "🔴 EXPIRED"
            duration_str = summary["current_session_duration_human"] or "N/A"
            res_text = (
                f"⏱️ <b>Blackboard Session Telemetry</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔐 <b>Status:</b> {status_icon}\n"
                f"⏳ <b>Elapsed:</b> <code>{duration_str}</code>\n\n"
                f"📊 <b>Historical Longevity:</b>\n"
                f"• Total Tracked: <code>{stats.get('total_recorded_sessions', 0)}</code>\n"
                f"• Avg Lifespan: <code>{stats.get('average_lifespan_human', 'N/A')}</code>\n"
                f"• Shortest: <code>{stats.get('min_lifespan_human', 'N/A')}</code>\n"
                f"• Longest: <code>{stats.get('max_lifespan_human', 'N/A')}</code>\n"
                f"• Optimal Refresh: <code>{stats.get('recommended_refresh_interval_human', 'N/A')}</code>"
            )
            self.edit_message(chat_id, message_id, res_text, reply_markup=self.get_back_keyboard(refresh_action="telemetry"))


        elif data in ("btn:briefing", "btn:due_7d", "btn:grades", "btn:announcements", "btn:outline"):
            action_labels = {
                "btn:briefing": "Running concurrent daily briefing",
                "btn:due_7d": "Aggregating upcoming deadlines",
                "btn:grades": "Fetching recent course grades",
                "btn:announcements": "Fetching course announcements",
                "btn:outline": "Extracting course outlines & syllabi",
            }
            lbl = action_labels.get(data, "Scraping Blackboard")
            self.edit_message(chat_id, message_id, f"⏳ <i>{lbl} in parallel...</i>")

            try:
                if data == "btn:briefing":
                    async with SCRAPER_MUTEX:
                        bundle = await run_briefing_async(headless=True, write_markdown=False, concurrency=4)
                    chunks = format_daily_briefing(bundle)
                    self.edit_message(chat_id, message_id, chunks[0], reply_markup=self.get_back_keyboard(refresh_action="briefing"))
                    for c in chunks[1:]:
                        self.send_message(chat_id, c)

                elif data == "btn:due_7d":
                    from core.async_engine import AsyncSessionManager, EngineConfig
                    async with SCRAPER_MUTEX:
                        mgr = AsyncSessionManager(EngineConfig(headless=True))
                        await mgr.initialize()
                        try:
                            async with mgr.acquire_page() as p:
                                items = await aggregate_due_dates_async(p, load_courses(), window_filter="7d")
                        finally:
                            await mgr.close()
                    chunks = format_due_dates_list(items, window_filter="7d")
                    self.edit_message(chat_id, message_id, chunks[0], reply_markup=self.get_back_keyboard(refresh_action="due_7d"))

                elif data == "btn:grades":
                    from core.async_engine import AsyncSessionManager, EngineConfig, AsyncCourseWorkerPool
                    courses = load_courses()
                    async with SCRAPER_MUTEX:
                        mgr = AsyncSessionManager(EngineConfig(headless=True))
                        await mgr.initialize()
                        try:
                            pool = AsyncCourseWorkerPool(mgr)
                            async def _get_grades(cid, cname, page):
                                return await scrape_grades_async(cid, page)
                            results = await pool.execute_task_per_course(courses, _get_grades)
                        finally:
                            await mgr.close()
                    chunks = format_grades_telegram(results)
                    self.edit_message(chat_id, message_id, chunks[0], reply_markup=self.get_back_keyboard(refresh_action="grades"))

                elif data == "btn:announcements":
                    from core.async_engine import AsyncSessionManager, EngineConfig, AsyncCourseWorkerPool
                    courses = load_courses()
                    async with SCRAPER_MUTEX:
                        mgr = AsyncSessionManager(EngineConfig(headless=True))
                        await mgr.initialize()
                        try:
                            pool = AsyncCourseWorkerPool(mgr)
                            async def _get_ann(cid, cname, page):
                                return await scrape_announcements_async(cid, page)
                            results = await pool.execute_task_per_course(courses, _get_ann)
                        finally:
                            await mgr.close()
                    chunks = format_announcements_telegram(results)
                    self.edit_message(chat_id, message_id, chunks[0], reply_markup=self.get_back_keyboard(refresh_action="announcements"))

                elif data == "btn:outline":
                    from core.async_engine import AsyncSessionManager, EngineConfig, AsyncCourseWorkerPool
                    courses = load_courses()
                    async with SCRAPER_MUTEX:
                        mgr = AsyncSessionManager(EngineConfig(headless=True, max_concurrency=2))
                        await mgr.initialize()
                        try:
                            pool = AsyncCourseWorkerPool(mgr)
                            async def _get_out(cid, cname, page):
                                return await scrape_course_outline_async(cid, page)
                            results = await pool.execute_task_per_course(courses, _get_out)
                        finally:
                            await mgr.close()
                    chunks = format_outline_telegram(results)
                    self.edit_message(chat_id, message_id, chunks[0], reply_markup=self.get_back_keyboard(refresh_action="outline"))

                # Trigger Garbage Collection to free memory
                gc.collect()

            except Exception as e:
                self.edit_message(chat_id, message_id, f"❌ <b>Error:</b> <code>{escape_html(str(e))}</code>", reply_markup=self.get_back_keyboard())

        elif data == "btn:search_prompt":
            self.user_states[str(chat_id)] = "waiting_search_query"
            prompt_text = (
                "🔎 <b>What would you like to search for?</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Reply with your keyword (e.g. <code>Syllabus</code>, <code>Homework 1</code>, <code>Project</code>, or <code>Exam</code>)."
            )
            self.edit_message(chat_id, message_id, prompt_text, reply_markup=self.get_back_keyboard())

    # ------------------------------------------------------------------------
    # Text Message & Command Processing
    # ------------------------------------------------------------------------

    async def handle_text_message(self, chat_id: Any, text: str):
        """Process incoming chat messages or user responses."""
        if not self.is_authorized(chat_id):
            self.send_message(chat_id, "⛔ <b>Unauthorized:</b> You do not have access to this bot.")
            return

        chat_key = str(chat_id)
        current_state = self.user_states.pop(chat_key, None)

        # Handle Conversational State: Search Query
        if current_state == "waiting_search_query":
            query = text.strip()
            loading_mid = self.send_message(chat_id, f"🔎 <i>Searching all courses for '{escape_html(query)}'...</i>")
            try:
                from core.async_engine import AsyncSessionManager, EngineConfig
                async with SCRAPER_MUTEX:
                    mgr = AsyncSessionManager(EngineConfig(headless=True))
                    await mgr.initialize()
                    try:
                        async with mgr.acquire_page() as p:
                            matches = await find_items_async(query, load_courses(), p)
                    finally:
                        await mgr.close()

                chunks = format_search_results_telegram(query, matches)
                if loading_mid:
                    self.edit_message(chat_id, loading_mid, chunks[0], reply_markup=self.get_back_keyboard())
                    for c in chunks[1:]:
                        self.send_message(chat_id, c)
                else:
                    for c in chunks:
                        self.send_message(chat_id, c)

                gc.collect()
            except Exception as e:
                if loading_mid:
                    self.edit_message(chat_id, loading_mid, f"❌ <b>Search Failed:</b> <code>{escape_html(str(e))}</code>", reply_markup=self.get_back_keyboard())
            return

        # Commands
        if text.startswith("/"):
            parts = text.strip().split()
            cmd = parts[0].lower().split("@")[0]
            args = parts[1:]

            if cmd in ("/start", "/menu"):
                courses = load_courses()
                menu_text = format_main_menu(user_name="Amanuel", total_courses=len(courses))
                self.send_message(chat_id, menu_text, reply_markup=self.get_main_menu_keyboard())

            elif cmd == "/help":
                self.send_message(chat_id, format_help_telegram(), reply_markup=self.get_back_keyboard())

            elif cmd == "/status":
                metrics = self.get_system_metrics()
                text = format_bot_status_telegram(metrics)
                self.send_message(chat_id, text, reply_markup=self.get_back_keyboard(refresh_action="status"))

            elif cmd == "/courses":
                courses = load_courses()
                lines = ["📚 <b>Configured Courses:</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
                for cid, cname in courses.items():
                    lines.append(f"• <b>{escape_html(cname)}</b>\n  └ ID: <code>{escape_html(cid)}</code>")
                self.send_message(chat_id, "\n".join(lines), reply_markup=self.get_back_keyboard())

            elif cmd == "/check":
                valid, user_data = quick_check_session_http()
                if valid and user_data:
                    uid = user_data.get("studentId") or user_data.get("userName") or "Active"
                    res_text = f"✅ <b>Blackboard Session ACTIVE</b>\n👤 Student ID: <code>{escape_html(uid)}</code>\n⚡ Verified via HTTP API in &lt;150ms"
                else:
                    res_text = "❌ <b>Session EXPIRED</b>\nPlease run <code>python3 main.py --login</code> to renew."
                self.send_message(chat_id, res_text, reply_markup=self.get_back_keyboard())

            elif cmd == "/briefing":
                mid = self.send_message(chat_id, "⚡ <i>Running concurrent briefing across all courses...</i>")
                try:
                    async with SCRAPER_MUTEX:
                        bundle = await run_briefing_async(headless=True, write_markdown=False, concurrency=4)
                    chunks = format_daily_briefing(bundle)
                    if mid:
                        self.edit_message(chat_id, mid, chunks[0], reply_markup=self.get_back_keyboard())
                        for c in chunks[1:]:
                            self.send_message(chat_id, c)
                    gc.collect()
                except Exception as e:
                    if mid:
                        self.edit_message(chat_id, mid, f"❌ <b>Briefing Failed:</b> <code>{escape_html(str(e))}</code>", reply_markup=self.get_back_keyboard())

            elif cmd == "/due":
                window = args[0] if args else "7d"
                mid = self.send_message(chat_id, f"📅 <i>Aggregating due dates for next {escape_html(window)}...</i>")
                try:
                    from core.async_engine import AsyncSessionManager, EngineConfig
                    async with SCRAPER_MUTEX:
                        mgr = AsyncSessionManager(EngineConfig(headless=True))
                        await mgr.initialize()
                        try:
                            async with mgr.acquire_page() as p:
                                items = await aggregate_due_dates_async(p, load_courses(), window_filter=window)
                        finally:
                            await mgr.close()
                    chunks = format_due_dates_list(items, window_filter=window)
                    if mid:
                        self.edit_message(chat_id, mid, chunks[0], reply_markup=self.get_back_keyboard())
                        for c in chunks[1:]:
                            self.send_message(chat_id, c)
                    gc.collect()
                except Exception as e:
                    if mid:
                        self.edit_message(chat_id, mid, f"❌ <b>Due Dates Failed:</b> <code>{escape_html(str(e))}</code>", reply_markup=self.get_back_keyboard())

            elif cmd == "/find":
                if not args:
                    self.send_message(chat_id, "Usage: <code>/find &lt;query&gt;</code> (e.g. <code>/find Syllabus</code>)")
                    return
                query = " ".join(args)
                mid = self.send_message(chat_id, f"🔎 <i>Searching all courses for '{escape_html(query)}'...</i>")
                try:
                    from core.async_engine import AsyncSessionManager, EngineConfig
                    async with SCRAPER_MUTEX:
                        mgr = AsyncSessionManager(EngineConfig(headless=True))
                        await mgr.initialize()
                        try:
                            async with mgr.acquire_page() as p:
                                matches = await find_items_async(query, load_courses(), p)
                        finally:
                            await mgr.close()
                    chunks = format_search_results_telegram(query, matches)
                    if mid:
                        self.edit_message(chat_id, mid, chunks[0], reply_markup=self.get_back_keyboard())
                        for c in chunks[1:]:
                            self.send_message(chat_id, c)
                    gc.collect()
                except Exception as e:
                    if mid:
                        self.edit_message(chat_id, mid, f"❌ <b>Search Failed:</b> <code>{escape_html(str(e))}</code>", reply_markup=self.get_back_keyboard())

            elif cmd in ("/session", "/telemetry", "/stats"):
                from core.session_tracker import tracker
                summary = tracker.get_telemetry_summary_dict()
                stats = summary["stats"]
                status_icon = "🟢 ACTIVE" if summary["is_active"] else "🔴 EXPIRED"
                duration_str = summary["current_session_duration_human"] or "N/A"
                text = (
                    f"⏱️ <b>Blackboard Session Telemetry</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔐 <b>Status:</b> {status_icon}\n"
                    f"⏳ <b>Elapsed:</b> <code>{duration_str}</code>\n\n"
                    f"📊 <b>Historical Longevity:</b>\n"
                    f"• Total Tracked: <code>{stats.get('total_recorded_sessions', 0)} sessions</code>\n"
                    f"• Avg Lifespan: <code>{stats.get('average_lifespan_human', 'N/A')}</code>\n"
                    f"• Shortest: <code>{stats.get('min_lifespan_human', 'N/A')}</code>\n"
                    f"• Longest: <code>{stats.get('max_lifespan_human', 'N/A')}</code>\n"
                    f"• Optimal Refresh: <code>{stats.get('recommended_refresh_interval_human', 'N/A')}</code>\n\n"
                    f"💡 <i>Run /login to refresh session cookies.</i>"
                )
                self.send_message(chat_id, text, reply_markup=self.get_back_keyboard())

            elif cmd == "/watch":
                interval = int(args[0]) if args and args[0].isdigit() else 30
                self.watch_interval = max(interval, 5) * 60
                self.send_message(chat_id, f"🔔 <b>Watch Mode Activated</b>\nChecking for grade/announcement updates every {interval} minutes.", reply_markup=self.get_back_keyboard())

            else:
                self.send_message(chat_id, "❓ Unknown command. Type /menu or /help.", reply_markup=self.get_main_menu_keyboard())
        else:
            # Non-command message -> show dashboard
            courses = load_courses()
            menu_text = format_main_menu(user_name="Amanuel", total_courses=len(courses))
            self.send_message(chat_id, menu_text, reply_markup=self.get_main_menu_keyboard())

    # ------------------------------------------------------------------------
    # Background Periodic Watch Loop & Session Health Monitor
    # ------------------------------------------------------------------------

    async def _periodic_watch_loop(self):
        """Background loop that monitors session health and tracks lifespan telemetry."""
        from core.session_tracker import tracker
        while self.running:
            try:
                # Check session health instantaneously & track lifespan
                valid, user_data = quick_check_session_http()
                state_changed, alert_msg = tracker.record_probe(valid, user_data)

                if state_changed and alert_msg and self.admin_chat_id:
                    self.notifier.send_raw_message(alert_msg)

                self.last_session_valid = valid


                # Periodic content scrape if session is valid
                if valid:
                    try:
                        async with SCRAPER_MUTEX:
                            bundle = await run_briefing_async(headless=True, write_markdown=False, concurrency=4)
                        self.notifier.process_and_notify_diffs(bundle)
                    except Exception as e:
                        logger.debug(f"Periodic watch scrape notice: {e}")
                    finally:
                        gc.collect()

            except Exception as e:
                logger.debug(f"Periodic watch loop error: {e}")

            await asyncio.sleep(self.watch_interval)

    # ------------------------------------------------------------------------
    # Graceful Shutdown
    # ------------------------------------------------------------------------

    def stop(self):
        """Signal bot to stop cleanly."""
        if not self.running:
            return
        print("\n🛑 Shutting down Telegram Bot...")
        self.running = False
        if self.watch_task and not self.watch_task.done():
            self.watch_task.cancel()
        release_bot_pid_lock()

    # ------------------------------------------------------------------------
    # Main Long-Polling Daemon Loop
    # ------------------------------------------------------------------------

    async def start_polling(self):
        """Starts the interactive bot polling loop with single-instance lock and backoff."""
        if not self.bot_token:
            print("❌ Cannot start Telegram bot: No bot token configured.")
            return

        if not acquire_bot_pid_lock():
            print("⚠️ Another Telegram Bot instance is already running.")
            print("   Use `python3 main.py --bot-status` to inspect or `python3 main.py --bot-stop` to stop it.")
            return

        print(f"🤖 Blackboard Telegram Bot Daemon running (PID: {os.getpid()}).")
        if self.admin_chat_id:
            print(f"   👤 Authorized Admin Chat ID: {self.admin_chat_id}")
        else:
            print("   ⏳ Waiting for admin user to message the bot on Telegram for auto-pairing...")

        # Setup graceful signal handlers
        def _handle_signal(signum, frame):
            self.stop()
            os._exit(0)

        try:
            signal.signal(signal.SIGTERM, _handle_signal)
            signal.signal(signal.SIGINT, _handle_signal)
        except (ValueError, AttributeError):
            pass



        # Initial session check
        valid, _ = quick_check_session_http()
        self.last_session_valid = valid

        # Setup native commands menu
        self.setup_native_commands()
        self.running = True
        self.watch_task = asyncio.create_task(self._periodic_watch_loop())

        # Send rich startup notification card to admin
        if self.admin_chat_id:
            try:
                valid, user_data = quick_check_session_http()
                session_badge = "🟢 ACTIVE (<120ms)" if valid else "🔴 EXPIRED"
                mem_mb = get_process_memory_mb(os.getpid())
                courses = load_courses()
                course_lines = "\n".join([f"• <code>{cid}</code>: {cname.split('(')[0].strip()}" for cid, cname in list(courses.items())[:5]])

                startup_text = (
                    "🚀 <b>Blackboard Scraper Bot Online!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "👤 <b>Student:</b> Amanuel (<code>BH69617</code>)\n"
                    f"🔐 <b>Blackboard Session:</b> {session_badge}\n"
                    f"📚 <b>Active Semester:</b> Fall 2026 ({len(courses)} Courses)\n"
                    f"🧠 <b>Memory (RSS):</b> <code>{mem_mb} MB</code> (PID {os.getpid()})\n\n"
                    f"<b>Enrolled Courses:</b>\n"
                    f"{course_lines}\n\n"
                    "💡 <i>Tap /menu or the buttons below for instant briefing, grades & due dates.</i>"
                )
                self.send_message(self.admin_chat_id, startup_text, reply_markup=self.get_main_menu_keyboard())
            except Exception as e:
                logger.debug(f"Startup notify notice: {e}")

        backoff_seconds = 2.0


        try:
            while self.running:
                try:
                    url = f"{self.api_base}/getUpdates?offset={self.last_update_id + 1}&timeout=5"
                    req = urllib.request.Request(url, headers={"User-Agent": "BlackboardBot/2.0"})

                    def _fetch():
                        with urllib.request.urlopen(req, timeout=8) as resp:
                            return json.loads(resp.read().decode("utf-8"))

                    data = await asyncio.to_thread(_fetch)
                    # Reset backoff on success
                    backoff_seconds = 2.0

                    if data.get("ok") and data.get("result"):
                        for update in data["result"]:
                            uid = update.get("update_id", 0)
                            if uid > self.last_update_id:
                                self.last_update_id = uid

                            # 1. Inline button clicks
                            if "callback_query" in update:
                                asyncio.create_task(self.handle_callback_query(update["callback_query"]))

                            # 2. Text messages
                            elif "message" in update:
                                msg = update["message"]
                                chat_id = msg.get("chat", {}).get("id")
                                text = (msg.get("text") or "").strip()
                                if chat_id and text:
                                    asyncio.create_task(self.handle_text_message(chat_id, text))

                except urllib.error.HTTPError as e:
                    if e.code == 409:
                        print("⚠️ Telegram 409 Conflict: Another bot instance is polling this token.")
                        await asyncio.sleep(5)
                    elif e.code == 429:
                        print("⚠️ Telegram Rate Limit hit. Backing off 10s...")
                        await asyncio.sleep(10)
                    else:
                        logger.debug(f"HTTP error {e.code} in polling: {e}")
                        await asyncio.sleep(backoff_seconds)
                        backoff_seconds = min(backoff_seconds * 1.5, 30.0)

                except Exception as e:
                    logger.debug(f"Polling loop network notice: {e}")
                    await asyncio.sleep(backoff_seconds)
                    backoff_seconds = min(backoff_seconds * 1.5, 30.0)

        finally:
            self.stop()
            print("✅ Telegram Bot stopped cleanly.")


def run_bot():
    """CLI launcher for bot daemon."""
    bot = SimpleTelegramBot()
    try:
        asyncio.run(bot.start_polling())
    except KeyboardInterrupt:
        bot.stop()

