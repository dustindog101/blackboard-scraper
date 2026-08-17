import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, List, Optional

from playwright.sync_api import sync_playwright

from core.config import BLACKBOARD_BASE, load_courses, save_courses
from core.export_json import build_export_doc, build_item, write_export
from core.session import _launch_context, _require_session, _require_session_async, check_session, check_session_async, login, login_auto
from core.async_engine import AsyncSessionManager, AsyncCourseWorkerPool, EngineConfig

# Scrapers
from scrapers.activity import save_activity, scrape_activity, scrape_activity_async
from scrapers.announcements import save_announcements, scrape_announcements, scrape_announcements_async
from scrapers.calendar import save_calendar, scrape_calendar, scrape_calendar_async
from scrapers.discussions import save_discussions, scrape_discussions
from scrapers.grades import save_grades, scrape_grades, scrape_grades_async
from scrapers.profile import save_profile, scrape_profile
from scrapers.briefing import run_briefing_async, run_briefing, format_briefing_cli
from scrapers.outline import scrape_course_outline_async, save_outline, format_outline_tree
from scrapers.assignments import scrape_course_assignments_async, save_assignments, format_assignments_summary
from scrapers.due_dates import aggregate_due_dates_async, save_due_dates, format_due_dates_table
from scrapers.search import find_items_async, grab_item_async


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _safe_relpath(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _emit_output(args: argparse.Namespace, items: list[dict]) -> None:
    """Print JSON to stdout or write to --out file."""
    pretty = not args.compact
    payload = build_export_doc(items, source=args.source, pretty=pretty)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload)
        print(f"💾 JSON written to: {_safe_relpath(out_path)}", file=sys.stderr)
    else:
        print(payload)


def _emit_raw(args: argparse.Namespace, data: Any) -> None:
    """Print raw scraper dicts (pre-transform) to stdout or --out file."""
    pretty = not args.compact
    if pretty:
        payload = json.dumps(data, indent=2, ensure_ascii=False)
    else:
        payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload)
        print(f"💾 Raw data written to: {_safe_relpath(out_path)}", file=sys.stderr)
    else:
        print(payload)


# ---------------------------------------------------------------------------
# Console print helpers
# ---------------------------------------------------------------------------

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
# Course name cleaning
# ---------------------------------------------------------------------------

_COURSE_CODE_RE = re.compile(r"\s*\([^)]*\)\s*(?:SP|FA|SU|WI)\d{4}\s*\([^)]*\)\s*$")


def _short_course_name(raw: str | None) -> str | None:
    """Strip trailing section/semester codes from Blackboard course names."""
    if not raw:
        return raw
    cleaned = _COURSE_CODE_RE.sub("", raw).strip()
    return cleaned or raw


# ---------------------------------------------------------------------------
# build_item wrappers
# ---------------------------------------------------------------------------

def _build_activity_items(activity: list[dict], default_group: str) -> list[dict]:
    out: list[dict] = []
    for entry in activity:
        raw_preview = (entry.get("message") or "").strip()
        date_str = (entry.get("date") or "").strip()
        context = (entry.get("context") or "").strip()
        course_name = _short_course_name(context)

        if raw_preview:
            title = raw_preview.split("\n")[0][:120]
        elif context:
            title = f"Activity: {course_name or context}"
        else:
            title = "Activity update"

        out.append(
            build_item(
                kind="activity",
                course_name=course_name,
                title=title,
                notes=None,
                due_text=entry.get("due_date") or None,
                source_ref=f"activity:{context}:{date_str}",
                group_name=default_group,
                metadata={"date": date_str, "raw_preview": raw_preview} if (date_str or raw_preview) else {},
            )
        )
    return out


def _build_calendar_items(calendar: list[dict], course_id: str | None, default_group: str) -> list[dict]:
    out: list[dict] = []
    for entry in calendar:
        course_name = _short_course_name(entry.get("course"))
        out.append(
            build_item(
                kind="calendar_due",
                course_id=course_id,
                course_name=course_name,
                title=entry.get("title") or "Calendar item",
                notes=None,
                due_text=entry.get("due"),
                source_ref=f"calendar:{course_id or 'global'}:{entry.get('title','')}:{entry.get('due','')}",
                group_name=default_group,
            )
        )
    return out


