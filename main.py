import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root is in sys.path when executed as a global console script
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ensure Windows console streams support UTF-8 (emojis, box drawing, unicode characters)
if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from core.config import BLACKBOARD_BASE, load_courses, save_courses
from core.export_json import build_composite_schema, build_export_doc
from core.session import _launch_context, _require_session, _require_session_async, check_session_async, login, login_auto, quick_check_session_http
from core.async_engine import AsyncSessionManager, AsyncCourseWorkerPool, EngineConfig, TaskProfile, get_optimal_concurrency

# Scrapers
from scrapers.activity import save_activity, scrape_activity_async
from scrapers.announcements import save_announcements, scrape_announcements_async
from scrapers.calendar import save_calendar, scrape_calendar_async
from scrapers.discussions import save_discussions, scrape_discussions
from scrapers.grades import save_grades, scrape_grades_async
from scrapers.profile import save_profile, scrape_profile_async
from scrapers.briefing import run_briefing_async, format_briefing_cli
from scrapers.outline import (
    scrape_course_outline_async,
    save_outline,
    format_outline_tree,
    clean_outline_json,
    filter_outline_by_folder,
    interactive_folder_picker,
)
from scrapers.assignments import scrape_course_assignments_async, save_assignments, format_assignments_summary
from scrapers.due_dates import aggregate_due_dates_async, save_due_dates, format_due_dates_table
from scrapers.search import find_items_async, grab_item_async


# ---------------------------------------------------------------------------
# Output & Formatting Helpers
# ---------------------------------------------------------------------------

def _safe_relpath(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _emit_json(args: argparse.Namespace, data: Any, source: str = "blackboard-scraper") -> None:
    """Print structured JSON to stdout or save to file if --out is provided."""
    pretty = not args.compact
    if isinstance(data, dict) and "courses" in data and isinstance(data.get("courses"), dict):
        # Full briefing bundle -> composite document
        payload = build_composite_schema(data, source=source, pretty=pretty)
    elif isinstance(data, list) and (not data or "kind" in data[0]):
        payload = build_export_doc(data, source=source, pretty=pretty)
    else:
        if pretty:
            payload = json.dumps(data, indent=2, ensure_ascii=False)
        else:
            payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload)
        print(f"💾 JSON exported to: {_safe_relpath(out_path)}", file=sys.stderr)
    else:
        print(payload)


def _print_profile(data: dict) -> None:
    if not data:
        print("No profile data found.")
        return
    print("\n👤 Blackboard Profile")
    print(f"  Name: {data.get('name', 'N/A')}")
    print(f"  Email: {data.get('email', 'N/A')}")
    print(f"  Student ID: {data.get('student_id', 'N/A')}")
    print(f"  Pronouns: {data.get('pronouns', 'N/A')}")
    print(f"  System Role: {data.get('system_role', 'N/A')}")
    print(f"  Privacy: {data.get('privacy', 'N/A')}\n")


# ---------------------------------------------------------------------------
# Smart Course Selection & Fuzzy Matching
# ---------------------------------------------------------------------------

_COURSE_CODE_RE = re.compile(r"\s*\([^)]*\)\s*(?:SP|FA|SU|WI)\d{4}\s*\([^)]*\)\s*$")


def _short_course_name(raw: str | None) -> str | None:
    """Strip trailing section/semester codes from Blackboard course names."""
    if not raw:
        return raw
    cleaned = _COURSE_CODE_RE.sub("", raw).strip()
    return cleaned or raw


def resolve_target_courses(course_arg: Optional[str], all_flag: bool, courses: Dict[str, str]) -> List[str]:
    """
    Smart multi-mode course selector:
    - Exact Blackboard IDs: '_105737_1'
    - Course codes: 'IS410', 'IS 410', 'ECON122', 'MATH 215'
    - Fuzzy title keywords: 'Database', 'Accounting'
    - Comma-separated list: 'IS410,ENGL100' or '_105737_1,_108410_1'
    - --all flag: all configured courses
    """
    if all_flag:
        return list(courses.keys())

    if not course_arg:
        return []

    tokens = [t.strip() for t in course_arg.split(",") if t.strip()]
    matched_ids: List[str] = []

    for token in tokens:
        token_clean = token.lower().replace(" ", "").replace("_", "")

        # 1. Exact ID match
        if token in courses:
            matched_ids.append(token)
            continue

        # 2. Match by course code or title keyword
        found = False
        for cid, cname in courses.items():
            cname_clean = cname.lower().replace(" ", "").replace("_", "")
            cid_clean = cid.lower().replace("_", "")

            if token_clean in cname_clean or token_clean in cid_clean:
                if cid not in matched_ids:
                    matched_ids.append(cid)
                found = True

        if not found:
            print(f"⚠️ Warning: Could not match course token '{token}' to any enrolled course.", file=sys.stderr)

    return matched_ids


