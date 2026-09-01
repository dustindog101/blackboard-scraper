import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from playwright.async_api import Page

from core.output import ensure_output_dir
from scrapers.calendar import scrape_calendar_async
from scrapers.grades import scrape_grades_async

logger = logging.getLogger("blackboard.scrapers.due_dates")


def _normalize_title(title: str) -> str:
    """Normalize title for cross-source matching."""
    t = title.lower()
    t = re.sub(r"[^\w\s]", "", t)
    return " ".join(t.split())


def _parse_due_datetime(due_str: str) -> Optional[datetime]:
    """Parse date string or ISO timestamp into datetime object."""
    if not due_str or due_str.strip().upper() in ("TBD", "UNKNOWN DATE"):
        return None
    try:
        return datetime.fromisoformat(due_str.replace("Z", "+00:00")).astimezone()
    except Exception:
        pass
    cleaned = re.sub(r"\s*\([A-Z0-9_-]+\)\s*$", "", due_str).strip()
    for fmt in [
        "%-m/%-d/%y, %-I:%M %p",
        "%m/%d/%y, %I:%M %p",
        "%-m/%-d/%Y, %-I:%M %p",
        "%m/%d/%Y, %I:%M %p",
        "%-m/%-d/%y",
        "%m/%d/%y",
        "%Y-%m-%d",
    ]:
        try:
            return datetime.strptime(cleaned, fmt).astimezone()
        except Exception:
            continue
    return None


async def aggregate_due_dates_async(
    page: Optional[Page] = None,
    courses: Optional[Dict[str, str]] = None,
    window_filter: str = "7d",
    exclude_completed: bool = False,
) -> List[Dict[str, Any]]:
    """
    Aggregates deadlines across global calendar and course gradebooks.
    Deduplicates records and applies window filters (e.g. 7d, 14d, 30d, overdue, all).
    """
    # 1. Scrape Global Calendar (HTTP fast-path with Playwright fallback)
    calendar_task = scrape_calendar_async(page)

    # 2. Concurrently fetch gradebook items from open courses
    gradebook_tasks = []
    course_list = list(courses.items()) if courses else []
    for cid, cname in course_list:
        gradebook_tasks.append(scrape_grades_async(cid))

    results_tuple = await asyncio.gather(calendar_task, *gradebook_tasks, return_exceptions=True)
    calendar_items = results_tuple[0] if isinstance(results_tuple[0], list) else []

    combined: Dict[str, Dict[str, Any]] = {}

    for c in calendar_items:
        title = c.get("title", "").strip()
        course = c.get("course", "General")
        due = c.get("due_date") or c.get("due") or ""
        key = f"{_normalize_title(title)}"

        combined[key] = {
            "title": title,
            "course": course,
            "due_date": due,
            "raw_due": c.get("raw_due", ""),
            "source": "calendar",
            "status": "Upcoming",
            "grade": None,
        }

    # Add gradebook items
    for idx, (cid, cname) in enumerate(course_list):
        grade_res = results_tuple[idx + 1]
        if isinstance(grade_res, list):
            for g in grade_res:
                due = g.get("dueDate") or ""
                if not due or due.strip() == "--":
                    continue
                title = g.get("name", "").strip()
                if not title or title.lower() in ("overall grade", "total"):
                    continue
                key = f"{_normalize_title(title)}"
                if key not in combined:
                    combined[key] = {
                        "title": title,
                        "course": cname,
                        "due_date": due,
                        "raw_due": due,
                        "source": "gradebook",
                        "status": g.get("status", "Upcoming"),
                        "grade": g.get("grade"),
                    }
                else:
                    if g.get("status") and g.get("status") != "Unopened":
                        combined[key]["status"] = g.get("status")
                    if g.get("grade") and g.get("grade") != "--":
                        combined[key]["grade"] = g.get("grade")

    # 3. Filter items by window (e.g. 7d, 14d, overdue, all)
    now = datetime.now().astimezone()
    window_lower = str(window_filter or "all").lower().strip()

    days_limit: Optional[float] = None
    is_overdue_only = "overdue" in window_lower

    if not is_overdue_only and window_lower not in ("all", "calendar", "global"):
        m = re.match(r"^(\d+)\s*d?$", window_lower)
        if m:
            days_limit = float(m.group(1))

    results: List[Dict[str, Any]] = []
    for item in combined.values():
        if exclude_completed and item.get("status", "").lower() in ("graded", "submitted", "completed"):
            continue

        due_text = item.get("raw_due") or item.get("due_date", "")
        dt = _parse_due_datetime(due_text)

        if dt is not None:
            diff_days = (dt - now).total_seconds() / 86400
            if is_overdue_only:
                if diff_days >= 0:
                    continue
            elif days_limit is not None:
                # Include upcoming items within the limit (diff_days between -0.1 and days_limit + 0.99)
                if diff_days < -0.5 or diff_days > (days_limit + 0.99):
                    continue

        results.append(item)

    return results


def format_due_dates_table(items: List[Dict[str, Any]], window_filter: str = "7d") -> str:
    """Formats aggregated due dates into a clean CLI table."""
    lines = [
        f"📅 Upcoming Deadlines & Due Dates ({window_filter.upper()})",
        "━" * 60,
    ]
    if not items:
        lines.append("  (No upcoming deadlines found in this window)")
        return "\n".join(lines)

    lines.append(f"{'Course':<25} | {'Assignment':<35} | {'Due Date':<20} | {'Status'}")
    lines.append("-" * 25 + "-+-" + "-" * 35 + "-+-" + "-" * 20 + "-+-" + "-" * 10)
    for it in items:
        c = (it.get("course") or "Unknown")[:24]
        t = (it.get("title") or "Untitled")[:34]
        d = (it.get("due_date") or it.get("due") or "TBD")[:19]
        s = it.get("status") or "Upcoming"
        lines.append(f"{c:<25} | {t:<35} | {d:<20} | {s}")

    return "\n".join(lines)


def save_due_dates(items: List[Dict[str, Any]], window_filter: str = "7d") -> Path:
    """Saves due dates report to output/calendar/due_dates.md."""
    out_dir = ensure_output_dir("calendar")
    filepath = out_dir / "due_dates.md"

    lines = [
        f"# Upcoming Due Dates & Deadlines ({window_filter.upper()})",
        f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "", "---", ""
    ]

    if not items:
        lines.append("_No upcoming deadlines found in this window._")
    else:
        lines.append("| Course | Assignment | Due Date | Status |")
        lines.append("|---|---|---|---|")
        for item in items:
            c = item.get("course", "Unknown").replace("|", "-")
            t = item.get("title", "Untitled").replace("|", "-")
            d = item.get("due_date", "TBD").replace("|", "-")
            s = item.get("status", "Upcoming").replace("|", "-")
            lines.append(f"| **{c}** | {t} | `{d}` | {s} |")

    filepath.write_text("\n".join(lines))
    return filepath
