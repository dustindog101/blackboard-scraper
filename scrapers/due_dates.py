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


def merge_due_items(calendar_items, gradebooks, courses, window_filter="all", exclude_completed=False,
                    include_completed=False, now=None):
    """Reconcile sources within a course before applying the Window Filter."""
    now = now or datetime.now().astimezone()
    combined = []
    course_ids = {name: cid for cid, name in courses.items()}
    for cid, rows in gradebooks.items():
        for g in rows:
            title = g.get("name", "").strip()
            if not title:
                continue
            due = g.get("raw_due") or g.get("dueDate") or ""
            combined.append({**g, "title": title, "course_id": cid, "course": courses.get(cid, cid),
                             "due_date": g.get("dueDate") or due, "raw_due": due, "source": "gradebook",
                             "completed": bool(g.get("completed")),
                             "submission_status": g.get("submission_status", "NOT_ATTEMPTED")})
    for c in calendar_items:
        cid = c.get("course_id") or course_ids.get(c.get("course"))
        title = c.get("title", "").strip()
        candidates = [g for g in combined if g.get("course_id") == cid and cid is not None]
        match = next((g for g in candidates if
                      (c.get("column_id") and c["column_id"] == g.get("column_id")) or
                      (c.get("content_id") and c["content_id"] == g.get("content_id"))), None)
        if match is None:
            matches = [g for g in candidates if _normalize_title(g["title"]) == _normalize_title(title)]
            match = matches[0] if len(matches) == 1 else None
        if match is not None:
            match.update({"due_date": c.get("due_date") or c.get("due") or match["due_date"],
                          "raw_due": c.get("raw_due") or match["raw_due"], "source": "calendar+gradebook"})
        else:
            combined.append({**c, "title": title, "course_id": cid, "course": c.get("course", "General"),
                             "due_date": c.get("due_date") or c.get("due") or "",
                             "status": "Upcoming", "submission_status": None, "completed": False,
                             "source": "calendar", "grade": None})
    window = str(window_filter).lower().strip()
    overdue = window == "overdue"
    m = re.fullmatch(r"(\d+)d?", window)
    limit = int(m.group(1)) if m else None
    results = []
    seen = set()
    for item in combined:
        identity = (item.get("course_id") or item.get("course"),
                    item.get("column_id") or item.get("content_id") or _normalize_title(item["title"]))
        if identity in seen:
            continue
        seen.add(identity)
        dt = _parse_due_datetime(item.get("raw_due") or item.get("due_date", ""))
        item["tracked"] = dt is not None
        completed = item["completed"]
        if exclude_completed and completed:
            continue
        if overdue and (dt is None or dt >= now or (completed and not include_completed)):
            continue
        if limit is not None and (dt is None or not 0 <= (dt - now).total_seconds() <= limit * 86400):
            continue
        if not completed and item.get("status") not in ("Submission unverified", "Not attempted (0 posts)"):
            item["status"] = "Overdue" if dt and dt < now else item.get("status", "Upcoming")
        results.append(item)
    return results


async def aggregate_due_dates_async(page: Optional[Page] = None, courses: Optional[Dict[str, str]] = None,
                                    window_filter: str = "7d", exclude_completed: bool = False,
                                    include_completed: bool = False) -> List[Dict[str, Any]]:
    courses = courses or {}
    results = await asyncio.gather(scrape_calendar_async(page),
                                   *(scrape_grades_async(cid) for cid in courses), return_exceptions=True)
    calendar = results[0] if isinstance(results[0], list) else []
    grades = {cid: rows for cid, rows in zip(courses, results[1:]) if isinstance(rows, list)}
    return merge_due_items(calendar, grades, courses, window_filter, exclude_completed, include_completed)


def format_due_dates_table(items: List[Dict[str, Any]], window_filter: str = "7d") -> str:
    """Formats aggregated due dates into a clean CLI table."""
    lines = [
        f"📅 Upcoming Deadlines & Due Dates ({window_filter.upper()})",
        "━" * 80,
    ]
    if not items:
        lines.append("  (No upcoming deadlines found in this window)")
        return "\n".join(lines)

    lines.append(f"{'Course':<25} | {'Assignment':<34} | {'Due Date':<24} | {'Status'}")
    lines.append("-" * 25 + "-+-" + "-" * 34 + "-+-" + "-" * 24 + "-+-" + "-" * 10)
    for it in [i for i in items if i.get("tracked", True)]:
        c_raw = (it.get("course") or "Unknown").strip()
        if ": " in c_raw:
            c = c_raw.split(": ", 1)[1][:24]
        else:
            c = c_raw[:24]
        t = (it.get("title") or "Untitled")[:33]
        d = (it.get("due_date") or it.get("due") or "TBD")[:24]
        s = it.get("status") or "Upcoming"
        lines.append(f"{c:<25} | {t:<34} | {d:<24} | {s}")

    untracked = [i for i in items if not i.get("tracked", True)]
    if untracked:
        lines.append("\nNot tracked by Blackboard (no due date):")
        lines.extend(f"  • {i['course']}: {i['title']} [{i.get('status', 'Unknown')}]" for i in untracked)
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