# ---------------------------------------------------------------------------
# Rich Topic Help Guides
# ---------------------------------------------------------------------------

HELP_GUIDES: Dict[str, str] = {
    "auth": """
🔐 Authentication & Full Headless Execution Guide:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Daily & Ongoing Scraping (100% Fully Headless):
   - Persistent session cookies are stored in .session/cookies.json.
   - Sessions last for weeks to months without re-authenticating.
   - All CLI commands, background cron jobs, and Telegram bot commands run
     completely in the background with zero browser popups and zero user intervention.

2. One-Time Setup & Automated SSO Logins:
   $ python3 main.py --auto-exp
   - If no config or login is detected, a template config.json is automatically created.
   - Launches an interactive TUI setup wizard to enter UMBC email/username and password.
   - Automatically intercepts incoming macOS Duo 2FA SMS passcodes in <3ms.
   - Saves cookies for seamless headless operation.

3. Standard Automated Login:
   $ python3 main.py --login --auto
   - Automatically fills your UMBC username and password on WebAuth.
   - Dispatches Duo 2FA SMS passcode to your phone.
   - Accepts 6-digit passcode via macOS SMS capture, Telegram reply, or terminal prompt.

4. Visible Manual Login Fallback:
   $ python3 main.py --login
   - Opens a visible browser if you prefer Duo Push, TouchID, or Security Keys.
""",
    "courses": """
🔀 Smart Course Selection Syntax:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You can target courses in multiple flexible ways:

• Target by Course Code:
  $ python3 main.py --outline -c IS410
  $ python3 main.py --assignments -c ENGL100
  $ python3 main.py --grades -c "ECON 122"

• Target Multiple Courses (Comma-Separated):
  $ python3 main.py --outline -c IS410,ENGL100,MATH215
  $ python3 main.py --assignments -c IS410,STAT351

• Target by Fuzzy Title Keyword:
  $ python3 main.py --outline -c Database
  $ python3 main.py --grades -c Accounting

• Target All Configured Courses:
  $ python3 main.py --outline --all
  $ python3 main.py --briefing
""",
    "schema": """
📦 Standardized v2 JSON Schemas:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Output clean JSON to stdout (--json) or export to file (--out <path>):

• Full Composite Document (--briefing --json):
  {
    "version": "2.0",
    "generated_at": 1786938000,
    "generated_at_human": "2026-08-16T23:40:00Z",
    "summary": { "total_courses": 5, "upcoming_deadlines_count": 2, ... },
    "user": { "username": "BH69617", "name": "Amanuel Hailie" },
    "courses": [ { "course_id": "...", "syllabus": {...}, "outline": [...], "assignments": [...], ... } ],
    "global": { "activity_stream": [...], "calendar_due_dates": [...] }
  }

• Targeted Deadline Items (--due 7d --json):
  { "version": "2.0", "total_items": 3, "items": [ { "title": "...", "course": "...", "due_date": "..." } ] }

• Targeted Outline Trees (--outline -c IS410 --json):
  [ { "course_id": "_105737_1", "course_name": "IS 410", "items": [ { "title": "...", "content_type": "folder", "depth": 0 } ] } ]
""",
    "telegram": """
🤖 Telegram Bot & Alerts Guide:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Setup in config.json:
   {
     "telegram": {
       "enabled": true,
       "bot_token": "123456789:ABCdefGhIJKlmNoPQRstuVWXyz",
       "admin_chat_id": 123456789
     }
   }

2. Launch Bot Daemon:
   $ python3 main.py --bot
   # or
   $ python3 telegram_bot.py

3. Supported Bot Commands:
   /briefing           - Trigger concurrent school briefing
   /due [days]         - View upcoming deadlines (e.g. /due 7)
   /grades [course]    - Check recent grades
   /announcements [c]  - View unread course announcements
   /courses            - List configured courses
   /check              - Verify Blackboard session health
   /watch [mins]       - Start background monitoring loop
   /help               - View command manual
""",
    "concurrency": """
⚡ Smart Adaptive Concurrency Engine:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The engine selects optimal worker profiles and auto-tunes dynamically:

• LIGHT Profile (6-8 workers):
  Used for shallow DOM queries: --announcements, --grades, --calendar.
• MEDIUM Profile (4-5 workers):
  Used for composite streams: --briefing, --due, --activity.
• HEAVY Profile (2-3 workers):
  Used for deep operations: --outline (tree expansion), --assignments (drawers).

• Dynamic Auto-Scaling:
  - Latency < 1.0s: Concurrency scales up automatically.
  - Timeouts / Slow Network: Concurrency throttles down to prevent browser stalls.
  - Closed Courses: Skipped in < 120ms via circuit-breaker detection.
"""
}


