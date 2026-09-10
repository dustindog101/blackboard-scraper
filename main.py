import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
from scrapers.assignments import (
    scrape_course_assignments_async,
    scrape_course_assignments_http,
    save_assignments,
    format_assignments_summary,
)
from scrapers.quiz import scrape_assessment_attempt_async, save_assessment_attempt, format_assessment_attempt_cli
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

2. One-Time Setup & Smart Automated SSO Login:
   $ bb login
   - If no config or login is detected, an interactive TUI setup wizard prompts for UMBC credentials.
   - Automatically intercepts incoming macOS Duo 2FA SMS passcodes in <3ms.
   - Saves cookies for seamless headless operation.
   - Accepts mode variants: `bb login auto`, `bb login --auto`, `bb login --force`.

3. Visible Manual Login Fallback:
   $ bb login --manual   (or `bb login manual`)
   - Opens a visible browser if you prefer Duo Push, TouchID, or Security Keys.

4. Session Health & Telemetry:
   $ bb check            (or `bb session check`)
   $ bb session stats    (displays rolling lifespan metrics)
""",
    "courses": """
🔀 Smart Course Selection Syntax:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You can target courses in multiple flexible ways:

• Target by Positional Course Code (Zero Flags!):
  $ bb outline IS410
  $ bb assignments ENGL100
  $ bb grades "ECON 122"

• Target by -c / --course Option:
  $ bb outline -c IS410
  $ bb grades -c MATH215

• Target Multiple Courses (Comma-Separated):
  $ bb outline IS410,ENGL100,MATH215
  $ bb assignments IS410,STAT351

• Target by Fuzzy Title Keyword:
  $ bb outline Database
  $ bb grades Accounting

• Target All Configured Courses:
  $ bb outline --all
  $ bb briefing
""",
    "schema": """
📦 Standardized v2 JSON Schemas:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Output clean JSON to stdout (--json) or export to file (--out <path>):

• Full Composite Document (bb briefing --json):
  {
    "version": "2.0",
    "generated_at": 1786938000,
    "generated_at_human": "2026-08-16T23:40:00Z",
    "summary": { "total_courses": 5, "upcoming_deadlines_count": 2, ... },
    "user": { "username": "BH69617", "name": "Amanuel Hailie" },
    "courses": [ { "course_id": "...", "syllabus": {...}, "outline": [...], "assignments": [...], ... } ],
    "global": { "activity_stream": [...], "calendar_due_dates": [...] }
  }

• Targeted Deadline Items (bb due 7d --json):
  { "version": "2.0", "total_items": 3, "items": [ { "title": "...", "course": "...", "due_date": "..." } ] }

• Targeted Outline Trees (bb outline IS410 --json):
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
   $ bb bot start        (runs detached in background)
   $ bb bot status       (checks running PID and memory)
   $ bb bot stop         (gracefully stops background bot)
   $ bb bot restart      (reloads daemon)
   $ bb bot run          (runs interactively in foreground)

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
# Transparent Legacy Flag Interceptor & Subcommands Parser
# ---------------------------------------------------------------------------