def _build_announcement_items(
    announcements: list[dict],
    course_id: str,
    course_name: str,
    default_group: str,
) -> list[dict]:
    out: list[dict] = []
    for ann in announcements:
        body = (ann.get("body") or "").strip()
        meta = (ann.get("meta") or "").strip()
        out.append(
            build_item(
                kind="announcement",
                course_id=course_id,
                course_name=_short_course_name(course_name),
                title=ann.get("title") or "Announcement",
                notes=body or None,
                source_ref=f"announcement:{course_id}:{ann.get('title','')}",
                group_name=default_group,
                priority=3 if ann.get("unread") else 2,
                is_starred=bool(ann.get("unread")),
                metadata={
                    "posted": meta,
                    "unread": str(bool(ann.get("unread"))).lower(),
                },
            )
        )
    return out


def _build_grade_items(
    grades: list[dict],
    course_id: str,
    course_name: str,
    default_group: str,
) -> list[dict]:
    out: list[dict] = []
    for item in grades:
        name = item.get("name") or "Grade item"
        due = item.get("dueDate") or ""
        status = item.get("status") or ""
        grade = item.get("grade") or ""
        notes_parts = []
        if status:
            notes_parts.append(f"Status: {status}")
        if grade:
            notes_parts.append(f"Grade: {grade}")
        out.append(
            build_item(
                kind="grade",
                course_id=course_id,
                course_name=_short_course_name(course_name),
                title=name,
                notes="\n".join(notes_parts) or None,
                due_text=due or None,
                source_ref=f"grade:{course_id}:{name}:{due}",
                group_name=default_group,
                metadata={"status": status, "grade": grade},
            )
        )
    return out


def _build_outline_items(
    outline: list[dict],
    course_id: str,
    course_name: str,
    default_group: str,
) -> list[dict]:
    out: list[dict] = []
    for item in outline:
        out.append(
            build_item(
                kind="content_item",
                course_id=course_id,
                course_name=_short_course_name(course_name),
                title=item.get("title") or "Content item",
                notes=item.get("description") or None,
                due_text=item.get("due_date") or None,
                source_ref=f"outline:{course_id}:{item.get('content_id','')}",
                group_name=default_group,
                metadata={
                    "content_type": item.get("content_type", "item"),
                    "depth": item.get("depth", 0),
                    "links": item.get("links", []),
                },
            )
        )
    return out