# ---------------------------------------------------------------------------
# CLI Argument Parser
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bb",
        description="""
╔═══════════════════════════════════════════════════════════════════════╗
║       UMBC Blackboard Ultra High-Performance Scraper & Assistant      ║
╚═══════════════════════════════════════════════════════════════════════╝
High-speed headless scraper with adaptive concurrency, deep outline & syllabus
extraction, safe assignment drawer inspection, dead-simple course selection,
clean terminal UI by default, and standardized v2 JSON schemas.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
═══════════════════════════════════════════════════════════════════════
💡 QUICK START EXAMPLES (Global CLI: bb / blackboard / bbscraper)
═══════════════════════════════════════════════════════════════════════
  bb --briefing                                       # Print high-speed briefing to CLI stdout
  bb --due 7d                                         # Print upcoming deadlines table to CLI stdout
  bb --outline -c MATH215                             # View shallow outline summary with folder counts
  bb --outline -c MATH215 -f "Homework"               # Selectively expand a specific folder
  bb --outline -c MATH215 --expand-all                # View full recursive outline tree
  bb --outline -c MATH215 -i                          # Interactive terminal folder explorer
  bb --outline -c MATH215 --json                      # Clean, compact outline JSON export
  bb --search "Python"                                # Search content across all courses
  bb --download "Chapter01.ipynb"                     # Auto-detects course and downloads file
  bb --download _8825690_1                            # Download by exact Blackboard item ID
  bb --grades -c MATH215                              # View gradebook and feedback
  bb --profile                                        # View student profile (<150ms)
  bb --auto-exp                                       # Fully automated SSO login + Duo 2FA SMS capture
  bb --bot -d                                         # Launch Telegram bot daemon in background

📖 DETAILED TOPIC GUIDES:
  bb --guide auth                                     # Authentication & Headless execution
  bb --guide courses                                  # Course selection & multi-course syntax
  bb --guide schema                                   # Standardized v2 JSON schemas
  bb --guide telegram                                 # Telegram bot & notifications
  bb --guide concurrency                              # Adaptive async worker engine
        """,
    )

    raw_args = sys.argv[1:]
    if "-auto" in raw_args:
        parser.error("Use --auto (or -a). The token '-auto' is ambiguous.")

    # --- help guides ---
    guides = parser.add_argument_group("help & documentation")
    guides.add_argument("--guide", choices=["auth", "courses", "schema", "telegram", "concurrency"], help="Show comprehensive topic manual")

    # --- authentication ---
    auth = parser.add_argument_group("authentication & session")
    auth.add_argument("--login", action="store_true", help="Login via UMBC SSO (skips if session is already active)")
    auth.add_argument("--logout", action="store_true", help="Logout (clear cached session cookies)")
    auth.add_argument("--auto", "-a", action="store_true", help="With --login: automated SSO + Duo text passcode login")
    auth.add_argument("--auto-exp", "--login-auto-exp", action="store_true", help="[EXPERIMENTAL] Fully automated SSO login with real-time macOS SMS Duo 2FA capture")
    auth.add_argument("--force", action="store_true", help="With --login: force re-login even if session exists")

    auth.add_argument("--username", "-u", help="Username for automated login (prompts if omitted)")
    auth.add_argument("--password", "-p", help="Password for automated login (prompts if omitted)")
    auth.add_argument("--duo-passcode", help="Provide 6-digit Duo SMS passcode directly via CLI")
    auth.add_argument("--check-session", action="store_true", help="Test if current session cookies are valid")
    auth.add_argument("--session-info", action="store_true", help="Show session creation & last used timestamps")
    auth.add_argument("--session-stats", "--session-telemetry", action="store_true", help="Show session longevity statistics and average lifespan telemetry")
    auth.add_argument("--debug", action="store_true", help="Print debug output (use with --check-session)")


    # --- discovery ---
    disc = parser.add_argument_group("course discovery")
    disc.add_argument("--discover", "--discover-courses", action="store_true", help="Auto-discover and intelligently save current active semester courses")
    disc.add_argument("--term", metavar="TERM", help="With --discover: filter academic term (e.g. 'current', 'FA2026', 'SP2026', 'all')")
    disc.add_argument("--list-terms", action="store_true", help="List all enrolled academic terms and courses without modifying config.json")
    disc.add_argument("--courses", "--list-courses", action="store_true", help="List configured courses and IDs")


    # --- scrapers ---
    scrapers = parser.add_argument_group("scrapers & features")
    scrapers.add_argument("--briefing", action="store_true", help="Run high-speed concurrent daily briefing across all courses")
    scrapers.add_argument("--activity", action="store_true", help="Scrape homepage activity stream")
    scrapers.add_argument("--calendar", action="store_true", help="Scrape calendar due-dates")
    scrapers.add_argument("--announcements", action="store_true", help="Scrape course announcements")
    scrapers.add_argument("--grades", action="store_true", help="Scrape gradebook")
    scrapers.add_argument("--discussions", action="store_true", help="Scrape course discussions")
    scrapers.add_argument("--outline", action="store_true", help="Scrape full course outline, modules, syllabi, and files")
    scrapers.add_argument("--assignments", action="store_true", help="Deep scrape assignments with prompts, rubrics, and files")
    scrapers.add_argument("--due", nargs="?", const="7d", default=None, metavar="WINDOW", help="Aggregate cross-course due dates (e.g. 7d, 14d, overdue)")
    scrapers.add_argument("--upcoming", type=int, metavar="DAYS", help="Alias for --due <N>d")
    scrapers.add_argument("--exclude-completed", action="store_true", help="With --due: exclude submitted/graded items")
    scrapers.add_argument("--find", "--search", metavar="QUERY", help="Search for content/assignments matching query across courses")
    scrapers.add_argument("--grab", "--download", metavar="ITEM_ID", help="Grab and download specific content item or file")
    scrapers.add_argument("--out-dir", default="downloads", help="Destination directory for downloaded course files (default: ./downloads)")
    scrapers.add_argument("--profile", action="store_true", help="Show student profile information")

    # --- item filtering & selection ---
    filt = parser.add_argument_group("filtering & course selection")
    filt.add_argument("--course", "-c", help="Target course ID(s) or code(s), e.g. 'IS410' or 'IS410,ENGL100'")
    filt.add_argument("--all", action="store_true", help="Run against all configured courses")
    filt.add_argument("--folder", "-f", metavar="FOLDER", help="Expand specific folder or module by name or ID (e.g. -f 'Homework' or -f _105740_1)")
    filt.add_argument("--expand-all", "--deep", "--all-folders", dest="expand_all", action="store_true", help="Expand all folders and display full recursive outline tree")
    filt.add_argument("--depth", type=int, metavar="N", help="Limit outline display depth (e.g. --depth 1 for top-level, --depth 2 for 1 level inside)")
    filt.add_argument("--interactive", "-i", action="store_true", help="Interactive terminal menu to select and explore course folders")
    filt.add_argument("--type", help="Filter outline items by type (e.g. syllabus, document, assignment, folder, link)")
    filt.add_argument("--filter", dest="keyword_filter", help="Filter items by text keyword")

    # --- performance & execution ---
    perf = parser.add_argument_group("performance & execution")
    perf.add_argument("--concurrency", type=int, metavar="N", help="Override dynamic concurrency worker pool size")
    perf.add_argument("--visible", "-v", action="store_true", help="Show browser window (useful for debugging)")
    perf.add_argument("--cdp", help="Connect to existing browser via CDP URL (e.g. http://localhost:9222)")

    # --- telegram integration ---
    tg = parser.add_argument_group("telegram integration")
    tg.add_argument("--telegram", action="store_true", help="Send briefing/results to configured Telegram chat")
    tg.add_argument("--bot", action="store_true", help="Start the interactive Telegram bot daemon")
    tg.add_argument("--daemon", "-d", action="store_true", help="With --bot: run daemon detached in background")
    tg.add_argument("--bot-status", action="store_true", help="Check running status, PID, and memory of Telegram bot daemon")
    tg.add_argument("--bot-stop", action="store_true", help="Gracefully stop background Telegram bot daemon")
    tg.add_argument("--bot-restart", action="store_true", help="Restart background Telegram bot daemon")


    # --- menubar & gui ---
    gui = parser.add_argument_group("gui & menubar")
    gui.add_argument("--menubar", action="store_true", help="Launch native macOS Menubar app for Blackboard & Telegram bot")

    # --- output formats & file saving ---

    output = parser.add_argument_group("output formats & file saving")
    output.add_argument("--json", action="store_true", help="Output standardized JSON to CLI stdout (No files saved)")
    output.add_argument("--out", metavar="FILE", help="Save JSON output directly to FILE")
    output.add_argument("--md", "--save", dest="md", action="store_true", help="Save formatted markdown file(s) to output/ directory")
    output.add_argument("--raw", action="store_true", help="Output raw unformatted scraper data")
    output.add_argument("--compact", action="store_true", help="Emit minified JSON")
    output.add_argument("--source", default="blackboard-scraper", metavar="NAME", help="Value for the JSON source field")

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Discovery Subcommand
# ---------------------------------------------------------------------------

