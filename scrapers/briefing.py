import asyncio
import logging
from datetime import datetime
from scrapers.due_dates import merge_due_items, _parse_due_datetime
from typing import Any, Dict, Optional

from core.config import load_courses
from core.gradebook import newly_graded
from core.output import OUTPUT_BASE
from core.async_engine import AsyncSessionManager, AsyncCourseWorkerPool, EngineConfig
from scrapers.activity import scrape_activity_async, save_activity
from scrapers.calendar import scrape_calendar_async, save_calendar
from scrapers.announcements import scrape_announcements_async, save_announcements
from scrapers.grades import scrape_grades_async, save_grades

logger = logging.getLogger("blackboard.scrapers.briefing")


SECTION_TITLES = {
    "overdue": "Overdue", "due_soon": "Due within 48 h", "this_week": "This week",
    "awaiting_grade": "Submitted, awaiting grade", "newly_graded": "Newly graded",
    "unread_announcements": "Unread announcements", "untracked": "Not tracked by Blackboard",
}


def build_briefing_sections(bundle, now=None):
    """One status builder for CLI, Telegram and menubar; activity is never urgent."""
    now = now or datetime.now().astimezone()
    courses = {cid: data.get("course_name", cid) for cid, data in bundle.get("courses", {}).items()
               if isinstance(data, dict)}
    gradebooks = {cid: data.get("grades", []) for cid, data in bundle.get("courses", {}).items()
                 if isinstance(data, dict)}
    items = merge_due_items(bundle.get("calendar", []), gradebooks, courses, now=now)
    sections = {key: [] for key in SECTION_TITLES}
    for item in items:
        if not item["tracked"]:
            sections["untracked"].append(item)
            continue
        if item.get("completed"):
            if item.get("submission_status") == "NEEDS_GRADING":
                sections["awaiting_grade"].append(item)
            if newly_graded(item):
                sections["newly_graded"].append(item)
            continue
        due = _parse_due_datetime(item.get("raw_due") or item.get("due_date"))
        seconds = (due - now).total_seconds()
        key = "overdue" if seconds < 0 else "due_soon" if seconds <= 48 * 3600 else \
            "this_week" if seconds <= 7 * 86400 else None
        if key:
            sections[key].append(item)
    for cid, data in bundle.get("courses", {}).items():
        if isinstance(data, dict):
            sections["unread_announcements"].extend(
                {**a, "course_id": cid, "course": courses[cid]} for a in data.get("announcements", [])
                if a.get("unread"))
    for key in ("overdue", "due_soon", "this_week"):
        sections[key].sort(key=lambda i: (i.get("submission_status") != "IN_PROGRESS", i.get("raw_due", "")))
    return sections


def briefing_rows(bundle):
    sections = build_briefing_sections(bundle)
    for key, heading in SECTION_TITLES.items():
        if sections[key]:
            yield heading, sections[key]


def briefing_item_text(item):
    text = f"{item.get('title') or item.get('name', 'Untitled')} ({item.get('course', '')})"
    if newly_graded(item):
        text += f" — {item.get('grade', '')}"
    elif item.get("due_date"):
        text += f" — Due: {item['due_date']}"
    if item.get("status"):
        text += f" [{item['status']}]"
    return text


def briefing_icon(bundle):
    sections = build_briefing_sections(bundle)
    return "🔴" if sections["overdue"] else "🟡" if sections["due_soon"] else "🟢"


def format_briefing_cli(bundle: Dict[str, Any]) -> str:
    lines = ["📋 Blackboard Daily Briefing", "━" * 60]
    for heading, items in briefing_rows(bundle):
        lines.extend(["", heading + ":"])
        lines.extend("  • " + briefing_item_text(item) for item in items)
    if len(lines) == 2:
        lines.append("No actionable updates.")
    return "\n".join(lines)


async def run_briefing_async(
    headless: bool = True,
    cdp_url: Optional[str] = None,
    write_markdown: bool = False,
    concurrency: int = 4,
) -> Dict[str, Any]:
    """
    High-speed concurrent daily briefing orchestrator.
    Runs global activity + calendar in parallel, and scrapes all courses concurrently via Async Worker Pool.
    """
    courses = load_courses()
    per_course_data: Dict[str, Any] = {}

    engine_config = EngineConfig(headless=headless, cdp_url=cdp_url, max_concurrency=concurrency)
    session_manager = AsyncSessionManager(engine_config)
    await session_manager.initialize()

    try:
        # Step 1: Global scrapers (Activity & Calendar) concurrently
        async def _get_activity():
            async with session_manager.acquire_page() as p:
                return await scrape_activity_async(p)

        async def _get_calendar():
            async with session_manager.acquire_page() as p:
                return await scrape_calendar_async(p)

        activity_res, calendar_res = await asyncio.gather(
            _get_activity(),
            _get_calendar(),
            return_exceptions=False,
        )

        activity = activity_res if isinstance(activity_res, list) else []
        calendar = calendar_res if isinstance(calendar_res, list) else []

        if write_markdown:
            save_activity(activity)
            save_calendar(calendar)

        # Step 2: Course Workers - scrape announcements & grades concurrently
        worker_pool = AsyncCourseWorkerPool(session_manager)

        async def _scrape_course(cid: str, cname: str, page: Any) -> Dict[str, Any]:
            ann_data = await scrape_announcements_async(cid, page)
            grade_data = await scrape_grades_async(cid, page)

            if write_markdown:
                save_announcements(ann_data, cid)
                save_grades(grade_data, cid)

            return {
                "course_name": cname,
                "announcements": ann_data,
                "grades": grade_data,
            }

        per_course_data = await worker_pool.execute_task_per_course(courses, _scrape_course)


    finally:
        await session_manager.close()

    filepath = OUTPUT_BASE / "briefing.md"
    bundle = {"briefing_path": filepath, "activity": activity, "calendar": calendar, "courses": per_course_data}
    bundle.update(build_briefing_sections(bundle))
    if write_markdown:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(format_briefing_cli(bundle))
    return bundle


def run_briefing(headless: bool = True, cdp_url: str = None, write_markdown: bool = False) -> Dict[str, Any]:
    """Synchronous entrypoint wrapper."""
    return asyncio.run(run_briefing_async(headless=headless, cdp_url=cdp_url, write_markdown=write_markdown))