def _intercept_legacy_args(argv: List[str]) -> Tuple[List[str], Optional[str]]:
    """
    Transparent pre-parsing compatibility layer. Maps deprecated root double-dash
    flags (e.g. --briefing, --due 7d, --auto-exp, --bot-status) to canonical subcommands.
    Returns (translated_argv, hint_command_string_if_legacy_detected).
    """
    if not argv:
        return argv, None

    subcommands = {
        "login", "logout", "session", "check", "briefing", "brief", "due", "grades",
        "announcements", "announce", "news", "outline", "assignments", "assign",
        "assignment", "quiz", "asmt", "assessment",
        "search", "find", "download", "grab", "get", "calendar", "cal", "activity",
        "profile", "whoami", "courses", "discover", "terms", "bot", "menubar", "app",
        "guide", "help", "discussions", "discuss"
    }
    if argv[0] in subcommands:
        return argv, None
    if argv[0] in ("-h", "--help", "-v", "--version"):
        return argv, None

    new_argv = list(argv)
    legacy_found = None

    if "--auto-exp" in new_argv or "--login-auto-exp" in new_argv:
        flag = "--auto-exp" if "--auto-exp" in new_argv else "--login-auto-exp"
        new_argv.remove(flag)
        new_argv.insert(0, "login")
        legacy_found = "login"
        if "--force" in new_argv or "-f" in new_argv:
            legacy_found = "login --force"
    elif "--login" in new_argv:
        new_argv.remove("--login")
        if "--auto" in new_argv:
            new_argv.remove("--auto")
            new_argv.insert(0, "auto")
            legacy_found = "login auto"
        elif "-a" in new_argv:
            new_argv.remove("-a")
            new_argv.insert(0, "auto")
            legacy_found = "login auto"
        elif "--visible" in new_argv:
            new_argv.remove("--visible")
            new_argv.insert(0, "--manual")
            legacy_found = "login --manual"
        else:
            legacy_found = "login"
        new_argv.insert(0, "login")
    elif "--logout" in new_argv:
        new_argv.remove("--logout")
        new_argv.insert(0, "logout")
        legacy_found = "logout"
    elif "--check-session" in new_argv:
        new_argv.remove("--check-session")
        new_argv.insert(0, "check")
        new_argv.insert(0, "session")
        legacy_found = "session check"
    elif "--session-stats" in new_argv or "--session-telemetry" in new_argv:
        flag = "--session-stats" if "--session-stats" in new_argv else "--session-telemetry"
        new_argv.remove(flag)
        new_argv.insert(0, "stats")
        new_argv.insert(0, "session")
        legacy_found = "session stats"
    elif "--session-info" in new_argv:
        new_argv.remove("--session-info")
        new_argv.insert(0, "info")
        new_argv.insert(0, "session")
        legacy_found = "session info"
    elif "--briefing" in new_argv:
        new_argv.remove("--briefing")
        new_argv.insert(0, "briefing")
        legacy_found = "briefing"
    elif "--due" in new_argv:
        idx = new_argv.index("--due")
        new_argv.pop(idx)
        window = "7d"
        if idx < len(new_argv) and not new_argv[idx].startswith("-"):
            window = new_argv.pop(idx)
        new_argv.insert(0, window)
        new_argv.insert(0, "due")
        legacy_found = f"due {window}"
    elif "--upcoming" in new_argv:
        idx = new_argv.index("--upcoming")
        new_argv.pop(idx)
        days = "7d"
        if idx < len(new_argv) and not new_argv[idx].startswith("-"):
            days = f"{new_argv.pop(idx)}d"
        new_argv.insert(0, days)
        new_argv.insert(0, "due")
        legacy_found = f"due {days}"
    elif "--grades" in new_argv:
        new_argv.remove("--grades")
        new_argv.insert(0, "grades")
        legacy_found = "grades"
    elif "--announcements" in new_argv:
        new_argv.remove("--announcements")
        new_argv.insert(0, "announcements")
        legacy_found = "announcements"
    elif "--outline" in new_argv:
        new_argv.remove("--outline")
        new_argv.insert(0, "outline")
        legacy_found = "outline"
    elif "--assignments" in new_argv:
        new_argv.remove("--assignments")
        new_argv.insert(0, "assignments")
        legacy_found = "assignments"
    elif "--assignment" in new_argv or "--quiz" in new_argv or "--asmt" in new_argv or "--assessment" in new_argv:
        flag = next(f for f in ("--assignment", "--quiz", "--asmt", "--assessment") if f in new_argv)
        idx = new_argv.index(flag)
        new_argv.pop(idx)
        target = ""
        if idx < len(new_argv) and not new_argv[idx].startswith("-"):
            target = new_argv.pop(idx)
        if "--begin-attempt" in new_argv:
            new_argv[new_argv.index("--begin-attempt")] = "--start-attempt"
        new_argv.insert(0, target)
        new_argv.insert(0, "assignment")
        legacy_found = f"assignment {target}".strip()
    elif "--discussions" in new_argv:
        new_argv.remove("--discussions")
        new_argv.insert(0, "discussions")
        legacy_found = "discussions"
    elif "--calendar" in new_argv:
        new_argv.remove("--calendar")
        new_argv.insert(0, "calendar")
        legacy_found = "calendar"
    elif "--activity" in new_argv:
        new_argv.remove("--activity")
        new_argv.insert(0, "activity")
        legacy_found = "activity"
    elif "--profile" in new_argv:
        new_argv.remove("--profile")
        new_argv.insert(0, "profile")
        legacy_found = "profile"
    elif "--courses" in new_argv or "--list-courses" in new_argv:
        flag = "--courses" if "--courses" in new_argv else "--list-courses"
        new_argv.remove(flag)
        new_argv.insert(0, "courses")
        legacy_found = "courses"
    elif "--discover" in new_argv or "--discover-courses" in new_argv:
        flag = "--discover" if "--discover" in new_argv else "--discover-courses"
        new_argv.remove(flag)
        new_argv.insert(0, "discover")
        legacy_found = "discover"
    elif "--list-terms" in new_argv:
        new_argv.remove("--list-terms")
        new_argv.insert(0, "terms")
        legacy_found = "terms"
    elif "--find" in new_argv or "--search" in new_argv:
        flag = "--find" if "--find" in new_argv else "--search"
        idx = new_argv.index(flag)
        new_argv.pop(idx)
        query = ""
        if idx < len(new_argv) and not new_argv[idx].startswith("-"):
            query = new_argv.pop(idx)
        new_argv.insert(0, query)
        new_argv.insert(0, "search")
        legacy_found = f"search {query}".strip()
    elif "--grab" in new_argv or "--download" in new_argv:
        flag = "--grab" if "--grab" in new_argv else "--download"
        idx = new_argv.index(flag)
        new_argv.pop(idx)
        item = ""
        if idx < len(new_argv) and not new_argv[idx].startswith("-"):
            item = new_argv.pop(idx)
        new_argv.insert(0, item)
        new_argv.insert(0, "download")
        legacy_found = f"download {item}".strip()
    elif "--bot-start" in new_argv:
        new_argv.remove("--bot-start")
        new_argv.insert(0, "start")
        new_argv.insert(0, "bot")
        legacy_found = "bot start"
    elif "--bot-status" in new_argv:
        new_argv.remove("--bot-status")
        new_argv.insert(0, "status")
        new_argv.insert(0, "bot")
        legacy_found = "bot status"
    elif "--bot-stop" in new_argv:
        new_argv.remove("--bot-stop")
        new_argv.insert(0, "stop")
        new_argv.insert(0, "bot")
        legacy_found = "bot stop"
    elif "--bot-restart" in new_argv:
        new_argv.remove("--bot-restart")
        new_argv.insert(0, "restart")
        new_argv.insert(0, "bot")
        legacy_found = "bot restart"
    elif "--bot" in new_argv:
        new_argv.remove("--bot")
        if "-d" in new_argv:
            new_argv.remove("-d")
            new_argv.insert(0, "start")
            legacy_found = "bot start"
        elif "--daemon" in new_argv:
            new_argv.remove("--daemon")
            new_argv.insert(0, "start")
            legacy_found = "bot start"
        else:
            new_argv.insert(0, "run")
            legacy_found = "bot"
        new_argv.insert(0, "bot")
    elif "--menubar" in new_argv:
        new_argv.remove("--menubar")
        new_argv.insert(0, "menubar")
        legacy_found = "menubar"
    elif "--guide" in new_argv:
        idx = new_argv.index("--guide")
        new_argv.pop(idx)
        topic = ""
        if idx < len(new_argv) and not new_argv[idx].startswith("-"):
            topic = new_argv.pop(idx)
        new_argv.insert(0, topic)
        new_argv.insert(0, "guide")
        legacy_found = f"guide {topic}".strip()

    return new_argv, legacy_found