def _handle_discover_courses(term_filter: str | None = None, list_only: bool = False, headless: bool = True, cdp: str | None = None) -> None:
    from core.course_discovery import handle_discover_courses_cli
    res = handle_discover_courses_cli(term_filter=term_filter, list_only=list_only)
    if not res and not list_only:
        if not _require_session(cdp):
            return
        print("🔍 Attempting fallback browser course discovery...", file=sys.stderr)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx, page = _launch_context(p, headless, cdp)
            from scrapers.base import _navigate_and_check_page

            url = f"{BLACKBOARD_BASE}/ultra/course"
            if not _navigate_and_check_page(page, url):
                ctx.close()
                return

            page.wait_for_timeout(3000)
            page.wait_for_selector(".course-element-card", timeout=15_000)

            discovered = page.evaluate(
                """() => {
                    const results = {};
                    document.querySelectorAll('.course-element-card').forEach(el => {
                        const titleEl = el.querySelector('.js-course-title-element');
                        const courseId = el.getAttribute('data-course-id');
                        if (titleEl && courseId) {
                            results[courseId] = titleEl.innerText.trim();
                        }
                    });
                    return results;
                }"""
            )

            if discovered:
                print(f"   ✅ Found {len(discovered)} courses via browser:", file=sys.stderr)
                for cid, cname in discovered.items():
                    print(f"      {cid}: {cname}", file=sys.stderr)
                save_courses(discovered, overwrite=True)
                print("   💾 Saved to config.json", file=sys.stderr)
            else:
                print("   ❌ No courses found.", file=sys.stderr)
            ctx.close()



