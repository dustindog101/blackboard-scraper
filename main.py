import argparse
import asyncio
import difflib
import json
import os
import re
import sys
import threading
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

from core.config import BLACKBOARD_BASE, load_courses
from core.gradebook import course_label
from core.export_json import build_composite_schema, build_export_doc
from core.async_engine import AsyncSessionManager, AsyncCourseWorkerPool, EngineConfig, TaskProfile, get_optimal_concurrency


# core.session imports Playwright, so it is loaded on first use. REST commands with a valid session never load it.
def _from_session(name: str):
    import core.session
    return getattr(core.session, name)


def _launch_context(*args, **kwargs):
    return _from_session("_launch_context")(*args, **kwargs)


def _require_session(*args, **kwargs):
    return _from_session("_require_session")(*args, **kwargs)


def login(*args, **kwargs):
    return _from_session("login")(*args, **kwargs)


def login_auto(*args, **kwargs):
    return _from_session("login_auto")(*args, **kwargs)


def quick_check_session_http(*args, **kwargs):
    return _from_session("quick_check_session_http")(*args, **kwargs)


async def check_session_async(*args, **kwargs):
    return await _from_session("check_session_async")(*args, **kwargs)


async def _require_session_async(cdp=None):
    """True when the saved session works. A quick REST probe first; otherwise the full check (and its login help)."""
    from core.rest import RestError, get_json
    try:
        await asyncio.to_thread(get_json, "/learn/api/public/v1/users/me", 6.0)
        return True
    except RestError:
        return await _from_session("_require_session_async")(cdp)


# Scrapers
from scrapers.calendar import save_calendar, scrape_calendar_async
from scrapers.discussions import (
    save_discussions,
    scrape_discussions,
    scrape_discussions_http,
)
from scrapers.grades import SessionExpired, fetch_gradebooks_async, format_grades_text, grades_json, save_grades
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
from scrapers.assignment_view import prepare_assignment_view, format_assignment_view
from scrapers.due_dates import due_report_async, format_calendar_list, format_due_dates_table, public_item, save_due_dates
from scrapers.search import find_items_async, format_download_result, format_search_results, grab_item_async


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


# ---------------------------------------------------------------------------
# Smart Course Selection & Fuzzy Matching
# ---------------------------------------------------------------------------

_COURSE_CODE_RE = re.compile(r"\s*\([^)]*\)\s*(?:SP|FA|SU|WI)\d{4}\s*\([^)]*\)\s*$")


_REQUIRES_COURSE = ("outline", "discussions", "discuss")
_TAKES_COURSE = (
    "outline", "discussions", "discuss", "assignments", "assign", "assignment", "quiz", "asmt", "assessment",
    "grades", "announcements", "announce", "news", "search", "find", "download", "grab", "get", "calendar", "cal",
)


def _usage_error(message: str, *hints: str) -> None:
    print(f"bb: {message}", file=sys.stderr)
    for hint in hints:
        print(f"  {hint}", file=sys.stderr)
    raise SystemExit(2)


def _example_code(courses: Dict[str, str]) -> str:
    return next(iter(_short_label(n).replace(" ", "") for n in courses.values()), "IS410")


def _short_label(name: str) -> str:
    return course_label(name)


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

        # 2. Prefer explicit course-code matches. This prevents a token such as
        # "ECON" from also matching AGNG's title "Longevity Economy".
        code_matches: List[str] = []
        for cid, cname in courses.items():
            code_match = re.match(r"^\s*([A-Za-z]{2,6})\s*([0-9]{3,4})\b", cname)
            if not code_match:
                continue
            department = code_match.group(1).lower()
            full_code = f"{department}{code_match.group(2)}"
            if token_clean in (department, full_code):
                code_matches.append(cid)

        if code_matches:
            for cid in code_matches:
                if cid not in matched_ids:
                    matched_ids.append(cid)
            continue

        # 3. Fall back to fuzzy title/ID matching for human keywords such as
        # "Accounting" or "Database".
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
   - Automatically captures Duo SMS passcodes when macOS Messages access is available.
   - Saves cookies for seamless headless operation.
   - Accepts mode variants: `bb login auto`, `bb login --auto`, `bb login --force`.

3. Visible Manual Login Fallback:
   $ bb login --manual   (or `bb login manual`)
   - Opens a visible browser if you prefer Duo Push, TouchID, or Security Keys.

4. Session Health & Telemetry:
   $ bb check            (or `bb session check`)
   $ bb session stats    (displays rolling lifespan metrics)

5. Move a Session Between Computers:
   $ bb session export bb-session.json
   $ bb session import bb-session.json
   - Copy the plain JSON file from Mac to Windows; import checks it before replacing cookies.
   - Keep the cookie file private. No password or OTP is included.
   - Stop background jobs before importing. Run `bb discover` if courses are not configured.
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
        # `bb <command> help` -> `bb <command> --help` (imsg-style helper).
        if len(argv) == 2 and argv[1] == "help":
            return [argv[0], "--help"], None
        return argv, None

    BOOLEAN_PRE_FLAGS = {"--json", "--compact", "--raw", "-v", "--visible", "--md", "--save"}
    if argv[0] in BOOLEAN_PRE_FLAGS and len(argv) > 1:
        subcmd_idx = None
        for idx, token in enumerate(argv):
            if token in subcommands:
                subcmd_idx = idx
                break
        if subcmd_idx is not None:
            reordered = [argv[subcmd_idx]] + [arg for i, arg in enumerate(argv) if i != subcmd_idx]
            return reordered, None

    if argv[0] in ("-h", "--help", "-V", "--version"):
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
        if target:
            new_argv.insert(0, target)
        new_argv.insert(0, "assignment")
        legacy_found = f"assignment {target}".strip() if target else "assignment"
    elif "--begin-attempt" in new_argv:
        idx = new_argv.index("--begin-attempt")
        new_argv.pop(idx)
        target = ""
        if idx < len(new_argv) and not new_argv[idx].startswith("-"):
            target = new_argv.pop(idx)
        new_argv.append("--start-attempt")
        if target:
            new_argv.insert(0, target)
        new_argv.insert(0, "assignment")
        legacy_found = f"assignment {target} --start-attempt".strip() if target else "assignment --start-attempt"
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
        legacy_found = f"search {query}".strip() if query else "search"
    elif "--grab" in new_argv or "--download" in new_argv:
        flag = "--grab" if "--grab" in new_argv else "--download"
        idx = new_argv.index(flag)
        new_argv.pop(idx)
        item = ""
        if idx < len(new_argv) and not new_argv[idx].startswith("-"):
            item = new_argv.pop(idx)
            new_argv.insert(0, item)
        new_argv.insert(0, "download")
        legacy_found = f"download {item}".strip() if item else "download"
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
        legacy_found = f"guide {topic}".strip() if topic else "guide"

    return new_argv, legacy_found


