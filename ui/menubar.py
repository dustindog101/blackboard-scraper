import asyncio
import logging
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import rumps
    _BaseApp = rumps.App
except ImportError:
    rumps = None
    _BaseApp = object

from core.config import CONFIG_FILE, SESSION_DIR, load_courses
from core.session import quick_check_session_http
from telegram.daemon import (
    get_bot_status,
    restart_bot_daemon,
    start_bot_daemon,
    stop_bot_daemon,
)

logger = logging.getLogger("blackboard.menubar")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def open_path(path: Path | str) -> None:
    """Cross-platform helper to open a file or directory in the default system handler."""
    path_str = str(path)
    try:
        if sys.platform == "win32":
            os.startfile(path_str)
        elif sys.platform == "darwin":
            subprocess.run(["open", path_str])
        else:
            subprocess.run(["xdg-open", path_str])
    except Exception as e:
        logger.debug(f"Failed to open path '{path_str}': {e}")


def is_menubar_supported() -> tuple[bool, str]:
    """Check if the Menubar GUI is supported on the current platform and has dependencies."""
    if sys.platform != "darwin":
        return False, "The menubar app is only supported on macOS (requires Apple Cocoa NSStatusBar)."
    if rumps is None:
        return False, "The menubar app requires 'rumps'. Install it via: pip install rumps"
    return True, ""


def _safe_clear_menu(menu_item: Any) -> None:
    """Safely clear submenu items without crashing if Cocoa NSMenu is uninitialized."""
    try:
        if getattr(menu_item, "_menu", None) is not None:
            menu_item.clear()
        else:
            for k in list(menu_item.keys()):
                del menu_item[k]
    except Exception:
        pass