def _build_parser() -> argparse.ArgumentParser:
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
💡 CANONICAL SUBCOMMANDS (Global CLI: bb / blackboard / bbscraper)
═══════════════════════════════════════════════════════════════════════
  bb briefing                                         # Print high-speed briefing to CLI stdout
  bb due 7d                                           # Print upcoming deadlines table (defaults to 7d)
  bb outline MATH215                                  # View shallow outline summary with folder counts
  bb outline MATH215 -f "Homework"                    # Selectively expand a specific folder
  bb outline MATH215 --expand-all                     # View full recursive outline tree
  bb outline MATH215 -i                               # Interactive terminal folder explorer
  bb search "Python"                                  # Search content across all courses
  bb download "Chapter01.ipynb"                       # Auto-detects course and downloads file
  bb assignments IS410                                # List assignments (HTTP fast-path, <150ms)
  bb assignment "Homework 1" -c IS410                 # Inspect one item, safe info mode (no attempt started)
  bb grades MATH215                                   # View gradebook and feedback
  bb announcements MATH215                            # View course announcements
  bb profile                                          # View student profile (<150ms)
  bb login                                            # Smart automated SSO login + Duo 2FA SMS capture
  bb login --manual                                   # Manual visible browser login
  bb bot start                                        # Launch Telegram bot daemon in background
  bb session check                                    # Rapid session token validity probe