__version__ = "2.0.0"


# ---------------------------------------------------------------------------
# Help UX infrastructure (imsg-style: `bb --help`, `bb <command> --help`)
# ---------------------------------------------------------------------------
# HOW TO ADD A NEW COMMAND (future-proofing):
#   1. Add the canonical name to _CANONICAL_COMMANDS (keep grouped order).
#   2. If it has aliases, add them to _COMMAND_ALIASES (alias -> canonical).
#   3. Add one add_parser() call in _build_parser() with:
#        help="<short verb phrase>",
#        description="<one-line description>",
#        epilog="Examples:\n  bb <cmd> ...\n  bb <cmd> ...",
#      following the template of the existing commands below.
#   4. `bb help <name>` / `bb <name> help` / did-you-mean then work for free.

_CANONICAL_COMMANDS = [
    # Getting started
    "login", "logout", "check", "session",
    # Daily workflow
    "briefing", "due", "grades", "announcements",
    # Course content
    "outline", "assignments", "assignment", "search", "download",
    "calendar", "activity", "profile", "discussions",
    # Discovery
    "courses", "discover", "terms",
    # System
    "bot", "menubar", "guide", "help",
]

_COMMAND_ALIASES = {
    "brief": "briefing",
    "announce": "announcements",
    "news": "announcements",
    "assign": "assignments",
    "quiz": "assignment",
    "asmt": "assignment",
    "assessment": "assignment",
    "find": "search",
    "grab": "download",
    "get": "download",
    "cal": "calendar",
    "whoami": "profile",
    "discuss": "discussions",
    "app": "menubar",
}

_GUIDE_TOPICS = ["auth", "courses", "schema", "telegram", "concurrency"]

# Last argv passed to _parse_args (lets HelpfulParser.error() scope invalid
# values to the right subcommand even when sys.argv belongs to a test runner).
_LAST_ARGV: List[str] = []


class HelpfulParser(argparse.ArgumentParser):
    """ArgumentParser with concise did-you-mean errors (imsg-style)."""

    def error(self, message: str) -> None:
        # Invalid subcommand choice -> short suggestion, not a 500-char dump.
        if "invalid choice" in message and "argument <command>" in message:
            bad = ""
            m = re.search(r"invalid choice:\s*'([^']+)'", message)
            if m:
                bad = m.group(1)
            suggestion = _suggest_command(bad) if bad else None
            self.print_usage(sys.stderr)
            lines = [f"bb: unknown command '{bad}'"] if bad else ["bb: unknown command"]
            if suggestion:
                lines.append(f"Did you mean 'bb {suggestion}'?")
            lines.append("Run 'bb --help' to see commands.")
            self.exit(2, "\n".join(lines) + "\n")
        # Invalid choice for a subcommand action (e.g. bot action, guide
        # topic) -> point at that command's help instead of dumping usage.
        m2 = re.search(r"invalid choice:\s*'([^']+)'", message)
        if m2:
            bad = m2.group(1)
            argv = _LAST_ARGV or (sys.argv[1:] if isinstance(sys.argv, list) else [])
            scope = argv[0] if argv and argv[0] in _CANONICAL_COMMANDS else None
            self.print_usage(sys.stderr)
            if scope:
                self.exit(2, f"bb: unknown value '{bad}' for 'bb {scope}'. Run 'bb {scope} --help' to see valid values.\n")
            self.exit(2, f"bb: unknown value '{bad}'. Run 'bb --help' to see commands.\n")
        super().error(message)


def _suggest_command(name: str) -> Optional[str]:
    """Closest canonical command or alias target for a typo."""
    candidates = _CANONICAL_COMMANDS + list(_COMMAND_ALIASES.keys())
    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=0.6)
    if not matches:
        return None
    hit = matches[0]
    return _COMMAND_ALIASES.get(hit, hit)


def _print_subcommand_help(canonical: str) -> None:
    """Print `bb <command> --help` for the help dispatcher (raises SystemExit)."""
    parser = _build_parser()
    parser.parse_args([canonical, "--help"])