def _build_assignment_items(
    assignments: list[dict],
    course_id: str,
    course_name: str,
    default_group: str,
) -> list[dict]:
    out: list[dict] = []
    for item in assignments:
        out.append(
            build_item(
                kind="assignment",
                course_id=course_id,
                course_name=_short_course_name(course_name),
                title=item.get("title") or "Assignment",
                notes=item.get("instructions") or None,
                due_text=item.get("due_date") or None,
                source_ref=f"assignment:{course_id}:{item.get('title','')}",
                group_name=default_group,
                metadata={
                    "points_possible": item.get("points_possible", ""),
                    "submission_status": item.get("submission_status", "Unattempted"),
                    "attempts": item.get("attempts", ""),
                    "is_timed_test": item.get("is_timed_test", False),
                    "attachments": item.get("attachments", []),
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# CLI Argument Parser
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bb",
        description="UMBC Blackboard Ultra High-Performance Scraper & Assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 main.py --briefing                          # Print high-speed briefing to CLI
  python3 main.py --due 7d                            # Print upcoming deadlines table to CLI
  python3 main.py --outline --all                     # Print hierarchical course outlines to CLI
  python3 main.py --assignments --all                 # Print assignment details & rubrics to CLI
  python3 main.py --find "Project 1"                  # Search content across all courses
  python3 main.py --outline --all --md                # Output to CLI AND save Markdown to output/
  python3 main.py --grades --all --out grades.json    # Export grades as JSON to file
  python3 main.py --bot                               # Launch interactive Telegram bot daemon
        """,
    )

    raw_args = sys.argv[1:]
    if "-auto" in raw_args:
        parser.error("Use --auto (or -a). The token '-auto' is ambiguous.")

    # --- authentication ---
    auth = parser.add_argument_group("authentication")
    auth.add_argument("--login", action="store_true", help="Login via SSO (skips if session is valid)")
    auth.add_argument("--logout", action="store_true", help="Logout (clear session cookies, keep config credentials)")
    auth.add_argument("--auto", "-a", action="store_true", help="With --login: fully automated SSO + Duo text passcode login")
    auth.add_argument("--force", action="store_true", help="With --login: force re-login even if session exists")
    auth.add_argument("--username", "-u", help="Username for automated login (prompts if omitted)")
    auth.add_argument("--password", "-p", help="Password for automated login (prompts if omitted)")
    auth.add_argument("--config-creds", action="store_true", help="With --login: use credentials from config.json")
    auth.add_argument("--check-session", action="store_true", help="Test if the current session is valid")
    auth.add_argument("--session-info", action="store_true", help="Show session creation / last-used timestamps")
    auth.add_argument("--debug", action="store_true", help="Print detailed debug output (use with --check-session)")

    # --- discovery ---
    disc = parser.add_argument_group("discovery")
    disc.add_argument("--discover", action="store_true", help="Find and save enrolled courses")
    disc.add_argument("--courses", action="store_true", help="List configured courses")

    # --- scrapers ---
    scrapers = parser.add_argument_group("scrapers")
    scrapers.add_argument("--briefing", action="store_true", help="Run high-speed concurrent briefing across all courses")
    scrapers.add_argument("--activity", action="store_true", help="Scrape homepage activity stream")
    scrapers.add_argument("--calendar", action="store_true", help="Scrape calendar due-dates")
    scrapers.add_argument("--announcements", action="store_true", help="Scrape course announcements")
    scrapers.add_argument("--grades", action="store_true", help="Scrape gradebook")
    scrapers.add_argument("--discussions", action="store_true", help="Scrape course discussions")
    scrapers.add_argument("--outline", action="store_true", help="Scrape full course outline and modules")
    scrapers.add_argument("--assignments", action="store_true", help="Deep scrape assignments with prompts and rubrics")
    scrapers.add_argument("--due", nargs="?", const="7d", default=None, metavar="WINDOW", help="Aggregate cross-course due dates (e.g. 7d, 14d, overdue)")
    scrapers.add_argument("--upcoming", type=int, metavar="DAYS", help="Alias for --due <N>d")
    scrapers.add_argument("--exclude-completed", action="store_true", help="With --due: exclude submitted/graded items")
    scrapers.add_argument("--find", metavar="QUERY", help="Search for content/assignments matching query")
    scrapers.add_argument("--grab", metavar="ITEM_ID", help="Grab and download specific content item")
    scrapers.add_argument("--profile", action="store_true", help="Show your Blackboard profile")

    # --- item filtering & selection ---
    filt = parser.add_argument_group("filtering & selection")
    filt.add_argument("--type", help="Filter items by type (e.g. syllabus, document, assignment, folder, link)")
    filt.add_argument("--filter", dest="keyword_filter", help="Filter items by text keyword")

    # --- scope & performance ---
    scope = parser.add_argument_group("scope & performance")
    scope.add_argument("--course", "-c", help="Target course ID (e.g. _100001_1)")
    scope.add_argument("--all", action="store_true", help="Run against all configured courses")
    scope.add_argument("--concurrency", type=int, default=4, metavar="N", help="Max concurrent browser workers (default: 4)")
    scope.add_argument("--visible", "-v", action="store_true", help="Show browser window (useful for debugging)")
    scope.add_argument("--cdp", help="Connect to an existing browser via CDP URL (e.g. http://localhost:9222)")

    # --- telegram integration ---
    tg = parser.add_argument_group("telegram integration")
    tg.add_argument("--telegram", action="store_true", help="Send briefing/results to configured Telegram chat")
    tg.add_argument("--bot", action="store_true", help="Start the interactive Telegram bot daemon")

    # --- output formats & file saving ---
    output = parser.add_argument_group("output")
    output.add_argument("--json", action="store_true", help="Output JSON envelope to CLI stdout")
    output.add_argument("--out", metavar="FILE", help="Save JSON output to FILE instead of printing")
    output.add_argument("--md", "--save", dest="md", action="store_true", help="Save formatted markdown file(s) to output/ directory")
    output.add_argument("--raw", action="store_true", help="Output raw scraper data structures")
    output.add_argument("--compact", action="store_true", help="Emit minified JSON (when outputting JSON)")
    output.add_argument("--source", default="blackboard-scraper", metavar="NAME", help="Value for the JSON source field")
    output.add_argument("--group", default="School", metavar="NAME", help="Group name for exported items")

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Discovery Subcommand
# ---------------------------------------------------------------------------

def _handle_discover_courses(headless: bool, cdp: str | None) -> None:
    if not _require_session(cdp):
        return
    print("🔍 Discovering enrolled courses...", file=sys.stderr)
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
            print(f"   ✅ Found {len(discovered)} courses:", file=sys.stderr)
            for cid, cname in discovered.items():
                print(f"      {cid}: {cname}", file=sys.stderr)
            save_courses(discovered)
            print("   💾 Saved to config.json", file=sys.stderr)
        else:
            print("   ❌ No courses found.", file=sys.stderr)
        ctx.close()


# ---------------------------------------------------------------------------
# Main Async Execution Dispatcher
# ---------------------------------------------------------------------------

async def main_async(args: argparse.Namespace) -> None:
    headless = not args.visible
    cdp = args.cdp
    courses = load_courses()
    os.environ["TMPDIR"] = "/tmp"

    # --- telegram bot daemon ---
    if args.bot:
        from telegram.bot import SimpleTelegramBot
        bot = SimpleTelegramBot()
        await bot.start_polling()
        return

    # --- course listing & auth ---
    if args.courses:
        print("\n📚 Configured Courses:", file=sys.stderr)
        for cid, name in courses.items():
            print(f"  {cid}: {name}", file=sys.stderr)
        print("", file=sys.stderr)
        return

    if args.login:
        if args.auto:
            login_auto(username=args.username, password=args.password, headless=headless, cdp_url=cdp)
        else:
            login(args.force, args.username, args.password, cdp)
        return

    if args.logout:
        from core.session import logout as do_logout
        do_logout(keep_config_creds=True)
        return

    if args.check_session:
        ok = await check_session_async(debug=args.debug, headless=headless)
        if not ok and not args.visible:
            visible_ok = await check_session_async(debug=args.debug, headless=False)
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

    if args.discover:
        _handle_discover_courses(headless, cdp)
        return

    if not await _require_session_async(cdp):
        return

    json_items: list[dict] = []

    # --- high-speed concurrent briefing ---
    if args.briefing:
        bundle = await run_briefing_async(
            headless=headless,
            cdp_url=cdp,
            write_markdown=args.md,
            concurrency=args.concurrency,
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
            _emit_raw(args, bundle)
            return

        if args.json or args.out:
            json_items.extend(_build_activity_items(bundle.get("activity", []), args.group))
            json_items.extend(_build_calendar_items(bundle.get("calendar", []), None, args.group))
            for course_id, course_data in bundle.get("courses", {}).items():
                if isinstance(course_data, dict):
                    course_name = course_data.get("course_name", courses.get(course_id, course_id))
                    json_items.extend(
                        _build_announcement_items(course_data.get("announcements", []), course_id, course_name, args.group)
                    )
                    json_items.extend(_build_grade_items(course_data.get("grades", []), course_id, course_name, args.group))
            _emit_output(args, json_items)
        else:
            # Default to clean CLI stdout digest
            print(format_briefing_cli(bundle))
        return

    # --- due dates aggregator ---
    window = f"{args.upcoming}d" if args.upcoming else (args.due if args.due is not None else None)
    if window is not None:
        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp))
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

        if args.raw:
            _emit_raw(args, items)
            return

        if args.json or args.out:
            json_items.extend(_build_calendar_items(items, None, args.group))
            _emit_output(args, json_items)
        else:
            # Default to CLI table
            print(format_due_dates_table(items, window_filter=window))
        return

    # --- course outline scraper ---
    if args.outline:
        target_courses = list(courses.keys()) if args.all else ([args.course] if args.course else [])
        if not target_courses:
            print("❌ Specify --course <ID> or --all", file=sys.stderr)
            return

        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp, max_concurrency=args.concurrency))
        await session_manager.initialize()
        raw_all: dict[str, list[dict]] = {}
        try:
            pool = AsyncCourseWorkerPool(session_manager)
            async def _worker(cid, cname, page):
                data = await scrape_course_outline_async(cid, page)
                if args.type:
                    data = [item for item in data if item.get("content_type", "").lower() == args.type.lower()]
                if args.keyword_filter:
                    kw = args.keyword_filter.lower()
                    data = [item for item in data if kw in item.get("title", "").lower() or kw in item.get("description", "").lower()]
                if args.md:
                    save_outline(data, cid)
                return data

            target_dict = {cid: courses.get(cid, cid) for cid in target_courses}
            raw_all = await pool.execute_task_per_course(target_dict, _worker)
        finally:
            await session_manager.close()

        if args.raw:
            _emit_raw(args, raw_all)
            return

        if args.json or args.out:
            for cid, data in raw_all.items():
                if isinstance(data, list):
                    json_items.extend(_build_outline_items(data, cid, courses.get(cid, cid), args.group))
            _emit_output(args, json_items)
        else:
            # Default to CLI outline tree
            for cid, data in raw_all.items():
                cname = courses.get(cid, cid)
                if isinstance(data, list):
                    print(format_outline_tree(data, cname, cid))
                    print("")
        return

    # --- deep assignments scraper ---
    if args.assignments:
        target_courses = list(courses.keys()) if args.all else ([args.course] if args.course else [])
        if not target_courses:
            print("❌ Specify --course <ID> or --all", file=sys.stderr)
            return

        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp, max_concurrency=args.concurrency))
        await session_manager.initialize()
        raw_all_assign: dict[str, list[dict]] = {}
        try:
            pool = AsyncCourseWorkerPool(session_manager)
            async def _worker(cid, cname, page):
                data = await scrape_course_assignments_async(cid, page)
                if args.keyword_filter:
                    kw = args.keyword_filter.lower()
                    data = [item for item in data if kw in item.get("title", "").lower() or kw in item.get("instructions", "").lower()]
                if args.md:
                    save_assignments(data, cid)
                return data

            target_dict = {cid: courses.get(cid, cid) for cid in target_courses}
            raw_all_assign = await pool.execute_task_per_course(target_dict, _worker)
        finally:
            await session_manager.close()

        if args.raw:
            _emit_raw(args, raw_all_assign)
            return

        if args.json or args.out:
            for cid, data in raw_all_assign.items():
                if isinstance(data, list):
                    json_items.extend(_build_assignment_items(data, cid, courses.get(cid, cid), args.group))
            _emit_output(args, json_items)
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
        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp))
        await session_manager.initialize()
        try:
            async with session_manager.acquire_page() as page:
                matches = await find_items_async(args.find, courses, page)
        finally:
            await session_manager.close()

        print(f"\n🔎 Search Results for '{args.find}':")
        if not matches:
            print("  (No matching items found across courses)")
        for m in matches:
            due_str = f" (Due: {m['due_date']})" if m.get("due_date") else ""
            print(f"• [{m['course_name']}] {m['title']} [{m['content_type']}]{due_str}")
        return

    # --- item grabber ---
    if args.grab:
        target_cid = args.course or (list(courses.keys())[0] if courses else None)
        if not target_cid:
            print("❌ Specify --course <ID> to grab item from.", file=sys.stderr)
            return
        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp))
        await session_manager.initialize()
        try:
            async with session_manager.acquire_page() as page:
                item = await grab_item_async(args.grab, target_cid, page)
        finally:
            await session_manager.close()

        _emit_raw(args, item)
        return

    # --- announcements ---
    if args.announcements:
        target_courses = list(courses.keys()) if args.all else ([args.course] if args.course else [])
        if not target_courses:
            print("❌ Specify --course <ID> or --all", file=sys.stderr)
            return

        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp, max_concurrency=args.concurrency))
        await session_manager.initialize()
        raw_ann: dict[str, list[dict]] = {}
        try:
            pool = AsyncCourseWorkerPool(session_manager)
            async def _worker(cid, cname, page):
                data = await scrape_announcements_async(cid, page)
                if args.md:
                    save_announcements(data, cid)
                return data

            target_dict = {cid: courses.get(cid, cid) for cid in target_courses}
            raw_ann = await pool.execute_task_per_course(target_dict, _worker)
        finally:
            await session_manager.close()

        if args.raw:
            _emit_raw(args, raw_ann)
            return

        if args.json or args.out:
            for cid, data in raw_ann.items():
                if isinstance(data, list):
                    json_items.extend(
                        _build_announcement_items(data, cid, courses.get(cid, cid), args.group)
                    )
            _emit_output(args, json_items)
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
        target_courses = list(courses.keys()) if args.all else ([args.course] if args.course else [])
        if not target_courses:
            print("❌ Specify --course <ID> or --all", file=sys.stderr)
            return

        session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp, max_concurrency=args.concurrency))
        await session_manager.initialize()
        raw_gr: dict[str, list[dict]] = {}
        try:
            pool = AsyncCourseWorkerPool(session_manager)
            async def _worker(cid, cname, page):
                data = await scrape_grades_async(cid, page)
                if args.md:
                    save_grades(data, cid)
                return data

            target_dict = {cid: courses.get(cid, cid) for cid in target_courses}
            raw_gr = await pool.execute_task_per_course(target_dict, _worker)
        finally:
            await session_manager.close()

        if args.raw:
            _emit_raw(args, raw_gr)
            return

        if args.json or args.out:
            for cid, data in raw_gr.items():
                if isinstance(data, list):
                    json_items.extend(_build_grade_items(data, cid, courses.get(cid, cid), args.group))
            _emit_output(args, json_items)
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
                calendar = await scrape_calendar_async(page, args.course)
                if args.md:
                    save_calendar(calendar, args.course)
        finally:
            await session_manager.close()

        if args.raw:
            _emit_raw(args, calendar)
            return
        if args.json or args.out:
            json_items.extend(_build_calendar_items(calendar, args.course, args.group))
            _emit_output(args, json_items)
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

        if args.raw:
            _emit_raw(args, activity)
            return
        if args.json or args.out:
            json_items.extend(_build_activity_items(activity, args.group))
            _emit_output(args, json_items)
        else:
            print("\n🌊 Activity Stream:")
            print("━" * 50)
            if not activity:
                print("  (No recent activity found)")
            for item in activity:
                print(f"• {item['title']} ({item.get('course','')}) — {item.get('date','')}")
                if item.get("message"):
                    print(f"  > {item['message'][:140]}")
        return

    # --- profile ---
    if args.profile:
        with sync_playwright() as p:
            ctx, page = _launch_context(p, headless, cdp)
            data = scrape_profile(page)
            if data:
                _print_profile(data)
                if args.md:
                    save_profile(data)
            ctx.close()
        return

    # --- discussions ---
    if args.discussions:
        kwargs = {
            "max_post_clicks": getattr(args, "max_posts", None),
            "max_participant_clicks": getattr(args, "max_parts", None),
            "posts_only": getattr(args, "posts_only", False),
            "participants_only": getattr(args, "participants_only", False),
            "titles_only": getattr(args, "titles_only", False),
        }
        target_courses = list(courses.keys()) if args.all else ([args.course] if args.course else [])
        if not target_courses:
            print("❌ Specify --course <ID> or --all", file=sys.stderr)
            return
        raw_all_disc: list[dict] = []
        with sync_playwright() as p:
            ctx, _ = _launch_context(p, headless, cdp)
            for course_id in target_courses:
                page = ctx.new_page()
                data = scrape_discussions(course_id, page, **kwargs)
                if args.md:
                    save_discussions(data, course_id, titles_only=getattr(args, "titles_only", False))
                if args.raw:
                    raw_all_disc.extend(data)
                else:
                    json_items.extend(
                        _build_discussion_items(data, course_id, courses.get(course_id, course_id), args.group)
                    )
                page.close()
            ctx.close()
        if args.raw:
            _emit_raw(args, raw_all_disc)
            return
        _emit_output(args, json_items)
        return

    print("No scraper action selected. Run with --help.", file=sys.stderr)


def main() -> None:
    args = _parse_args()
    if len(sys.argv) == 1:
        print("Run with --help to see available commands.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