# ---------------------------------------------------------------------------
# Main Async Execution Dispatcher
# ---------------------------------------------------------------------------

async def main_async(args: argparse.Namespace) -> None:
    # --- topic guides ---
    if args.guide:
        guide_text = HELP_GUIDES.get(args.guide)
        if guide_text:
            print(guide_text.strip())
        return

    headless = not args.visible
    cdp = args.cdp
    courses = load_courses()
    if sys.platform != "win32" and os.path.exists("/tmp"):
        os.environ["TMPDIR"] = "/tmp"

    # --- telegram bot daemon management ---
    if args.bot_status:
        from telegram.daemon import get_bot_status
        status = get_bot_status()
        if status["running"]:
            is_valid, _ = quick_check_session_http()
            sess_str = "✅ ACTIVE" if is_valid else "❌ EXPIRED"
            print("\n🤖 Telegram Bot Daemon Status:")
            print(f"  • State:       🟢 RUNNING (PID: {status['pid']})")
            print(f"  • Memory:      {status['memory_mb']} MB (RSS)")
            print(f"  • Session:     {sess_str}")
            print(f"  • Courses:     {len(courses)} configured")
            print(f"  • Log File:    {status['log_file']}\n")
        else:
            print("\n🤖 Telegram Bot Daemon: 🔴 STOPPED\n   Run `python3 main.py --bot -d` to launch in background.\n")
        return

    if args.bot_stop:
        from telegram.daemon import stop_bot_daemon
        stop_bot_daemon()
        return

    if args.bot_restart:
        from telegram.daemon import restart_bot_daemon
        restart_bot_daemon()
        return

    if args.bot:
        if args.daemon:
            from telegram.daemon import start_bot_daemon
            start_bot_daemon()
        else:
            from telegram.bot import SimpleTelegramBot
            bot = SimpleTelegramBot()
            await bot.start_polling()
        return

    # --- menubar app ---
    if args.menubar:
        from ui.menubar import run_menubar
        run_menubar()
        return



    # --- course listing & auth ---
    if args.courses:
        if args.json or args.out:
            course_list = [{"course_id": cid, "course_name": name} for cid, name in courses.items()]
            _emit_json(args, {"courses": course_list})
        else:
            print("\n📚 Configured Courses:")
            for cid, name in courses.items():
                print(f"  • {cid}: {name}")
            print("")
        return

    if args.login or args.auto_exp:
        auto_exp_mode = args.auto_exp
        if args.visible and not args.auto and not auto_exp_mode:
            await asyncio.to_thread(login, args.force, args.username, args.password, cdp)
        else:
            await asyncio.to_thread(
                login_auto,
                username=args.username,
                password=args.password,
                headless=headless,
                cdp_url=cdp,
                auto_exp=auto_exp_mode,
                force=args.force,
            )
        return



    if args.logout:
        from core.session import logout as do_logout
        await asyncio.to_thread(do_logout, keep_config_creds=True)
        return


    if args.check_session:
        fast_only = not args.visible
        ok = await check_session_async(debug=args.debug, headless=headless, fast_only=fast_only)
        if not ok and args.visible:
            visible_ok = await check_session_async(debug=args.debug, headless=False, fast_only=False)
            if visible_ok:
                print(
                    "⚠️  Headless check failed but visible check passed.\n"
                    "   Likely a headless-detection/timing issue; session is probably valid.",
                    file=sys.stderr,
                )
        return

    if args.session_info:
        from core.config import SESSION_DIR
        meta = SESSION_DIR / "session_metadata.json"
        print("\n🕒 Session Info:", file=sys.stderr)
        if meta.exists():
            data = json.loads(meta.read_text())
            print(f"  Created:   {data.get('login_time_human', 'Unknown')}", file=sys.stderr)
            print(f"  Last Used: {data.get('last_used_time_human', 'Unknown')}", file=sys.stderr)
        else:
            print("  No session metadata found. Run --login first.", file=sys.stderr)
        print("", file=sys.stderr)
        return

    if args.session_stats:
        from core.session_tracker import tracker
        is_valid, user_data = quick_check_session_http()
        tracker.record_probe(is_valid, user_data)
        print(tracker.format_cli_summary())
        return

    if args.discover or args.list_terms:
        await asyncio.to_thread(_handle_discover_courses, term_filter=args.term, list_only=args.list_terms, headless=headless, cdp=cdp)
        return


    if not await _require_session_async(cdp):
        return


    # --- resolve target courses ---
    target_cids = resolve_target_courses(args.course, args.all, courses)

    # --- high-speed concurrent briefing ---
    if args.briefing:
        concurrency = get_optimal_concurrency(TaskProfile.MEDIUM, args.concurrency)
        bundle = await run_briefing_async(
            headless=headless,
            cdp_url=cdp,
            write_markdown=args.md,
            concurrency=concurrency,
        )

        if args.telegram:
            try:
                from telegram.notifier import TelegramNotifier
                notifier = TelegramNotifier()
                if notifier.enabled:
                    notifier.notify_briefing(bundle)
                    notifier.process_and_notify_diffs(bundle)
                    print("📬 Sent daily briefing to Telegram.", file=sys.stderr)
                else:
                    print("⚠️ Telegram is not enabled or configured in config.json.", file=sys.stderr)
            except Exception as e:
                print(f"⚠️ Telegram notification error: {e}", file=sys.stderr)

        if args.raw:
            _emit_json(args, bundle)
            return

        if args.json or args.out:
            _emit_json(args, bundle)
        else:
            # Default to clean CLI stdout digest
            print(format_briefing_cli(bundle))
        return

    # --- due dates aggregator ---
    window = f"{args.upcoming}d" if args.upcoming else (args.due if args.due is not None else None)
    if window is not None:
        concurrency = get_optimal_concurrency(TaskProfile.MEDIUM, args.concurrency)
        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp, max_concurrency=concurrency))
        await session_manager.initialize()
        try:
            async with session_manager.acquire_page() as page:
                items = await aggregate_due_dates_async(
                    page,
                    courses,
                    window_filter=window,
                    exclude_completed=args.exclude_completed,
                )
                if args.md:
                    save_due_dates(items, window_filter=window)
        finally:
            await session_manager.close()

        if args.raw or args.json or args.out:
            _emit_json(args, items)
        else:
            # Default to CLI table
            print(format_due_dates_table(items, window_filter=window))
        return

    # --- course outline scraper ---
    if args.outline:
        if not target_cids:
            print("❌ Specify course via -c <ID/Code> (e.g. -c IS410) or --all", file=sys.stderr)
            return

        raw_all: dict[str, list[dict]] = {}
        async def _fetch_outline(cid: str) -> tuple[str, list[dict]]:
            data = await scrape_course_outline_async(cid)
            if args.folder and (args.json or args.out or args.raw or args.md):
                data = filter_outline_by_folder(data, args.folder)
            if args.type:
                data = [item for item in data if item.get("content_type", "").lower() == args.type.lower()]
            if args.keyword_filter:
                kw = args.keyword_filter.lower()
                data = [item for item in data if kw in item.get("title", "").lower() or kw in item.get("description", "").lower()]
            if args.md:
                save_outline(data, cid)
            return cid, data

        tasks = [_fetch_outline(cid) for cid in target_cids]
        results = await asyncio.gather(*tasks)
        raw_all = dict(results)

        if args.interactive:
            for cid, data in raw_all.items():
                cname = courses.get(cid, cid)
                if isinstance(data, list):
                    interactive_folder_picker(data, cname, cid)
            return

        if args.raw:
            formatted_json = [
                {
                    "course_id": cid,
                    "course_name": courses.get(cid, cid),
                    "items": data if isinstance(data, list) else [],
                }
                for cid, data in raw_all.items()
            ]
            _emit_json(args, formatted_json)
        elif args.json or args.out:
            formatted_json = [
                {
                    "course_id": cid,
                    "course_name": courses.get(cid, cid),
                    "items": clean_outline_json(data) if isinstance(data, list) else [],
                }
                for cid, data in raw_all.items()
            ]
            _emit_json(args, formatted_json)
        else:
            # Default to CLI outline tree (shallow summary by default, with folder / expand-all / depth support)
            for cid, data in raw_all.items():
                cname = courses.get(cid, cid)
                if isinstance(data, list):
                    print(format_outline_tree(
                        data,
                        cname,
                        cid,
                        target_folder=args.folder,
                        expand_all=args.expand_all,
                        depth=args.depth,
                    ))
                    print("")
        return

    # --- deep assignments scraper ---
    if args.assignments:
        if not target_cids:
            print("❌ Specify course via -c <ID/Code> (e.g. -c IS410) or --all", file=sys.stderr)
            return

        concurrency = get_optimal_concurrency(TaskProfile.HEAVY, args.concurrency)
        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp, max_concurrency=concurrency))
        await session_manager.initialize()
        raw_all_assign: dict[str, list[dict]] = {}
        try:
            pool = AsyncCourseWorkerPool(session_manager, task_profile=TaskProfile.HEAVY)
            async def _worker(cid, cname, page):
                data = await scrape_course_assignments_async(cid, page)
                if args.keyword_filter:
                    kw = args.keyword_filter.lower()
                    data = [item for item in data if kw in item.get("title", "").lower() or kw in item.get("instructions", "").lower()]
                if args.md:
                    save_assignments(data, cid)
                return data

            target_dict = {cid: courses.get(cid, cid) for cid in target_cids}
            raw_all_assign = await pool.execute_task_per_course(target_dict, _worker)
        finally:
            await session_manager.close()

        if args.raw or args.json or args.out:
            formatted_json = [
                {
                    "course_id": cid,
                    "course_name": courses.get(cid, cid),
                    "assignments": data if isinstance(data, list) else [],
                }
                for cid, data in raw_all_assign.items()
            ]
            _emit_json(args, formatted_json)
        else:
            # Default to CLI summary
            for cid, data in raw_all_assign.items():
                cname = courses.get(cid, cid)
                if isinstance(data, list):
                    print(format_assignments_summary(data, cname, cid))
                    print("")
        return

    # --- omnisearch ---
    if args.find:
        matches = await find_items_async(args.find, courses, page=None, type_filter=args.type)

        if args.json or args.out:
            _emit_json(args, matches)
        else:
            print(f"\n🔎 Search Results for '{args.find}':")
            print("━" * 50)
            if not matches:
                print("  (No matching items found across courses)")
            for m in matches:
                due_str = f" (Due: {m['due_date']})" if m.get("due_date") else ""
                dl_str = " 💾 [File Ready]" if m.get("is_downloadable") else ""
                path_str = f" [{ ' > '.join(m.get('parent_path', [])) }]" if m.get("parent_path") else ""
                print(f"• [{m['course_name']}]{path_str} {m['title']} [{m['content_type']}]{dl_str}{due_str}")
                if m.get("description"):
                    print(f"  > 💬 {m['description'][:130]}")
                if m.get("download_url"):
                    print(f"  └ 📥 ID: {m.get('content_id')}")
                elif m.get("external_url"):
                    print(f"  └ 🔗 {m['external_url']}")
        return

    # --- item grabber & downloader ---
    if args.grab:
        download_folder = Path(args.out_dir)
        item = await grab_item_async(
            target_id_or_title=args.grab,
            courses=courses,
            target_cids=target_cids if target_cids else None,
            page=None,
            download_dir=download_folder,
        )

        if args.json or args.out:
            _emit_json(args, item)
        return

    # --- announcements ---
    if args.announcements:
        if not target_cids:
            print("❌ Specify course via -c <ID/Code> (e.g. -c IS410) or --all", file=sys.stderr)
            return

        concurrency = get_optimal_concurrency(TaskProfile.LIGHT, args.concurrency)
        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp, max_concurrency=concurrency))
        await session_manager.initialize()
        raw_ann: dict[str, list[dict]] = {}
        try:
            pool = AsyncCourseWorkerPool(session_manager, task_profile=TaskProfile.LIGHT)
            async def _worker(cid, cname, page):
                data = await scrape_announcements_async(cid, page)
                if args.md:
                    save_announcements(data, cid)
                return data

            target_dict = {cid: courses.get(cid, cid) for cid in target_cids}
            raw_ann = await pool.execute_task_per_course(target_dict, _worker)
        finally:
            await session_manager.close()

        if args.raw or args.json or args.out:
            formatted_json = [
                {
                    "course_id": cid,
                    "course_name": courses.get(cid, cid),
                    "announcements": data if isinstance(data, list) else [],
                }
                for cid, data in raw_ann.items()
            ]
            _emit_json(args, formatted_json)
        else:
            # Default to CLI output
            for cid, data in raw_ann.items():
                cname = courses.get(cid, cid)
                print(f"\n📢 Announcements: {cname}")
                print("━" * 50)
                if not data:
                    print("  (No announcements found)")
                for ann in data:
                    unread = "[UNREAD] " if ann.get("unread") else ""
                    print(f"• {unread}{ann['title']} ({ann.get('meta','')})")
                    if ann.get("body"):
                        print(f"  > {ann['body'][:140]}")
        return

    # --- grades ---
    if args.grades:
        if not target_cids:
            print("❌ Specify course via -c <ID/Code> (e.g. -c IS410) or --all", file=sys.stderr)
            return

        concurrency = get_optimal_concurrency(TaskProfile.LIGHT, args.concurrency)
        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp, max_concurrency=concurrency))
        await session_manager.initialize()
        raw_gr: dict[str, list[dict]] = {}
        try:
            pool = AsyncCourseWorkerPool(session_manager, task_profile=TaskProfile.LIGHT)
            async def _worker(cid, cname, page):
                data = await scrape_grades_async(cid, page)
                if args.md:
                    save_grades(data, cid)
                return data

            target_dict = {cid: courses.get(cid, cid) for cid in target_cids}
            raw_gr = await pool.execute_task_per_course(target_dict, _worker)
        finally:
            await session_manager.close()

        if args.raw or args.json or args.out:
            formatted_json = [
                {
                    "course_id": cid,
                    "course_name": courses.get(cid, cid),
                    "grades": data if isinstance(data, list) else [],
                }
                for cid, data in raw_gr.items()
            ]
            _emit_json(args, formatted_json)
        else:
            # Default to CLI table
            for cid, data in raw_gr.items():
                cname = courses.get(cid, cid)
                print(f"\n🎓 Grades: {cname}")
                print("━" * 50)
                if not data:
                    print("  (No graded items found)")
                else:
                    for g in data:
                        due = f" (Due: {g['dueDate']})" if g.get("dueDate") else ""
                        print(f"• {g['name']}: {g.get('grade','Not graded')}{due} [{g.get('status','')}]")
        return

    # --- calendar ---
    if args.calendar:
        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp))
        await session_manager.initialize()
        try:
            async with session_manager.acquire_page() as page:
                target_cid = target_cids[0] if target_cids else None
                calendar = await scrape_calendar_async(page, target_cid)
                if args.md:
                    save_calendar(calendar, target_cid)
        finally:
            await session_manager.close()

        if args.raw or args.json or args.out:
            _emit_json(args, calendar)
        else:
            print(format_due_dates_table(calendar, window_filter="calendar"))
        return

    # --- activity ---
    if args.activity:
        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp))
        await session_manager.initialize()
        try:
            async with session_manager.acquire_page() as page:
                activity = await scrape_activity_async(page)
                if args.md:
                    save_activity(activity)
        finally:
            await session_manager.close()

        if args.raw or args.json or args.out:
            _emit_json(args, activity)
        else:
            print("\n🌊 Activity Stream:")
            print("━" * 60)
            if not activity:
                print("  (No recent activity found)")
            for item in activity:
                date_label = f" — {item['date']}" if item.get('date') else ""
                due_label = f" [Due: {item['due_date']}]" if item.get('due_date') else ""
                cname = item.get('course') or 'General'
                print(f"• [{cname}] {item['title']}{due_label}{date_label}")
                if item.get("message") and item.get("message") != item.get("title"):
                    print(f"  > {item['message'][:160]}")
        return

    # --- profile ---
    if args.profile:
        data = await scrape_profile_async()
        if data:
            if args.json or args.out:
                _emit_json(args, data)
            else:
                _print_profile(data)
            if args.md:
                save_profile(data)
        return

    # --- discussions ---
    if args.discussions:
        if not target_cids:
            print("❌ Specify course via -c <ID/Code> or --all", file=sys.stderr)
            return
        kwargs = {
            "max_post_clicks": getattr(args, "max_posts", None),
            "max_participant_clicks": getattr(args, "max_parts", None),
            "posts_only": getattr(args, "posts_only", False),
            "participants_only": getattr(args, "participants_only", False),
            "titles_only": getattr(args, "titles_only", False),
        }
        raw_all_disc: list[dict] = []
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx, _ = _launch_context(p, headless, cdp)
            for course_id in target_cids:
                page = ctx.new_page()
                data = scrape_discussions(course_id, page, **kwargs)
                if args.md:
                    save_discussions(data, course_id, titles_only=getattr(args, "titles_only", False))
                raw_all_disc.append({
                    "course_id": course_id,
                    "course_name": courses.get(course_id, course_id),
                    "discussions": data
                })
                page.close()
            ctx.close()
        if args.raw or args.json or args.out:
            _emit_json(args, raw_all_disc)
            return
        print(f"Scraped discussions for {len(target_cids)} courses.")
        return

    print("No scraper action selected. Run 'python3 main.py --help' to see commands.", file=sys.stderr)


def main() -> None:
    args = _parse_args()
    if len(sys.argv) == 1:
        print("Run 'python3 main.py --help' to see available commands.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