def _build_parser() -> argparse.ArgumentParser:
    parser = HelpfulParser(
        prog="bb",
        description="UMBC Blackboard Ultra scraper & assistant.\nHeadless course scraping with smart course selection and v2 JSON output.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Common commands:
  bb login                  First-time SSO login (auto Duo SMS capture)
  bb due 7d                 Upcoming deadlines (7d, 14d, overdue, all)
  bb outline IS410          Course outline (shallow summary with counts)
  bb grades IS410           Gradebook and feedback
  bb briefing               Daily briefing across all courses

Run 'bb <command> --help' for options, usage, and examples.
Guides: 'bb guide <topic>' (auth, courses, schema, telegram, concurrency).""",
    )
    parser.add_argument("--version", "-V", action="version", version=f"bb {__version__}")

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
    course_parent.add_argument("course_pos", nargs="?", metavar="COURSE", help="Course code, ID, or keyword (e.g. IS410). Comma-separated or --all")
    course_parent.add_argument("--course", "-c", help="Target course code(s) or ID(s)")
    course_parent.add_argument("--all", action="store_true", help="Target all configured courses")

    subparsers = parser.add_subparsers(dest="subcommand", metavar="<command>")
    # Every subcommand keeps newlines in description/epilog (Examples stay
    # copy-pasteable). Future add_parser() calls inherit this automatically.
    _orig_add_parser = subparsers.add_parser

    def _add_parser(name: str, **kwargs: object) -> argparse.ArgumentParser:
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        return _orig_add_parser(name, **kwargs)

    subparsers.add_parser = _add_parser  # type: ignore[method-assign]

    # --- getting started ---
    login_p = subparsers.add_parser(
        "login",
        parents=[engine_parent],
        help="Log in via UMBC SSO",
        description="Smart Login: automated headless SSO with Duo SMS capture.",
        epilog="Examples:\n  bb login\n  bb login --manual\n  bb login --force",
    )
    login_p.add_argument("mode", nargs="?", choices=["auto", "manual", "browser", "sms"], default="auto", help="Login mode ('auto' or 'manual')")
    login_p.add_argument("--manual", action="store_true", help="Open visible browser window for manual 2FA")
    login_p.add_argument("--auto", "-a", action="store_true", help="Automated SSO login (default)")
    login_p.add_argument("--force", "-f", action="store_true", help="Force re-login even if active session exists")
    login_p.add_argument("--username", "-u", help="Username for login (prompts if omitted)")
    login_p.add_argument("--password", "-p", help="Password for login (prompts if omitted)")
    login_p.add_argument("--passcode", "--duo-passcode", dest="duo_passcode", help="Provide a 6–8 digit Duo SMS passcode directly via CLI")
    login_p.add_argument("--code-file", dest="code_file", metavar="PATH", help="Start SSO once, send the Duo SMS, print WAITING_FOR_CODE, then poll PATH for the code (deleted after reading)")
    login_p.add_argument("--timeout", dest="code_timeout", type=float, default=300.0, help="Seconds to wait for the --code-file code (default: 300)")

    subparsers.add_parser(
        "logout",
        help="Log out (clear session)",
        description="Clear cached session cookies.",
        epilog="Examples:\n  bb logout",
    )

    # --- session ---
    session_p = subparsers.add_parser(
        "session",
        parents=[engine_parent],
        help="Check or transfer a saved session",
        description="Session probes, telemetry, and plain JSON cookie export/import.",
        epilog="Examples:\n  bb session check\n  bb session stats\n  bb session info\n  bb session export bb-session.json\n  bb session import bb-session.json",
    )
    session_p.add_argument("action", nargs="?", choices=["check", "stats", "info", "telemetry", "export", "import"], default="check", help="Session action")
    session_p.add_argument("file", nargs="?", metavar="FILE", help="JSON cookie file for export/import")
    session_p.add_argument("--overwrite", action="store_true", help="Replace an existing export file")
    session_p.add_argument("--debug", action="store_true", help="Print debug output with check")

    check_p = subparsers.add_parser(
        "check",
        parents=[engine_parent],
        help="Quick session health check",
        description="Rapid session token validity probe.",
        epilog="Examples:\n  bb check\n  bb check --debug",
    )
    check_p.add_argument("--debug", action="store_true", help="Print debug output")

    # --- daily workflow ---
    brief_p = subparsers.add_parser(
        "briefing",
        aliases=["brief"],
        parents=[output_parent, engine_parent],
        help="Daily briefing across all courses",
        description="Concurrent daily briefing across all courses.",
        epilog="Examples:\n  bb briefing\n  bb briefing --json\n  bb briefing --telegram",
    )
    brief_p.add_argument("--telegram", action="store_true", help="Send briefing to Telegram")

    due_p = subparsers.add_parser(
        "due",
        parents=[output_parent, engine_parent],
        help="Upcoming deadlines across courses",
        description="Cross-Source Aggregator: calendar events + gradebook columns, filtered by a Window Filter.",
        epilog="Examples:\n  bb due\n  bb due 7d\n  bb due 14d --exclude-completed\n  bb due overdue",
    )
    due_p.add_argument("window", nargs="?", default="7d", metavar="WINDOW", help="Relative date window (e.g. 7d, 14d, overdue, all; default: 7d)")
    due_p.add_argument("--exclude-completed", action="store_true", help="Exclude submitted/graded items")

    subparsers.add_parser(
        "grades",
        parents=[course_parent, output_parent, engine_parent],
        help="Gradebook and feedback",
        description="Scrape a course gradebook.",
        epilog="Examples:\n  bb grades\n  bb grades --summary\n  bb grades IS410\n  bb grades -c IS410 --json\n  bb grades --all",
    ).add_argument("--summary", action="store_true", help="Print only the one-line-per-course overall grade table")
    ann_p = subparsers.add_parser(
        "announcements",
        aliases=["announce", "news"],
        parents=[course_parent, output_parent, engine_parent],
        help="Course announcements (newest first)",
        description="Announcements for all current courses (or one), newest first. Read state is not in the REST API, so --unread checks it in the browser.",
        epilog="Examples:\n  bb announcements\n  bb announcements IS410 --limit 3\n  bb announcements --since 14d\n  bb announcements --unread",
    )
    ann_p.add_argument("--since", metavar="WHEN", help="Only posts since 14d, 2w or a date (2026-09-01)")
    ann_p.add_argument("--limit", type=int, metavar="N", help="At most N posts per course")
    ann_p.add_argument("--unread", action="store_true", help="Only posts verified unread in the browser (unknown state is excluded)")

    # --- course content ---
    outline_p = subparsers.add_parser(
        "outline",
        parents=[course_parent, output_parent, engine_parent],
        help="Course outline and files",
        description="Shallow Outline by default; expand one folder with -f or everything with --expand-all.",
        epilog='Examples:\n  bb outline IS410\n  bb outline IS410 -f "Homework"\n  bb outline IS410 --expand-all\n  bb outline IS410 -i',
    )
    outline_p.add_argument("--folder", "-f", metavar="FOLDER", help="Expand specific folder or module by name or ID")
    outline_p.add_argument("--expand-all", "--deep", "--all-folders", dest="expand_all", action="store_true", help="Expand all folders into full tree")
    outline_p.add_argument("--depth", type=int, metavar="N", help="Limit outline display depth")
    outline_p.add_argument("--interactive", "-i", action="store_true", help="Interactive terminal menu to select and explore folders")
    outline_p.add_argument("--type", help="Filter items by type (e.g. syllabus, document, assignment, folder, link)")
    outline_p.add_argument("--filter", dest="keyword_filter", help="Filter items by text keyword")

    assign_p = subparsers.add_parser(
        "assignments",
        aliases=["assign"],
        parents=[course_parent, output_parent, engine_parent],
        help="List assignments and their status",
        description="List Gradable Items for all current courses (or one); inspect one with bb assignment.",
        epilog="Examples:\n  bb assignments\n  bb assignments IS410\n  bb assignments IS410 --json",
    )
    assign_p.add_argument("--filter", dest="keyword_filter", help="Filter assignments by keyword")

    single_p = subparsers.add_parser(
        "assignment",
        aliases=["quiz", "asmt", "assessment"],
        parents=[output_parent, engine_parent],
        help="Inspect an assignment and your submission",
        description="Show assignment instructions and brief details by default. Use --rubric for the rubric or --full for existing work and all accessible details. Never starts an attempt unless requested.",
        epilog=(
            "Examples:\n"
            '  bb assignment "M3 Discussion" -c ECON\n'
            '  bb assignment "Module 7" -c AGNG --rubric\n'
            '  bb assignment "Module 4" -c AGNG --full\n'
            '  bb assignments ECON --filter "M3"   # Find the exact title first\n'
            "  bb assignment _8954640_1 --json\n"
            '  bb assignment "Final" -c IS410 --start-attempt --force-start'
        ),
    )
    single_p.add_argument("target", nargs="?", metavar="TARGET", help="Assignment ID or title (e.g. _8954640_1 or 'Homework 1')")
    single_p.add_argument("--course", "-c", help="Scope the title search to course ID(s) or code(s)")
    single_p.add_argument("--all", action="store_true", help="Search across all configured courses")
    view_group = single_p.add_mutually_exclusive_group()
    view_group.add_argument("--instructions", dest="assignment_view", action="store_const", const="instructions", help="Only instructions and a rubric availability hint")
    view_group.add_argument("--rubric", dest="assignment_view", action="store_const", const="rubric", help="Only the rubric visible before starting (no instructions or previous answers)")
    view_group.add_argument("--full", dest="assignment_view", action="store_const", const="full", help="Instructions, visible rubric, existing questions/answers or posts, and submission file details")
    single_p.set_defaults(assignment_view="default")
    single_p.add_argument("--start-attempt", "--begin-attempt", dest="start_attempt", action="store_true", help="Allow beginning a new attempt if none is active (guarded)")
    single_p.add_argument("--force-start", dest="force_start", action="store_true", help="With --start-attempt: confirm starting timed assessments/exams")

    search_p = subparsers.add_parser(
        "search",
        aliases=["find"],
        parents=[output_parent, engine_parent],
        help="Search content across courses",
        description="Search Content Items across one, several, or all courses.",
        epilog='Examples:\n  bb search "Python"\n  bb search "Syllabus" -c IS410\n  bb search "Exam" --all --json',
    )
    search_p.add_argument("query", metavar="QUERY", help="Search query string")
    search_p.add_argument("--type", help="Filter results by content type")
    search_p.add_argument("--course", "-c", help="Restrict search to specific course ID(s) or code(s)")
    search_p.add_argument("--all", action="store_true", help="Search across all configured courses")

    dl_p = subparsers.add_parser(
        "download",
        aliases=["grab", "get"],
        parents=[output_parent, engine_parent],
        help="Download a file by name or ID",
        description="Find a Content Item by title or ID and download it.",
        epilog='Examples:\n  bb download "Chapter01.ipynb"\n  bb download "Syllabus.pdf" -c IS410\n  bb download _123_1 --out-dir downloads',
    )
    dl_p.add_argument("item", metavar="ITEM_ID_OR_NAME", help="Content item ID or file title")
    dl_p.add_argument("--out-dir", default="downloads", help="Destination directory (default: downloads)")
    dl_p.add_argument("--course", "-c", help="Restrict download target to specific course ID(s) or code(s)")
    dl_p.add_argument("--all", action="store_true", help="Search across all configured courses")

    subparsers.add_parser(
        "calendar",
        aliases=["cal"],
        parents=[course_parent, output_parent, engine_parent],
        help="Calendar due dates",
        description="Scrape calendar due dates.",
        epilog="Examples:\n  bb calendar\n  bb calendar IS410\n  bb calendar --json",
    )
    act_p = subparsers.add_parser(
        "activity",
        parents=[output_parent, engine_parent],
        help="Homepage activity stream",
        description="Recent activity (grades, due dates, posts, new content) grouped by course, newest first.",
        epilog="Examples:\n  bb activity\n  bb activity --limit 100\n  bb activity --json",
    )
    act_p.add_argument("--limit", type=int, metavar="N", help="Newest N items (default 40; 0 = everything)")
    act_p.add_argument("--full", action="store_true", help="Include classmates' names and post excerpts (hidden by default)")
    subparsers.add_parser(
        "profile",
        aliases=["whoami"],
        parents=[output_parent, engine_parent],
        help="Show your student profile",
        description="Show student profile information.",
        epilog="Examples:\n  bb profile\n  bb whoami --json",
    )

    disc_p = subparsers.add_parser(
        "discussions",
        aliases=["discuss"],
        parents=[course_parent, output_parent, engine_parent],
        help="Course discussions",
        description="Scrape course discussions.",
        epilog="Examples:\n  bb discussions IS410\n  bb discussions IS410 --titles-only",
    )
    disc_p.add_argument("--max-posts", type=int, help="Maximum posts to click (browser fallback only)")
    disc_p.add_argument("--max-parts", type=int, help="Maximum participants to click (browser fallback only)")
    disc_p.add_argument("--full", action="store_true", help="Read posts and replies, including classmates' names and text (hidden by default)")
    disc_p.add_argument("--posts-only", action="store_true", help="Scrape posts only")
    disc_p.add_argument("--participants-only", action="store_true", help="Scrape participants only")
    disc_p.add_argument("--titles-only", action="store_true", help="Scrape thread titles only")

    # --- discovery ---
    courses_p = subparsers.add_parser(
        "courses",
        parents=[output_parent, engine_parent],
        help="List configured courses",
        description="Course configuration and discovery.",
        epilog="Examples:\n  bb courses\n  bb courses discover\n  bb courses terms",
    )
    courses_p.add_argument("action", nargs="?", choices=["list", "discover", "terms"], default="list", help="Course action ('list', 'discover', 'terms')")
    courses_p.add_argument("--term", metavar="TERM", help="Filter academic term (e.g. current, FA2026, all)")

    discover_p = subparsers.add_parser(
        "discover",
        parents=[output_parent, engine_parent],
        help="Find your courses and SAVE them to config.json",
        description="Find the courses of the active term (or --term) and replace the course list in config.json. Use 'bb terms' to look without saving.",
        epilog="Examples:\n  bb terms                 # look, changes nothing\n  bb discover\n  bb discover --term FA2026",
    )
    discover_p.add_argument("--term", metavar="TERM", help="Filter academic term (e.g. current, FA2026, all)")

    subparsers.add_parser(
        "terms",
        parents=[output_parent, engine_parent],
        help="List enrolled terms (read-only)",
        description="List every enrolled term and its courses. Read-only; 'bb discover' saves the list.",
        epilog="Examples:\n  bb terms",
    )

    # --- system ---
    bot_p = subparsers.add_parser(
        "bot",
        help="Manage the Telegram bot daemon",
        description="Daemon Command Group: control the background Telegram bot.",
        epilog="Examples:\n  bb bot run\n  bb bot start\n  bb bot status\n  bb bot stop",
    )
    bot_p.add_argument("action", nargs="?", choices=["run", "start", "stop", "restart", "status"], default="run", help="Bot action ('start', 'stop', 'restart', 'status', 'run')")
    bot_p.add_argument("--daemon", "-d", action="store_true", help="Run daemon detached in background")

    subparsers.add_parser(
        "menubar",
        aliases=["app"],
        help="Launch the macOS menubar app",
        description="Launch the native macOS menubar app.",
        epilog="Examples:\n  bb menubar",
    )

    guide_p = subparsers.add_parser(
        "guide",
        help="Topic guides (auth, courses, ...)",
        description="Comprehensive topic manuals. For command help use 'bb help <command>'.",
        epilog="Examples:\n  bb guide\n  bb guide auth\n  bb guide courses",
    )
    guide_p.add_argument("topic", nargs="?", help="Guide topic (auth, courses, schema, telegram, concurrency)")

    help_p = subparsers.add_parser(
        "help",
        help="Show help for a command or topic",
        description="Show help for a command or guide topic. Same as 'bb <command> --help'.",
        epilog="Examples:\n  bb help\n  bb help outline\n  bb help auth",
    )
    help_p.add_argument("topic", nargs="?", metavar="COMMAND_OR_TOPIC", help="Command or guide topic (e.g. outline, auth)")

    return parser


def _parse_args(args_list: Optional[List[str]] = None) -> argparse.Namespace:
    global _LAST_ARGV
    raw_args = list(sys.argv[1:] if args_list is None else args_list)
    translated_args, legacy_hint = _intercept_legacy_args(raw_args)
    _LAST_ARGV = translated_args
    if legacy_hint and sys.stderr.isatty():
        print(f"💡 Tip: You can run 'bb {legacy_hint}' directly without '--'.", file=sys.stderr)

    parser = _build_parser()
    args = parser.parse_args(translated_args)
    if args.subcommand == "session":
        transfer = args.action in ("export", "import")
        if transfer and not args.file:
            parser.error(f"session {args.action} requires FILE")
        if not transfer and args.file:
            parser.error("FILE is only supported for session export/import")
        if args.overwrite and args.action != "export":
            parser.error("--overwrite is only supported for session export")
        if transfer and args.cdp:
            parser.error("session export/import uses the saved session; --cdp is not supported")
    if args.subcommand in ("announcements", "announce", "news"):
        from scrapers.announcements import parse_since
        try:
            if args.since:
                parse_since(args.since)
        except ValueError as exc:
            parser.error(str(exc))
        if args.limit is not None and args.limit < 0:
            parser.error("--limit must be 0 or more")
    if getattr(args, "start_attempt", False) and getattr(args, "assignment_view", "default") in ("instructions", "rubric"):
        parser.error("--start-attempt cannot be combined with --instructions or --rubric; use --full")
    return args



# ---------------------------------------------------------------------------
# Discovery Subcommand
# ---------------------------------------------------------------------------

def _discover_via_browser(headless: bool = True, cdp: str | None = None) -> None:
    """Browser fallback for `bb discover` when the course REST API is unavailable. Saves config.json."""
    if not _require_session(cdp):
        return
    from playwright.sync_api import sync_playwright
    from scrapers.base import _navigate_and_check_page

    with sync_playwright() as p:
        ctx, page = _launch_context(p, headless, cdp)
        try:
            if not _navigate_and_check_page(page, f"{BLACKBOARD_BASE}/ultra/course"):
                return
            page.wait_for_selector(".course-element-card", timeout=15_000)
            discovered = page.evaluate(
                """() => {
                    const results = {};
                    document.querySelectorAll('.course-element-card').forEach(el => {
                        const titleEl = el.querySelector('.js-course-title-element');
                        const courseId = el.getAttribute('data-course-id');
                        if (titleEl && courseId) results[courseId] = titleEl.innerText.trim();
                    });
                    return results;
                }"""
            )
        finally:
            ctx.close()
    if discovered:
        from core.course_discovery import save_selected
        save_selected("browser", discovered)
        print(f"Saved {len(discovered)} courses to config.json:")
        for cid, cname in discovered.items():
            print(f"  {cid}  {cname}")
    else:
        print("bb: no courses found in the browser either; config.json left unchanged.", file=sys.stderr)


def _run_discussions_sync(
    target_cids: list[str],
    courses: dict[str, str],
    headless: bool,
    cdp: str | None,
    kwargs: dict,
    md: bool,
    titles_only: bool,
) -> list[dict]:
    # 1. REST Fast-Path, all courses at once (each course bounds its own per-topic pool).
    data_by_cid: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}

    def _rest(cid: str):
        return scrape_discussions_http(
            cid,
            include_posts=not titles_only,
            posts_only=kwargs.get("posts_only", False),
            participants_only=kwargs.get("participants_only", False),
        )

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(target_cids)))) as pool:
        for cid, rest in zip(target_cids, pool.map(_rest, target_cids)):
            if rest is not None:
                data_by_cid[cid] = rest

    # 2. Playwright Fallback for courses where the REST API was unavailable.
    browser_cids = [cid for cid in target_cids if cid not in data_by_cid]
    if browser_cids:
        print("REST discussions unavailable; reading them in the browser.", file=sys.stderr)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx, _ = _launch_context(p, headless, cdp)
            for course_id in browser_cids:
                page = ctx.new_page()
                try:
                    data_by_cid[course_id] = scrape_discussions(course_id, page, **kwargs)
                except Exception as exc:
                    errors[course_id] = str(exc) or type(exc).__name__
                    print(f"bb: browser read of {_short_label(courses.get(course_id, course_id))} failed: {errors[course_id]}", file=sys.stderr)
                    data_by_cid[course_id] = []
                finally:
                    page.close()
            ctx.close()

    raw_all_disc: list[dict] = []
    for course_id in target_cids:
        data = data_by_cid.get(course_id, [])
        if md:
            save_discussions(data, course_id, titles_only=titles_only)
        entry = {
            "course_id": course_id,
            "course_name": courses.get(course_id, course_id),
            "discussions": data,
        }
        if course_id in errors:
            entry["error"] = errors[course_id]
        raw_all_disc.append(entry)
    return raw_all_disc



# ---------------------------------------------------------------------------
# Main Async Execution Dispatcher
# ---------------------------------------------------------------------------

async def main_async(args: argparse.Namespace) -> None:
    subcmd = getattr(args, "subcommand", None)

    # --- help dispatcher (`bb help [command|topic]`) ---
    if subcmd == "help":
        topic = getattr(args, "topic", None)
        if not topic:
            _build_parser().print_help()
            return
        canonical = _COMMAND_ALIASES.get(topic, topic)
        if topic in HELP_GUIDES or canonical in HELP_GUIDES:
            print(HELP_GUIDES[canonical if canonical in HELP_GUIDES else topic].strip())
            return
        if canonical in _CANONICAL_COMMANDS:
            try:
                _print_subcommand_help(canonical)
            except SystemExit as e:
                # argparse prints subcommand help then exits 0; propagate.
                raise e
            return
        suggestion = _suggest_command(topic)
        print(f"bb: unknown help topic '{topic}'", file=sys.stderr)
        if suggestion:
            print(f"Did you mean 'bb help {suggestion}'?", file=sys.stderr)
        print("Run 'bb --help' to see commands or 'bb guide' to see guide topics.", file=sys.stderr)
        sys.exit(2)

    # --- topic guides (`bb guide [topic]`) ---
    if subcmd == "guide":
        topic = getattr(args, "topic", None)
        if not topic:
            print("Available guides: auth, courses, schema, telegram, concurrency\nRun 'bb guide <topic>' (e.g. 'bb guide auth').")
            print("For command help run 'bb help <command>' (e.g. 'bb help outline').")
            return
        guide_text = HELP_GUIDES.get(topic)
        if guide_text:
            print(guide_text.strip())
        else:
            suggestion = _suggest_command(topic)
            print(f"bb: unknown guide topic '{topic}'. Available: auth, courses, schema, telegram, concurrency", file=sys.stderr)
            if suggestion and suggestion not in HELP_GUIDES:
                print(f"Hint: 'bb help {suggestion}' shows command help.", file=sys.stderr)
            sys.exit(2)
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
        from core import commands
        c_action = getattr(args, "action", "list") if subcmd == "courses" else ("discover" if subcmd == "discover" or getattr(args, "discover", False) else ("terms" if subcmd == "terms" or getattr(args, "list_terms", False) else "list"))
        emit = lambda data: _emit_json(args, data)  # noqa: E731
        if c_action == "discover":
            if not await commands.run_discover(args, emit):
                await asyncio.to_thread(_discover_via_browser, headless, cdp)
            return
        if c_action == "terms":
            await commands.run_terms(args, emit)
            return
        # list configured courses
        if commands.machine_mode(args):
            _emit_json(args, {"courses": [{"course_id": cid, "course_name": name} for cid, name in courses.items()]})
        else:
            for cid, name in courses.items():
                print(f"{_short_label(name):<10}{cid}  {name}")
        return

    # --- login & logout ---
    if subcmd == "login" or getattr(args, "login", False) or getattr(args, "auto_exp", False):
        is_manual = getattr(args, "manual", False) or getattr(args, "mode", "auto") in ("manual", "browser") or (getattr(args, "visible", False) and not getattr(args, "auto", False))
        force_flag = getattr(args, "force", False)
        username = getattr(args, "username", None)
        password = getattr(args, "password", None)

        code_file = getattr(args, "code_file", None)
        if is_manual and not code_file:
            await asyncio.to_thread(login, force_flag, username, password, cdp)
        else:
            cancel_event = threading.Event()
            try:
                success = await asyncio.to_thread(
                    login_auto,
                    username=username,
                    password=password,
                    headless=headless,
                    cdp_url=cdp,
                    auto_exp=True,
                    force=force_flag,
                    passcode=getattr(args, "duo_passcode", None),
                    cancel_event=cancel_event,
                    code_file=code_file,
                    code_timeout=getattr(args, "code_timeout", 300.0),
                )
            except asyncio.CancelledError:
                cancel_event.set()
                raise
            if not success:
                raise SystemExit(1)
        return

    if subcmd == "logout" or getattr(args, "logout", False):
        from core.session import logout as do_logout
        await asyncio.to_thread(do_logout, keep_config_creds=True)
        return

    # --- session probes & stats ---
    if subcmd in ("session", "check") or getattr(args, "check_session", False) or getattr(args, "session_stats", False) or getattr(args, "session_info", False):
        sess_action = "check" if subcmd == "check" or getattr(args, "check_session", False) else (getattr(args, "action", "check") if subcmd == "session" else ("stats" if getattr(args, "session_stats", False) else "info"))
        if sess_action in ("export", "import"):
            from core.session_transfer import SessionTransferError, export_session, import_session
            try:
                if sess_action == "export":
                    count = await asyncio.to_thread(export_session, args.file, overwrite=args.overwrite)
                    print(f"Exported {count} Blackboard cookies to {Path(args.file).expanduser()}")
                else:
                    count, user = await asyncio.to_thread(import_session, args.file)
                    account = user.get("userName") or user.get("studentId") or user.get("id")
                    print(f"Imported {count} Blackboard cookies. Verified account: {account}")
            except SessionTransferError as error:
                print(f"Session {sess_action} failed: {error}", file=sys.stderr)
                raise SystemExit(1) from None
            return
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

    # Usage errors are short, exit 2, and name the fix. They run before any network or session probe.
    if subcmd in _REQUIRES_COURSE and not target_cids:
        _usage_error(f"{subcmd} needs a course.", f"Try: bb {subcmd} {_example_code(courses)}   (or --all)", "List yours: bb courses")
    if target_course_arg and not target_cids and subcmd in _TAKES_COURSE:
        _usage_error(
            f"no course matches '{target_course_arg}'.",
            "Yours: " + ", ".join(_short_label(n) for n in courses.values()),
            "Use a code like IS410, part of a title, a course ID, or --all.",
        )

    # From here down, academic commands require an active session
    if not await _require_session_async(cdp):
        return

    # --- briefing ---
    if subcmd in ("briefing", "brief") or getattr(args, "briefing", False):
        concurrency = get_optimal_concurrency(TaskProfile.MEDIUM, getattr(args, "concurrency", None))
        try:
            bundle = await run_briefing_async(
                headless=headless,
                cdp_url=cdp,
                write_markdown=getattr(args, "md", False),
                concurrency=concurrency,
            )
        except SessionExpired as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(1)

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
        try:
            report = await due_report_async(
                None, courses, window_filter=window,
                exclude_completed=getattr(args, "exclude_completed", False),
            )
        except SessionExpired as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(1)
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(2)
        if getattr(args, "md", False):
            save_due_dates(report["items"], window_filter=window)

        if getattr(args, "raw", False) or getattr(args, "json", False) or getattr(args, "out", None):
            _emit_json(args, [public_item(e) for e in report["items"]])
        else:
            print(format_due_dates_table(report["items"], window_filter=window, notes=report["notes"], overdue=report["overdue"]))
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
            target_cids = list(courses.keys())  # no course given: all current courses, like due/announcements
        if not target_cids:
            _usage_error("no courses configured.", "Run: bb discover")

        raw_all_assign: dict[str, list[dict]] = {}

        # 1. REST Fast-Path, all courses at once
        if not getattr(args, "visible", False):
            def _filter(data):
                if args.keyword_filter:
                    kw = args.keyword_filter.lower()
                    data = [item for item in data if kw in item.get("title", "").lower() or kw in item.get("instructions", "").lower()]
                return data

            rest_results = await asyncio.gather(*(asyncio.to_thread(scrape_course_assignments_http, cid) for cid in target_cids))
            for cid, data in zip(target_cids, rest_results):
                if data is not None:
                    data = _filter(data)
                    if args.md:
                        save_assignments(data, cid)
                    raw_all_assign[cid] = data

        # 2. Playwright Browser Fallback (if REST unavailable for any course)
        missing_cids = [cid for cid in target_cids if cid not in raw_all_assign]
        if missing_cids:
            print("REST unavailable for some courses; using the browser.", file=sys.stderr)
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
            sys.exit(1)
        target_cid = target_cids[0] if target_cids else None
        force_browser = getattr(args, "visible", False)
        allow_start = getattr(args, "start_attempt", False)
        force_start = getattr(args, "force_start", False)
        view = getattr(args, "assignment_view", "default")
        if allow_start and view == "default":
            view = "full"
        data = await scrape_assessment_attempt_async(
            target=assignment_target,
            course_id=target_cid,
            headless=headless,
            force_browser=force_browser,
            allow_start=allow_start,
            force_start=force_start,
            include_work=view == "full",
        )
        data = await asyncio.to_thread(prepare_assignment_view, data, view)

        if data.get("error"):
            if getattr(args, "raw", False) or getattr(args, "json", False) or getattr(args, "out", None):
                _emit_json(args, data)
            else:
                print(format_assessment_attempt_cli(data), file=sys.stderr)
            sys.exit(2)

        if getattr(args, "md", False):
            saved_path = save_assessment_attempt(data)
            print(f"💾 Saved assessment Markdown report to: {_safe_relpath(saved_path)}", file=sys.stderr)

        if getattr(args, "raw", False) or getattr(args, "json", False) or getattr(args, "out", None):
            _emit_json(args, data)
        else:
            print(format_assignment_view(data))
        return

    # --- search / find ---
    search_query = getattr(args, "query", None)
    if subcmd in ("search", "find") or search_query:
        search_scope = {cid: courses[cid] for cid in target_cids} if target_cids else courses
        matches = await find_items_async(search_query, search_scope, page=None, type_filter=getattr(args, "type", None))
        if getattr(args, "json", False) or getattr(args, "out", None):
            _emit_json(args, matches)
        else:
            print(format_search_results(matches, search_query))
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
        else:
            print(format_download_result(item))
        if item.get("status") in ("not_found", "multiple_matches"):
            raise SystemExit(1)
        return

    # --- announcements ---
    if subcmd in ("announcements", "announce", "news") or getattr(args, "announcements", False):
        if not target_cids:
            target_cids = list(courses.keys())
        if not target_cids:
            _usage_error("no courses configured.", "Run: bb discover")
        from core import commands
        await commands.run_announcements(args, courses, target_cids, lambda data: _emit_json(args, data))
        return

    # --- grades ---
    if subcmd in ("grades",) or getattr(args, "grades", False):
        if not target_cids:
            target_cids = list(courses.keys())
        if not target_cids:
            print("❌ No courses configured. Run 'bb courses discover' first.", file=sys.stderr)
            return

        target_dict = {cid: courses.get(cid, cid) for cid in target_cids}
        try:
            results = await fetch_gradebooks_async(target_dict)
        except SessionExpired as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(1)
        books = [r for r in results.values() if not r.get("error")]
        skipped = [courses.get(cid, cid) for cid, r in results.items() if r.get("error")]
        if getattr(args, "md", False):
            for book in books:
                save_grades(book, book["course_id"])
        for name in skipped:
            print(f"⚠️ {course_label(name)}: gradebook not visible to students, skipped", file=sys.stderr)

        if getattr(args, "raw", False) or getattr(args, "json", False) or getattr(args, "out", None):
            _emit_json(args, grades_json(books))
        else:
            print(format_grades_text(books, summary=not target_course_arg, summary_only=getattr(args, "summary", False)))
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
            print(format_calendar_list(calendar))
        return

    # --- activity ---
    if subcmd == "activity" or getattr(args, "activity", False):
        from core import commands
        await commands.run_activity(args, lambda data: _emit_json(args, data))
        return

    # --- profile ---
    if subcmd in ("profile", "whoami") or getattr(args, "profile", False):
        from core import commands
        await commands.run_profile(args, lambda data: _emit_json(args, data))
        return

    # --- discussions ---
    if subcmd in ("discussions", "discuss") or getattr(args, "discussions", False):
        machine = bool(getattr(args, "raw", False) or getattr(args, "json", False) or getattr(args, "out", None))
        # Classmates' posts are private by default, and reading every reply costs ~1 request per response:
        # topics, prompts and due dates unless --full (or an explicit posts/participants flag) is given.
        glance = not (args.full or args.posts_only or args.participants_only or args.titles_only)
        kwargs = {
            "max_post_clicks": getattr(args, "max_posts", None),
            "max_participant_clicks": getattr(args, "max_parts", None),
            "posts_only": getattr(args, "posts_only", False),
            "participants_only": getattr(args, "participants_only", False),
            "titles_only": getattr(args, "titles_only", False) or glance,
        }
        raw_all_disc = await asyncio.to_thread(
            _run_discussions_sync,
            target_cids=target_cids,
            courses=courses,
            headless=headless,
            cdp=cdp,
            kwargs=kwargs,
            md=getattr(args, "md", False),
            titles_only=getattr(args, "titles_only", False) or glance,
        )
        if machine:
            _emit_json(args, raw_all_disc)
        else:
            from scrapers.discussions import format_discussions
            print(format_discussions(raw_all_disc))
            if glance:
                print("\nTopics only. Add --full to read posts and replies (includes classmates' names).", file=sys.stderr)
        if any(c.get("error") for c in raw_all_disc):
            raise SystemExit(1)
        return

    print("No subcommand or scraper action selected. Run 'bb --help' to see commands.", file=sys.stderr)


def main() -> None:
    if len(sys.argv) == 1:
        parser = _build_parser()
        parser.print_help(sys.stderr)
        sys.exit(1)

    args = _parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