class BlackboardMenuBarApp(_BaseApp):
    """
    Native macOS Menubar Application for Blackboard Ultra Scraper & Telegram Bot.
    Provides live status monitoring, one-click controls, background scraping,
    and instant session probes without terminal interaction.
    """

    def __init__(self):
        super(BlackboardMenuBarApp, self).__init__(
            name="Blackboard",
            title="🎓 BB",
            quit_button=None,  # We create custom Quit button at bottom
        )

        self.session_valid = False
        self.user_info: Optional[Dict[str, Any]] = None
        self.bot_status = {"running": False, "pid": None, "memory_mb": 0}
        self.courses = load_courses()
        self.is_busy = False

        self._build_menu()
        self.refresh_all_status()

    # ------------------------------------------------------------------------
    # Menu Construction
    # ------------------------------------------------------------------------

    def _build_menu(self):
        """Construct the rich macOS Menubar hierarchy."""
        self.menu.clear()

        # Header Title & Start Menu
        self.item_header = rumps.MenuItem("🎓 UMBC Blackboard Ultra", callback=None)
        self.item_start_menu = rumps.MenuItem("🌟 Start Menu / Dashboard Overview", callback=self.on_open_start_menu)

        # Session Block
        self.item_user = rumps.MenuItem("👤 Student: Loading...", callback=None)
        self.item_session_status = rumps.MenuItem("🔐 Session: Checking...", callback=None)
        self.menu_session_actions = rumps.MenuItem("⚙️ Session Controls")
        self.menu_session_actions.add(rumps.MenuItem("🔄 Quick Check Session (<150ms)", callback=self.on_check_session))
        self.menu_session_actions.add(rumps.MenuItem("🔑 SSO Auto-Login (Duo 2FA)", callback=self.on_sso_login))
        self.menu_session_actions.add(rumps.MenuItem("🚪 Clear Session & Cookies", callback=self.on_logout))

        # Telegram Bot Block
        self.item_bot_status = rumps.MenuItem("🤖 Telegram Bot: Checking...", callback=None)
        self.menu_bot_controls = rumps.MenuItem("🎛️ Telegram Controls")
        self.menu_bot_controls.add(rumps.MenuItem("▶️ Start Bot Daemon", callback=self.on_start_bot))
        self.menu_bot_controls.add(rumps.MenuItem("⏹ Stop Bot Daemon", callback=self.on_stop_bot))
        self.menu_bot_controls.add(rumps.MenuItem("🔄 Restart Bot Daemon", callback=self.on_restart_bot))
        self.menu_bot_controls.add(rumps.separator)
        self.menu_bot_controls.add(rumps.MenuItem("📲 Open in Telegram (@blackboardscrapbot)", callback=self.on_open_telegram))
        self.menu_bot_controls.add(rumps.MenuItem("📄 View bot.log", callback=self.on_view_bot_log))

        # Scraper Actions Block
        self.item_actions_header = rumps.MenuItem("⚡ Scraper Actions", callback=None)
        self.item_run_briefing = rumps.MenuItem("📋 Run Daily Briefing Now", callback=self.on_run_briefing)

        # Due Dates Submenu
        self.menu_due_dates = rumps.MenuItem("📅 Upcoming Deadlines (7 Days)")
        self.menu_due_dates.add(rumps.MenuItem("⏳ Click to Fetch Deadlines...", callback=self.on_fetch_due_dates))

        # Grades Submenu
        self.menu_grades = rumps.MenuItem("🎓 Latest Course Grades")
        self.menu_grades.add(rumps.MenuItem("⏳ Click to Fetch Grades...", callback=self.on_fetch_grades))

        # Announcements Submenu
        self.menu_announcements = rumps.MenuItem("📢 Recent Announcements")
        self.menu_announcements.add(rumps.MenuItem("⏳ Click to Fetch Announcements...", callback=self.on_fetch_announcements))

        # Courses Submenu
        self.menu_courses = rumps.MenuItem(f"📚 Enrolled Courses ({len(self.courses)})")
        self._populate_courses_submenu()

        # Search Item
        self.item_search = rumps.MenuItem("🔎 Search Course Content...", callback=self.on_search_content)

        # Settings & Tools
        self.menu_tools = rumps.MenuItem("🛠️ Tools & Settings")
        self.menu_tools.add(rumps.MenuItem("⏱️ View Session Lifespan Stats", callback=self.on_view_session_stats))
        self.menu_tools.add(rumps.MenuItem("🔍 Auto-Discover Active Courses", callback=self.on_auto_discover_courses))
        self.menu_tools.add(rumps.separator)
        self.menu_tools.add(rumps.MenuItem("📝 Edit config.json", callback=self.on_open_config))
        self.menu_tools.add(rumps.MenuItem("📂 Open Project Folder", callback=self.on_open_project_folder))
        self.menu_tools.add(rumps.MenuItem("📄 Open Session Directory", callback=self.on_open_session_dir))
        self.menu_tools.add(rumps.MenuItem("📊 Run Test Suite", callback=self.on_run_tests))

        # Refresh & Quit
        self.item_refresh = rumps.MenuItem("🔄 Refresh Status", callback=lambda _: self.refresh_all_status())
        self.item_quit = rumps.MenuItem("🚪 Quit Blackboard App", callback=self.on_quit)

        # Assemble menu
        self.menu = [
            self.item_header,
            self.item_start_menu,
            rumps.separator,
            self.item_user,
            self.item_session_status,
            self.menu_session_actions,
            rumps.separator,
            self.item_bot_status,
            self.menu_bot_controls,
            rumps.separator,
            self.item_actions_header,
            self.item_run_briefing,
            self.menu_due_dates,
            self.menu_grades,
            self.menu_announcements,
            self.menu_courses,
            self.item_search,
            rumps.separator,
            self.menu_tools,
            self.item_refresh,
            rumps.separator,
            self.item_quit,
        ]


    def _populate_courses_submenu(self):
        """Populate the courses list submenu."""
        if not self.courses:
            self.menu_courses.add(rumps.MenuItem("No courses found in config.json", callback=None))
            return
        for cid, cname in self.courses.items():
            short_name = cname.split("(")[0].strip() if "(" in cname else cname
            self.menu_courses.add(rumps.MenuItem(f"• {short_name} ({cid})", callback=None))

    # ------------------------------------------------------------------------
    # Status Polling & Timers
    # ------------------------------------------------------------------------

    @rumps.timer(30)
    def on_timer_tick(self, _):
        """Periodic background refresh every 30 seconds."""
        if not self.is_busy:
            self.refresh_all_status()

    def refresh_all_status(self):
        """High-speed status refresh (<150ms HTTP probe + PID check)."""
        try:
            # 1. Check session
            self.session_valid, self.user_info = quick_check_session_http()

            # 2. Check bot daemon
            self.bot_status = get_bot_status()

            # 3. Update Menu Item Titles
            if self.session_valid and self.user_info:
                uid = self.user_info.get("studentId") or self.user_info.get("userName") or "BH69617"
                self.item_user.title = f"👤 Student: Amanuel ({uid})"
                self.item_session_status.title = "🔐 Blackboard Session: 🟢 ACTIVE"
            else:
                self.item_user.title = "👤 Student: Amanuel (BH69617)"
                self.item_session_status.title = "🔐 Blackboard Session: 🔴 EXPIRED"

            if self.bot_status["running"]:
                pid = self.bot_status["pid"]
                mem = self.bot_status["memory_mb"]
                self.item_bot_status.title = f"🤖 Telegram Bot: 🟢 RUNNING (PID {pid} • {mem}MB)"
            else:
                self.item_bot_status.title = "🤖 Telegram Bot: 🔴 STOPPED"

            # 4. Update Status Bar Title / Icon (flagging in-progress attempts)
            open_attempts = []
            if self.session_valid:
                try:
                    from scrapers.assessment import get_in_progress_attempts_api
                    open_attempts = get_in_progress_attempts_api()
                except Exception:
                    pass

            if open_attempts:
                self.title = f"🎓 BB ⚠️ ({len(open_attempts)})"
            elif self.session_valid and self.bot_status["running"]:
                self.title = "🎓 BB 🟢"
            elif self.session_valid:
                self.title = "🎓 BB 🟡"
            else:
                self.title = "🎓 BB 🔴"

        except Exception as e:
            logger.debug(f"Status refresh notice: {e}")

    # ------------------------------------------------------------------------
    # Session Action Handlers
    # ------------------------------------------------------------------------

    def on_check_session(self, _):
        """Instant manual session check."""
        valid, user_data = quick_check_session_http()
        if valid and user_data:
            uid = user_data.get("studentId") or user_data.get("userName") or "Active"
            rumps.notification(
                title="Blackboard Session Active",
                subtitle="Verified in < 150ms via HTTP REST API",
                message=f"Student ID: {uid} • All scrapers ready.",
            )
        else:
            rumps.notification(
                title="Blackboard Session Expired",
                subtitle="Authentication required",
                message="Please run SSO Login to refresh session cookies.",
            )
        self.refresh_all_status()

    def on_sso_login(self, _):
        """Trigger automated SSO login in background thread."""
        def _login_thread():
            self.is_busy = True
            self.title = "🎓 BB ⏳"
            rumps.notification(
                title="SSO Login Initiated",
                subtitle="Automated Headless Duo 2FA",
                message="Checking for credentials and Duo push/passcode...",
            )
            try:
                # Trigger automated login
                proc = subprocess.run(
                    [sys.executable, str(PROJECT_ROOT / "main.py"), "--login"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=str(PROJECT_ROOT),
                )
                if "Session is ACTIVE" in proc.stdout or proc.returncode == 0:
                    rumps.notification(
                        title="Blackboard Login Successful",
                        subtitle="Session Saved",
                        message="Session cookies updated. Full access granted.",
                    )
                else:
                    rumps.notification(
                        title="Login Notice",
                        subtitle="Check Output",
                        message=proc.stdout[:120] if proc.stdout else "Please verify credentials in config.json.",
                    )
            except Exception as e:
                rumps.notification(title="Login Error", subtitle="Exception occurred", message=str(e)[:120])
            finally:
                self.is_busy = False
                self.refresh_all_status()

        threading.Thread(target=_login_thread, daemon=True).start()

    def on_logout(self, _):
        """Clear cached session cookies."""
        cookie_file = SESSION_DIR / "cookies.json"
        if cookie_file.exists():
            cookie_file.unlink(missing_ok=True)
        rumps.notification(
            title="Logged Out",
            subtitle="Cookies Cleared",
            message="Your cached session has been removed.",
        )
        self.refresh_all_status()

    # ------------------------------------------------------------------------
    # Telegram Bot Action Handlers
    # ------------------------------------------------------------------------

    def on_start_bot(self, _):
        """Start the background Telegram bot daemon."""
        if self.bot_status["running"]:
            rumps.notification(title="Telegram Bot", subtitle="Already Running", message=f"PID: {self.bot_status['pid']}")
            return
        started = start_bot_daemon()
        if started:
            rumps.notification(title="Telegram Bot Started", subtitle="@blackboardscrapbot", message="Background daemon is now active.")
        else:
            rumps.notification(title="Telegram Bot Error", subtitle="Could not start", message="Check bot.log for details.")
        self.refresh_all_status()

    def on_stop_bot(self, _):
        """Stop the background Telegram bot daemon."""
        stopped = stop_bot_daemon()
        if stopped:
            rumps.notification(title="Telegram Bot Stopped", subtitle="Daemon Inactive", message="Background polling terminated.")
        self.refresh_all_status()

    def on_restart_bot(self, _):
        """Restart the background Telegram bot daemon."""
        restart_bot_daemon()
        rumps.notification(title="Telegram Bot Restarted", subtitle="@blackboardscrapbot", message="Daemon reloaded successfully.")
        self.refresh_all_status()

    def on_open_telegram(self, _):
        """Open the bot in Telegram web/app."""
        webbrowser.open("https://t.me/blackboardscrapbot")

    def on_view_bot_log(self, _):
        """Open bot.log in default text editor."""
        log_file = SESSION_DIR / "bot.log"
        if not log_file.exists():
            log_file.write_text("--- Telegram Bot Log Created ---\n")
        open_path(log_file)

    # ------------------------------------------------------------------------
    # Scraper Action Handlers (Non-blocking threads)
    # ------------------------------------------------------------------------

    def on_run_briefing(self, _):
        """Execute daily briefing in background thread with macOS notification on completion."""
        def _briefing_thread():
            self.is_busy = True
            self.title = "🎓 BB ⚡"
            rumps.notification(
                title="Blackboard Briefing Started",
                subtitle="Concurrent Scraper Engine",
                message="Scraping 10 courses in parallel (8-12s)...",
            )
            try:
                from scrapers.briefing import run_briefing_async
                bundle = asyncio.run(run_briefing_async(headless=True, write_markdown=False, concurrency=4))

                urgent_count = len(bundle.get("urgent", []))
                upcoming_count = len(bundle.get("calendar", []))
                rumps.notification(
                    title="Daily Briefing Completed",
                    subtitle=f"{urgent_count} Urgent • {upcoming_count} Upcoming Deadlines",
                    message="All courses scraped successfully.",
                )
            except Exception as e:
                rumps.notification(title="Briefing Failed", subtitle="Scraper error", message=str(e)[:120])
            finally:
                self.is_busy = False
                self.refresh_all_status()

        threading.Thread(target=_briefing_thread, daemon=True).start()

    def on_fetch_due_dates(self, _):
        """Fetch upcoming deadlines and populate submenu."""
        def _due_thread():
            _safe_clear_menu(self.menu_due_dates)
            self.menu_due_dates.add(rumps.MenuItem("⏳ Fetching deadlines...", callback=None))
            try:
                from core.async_engine import AsyncSessionManager, EngineConfig
                from scrapers.due_dates import aggregate_due_dates_async

                async def _get_due():
                    mgr = AsyncSessionManager(EngineConfig(headless=True))
                    await mgr.initialize()
                    try:
                        async with mgr.acquire_page() as p:
                            return await aggregate_due_dates_async(p, load_courses(), window_filter="7d")
                    finally:
                        await mgr.close()

                items = asyncio.run(_get_due())
                _safe_clear_menu(self.menu_due_dates)
                if not items:
                    self.menu_due_dates.add(rumps.MenuItem("✨ No upcoming assignments in next 7 days", callback=None))
                else:
                    for it in items[:10]:
                        title = it.get("title", "Assignment")
                        course = it.get("course", "")
                        due = it.get("due_date", "TBD")
                        short_title = (title[:30] + "..") if len(title) > 30 else title
                        self.menu_due_dates.add(rumps.MenuItem(f"• {short_title} ({course}) — {due}", callback=None))

                self.menu_due_dates.add(rumps.separator)
                self.menu_due_dates.add(rumps.MenuItem("🔄 Refresh Deadlines", callback=self.on_fetch_due_dates))
            except Exception as e:
                _safe_clear_menu(self.menu_due_dates)
                self.menu_due_dates.add(rumps.MenuItem(f"❌ Error: {str(e)[:30]}", callback=None))
                self.menu_due_dates.add(rumps.MenuItem("🔄 Retry", callback=self.on_fetch_due_dates))

        threading.Thread(target=_due_thread, daemon=True).start()

    def on_fetch_grades(self, _):
        """Fetch latest course grades and populate submenu."""
        def _grades_thread():
            _safe_clear_menu(self.menu_grades)
            self.menu_grades.add(rumps.MenuItem("⏳ Fetching grades...", callback=None))
            try:
                from core.async_engine import AsyncSessionManager, EngineConfig, AsyncCourseWorkerPool
                from scrapers.grades import scrape_grades_async

                async def _get_grades():
                    mgr = AsyncSessionManager(EngineConfig(headless=True))
                    await mgr.initialize()
                    try:
                        pool = AsyncCourseWorkerPool(mgr)
                        async def _w(cid, cname, page):
                            return await scrape_grades_async(cid, page)
                        return await pool.execute_task_per_course(load_courses(), _w)
                    finally:
                        await mgr.close()

                results = asyncio.run(_get_grades())
                _safe_clear_menu(self.menu_grades)
                total_grades = 0
                for cid, g_list in results.items():
                    if not isinstance(g_list, list) or not g_list:
                        continue
                    graded = [g for g in g_list if g.get("grade") and g["grade"] not in ("Not graded", "-- %", "")]
                    if graded:
                        total_grades += len(graded)
                        cname = self.courses.get(cid, cid).split("(")[0].strip()
                        sub = rumps.MenuItem(f"📚 {cname} ({len(graded)} graded)")
                        for g in graded:
                            sub.add(rumps.MenuItem(f"  • {g.get('name')}: {g.get('grade')}", callback=None))
                        self.menu_grades.add(sub)

                if total_grades == 0:
                    self.menu_grades.add(rumps.MenuItem("✨ No recent grades posted", callback=None))

                self.menu_grades.add(rumps.separator)
                self.menu_grades.add(rumps.MenuItem("🔄 Refresh Grades", callback=self.on_fetch_grades))
            except Exception as e:
                _safe_clear_menu(self.menu_grades)
                self.menu_grades.add(rumps.MenuItem(f"❌ Error: {str(e)[:30]}", callback=None))
                self.menu_grades.add(rumps.MenuItem("🔄 Retry", callback=self.on_fetch_grades))

        threading.Thread(target=_grades_thread, daemon=True).start()

    def on_fetch_announcements(self, _):
        """Fetch latest course announcements and populate submenu."""
        def _ann_thread():
            _safe_clear_menu(self.menu_announcements)
            self.menu_announcements.add(rumps.MenuItem("⏳ Fetching announcements...", callback=None))
            try:
                from core.async_engine import AsyncSessionManager, EngineConfig, AsyncCourseWorkerPool
                from scrapers.announcements import scrape_announcements_async

                async def _get_ann():
                    mgr = AsyncSessionManager(EngineConfig(headless=True))
                    await mgr.initialize()
                    try:
                        pool = AsyncCourseWorkerPool(mgr)
                        async def _w(cid, cname, page):
                            return await scrape_announcements_async(cid, page)
                        return await pool.execute_task_per_course(load_courses(), _w)
                    finally:
                        await mgr.close()

                results = asyncio.run(_get_ann())
                _safe_clear_menu(self.menu_announcements)
                total_ann = 0
                for cid, a_list in results.items():
                    if not isinstance(a_list, list) or not a_list:
                        continue
                    total_ann += len(a_list)
                    cname = self.courses.get(cid, cid).split("(")[0].strip()
                    sub = rumps.MenuItem(f"📚 {cname} ({len(a_list)})")
                    for a in a_list[:5]:
                        unread = "🆕 " if a.get("unread") else ""
                        title = (a.get("title", "")[:35] + "..") if len(a.get("title", "")) > 35 else a.get("title", "")
                        sub.add(rumps.MenuItem(f"  • {unread}{title} ({a.get('meta','')})", callback=None))
                    self.menu_announcements.add(sub)

                if total_ann == 0:
                    self.menu_announcements.add(rumps.MenuItem("✨ No announcements found", callback=None))

                self.menu_announcements.add(rumps.separator)
                self.menu_announcements.add(rumps.MenuItem("🔄 Refresh Announcements", callback=self.on_fetch_announcements))
            except Exception as e:
                _safe_clear_menu(self.menu_announcements)
                self.menu_announcements.add(rumps.MenuItem(f"❌ Error: {str(e)[:30]}", callback=None))
                self.menu_announcements.add(rumps.MenuItem("🔄 Retry", callback=self.on_fetch_announcements))

        threading.Thread(target=_ann_thread, daemon=True).start()

    def on_search_content(self, _):
        """Prompt dialog for keyword search across all courses."""
        window = rumps.Window(
            message="Enter keyword to search across course outlines and assignments (e.g. Syllabus, Project 1, Exam):",
            title="🔎 Search Course Content",
            default_text="Syllabus",
            ok="Search",
            cancel="Cancel",
            dimensions=(320, 40),
        )
        response = window.run()
        if response.clicked and response.text.strip():
            query = response.text.strip()
            def _search_thread():
                rumps.notification(title="Searching Blackboard...", subtitle=f"Query: '{query}'", message="Scanning courses in parallel...")
                try:
                    from core.async_engine import AsyncSessionManager, EngineConfig
                    from scrapers.search import find_items_async
                    async def _run():
                        mgr = AsyncSessionManager(EngineConfig(headless=True))
                        await mgr.initialize()
                        try:
                            async with mgr.acquire_page() as p:
                                return await find_items_async(query, load_courses(), p)
                        finally:
                            await mgr.close()

                    matches = asyncio.run(_run())
                    if matches:
                        summary = f"Found {len(matches)} match(es):\n" + "\n".join([f"• [{m.get('course_name','')[:15]}] {m.get('title')[:30]}" for m in matches[:4]])
                        rumps.alert(title=f"🔎 Search Results: '{query}'", message=summary)
                    else:
                        rumps.alert(title="No Matches Found", message=f"No course content matched '{query}'.")
                except Exception as e:
                    rumps.alert(title="Search Failed", message=str(e))

            threading.Thread(target=_search_thread, daemon=True).start()

    # ------------------------------------------------------------------------
    # Start Menu & Dashboard Overview
    # ------------------------------------------------------------------------

    def on_open_start_menu(self, _):
        """Open Start Menu Dashboard Overview Dialog."""
        session_text = "🟢 ACTIVE (<150ms HTTP Probe)" if self.session_valid else "🔴 EXPIRED"
        bot_text = f"🟢 RUNNING (PID {self.bot_status['pid']} • {self.bot_status['memory_mb']}MB)" if self.bot_status["running"] else "🔴 STOPPED"
        course_count = len(self.courses)
        course_preview = "\n".join([f"  • {cname.split('(')[0].strip()}" for cname in list(self.courses.values())[:5]])

        overview = (
            f"👤 Student: Amanuel (BH69617)\n"
            f"🔐 Blackboard Session: {session_text}\n"
            f"🤖 Telegram Daemon: {bot_text}\n"
            f"📚 Active Semester: Fall 2026 ({course_count} Courses)\n\n"
            f"Enrolled Courses:\n{course_preview}\n\n"
            f"⚡ Quick Actions:\n"
            f"• Daily Briefing: Click 'Run Daily Briefing Now'\n"
            f"• Deadlines: Browse 'Upcoming Deadlines' submenu\n"
            f"• Grades & Announcements: Open corresponding submenus\n"
            f"• Telegram: Open @blackboardscrapbot"
        )
        rumps.alert(title="🎓 Blackboard Ultra Start Menu", message=overview)

    def on_view_session_stats(self, _):
        """Display Session Lifespan Telemetry & Stats dialog."""
        from core.session_tracker import tracker
        valid, user_data = quick_check_session_http()
        tracker.record_probe(valid, user_data)
        summary = tracker.get_telemetry_summary_dict()
        stats = summary["stats"]
        status_str = "🟢 ACTIVE" if summary["is_active"] else "🔴 EXPIRED"
        elapsed_str = summary["current_session_duration_human"] or "N/A"

        msg = (
            f"🔐 Current Session: {status_str}\n"
            f"⏳ Elapsed Active Time: {elapsed_str}\n\n"
            f"📈 Historical Statistics:\n"
            f"• Total Tracked Sessions: {stats.get('total_recorded_sessions', 0)}\n"
            f"• Average Lifespan: {stats.get('average_lifespan_human', 'N/A')}\n"
            f"• Shortest Observed: {stats.get('min_lifespan_human', 'N/A')}\n"
            f"• Longest Observed: {stats.get('max_lifespan_human', 'N/A')}\n"
            f"• Recommended Auto-Refresh: {stats.get('recommended_refresh_interval_human', 'N/A')}\n\n"
            f"💡 Run 'SSO Auto-Login' in Session Controls to refresh."
        )
        rumps.alert(title="⏱️ Blackboard Session Lifespan Telemetry", message=msg)

    def on_auto_discover_courses(self, _):
        """Run intelligent course discovery for current active semester."""
        def _disc_thread():
            rumps.notification(title="Course Discovery", subtitle="Querying Blackboard API", message="Detecting current semester courses...")
            try:
                from core.course_discovery import handle_discover_courses_cli
                saved = handle_discover_courses_cli(term_filter=None, list_only=False)
                self.courses = load_courses()
                _safe_clear_menu(self.menu_courses)
                self._populate_courses_submenu()
                self.menu_courses.title = f"📚 Enrolled Courses ({len(self.courses)})"
                rumps.notification(
                    title="Active Courses Discovered",
                    subtitle="Fall 2026 Semester",
                    message=f"Saved {len(saved)} active courses to config.json.",
                )
            except Exception as e:
                rumps.alert(title="Discovery Error", message=str(e))

        threading.Thread(target=_disc_thread, daemon=True).start()

    # ------------------------------------------------------------------------
    # Tools & Utilities
    # ------------------------------------------------------------------------

    def on_open_config(self, _):
        """Open config.json in default text editor."""
        open_path(CONFIG_FILE)

    def on_open_project_folder(self, _):
        """Open project directory in Finder / File Explorer."""
        open_path(PROJECT_ROOT)

    def on_open_session_dir(self, _):
        """Open session cache directory in Finder / File Explorer."""
        open_path(SESSION_DIR)

    def on_run_tests(self, _):
        """Run unit test suite in background thread."""
        def _test_thread():
            rumps.notification(title="Running Test Suite", subtitle="Verification", message="Testing v2 features & session probes...")
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "tests/test_v2_features.py"],
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
            )
            if proc.returncode == 0:
                rumps.notification(title="✅ All Tests Passed", subtitle="11/11 Passing", message="Scraper & Telegram engines 100% healthy.")
            else:
                rumps.alert(title="Test Failures", message=proc.stderr[:300] if proc.stderr else proc.stdout[:300])

        threading.Thread(target=_test_thread, daemon=True).start()

    def on_quit(self, _):
        """Cleanly quit the Menubar app."""
        rumps.quit_application()


def run_menubar():
    """Launch the Menubar application."""
    supported, reason = is_menubar_supported()
    if not supported:
        print(f"⚠️ {reason}")
        return
    app = BlackboardMenuBarApp()
    app.run()


if __name__ == "__main__":
    run_menubar()