📖 DETAILED TOPIC GUIDES:
  bb guide auth                                       # Authentication & Headless execution
  bb guide courses                                    # Course selection & multi-course syntax
  bb guide schema                                     # Standardized v2 JSON schemas
  bb guide telegram                                   # Telegram bot & notifications
  bb guide concurrency                                # Adaptive async worker engine
        """,
    )

    # Common parent parsers
    output_parent = argparse.ArgumentParser(add_help=False)
    output_parent.add_argument("--json", action="store_true", help="Output standardized JSON to CLI stdout")
    output_parent.add_argument("--out", metavar="FILE", help="Save JSON output directly to FILE")
    output_parent.add_argument("--md", "--save", dest="md", action="store_true", help="Save formatted markdown file(s) to output/ directory")
    output_parent.add_argument("--raw", action="store_true", help="Output raw unformatted scraper data")
    output_parent.add_argument("--compact", action="store_true", help="Emit minified JSON")
    output_parent.add_argument("--source", default="blackboard-scraper", metavar="NAME", help="Value for the JSON source field")

    engine_parent = argparse.ArgumentParser(add_help=False)
    engine_parent.add_argument("--concurrency", type=int, metavar="N", help="Override dynamic concurrency worker pool size")
    engine_parent.add_argument("--visible", "-v", action="store_true", help="Show browser window (useful for debugging)")
    engine_parent.add_argument("--cdp", help="Connect to existing browser via CDP URL")

    course_parent = argparse.ArgumentParser(add_help=False)
    course_parent.add_argument("course_pos", nargs="?", metavar="COURSE", help="Course code or ID (e.g. IS410, MATH215)")
    course_parent.add_argument("--course", "-c", help="Target course ID(s) or code(s)")
    course_parent.add_argument("--all", action="store_true", help="Target all configured courses")

    subparsers = parser.add_subparsers(dest="subcommand", metavar="<command>")

    # --- auth ---
    login_p = subparsers.add_parser("login", parents=[engine_parent], help="Login via UMBC SSO (smart automated headless default)")
    login_p.add_argument("mode", nargs="?", choices=["auto", "manual", "browser", "sms"], default="auto", help="Login mode ('auto' or 'manual')")
    login_p.add_argument("--manual", action="store_true", help="Open visible browser window for manual 2FA")
    login_p.add_argument("--auto", "-a", action="store_true", help="Automated SSO login (default)")
    login_p.add_argument("--force", "-f", action="store_true", help="Force re-login even if active session exists")
    login_p.add_argument("--username", "-u", help="Username for login (prompts if omitted)")
    login_p.add_argument("--password", "-p", help="Password for login (prompts if omitted)")
    login_p.add_argument("--duo-passcode", help="Provide 6-digit Duo SMS passcode directly via CLI")

    subparsers.add_parser("logout", help="Logout (clear cached session cookies)")

    # --- session ---
    session_p = subparsers.add_parser("session", parents=[engine_parent], help="Session health probes, metadata, and telemetry")
    session_p.add_argument("action", nargs="?", choices=["check", "stats", "info", "telemetry"], default="check", help="Session action ('check', 'stats', 'info')")
    session_p.add_argument("--debug", action="store_true", help="Print debug output with check")

    check_p = subparsers.add_parser("check", parents=[engine_parent], help="Quick session health check probe")
    check_p.add_argument("--debug", action="store_true", help="Print debug output")

    # --- scrapers ---
    brief_p = subparsers.add_parser("briefing", aliases=["brief"], parents=[output_parent, engine_parent], help="Run concurrent daily briefing across all courses")
    brief_p.add_argument("--telegram", action="store_true", help="Send briefing to Telegram")

    due_p = subparsers.add_parser("due", parents=[output_parent, engine_parent], help="Aggregate cross-course due dates")
    due_p.add_argument("window", nargs="?", default="7d", metavar="WINDOW", help="Relative date window (e.g. 7d, 14d, overdue, all; default: 7d)")
    due_p.add_argument("--exclude-completed", action="store_true", help="Exclude submitted/graded items")

    subparsers.add_parser("grades", parents=[course_parent, output_parent, engine_parent], help="Scrape course gradebook")
    subparsers.add_parser("announcements", aliases=["announce", "news"], parents=[course_parent, output_parent, engine_parent], help="Scrape course announcements")

    outline_p = subparsers.add_parser("outline", parents=[course_parent, output_parent, engine_parent], help="Scrape course outline, modules, syllabi, and files")
    outline_p.add_argument("--folder", "-f", metavar="FOLDER", help="Expand specific folder or module by name or ID")
    outline_p.add_argument("--expand-all", "--deep", "--all-folders", dest="expand_all", action="store_true", help="Expand all folders into full tree")
    outline_p.add_argument("--depth", type=int, metavar="N", help="Limit outline display depth")
    outline_p.add_argument("--interactive", "-i", action="store_true", help="Interactive terminal menu to select and explore folders")
    outline_p.add_argument("--type", help="Filter items by type (e.g. syllabus, document, assignment, folder, link)")
    outline_p.add_argument("--filter", dest="keyword_filter", help="Filter items by text keyword")

    assign_p = subparsers.add_parser("assignments", aliases=["assign"], parents=[course_parent, output_parent, engine_parent], help="Deep scrape assignments with prompts, rubrics, and files")
    assign_p.add_argument("--filter", dest="keyword_filter", help="Filter assignments by keyword")

    single_p = subparsers.add_parser("assignment", aliases=["quiz", "asmt", "assessment"], parents=[output_parent, engine_parent], help="Inspect a single assignment/quiz: prompts, questions, attempts (safe info mode by default)")
    single_p.add_argument("target", nargs="?", metavar="TARGET", help="Assignment ID or title (e.g. _8954640_1 or 'Homework 1')")
    single_p.add_argument("--course", "-c", help="Scope the title search to course ID(s) or code(s)")
    single_p.add_argument("--all", action="store_true", help="Search across all configured courses")
    single_p.add_argument("--start-attempt", "--begin-attempt", dest="start_attempt", action="store_true", help="Allow beginning a new attempt if none is active (guarded)")
    single_p.add_argument("--force-start", dest="force_start", action="store_true", help="With --start-attempt: confirm starting timed assessments/exams")

    search_p = subparsers.add_parser("search", aliases=["find"], parents=[output_parent, engine_parent], help="Search for content/assignments matching query across courses")
    search_p.add_argument("query", metavar="QUERY", help="Search query string")
    search_p.add_argument("--type", help="Filter results by content type")

    dl_p = subparsers.add_parser("download", aliases=["grab", "get"], parents=[output_parent, engine_parent], help="Grab and download specific content item or file")
    dl_p.add_argument("item", metavar="ITEM_ID_OR_NAME", help="Content item ID or file title")
    dl_p.add_argument("--out-dir", default="downloads", help="Destination directory (default: downloads)")

    subparsers.add_parser("calendar", aliases=["cal"], parents=[course_parent, output_parent, engine_parent], help="Scrape calendar due-dates")
    subparsers.add_parser("activity", parents=[output_parent, engine_parent], help="Scrape homepage activity stream")
    subparsers.add_parser("profile", aliases=["whoami"], parents=[output_parent, engine_parent], help="Show student profile information")

    disc_p = subparsers.add_parser("discussions", aliases=["discuss"], parents=[course_parent, output_parent, engine_parent], help="Scrape course discussions")
    disc_p.add_argument("--max-posts", type=int, help="Maximum posts to click")
    disc_p.add_argument("--max-parts", type=int, help="Maximum participants to click")
    disc_p.add_argument("--posts-only", action="store_true", help="Scrape posts only")
    disc_p.add_argument("--participants-only", action="store_true", help="Scrape participants only")
    disc_p.add_argument("--titles-only", action="store_true", help="Scrape thread titles only")

    # --- discovery ---
    courses_p = subparsers.add_parser("courses", parents=[output_parent, engine_parent], help="Course configuration and discovery")
    courses_p.add_argument("action", nargs="?", choices=["list", "discover", "terms"], default="list", help="Course action ('list', 'discover', 'terms')")
    courses_p.add_argument("--term", metavar="TERM", help="Filter academic term (e.g. current, FA2026, all)")

    disc_p = subparsers.add_parser("discover", parents=[output_parent, engine_parent], help="Auto-discover and intelligently save current active semester courses")
    disc_p.add_argument("--term", metavar="TERM", help="Filter academic term (e.g. current, FA2026, all)")

    subparsers.add_parser("terms", parents=[output_parent, engine_parent], help="List all lifetime enrolled academic terms and courses")

    # --- bot ---
    bot_p = subparsers.add_parser("bot", help="Manage background Telegram bot daemon")
    bot_p.add_argument("action", nargs="?", choices=["run", "start", "stop", "restart", "status"], default="run", help="Bot action ('start', 'stop', 'restart', 'status', 'run')")
    bot_p.add_argument("--daemon", "-d", action="store_true", help="Run daemon detached in background")

    subparsers.add_parser("menubar", aliases=["app"], help="Launch native macOS Menubar app")

    guide_p = subparsers.add_parser("guide", aliases=["help"], help="Show comprehensive topic manual")
    guide_p.add_argument("topic", nargs="?", choices=["auth", "courses", "schema", "telegram", "concurrency"], help="Topic manual")

    return parser


def _parse_args(args_list: Optional[List[str]] = None) -> argparse.Namespace:
    raw_args = list(sys.argv[1:] if args_list is None else args_list)
    translated_args, legacy_hint = _intercept_legacy_args(raw_args)
    if legacy_hint and sys.stderr.isatty():
        print(f"💡 Tip: You can run 'bb {legacy_hint}' directly without '--'.", file=sys.stderr)

    parser = _build_parser()
    return parser.parse_args(translated_args)



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
    subcmd = getattr(args, "subcommand", None)

    # --- topic guides ---
    topic = getattr(args, "topic", None) or getattr(args, "guide", None)
    if subcmd in ("guide", "help") or topic:
        if not topic:
            print("📖 Available guides: auth, courses, schema, telegram, concurrency\nRun 'bb guide <topic>' (e.g. 'bb guide auth').")
            return
        guide_text = HELP_GUIDES.get(topic)
        if guide_text:
            print(guide_text.strip())
        else:
            print(f"Unknown guide topic '{topic}'. Available: auth, courses, schema, telegram, concurrency", file=sys.stderr)
        return

    headless = not getattr(args, "visible", False)
    cdp = getattr(args, "cdp", None)
    courses = load_courses()
    if sys.platform != "win32" and os.path.exists("/tmp"):
        os.environ["TMPDIR"] = "/tmp"

    # --- telegram bot daemon management ---
    if subcmd == "bot" or getattr(args, "bot", False) or getattr(args, "bot_status", False) or getattr(args, "bot_stop", False) or getattr(args, "bot_restart", False):
        bot_action = getattr(args, "action", "run") if subcmd == "bot" else ("status" if getattr(args, "bot_status", False) else ("stop" if getattr(args, "bot_stop", False) else ("restart" if getattr(args, "bot_restart", False) else ("start" if getattr(args, "daemon", False) else "run"))))
        if getattr(args, "daemon", False) and bot_action == "run":
            bot_action = "start"

        if bot_action == "status":
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
                print("\n🤖 Telegram Bot Daemon: 🔴 STOPPED\n   Run `bb bot start` to launch in background.\n")
            return

        if bot_action == "stop":
            from telegram.daemon import stop_bot_daemon
            stop_bot_daemon()
            return

        if bot_action == "restart":
            from telegram.daemon import restart_bot_daemon
            restart_bot_daemon()
            return

        if bot_action == "start":
            from telegram.daemon import start_bot_daemon
            start_bot_daemon()
            return

        # Foreground run
        from telegram.bot import SimpleTelegramBot
        bot = SimpleTelegramBot()
        await bot.start_polling()
        return

    # --- menubar app ---
    if subcmd in ("menubar", "app") or getattr(args, "menubar", False):
        from ui.menubar import run_menubar
        run_menubar()
        return

    # --- course listing & discovery ---
    if subcmd in ("courses", "discover", "terms") or getattr(args, "courses", False) or getattr(args, "discover", False) or getattr(args, "list_terms", False):
        c_action = getattr(args, "action", "list") if subcmd == "courses" else ("discover" if subcmd == "discover" or getattr(args, "discover", False) else ("terms" if subcmd == "terms" or getattr(args, "list_terms", False) else "list"))
        if c_action == "discover":
            await asyncio.to_thread(_handle_discover_courses, term_filter=getattr(args, "term", None), list_only=False, headless=headless, cdp=cdp)
            return
        if c_action == "terms":
            await asyncio.to_thread(_handle_discover_courses, term_filter=None, list_only=True, headless=headless, cdp=cdp)
            return
        # list courses
        if getattr(args, "json", False) or getattr(args, "out", None):
            course_list = [{"course_id": cid, "course_name": name} for cid, name in courses.items()]
            _emit_json(args, {"courses": course_list})
        else:
            print("\n📚 Configured Courses:")
            for cid, name in courses.items():
                print(f"  • {cid}: {name}")
            print("")
        return

    # --- login & logout ---
    if subcmd == "login" or getattr(args, "login", False) or getattr(args, "auto_exp", False):
        is_manual = getattr(args, "manual", False) or getattr(args, "mode", "auto") in ("manual", "browser") or (getattr(args, "visible", False) and not getattr(args, "auto", False))
        force_flag = getattr(args, "force", False)
        username = getattr(args, "username", None)
        password = getattr(args, "password", None)

        if is_manual:
            await asyncio.to_thread(login, force_flag, username, password, cdp)
        else:
            await asyncio.to_thread(
                login_auto,
                username=username,
                password=password,
                headless=headless,
                cdp_url=cdp,
                auto_exp=True,
                force=force_flag,
            )
        return

    if subcmd == "logout" or getattr(args, "logout", False):
        from core.session import logout as do_logout
        await asyncio.to_thread(do_logout, keep_config_creds=True)
        return

    # --- session probes & stats ---
    if subcmd in ("session", "check") or getattr(args, "check_session", False) or getattr(args, "session_stats", False) or getattr(args, "session_info", False):
        sess_action = "check" if subcmd == "check" or getattr(args, "check_session", False) else (getattr(args, "action", "check") if subcmd == "session" else ("stats" if getattr(args, "session_stats", False) else "info"))
        if sess_action in ("check",):
            fast_only = not getattr(args, "visible", False)
            ok = await check_session_async(debug=getattr(args, "debug", False), headless=headless, fast_only=fast_only)
            if not ok and getattr(args, "visible", False):
                visible_ok = await check_session_async(debug=getattr(args, "debug", False), headless=False, fast_only=False)
                if visible_ok:
                    print(
                        "⚠️  Headless check failed but visible check passed.\n"
                        "   Likely a headless-detection/timing issue; session is probably valid.",
                        file=sys.stderr,
                    )
            return
        if sess_action in ("stats", "telemetry"):
            from core.session_tracker import tracker
            is_valid, user_data = quick_check_session_http()
            tracker.record_probe(is_valid, user_data)
            print(tracker.format_cli_summary())
            return
        if sess_action in ("info",):
            from core.config import SESSION_DIR
            meta = SESSION_DIR / "session_metadata.json"
            print("\n🕒 Session Info:", file=sys.stderr)
            if meta.exists():
                data = json.loads(meta.read_text())
                print(f"  Created:   {data.get('login_time_human', 'Unknown')}", file=sys.stderr)
                print(f"  Last Used: {data.get('last_used_time_human', 'Unknown')}", file=sys.stderr)
            else:
                print("  No session metadata found. Run `bb login` first.", file=sys.stderr)
            print("", file=sys.stderr)
            return

    # Resolve target courses early so course-requiring commands fail fast
    # without probing or refreshing the session first.
    target_course_arg = getattr(args, "course", None) or getattr(args, "course_pos", None)
    all_courses_flag = getattr(args, "all", False)
    target_cids = resolve_target_courses(target_course_arg, all_courses_flag, courses)

    _NEEDS_COURSE_EXAMPLE = {
        "outline": "bb outline IS410",
        "assignments": "bb assignments IS410",
        "assign": "bb assignments IS410",
        "discussions": "bb discussions IS410",
        "discuss": "bb discussions IS410",
    }
    if subcmd in _NEEDS_COURSE_EXAMPLE and not target_cids:
        print(f"❌ Specify course (e.g. '{_NEEDS_COURSE_EXAMPLE[subcmd]}' or '-c IS410') or '--all'", file=sys.stderr)
        return

    # From here down, academic commands require an active session
    if not await _require_session_async(cdp):
        return

    # --- briefing ---
    if subcmd in ("briefing", "brief") or getattr(args, "briefing", False):
        concurrency = get_optimal_concurrency(TaskProfile.MEDIUM, getattr(args, "concurrency", None))
        bundle = await run_briefing_async(
            headless=headless,
            cdp_url=cdp,
            write_markdown=getattr(args, "md", False),
            concurrency=concurrency,
        )

        if getattr(args, "telegram", False):
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

        if getattr(args, "raw", False):
            _emit_json(args, bundle)
            return

        if getattr(args, "json", False) or getattr(args, "out", None):
            _emit_json(args, bundle)
        else:
            print(format_briefing_cli(bundle))
        return

    # --- due dates aggregator ---
    window = getattr(args, "window", None) or (f"{getattr(args, 'upcoming', None)}d" if getattr(args, "upcoming", None) else (getattr(args, "due", None) if getattr(args, "due", None) is not None else ("7d" if subcmd == "due" else None)))
    if subcmd == "due" or window is not None:
        if not window:
            window = "7d"
        concurrency = get_optimal_concurrency(TaskProfile.MEDIUM, getattr(args, "concurrency", None))
        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp, max_concurrency=concurrency))
        await session_manager.initialize()
        try:
            async with session_manager.acquire_page() as page:
                items = await aggregate_due_dates_async(
                    page,
                    courses,
                    window_filter=window,
                    exclude_completed=getattr(args, "exclude_completed", False),
                )
                if getattr(args, "md", False):
                    save_due_dates(items, window_filter=window)
        finally:
            await session_manager.close()

        if getattr(args, "raw", False) or getattr(args, "json", False) or getattr(args, "out", None):
            _emit_json(args, items)
        else:
            print(format_due_dates_table(items, window_filter=window))
        return

    # --- outline ---
    if subcmd == "outline" or getattr(args, "outline", False):
        if not target_cids:
            print("❌ Specify course (e.g. 'bb outline IS410' or '-c IS410') or '--all'", file=sys.stderr)
            return

        raw_all: dict[str, list[dict]] = {}
        async def _fetch_outline(cid: str) -> tuple[str, list[dict]]:
            data = await scrape_course_outline_async(cid)
            folder_arg = getattr(args, "folder", None)
            if folder_arg and (getattr(args, "json", False) or getattr(args, "out", None) or getattr(args, "raw", False) or getattr(args, "md", False)):
                data = filter_outline_by_folder(data, folder_arg)
            type_arg = getattr(args, "type", None)
            if type_arg:
                data = [item for item in data if item.get("content_type", "").lower() == type_arg.lower()]
            kw_arg = getattr(args, "keyword_filter", None)
            if kw_arg:
                kw = kw_arg.lower()
                data = [item for item in data if kw in item.get("title", "").lower() or kw in item.get("description", "").lower()]
            if getattr(args, "md", False):
                save_outline(data, cid)
            return cid, data

        tasks = [_fetch_outline(cid) for cid in target_cids]
        results = await asyncio.gather(*tasks)
        raw_all = dict(results)

        if getattr(args, "interactive", False):
            for cid, data in raw_all.items():
                cname = courses.get(cid, cid)
                if isinstance(data, list):
                    interactive_folder_picker(data, cname, cid)
            return

        if getattr(args, "raw", False):
            formatted_json = [
                {
                    "course_id": cid,
                    "course_name": courses.get(cid, cid),
                    "items": data if isinstance(data, list) else [],
                }
                for cid, data in raw_all.items()
            ]
            _emit_json(args, formatted_json)
        elif getattr(args, "json", False) or getattr(args, "out", None):
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
            for cid, data in raw_all.items():
                cname = courses.get(cid, cid)
                if isinstance(data, list):
                    print(format_outline_tree(
                        data,
                        cname,
                        cid,
                        target_folder=getattr(args, "folder", None),
                        expand_all=getattr(args, "expand_all", False),
                        depth=getattr(args, "depth", None),
                    ))
                    print("")
        return

    # --- assignments list scraper (HTTP REST fast-path + Playwright fallback) ---
    if subcmd in ("assignments", "assign"):
        if not target_cids:
            print("❌ Specify course (e.g. 'bb assignments IS410' or '-c IS410') or '--all'", file=sys.stderr)
            return

        raw_all_assign: dict[str, list[dict]] = {}

        # 1. High-speed HTTP REST Fast-Path (<150ms)
        if not getattr(args, "visible", False):
            for cid in target_cids:
                data = scrape_course_assignments_http(cid)
                if data is not None:
                    if args.keyword_filter:
                        kw = args.keyword_filter.lower()
                        data = [item for item in data if kw in item.get("title", "").lower() or kw in item.get("instructions", "").lower()]
                    if args.md:
                        save_assignments(data, cid)
                    raw_all_assign[cid] = data

        # 2. Playwright Browser Fallback (if REST unavailable for any course)
        missing_cids = [cid for cid in target_cids if cid not in raw_all_assign]
        if missing_cids:
            concurrency = get_optimal_concurrency(TaskProfile.HEAVY, args.concurrency)
            session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp, max_concurrency=concurrency))
            await session_manager.initialize()
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

                target_dict = {cid: courses.get(cid, cid) for cid in missing_cids}
                fallback_results = await pool.execute_task_per_course(target_dict, _worker)
                raw_all_assign.update(fallback_results)
            finally:
                await session_manager.close()

        if getattr(args, "raw", False) or getattr(args, "json", False) or getattr(args, "out", None):
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
            for cid, data in raw_all_assign.items():
                cname = courses.get(cid, cid)
                if isinstance(data, list):
                    print(format_assignments_summary(data, cname, cid))
                    print("")
        return

    # --- assignment / quiz / assessment attempt inspector (non-destructive info mode by default) ---
    assignment_target = getattr(args, "target", None)
    if subcmd in ("assignment", "quiz", "asmt", "assessment") or assignment_target:
        if not assignment_target:
            print("❌ Specify an assignment ID or title (e.g. 'bb assignment \"Homework 1\" -c IS410')", file=sys.stderr)
            return
        target_cid = target_cids[0] if target_cids else None
        force_browser = getattr(args, "visible", False)
        allow_start = getattr(args, "start_attempt", False)
        force_start = getattr(args, "force_start", False)
        data = await scrape_assessment_attempt_async(
            target=assignment_target,
            course_id=target_cid,
            headless=headless,
            force_browser=force_browser,
            allow_start=allow_start,
            force_start=force_start,
        )

        if getattr(args, "md", False):
            saved_path = save_assessment_attempt(data)
            print(f"💾 Saved assessment Markdown report to: {_safe_relpath(saved_path)}", file=sys.stderr)

        if getattr(args, "raw", False) or getattr(args, "json", False) or getattr(args, "out", None):
            _emit_json(args, data)
        else:
            print(format_assessment_attempt_cli(data))
        return

    # --- search / find ---
    search_query = getattr(args, "query", None)
    if subcmd in ("search", "find") or search_query:
        matches = await find_items_async(search_query, courses, page=None, type_filter=getattr(args, "type", None))
        if getattr(args, "json", False) or getattr(args, "out", None):
            _emit_json(args, matches)
        else:
            print(f"\n🔎 Search Results for '{search_query}':")
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

    # --- download / grab ---
    target_item = getattr(args, "item", None) or getattr(args, "grab", None)
    if subcmd in ("download", "grab", "get") or target_item:
        download_folder = Path(getattr(args, "out_dir", "downloads"))
        item = await grab_item_async(
            target_id_or_title=target_item,
            courses=courses,
            target_cids=target_cids if target_cids else None,
            page=None,
            download_dir=download_folder,
        )
        if getattr(args, "json", False) or getattr(args, "out", None):
            _emit_json(args, item)
        return

    # --- announcements ---
    if subcmd in ("announcements", "announce", "news") or getattr(args, "announcements", False):
        if not target_cids:
            target_cids = list(courses.keys())
        if not target_cids:
            print("❌ No courses configured. Run 'bb courses discover' first.", file=sys.stderr)
            return

        concurrency = get_optimal_concurrency(TaskProfile.LIGHT, getattr(args, "concurrency", None))
        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp, max_concurrency=concurrency))
        await session_manager.initialize()
        raw_ann: dict[str, list[dict]] = {}
        try:
            pool = AsyncCourseWorkerPool(session_manager, task_profile=TaskProfile.LIGHT)
            async def _worker(cid, cname, page):
                data = await scrape_announcements_async(cid, page)
                if getattr(args, "md", False):
                    save_announcements(data, cid)
                return data

            target_dict = {cid: courses.get(cid, cid) for cid in target_cids}
            raw_ann = await pool.execute_task_per_course(target_dict, _worker)
        finally:
            await session_manager.close()

        if getattr(args, "raw", False) or getattr(args, "json", False) or getattr(args, "out", None):
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
    if subcmd in ("grades",) or getattr(args, "grades", False):
        if not target_cids:
            target_cids = list(courses.keys())
        if not target_cids:
            print("❌ No courses configured. Run 'bb courses discover' first.", file=sys.stderr)
            return

        concurrency = get_optimal_concurrency(TaskProfile.LIGHT, getattr(args, "concurrency", None))
        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp, max_concurrency=concurrency))
        await session_manager.initialize()
        raw_gr: dict[str, list[dict]] = {}
        try:
            pool = AsyncCourseWorkerPool(session_manager, task_profile=TaskProfile.LIGHT)
            async def _worker(cid, cname, page):
                data = await scrape_grades_async(cid, page)
                if getattr(args, "md", False):
                    save_grades(data, cid)
                return data

            target_dict = {cid: courses.get(cid, cid) for cid in target_cids}
            raw_gr = await pool.execute_task_per_course(target_dict, _worker)
        finally:
            await session_manager.close()

        if getattr(args, "raw", False) or getattr(args, "json", False) or getattr(args, "out", None):
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
    if subcmd in ("calendar", "cal") or getattr(args, "calendar", False):
        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp))
        await session_manager.initialize()
        try:
            async with session_manager.acquire_page() as page:
                target_cid = target_cids[0] if target_cids else None
                calendar = await scrape_calendar_async(page, target_cid)
                if getattr(args, "md", False):
                    save_calendar(calendar, target_cid)
        finally:
            await session_manager.close()

        if getattr(args, "raw", False) or getattr(args, "json", False) or getattr(args, "out", None):
            _emit_json(args, calendar)
        else:
            print(format_due_dates_table(calendar, window_filter="calendar"))
        return

    # --- activity ---
    if subcmd == "activity" or getattr(args, "activity", False):
        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp))
        await session_manager.initialize()
        try:
            async with session_manager.acquire_page() as page:
                activity = await scrape_activity_async(page)
                if getattr(args, "md", False):
                    save_activity(activity)
        finally:
            await session_manager.close()

        if getattr(args, "raw", False) or getattr(args, "json", False) or getattr(args, "out", None):
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
    if subcmd in ("profile", "whoami") or getattr(args, "profile", False):
        data = await scrape_profile_async()
        if data:
            if getattr(args, "json", False) or getattr(args, "out", None):
                _emit_json(args, data)
            else:
                _print_profile(data)
            if getattr(args, "md", False):
                save_profile(data)
        return

    # --- discussions ---
    if subcmd in ("discussions", "discuss") or getattr(args, "discussions", False):
        if not target_cids:
            print("❌ Specify course (e.g. 'bb discussions IS410' or '-c IS410') or '--all'", file=sys.stderr)
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
                if getattr(args, "md", False):
                    save_discussions(data, course_id, titles_only=getattr(args, "titles_only", False))
                raw_all_disc.append({
                    "course_id": course_id,
                    "course_name": courses.get(course_id, course_id),
                    "discussions": data
                })
                page.close()
            ctx.close()
        if getattr(args, "raw", False) or getattr(args, "json", False) or getattr(args, "out", None):
            _emit_json(args, raw_all_disc)
            return
        print(f"Scraped discussions for {len(target_cids)} courses.")
        return

    print("No subcommand or scraper action selected. Run 'bb --help' to see commands.", file=sys.stderr)


def main() -> None:
    if len(sys.argv) == 1:
        print("Run 'bb --help' to see available commands.", file=sys.stderr)
        sys.exit(1)

    args = _parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

