import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional
import urllib.parse
import urllib.request

from core.config import load_courses
from core.session import check_session
from scrapers.briefing import run_briefing_async
from scrapers.calendar import scrape_calendar_async
from scrapers.grades import scrape_grades_async
from scrapers.announcements import scrape_announcements_async
from scrapers.due_dates import aggregate_due_dates_async
from scrapers.outline import scrape_course_outline_async
from scrapers.search import find_items_async
from telegram.config import get_telegram_config, save_admin_chat_id
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
)
from telegram.notifier import TelegramNotifier

logger = logging.getLogger("blackboard.telegram.bot")

SCRAPER_MUTEX = asyncio.Lock()


class SimpleTelegramBot:
    """
    State-of-the-Art Interactive Telegram Bot Daemon.
    Supports in-place message editing, inline keyboards, callback routing,
    conversational state management, and native Telegram command menus.
    Built with pure Python standard library (0 external pip dependencies).
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
        self.watch_task: Optional[asyncio.Task] = None
        self.watch_interval = 1800  # Default 30 min
        self.user_states: Dict[str, str] = {}  # chat_id -> state (e.g. 'waiting_search_query')

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

        elif data == "btn:check":
            self.edit_message(chat_id, message_id, "⏳ <i>Checking Blackboard session health...</i>")
            async with SCRAPER_MUTEX:
                valid = await asyncio.to_thread(check_session, debug=False, headless=True)
            if valid:
                res_text = "✅ <b>Blackboard Session ACTIVE</b>\nAll scraper features and persistent tokens are ready."
            else:
                res_text = "❌ <b>Session EXPIRED</b>\nPlease run <code>python3 main.py --login</code> in your terminal."
            self.edit_message(chat_id, message_id, res_text, reply_markup=self.get_back_keyboard(refresh_action="check"))

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

            elif cmd == "/courses":
                courses = load_courses()
                lines = ["📚 <b>Configured Courses:</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
                for cid, cname in courses.items():
                    lines.append(f"• <b>{escape_html(cname)}</b>\n  └ ID: <code>{escape_html(cid)}</code>")
                self.send_message(chat_id, "\n".join(lines), reply_markup=self.get_back_keyboard())

            elif cmd == "/check":
                mid = self.send_message(chat_id, "⏳ <i>Checking Blackboard session health...</i>")
                async with SCRAPER_MUTEX:
                    valid = await asyncio.to_thread(check_session, debug=False, headless=True)
                if valid:
                    res_text = "✅ <b>Blackboard Session ACTIVE</b>\nAll scraper features ready."
                else:
                    res_text = "❌ <b>Session EXPIRED</b>\nPlease run <code>python3 main.py --login</code> to renew."
                if mid:
                    self.edit_message(chat_id, mid, res_text, reply_markup=self.get_back_keyboard())

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
                except Exception as e:
                    if mid:
                        self.edit_message(chat_id, mid, f"❌ <b>Search Failed:</b> <code>{escape_html(str(e))}</code>", reply_markup=self.get_back_keyboard())

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
    # Background Periodic Watch Loop
    # ------------------------------------------------------------------------

    async def _periodic_watch_loop(self):
        """Background loop that periodically checks for new grades and announcements."""
        while self.running:
            await asyncio.sleep(self.watch_interval)
            logger.info("Executing periodic watch check...")
            try:
                async with SCRAPER_MUTEX:
                    bundle = await run_briefing_async(headless=True, write_markdown=False, concurrency=4)
                self.notifier.process_and_notify_diffs(bundle)
            except Exception as e:
                logger.error(f"Periodic watch error: {e}")

    # ------------------------------------------------------------------------
    # Main Long-Polling Daemon Loop
    # ------------------------------------------------------------------------

    async def start_polling(self):
        """Starts the interactive bot polling loop."""
        if not self.bot_token:
            print("❌ Cannot start Telegram bot: No bot token configured.")
            return

        print(f"🤖 Blackboard Telegram Bot Daemon running (@blackboardscrapbot).")
        if self.admin_chat_id:
            print(f"   👤 Authorized Admin Chat ID: {self.admin_chat_id}")
        else:
            print("   ⏳ Waiting for admin user to message the bot on Telegram for auto-pairing...")

        # Setup native commands menu
        self.setup_native_commands()
        self.running = True
        self.watch_task = asyncio.create_task(self._periodic_watch_loop())

        while self.running:
            try:
                url = f"{self.api_base}/getUpdates?offset={self.last_update_id + 1}&timeout=20"
                req = urllib.request.Request(url, headers={"User-Agent": "BlackboardBot/2.0"})

                def _fetch():
                    with urllib.request.urlopen(req, timeout=25) as resp:
                        return json.loads(resp.read().decode("utf-8"))

                data = await asyncio.to_thread(_fetch)
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

            except Exception as e:
                logger.debug(f"Polling loop notice: {e}")
                await asyncio.sleep(2)


def run_bot():
    """CLI launcher for bot daemon."""
    bot = SimpleTelegramBot()
    asyncio.run(bot.start_polling())
