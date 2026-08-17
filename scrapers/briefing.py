import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import load_courses
from core.output import OUTPUT_BASE
from core.async_engine import AsyncSessionManager, AsyncCourseWorkerPool, EngineConfig
from scrapers.activity import scrape_activity_async, save_activity
from scrapers.calendar import scrape_calendar_async, save_calendar
from scrapers.announcements import scrape_announcements_async, save_announcements
from scrapers.grades import scrape_grades_async, save_grades

logger = logging.getLogger("blackboard.scrapers.briefing")


async def run_briefing_async(
    headless: bool = True,
    cdp_url: Optional[str] = None,
    write_markdown: bool = True,
    concurrency: int = 4,
) -> Dict[str, Any]:
    """
    High-speed concurrent daily briefing orchestrator.
    Runs global activity + calendar in parallel, and scrapes all courses concurrently via Async Worker Pool.
    """
    courses = load_courses()
    now = datetime.now()
    lines = [
        f"# Blackboard Daily Briefing",
        f"_Generated: {now.strftime('%Y-%m-%d %H:%M')}_",
        "",
        "---",
        "",
    ]
    per_course_data: Dict[str, Any] = {}

    engine_config = EngineConfig(headless=headless, cdp_url=cdp_url, max_concurrency=concurrency)
    session_manager = AsyncSessionManager(engine_config)
    await session_manager.initialize()

    try:
        # Step 1: Global scrapers (Activity & Calendar) concurrently
        print("\n🌊 Scraping Global Streams (Activity + Calendar)...")

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
        print(f"\n🚀 Launching Concurrent Course Workers across {len(courses)} courses (Concurrency={concurrency})...")
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

        # Step 3: Build Consolidated Briefing Document
        urgent = [a for a in activity if "due" in a.get("title", "").lower() or a.get("due_date")]
        if urgent:
            lines.append("## 🚨 Urgent & Overdue")
            for a in urgent:
                lines.append(f"- **{a['title']}** ({a['course']}) — _Due: {a.get('due_date', 'Today')}_")
            lines.append("")

        if calendar:
            lines.append("## 📅 Upcoming Assignments (Global Calendar)")
            for item in calendar[:10]:
                lines.append(f"- **{item['title']}** ({item['course']}) — _Due: {item['due']}_")
            lines.append("")

        lines.append("## 📚 Course Updates")
        for course_id, course_data in per_course_data.items():
            if not isinstance(course_data, dict):
                continue
            course_name = course_data.get("course_name", courses.get(course_id, course_id))
            lines.append(f"### {course_name}")

            announcements = course_data.get("announcements", [])
            unread_ann = [a for a in announcements if a.get("unread")]
            if unread_ann:
                lines.append(f"#### 📢 Announcements ({len(unread_ann)} unread)")
                for a in unread_ann:
                    lines.append(f"- **{a['title']}** _{a['meta']}_")
                    snippet = a["body"].split("\n")[0][:140]
                    if snippet:
                        if len(a["body"]) > 140:
                            snippet += "..."
                        lines.append(f"> {snippet}")
                    lines.append("")

            grades = course_data.get("grades", [])
            graded = [g for g in grades if g.get("grade") and g["grade"] not in ("Not graded", "-- %", "")]
            if graded:
                lines.append("#### 📊 Recent Grades")
                lines.append("| Assignment | Due | Grade |")
                lines.append("|---|---|---|")
                for g in graded:
                    lines.append(f"| {g['name']} | {g.get('dueDate','')} | {g['grade']} |")
                lines.append("")

            lines.append("---\n")

    finally:
        await session_manager.close()

    filepath = OUTPUT_BASE / "briefing.md"
    if write_markdown:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text("\n".join(lines))
        print(f"\n✅ Briefing written to: {filepath.name}")

    return {
        "briefing_path": filepath,
        "activity": activity,
        "calendar": calendar,
        "courses": per_course_data,
    }


def run_briefing(headless: bool = True, cdp_url: str = None, write_markdown: bool = True) -> Dict[str, Any]:
    """Synchronous entrypoint wrapper."""
    return asyncio.run(run_briefing_async(headless=headless, cdp_url=cdp_url, write_markdown=write_markdown))
