import asyncio
import functools
import json
import logging
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
from telegram.config import get_telegram_config
from telegram.formatter import (
    chunk_message,
    escape_html,
    format_daily_briefing,
    format_due_dates_list,
)
from telegram.notifier import TelegramNotifier

logger = logging.getLogger("blackboard.telegram.bot")

SCRAPER_MUTEX = asyncio.Lock()


class SimpleTelegramBot:
    """
    Lightweight, self-contained Telegram Polling Bot Daemon.
    Requires zero external pip packages (pure Python standard library).
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

    def is_authorized(self, chat_id: Any) -> bool:
        """Verify chat_id is in allowed list."""
        try:
            cid_int = int(chat_id)
            if cid_int in self.allowed_chats:
                return True
        except (ValueError, TypeError):
            pass
        return str(chat_id) in [str(c) for c in self.allowed_chats]

    def _get_updates(self, offset: int, timeout: int = 25) -> List[Dict[str, Any]]:
        """Fetch updates from Telegram getUpdates endpoint."""
        url = f"{self.api_base}/getUpdates?offset={offset}&timeout={timeout}"
        req = urllib.request.Request(url, headers={"User-Agent": "BlackboardScraperBot/2.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
                data = json.loads(resp.read().decode())
                if data.get("ok"):
                    return data.get("results", [])
        except Exception as e:
            logger.debug(f"Polling error: {e}")
        return []

    async def send_reply(self, chat_id: Any, text: str, parse_mode: str = "HTML"):
        """Send reply message to chat."""
        chunks = chunk_message(text)
        for chunk in chunks:
            self.notifier.send_raw_message(chunk, chat_id=chat_id, parse_mode=parse_mode)
            await asyncio.sleep(0.2)

    async def handle_command(self, chat_id: Any, command_text: str):
        """Process incoming chat commands."""
        parts = command_text.strip().split()
        cmd = parts[0].lower().split("@")[0]  # Remove bot username if present
        args = parts[1:]

        logger.info(f"Received command: {cmd} from chat {chat_id}")

        if cmd in ("/start", "/help"):
            help_msg = (
                "🤖 <b>Blackboard Ultra Bot Online</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "• /briefing — Run full concurrent daily briefing\n"
                "• /due [days] — Upcoming deadlines (e.g. <code>/due 7</code>)\n"
                "• /grades [course_id] — View latest course grades\n"
                "• /announcements [course_id] — View recent announcements\n"
                "• /outline [course_id] — View course outline tree\n"
                "• /courses — Show configured courses & IDs\n"
                "• /check — Verify Blackboard SSO session health\n"
                "• /watch [mins] — Toggle periodic background check\n"
                "• /help — Show this command manual"
            )
            await self.send_reply(chat_id, help_msg)

        elif cmd == "/courses":
            courses = load_courses()
            lines = ["📚 <b>Configured Courses:</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
            for cid, cname in courses.items():
                lines.append(f"• <b>{escape_html(cname)}</b>\n  └ ID: <code>{escape_html(cid)}</code>")
            await self.send_reply(chat_id, "\n".join(lines))

        elif cmd == "/check":
            await self.send_reply(chat_id, "⏳ <i>Checking Blackboard session health...</i>")
            async with SCRAPER_MUTEX:
                valid = await asyncio.to_thread(check_session, debug=False, headless=True)
            if valid:
                await self.send_reply(chat_id, "✅ <b>Blackboard Session ACTIVE</b>\nAll scraper features ready.")
            else:
                await self.send_reply(chat_id, "❌ <b>Session EXPIRED</b>\nPlease run <code>python3 main.py --login</code> to renew.")

        elif cmd == "/briefing":
            await self.send_reply(chat_id, "⚡ <i>Running concurrent briefing across all courses (8-12s)...</i>")
            try:
                async with SCRAPER_MUTEX:
                    bundle = await run_briefing_async(headless=True, write_markdown=True, concurrency=4)
                chunks = format_daily_briefing(bundle)
                for chunk in chunks:
                    await self.send_reply(chat_id, chunk)
                # Diff check
                self.notifier.process_and_notify_diffs(bundle, chat_id=chat_id)
            except Exception as e:
                await self.send_reply(chat_id, f"❌ <b>Briefing Failed:</b> <code>{escape_html(str(e))}</code>")

        elif cmd == "/due":
            window = args[0] if args else "7d"
            await self.send_reply(chat_id, f"📅 <i>Aggregating due dates for next {window}...</i>")
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
                for chunk in chunks:
                    await self.send_reply(chat_id, chunk)
            except Exception as e:
                await self.send_reply(chat_id, f"❌ <b>Due Date Scrape Failed:</b> <code>{escape_html(str(e))}</code>")

        elif cmd == "/grades":
            target_course = args[0] if args else None
            courses = load_courses()
            target_courses = {target_course: courses.get(target_course, target_course)} if target_course else courses

            await self.send_reply(chat_id, "📊 <i>Fetching course grades...</i>")
            try:
                from core.async_engine import AsyncSessionManager, EngineConfig, AsyncCourseWorkerPool
                async with SCRAPER_MUTEX:
                    mgr = AsyncSessionManager(EngineConfig(headless=True))
                    await mgr.initialize()
                    try:
                        pool = AsyncCourseWorkerPool(mgr)

                        async def _get_grades(cid, cname, page):
                            return await scrape_grades_async(cid, page)

                        results = await pool.execute_task_per_course(target_courses, _get_grades)
                    finally:
                        await mgr.close()

                lines = ["📊 <b>Course Grades:</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
                for cid, g_list in results.items():
                    cname = courses.get(cid, cid)
                    lines.append(f"\n<b>{escape_html(cname)}</b>")
                    if isinstance(g_list, list) and g_list:
                        for g in g_list:
                            score = g.get("grade") or "Not graded"
                            due = f" (Due: {g['dueDate']})" if g.get("dueDate") else ""
                            lines.append(f"• {escape_html(g['name'])}: <code>{escape_html(score)}</code>{due}")
                    else:
                        lines.append("<i>No grade entries found.</i>")

                await self.send_reply(chat_id, "\n".join(lines))
            except Exception as e:
                await self.send_reply(chat_id, f"❌ <b>Grades Scrape Failed:</b> <code>{escape_html(str(e))}</code>")

        elif cmd == "/watch":
            interval = int(args[0]) if args and args[0].isdigit() else 30
            self.watch_interval = max(interval, 5) * 60
            await self.send_reply(chat_id, f"🔔 <b>Watch Mode Activated</b>\nChecking for grade/announcement updates every {interval} minutes.")

        else:
            await self.send_reply(chat_id, "❓ Unknown command. Type /help to see available commands.")

    async def _periodic_watch_loop(self):
        """Background loop that periodically checks for new grades and announcements."""
        while self.running:
            await asyncio.sleep(self.watch_interval)
            logger.info("Executing periodic watch check...")
            try:
                async with SCRAPER_MUTEX:
                    bundle = await run_briefing_async(headless=True, write_markdown=True, concurrency=4)
                self.notifier.process_and_notify_diffs(bundle)
            except Exception as e:
                logger.error(f"Periodic watch error: {e}")

    async def start_polling(self):
        """Starts polling loop."""
        if not self.bot_token:
            print("❌ Cannot start Telegram bot: No bot token configured.")
            return

        print(f"🤖 Blackboard Telegram Bot Daemon running. Authorized Admin ID: {self.admin_chat_id}")
        self.running = True
        self.watch_task = asyncio.create_task(self._periodic_watch_loop())

        while self.running:
            try:
                updates = await asyncio.to_thread(self._get_updates, self.last_update_id + 1, 20)
                for update in updates:
                    uid = update.get("update_id", 0)
                    if uid > self.last_update_id:
                        self.last_update_id = uid

                    msg = update.get("message", {})
                    chat = msg.get("chat", {})
                    chat_id = chat.get("id")
                    text = msg.get("text", "").strip()

                    if not chat_id or not text:
                        continue

                    # Authorization Guard
                    if not self.is_authorized(chat_id):
                        logger.warning(f"Unauthorized message attempt by chat_id: {chat_id}")
                        await self.send_reply(chat_id, "⛔ <b>Unauthorized:</b> You do not have access to this bot.")
                        continue

                    if text.startswith("/"):
                        asyncio.create_task(self.handle_command(chat_id, text))

            except Exception as e:
                logger.error(f"Polling loop error: {e}")
                await asyncio.sleep(3)


def run_bot():
    """CLI launcher for bot daemon."""
    bot = SimpleTelegramBot()
    asyncio.run(bot.start_polling())
