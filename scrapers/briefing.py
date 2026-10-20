"""`bb briefing`: overdue, urgent, upcoming, grades and announcements from the reconciled status model.

REST only on the happy path (no browser): gradebooks, calendar and announcements are fetched concurrently.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.config import load_courses
from core.gradebook import FORBIDDEN, GRADED, NOT_FOUND, NOT_SUBMITTED, UNKNOWN, course_label
from core.output import OUTPUT_BASE
from core.time import format_local, local_zone, parse_datetime
from scrapers.announcements import scrape_announcements_api
from scrapers.calendar import scrape_calendar_async
from scrapers.due_dates import public_item, reconcile, rows, select
from scrapers.grades import _num, fetch_gradebooks_async, save_grades, summary_lines

logger = logging.getLogger("blackboard.scrapers.briefing")

LOW_SCORE_PERCENT = 60
LOW_SCORE_DAYS = 21
UPCOMING_DAYS = 7
ANNOUNCEMENT_DAYS = 7
RECENT_GRADE_DAYS = 14


def _grade_string(item: Dict[str, Any]) -> str:
    if item["score"] is None or not item["possible"]:
        return ""
    return f"{_num(item['score'])} / {_num(item['possible'])}"


def _recent_announcements(raw: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    """Announcements from the last week, newest first. REST carries no read state, so none is claimed."""
    recent = []
    for a in raw:
        created = parse_datetime(a.get("created"))
        if created and now - created <= timedelta(days=ANNOUNCEMENT_DAYS):
            recent.append({"title": a.get("title"), "created": a.get("created"), "meta": a.get("meta"), "body": a.get("body", "")})
    recent.sort(key=lambda a: a["created"], reverse=True)
    return recent


def _course_block(cid: str, name: str, book: Dict[str, Any], announcements: Optional[List[Dict[str, Any]]], now: datetime) -> Dict[str, Any]:
    block: Dict[str, Any] = {"course_name": name, "course_label": course_label(name), "announcements": announcements or [],
                             "announcements_available": announcements is not None}
    if book.get("error"):
        block.update(grades=[], overall=None, error=book["error"])
        return block
    block["overall"] = book["overall"]
    block["grades"] = [
        {**{k: v for k, v in i.items() if not k.startswith("_")}, "grade": _grade_string(i)}
        for i in book["items"]
    ]
    return block


async def run_briefing_async(
    headless: bool = True,
    cdp_url: Optional[str] = None,
    write_markdown: bool = False,
    concurrency: int = 4,
) -> Dict[str, Any]:
    """Build the briefing bundle. Raises scrapers.grades.SessionExpired on a 401."""
    courses = load_courses()
    now = datetime.now(local_zone())

    books_task = fetch_gradebooks_async(courses)
    calendar_task = scrape_calendar_async(None)
    announcement_tasks = [asyncio.to_thread(scrape_announcements_api, cid) for cid in courses]
    books, calendar, *announcements = await asyncio.gather(books_task, calendar_task, *announcement_tasks, return_exceptions=True)
    if isinstance(books, BaseException):
        raise books
    calendar_items = calendar if isinstance(calendar, list) else []
    ann_by_course = {cid: (a if isinstance(a, list) else None) for cid, a in zip(courses, announcements)}

    entries = reconcile(books, calendar_items, courses)
    overdue = select(entries, "overdue", False, now)
    upcoming = [e for e in select(entries, f"{UPCOMING_DAYS}d", False, now) if not e["completed"]]
    tomorrow = now.date() + timedelta(days=1)
    urgent = [e for e in upcoming if e["_dt"].astimezone(local_zone()).date() <= tomorrow]
    soon = [e for e in upcoming if e not in urgent]

    per_course: Dict[str, Any] = {}
    notes: List[str] = []
    for cid, name in courses.items():
        announcements_here = ann_by_course.get(cid)
        block = _course_block(cid, name, books.get(cid, {"error": "unavailable"}), None if announcements_here is None else _recent_announcements(announcements_here, now), now)
        per_course[cid] = block
        if block.get("error") in (FORBIDDEN, NOT_FOUND):
            notes.append(f"{block['course_label']}: gradebook not visible to students")
        elif block.get("error"):
            notes.append(f"{block['course_label']}: grades unavailable")
        if announcements_here is None:
            notes.append(f"{block['course_label']}: announcements unavailable")

    def legacy(e: Dict[str, Any]) -> Dict[str, Any]:
        text = format_local(e["_dt"]) if e["_dt"] else "TBD"
        return {**public_item(e), "due_date": text, "due": text}

    low = []
    for cid, block in per_course.items():
        for g in block["grades"]:
            dt = parse_datetime(g["due_at"])
            if (g["status"] == GRADED and g["possible"] and g["percent"] is not None and g["percent"] < LOW_SCORE_PERCENT
                    and dt and now - timedelta(days=LOW_SCORE_DAYS) <= dt <= now):
                low.append({**g, "title": g["name"], "course": block["course_label"], "course_id": cid})
    low.sort(key=lambda g: g["due_at"], reverse=True)
    open_urgent = [e for e in urgent if e["status"] in (NOT_SUBMITTED, UNKNOWN)]

    bundle = {
        "briefing_path": OUTPUT_BASE / "briefing.md",
        "generated_at": now.isoformat(),
        "overdue": [legacy(e) for e in overdue],
        "attention": low + [legacy(e) for e in open_urgent],
        "urgent": [legacy(e) for e in urgent],
        "upcoming": [legacy(e) for e in soon],
        "calendar": [legacy(e) for e in urgent + soon],  # kept for Telegram / menubar
        "activity": [],
        "courses": per_course,
        "notes": notes,
    }
    if write_markdown:
        for cid, book in books.items():
            if not book.get("error"):
                save_grades(book, cid)
        path = bundle["briefing_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("```\n" + format_briefing_cli(bundle) + "\n```\n")
    return bundle


def run_briefing(headless: bool = True, cdp_url: Optional[str] = None, write_markdown: bool = False) -> Dict[str, Any]:
    return asyncio.run(run_briefing_async(headless=headless, cdp_url=cdp_url, write_markdown=write_markdown))


def format_briefing_cli(bundle: Dict[str, Any]) -> str:
    now = datetime.now(local_zone())
    lines = [f"Briefing · {format_local(now)}"]

    def section(title: str, items: List[Dict[str, Any]], full_date: bool):
        if items:
            lines.extend(["", f"{title} ({len(items)})"] + rows(items, full_date=full_date))

    section("NEEDS ATTENTION", bundle["attention"], True)
    section("OVERDUE", bundle["overdue"], True)
    section("DUE TODAY / TOMORROW", bundle["urgent"], True)
    section("LATER THIS WEEK", bundle["upcoming"], True)
    if not (bundle["overdue"] or bundle["urgent"] or bundle["upcoming"]):
        lines.extend(["", f"Nothing overdue or due in the next {UPCOMING_DAYS} days."])

    graded_blocks = [(b["course_label"], b) for b in bundle["courses"].values() if b.get("overall")]
    if graded_blocks:
        lines.extend(["", "GRADES"])
        lines.extend("  " + line for line in summary_lines([(label, b["overall"]) for label, b in graded_blocks]))
        recent = []
        cutoff = now - timedelta(days=RECENT_GRADE_DAYS)
        for b in bundle["courses"].values():
            for g in b["grades"]:
                dt = parse_datetime(g["due_at"])
                if g["status"] == GRADED and dt and cutoff <= dt <= now:
                    recent.append({**g, "title": g["name"], "course": b["course_label"], "_dt": dt})
        if recent:
            recent.sort(key=lambda g: g["_dt"], reverse=True)
            lines.append("  Recently graded:")
            lines.extend("  " + r for r in rows(recent[:5], full_date=True))

    anns = [(b["course_label"], a) for b in bundle["courses"].values() for a in b["announcements"]]
    if anns:
        anns.sort(key=lambda x: x[1]["created"], reverse=True)
        lines.extend(["", f"ANNOUNCEMENTS, last {ANNOUNCEMENT_DAYS} days ({len(anns)})"])
        width = max(len(label) for label, _ in anns)
        for label, a in anns[:8]:
            lines.append(f"  {format_local(parse_datetime(a['created']), with_zone=False):<18}  {label:<{width}}  {(a['title'] or '')[:60]}")
        if len(anns) > 8:
            lines.append(f"  +{len(anns) - 8} more: bb announcements")
    if bundle["notes"]:
        lines.extend(["", "partial: " + "; ".join(bundle["notes"])])
    return "\n".join(lines)
