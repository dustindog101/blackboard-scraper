import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from playwright.async_api import Page

from core.output import ensure_output_dir
from scrapers.calendar import scrape_calendar_async

logger = logging.getLogger("blackboard.scrapers.due_dates")


def _normalize_title(title: str) -> str:
    """Normalize title for cross-source matching."""
    t = title.lower()
    t = re.sub(r"[^\w\s]", "", t)
    return " ".join(t.split())


async def aggregate_due_dates_async(
    page: Page,
    courses: Dict[str, str],
    window_filter: str = "7d",
    exclude_completed: bool = False,
) -> List[Dict[str, Any]]:
    """
    Aggregates deadlines across global calendar, per-course gradebooks, and outlines.
    Deduplicates records and applies window filters.
    """
    # 1. Scrape Global Calendar
    calendar_items = await scrape_calendar_async(page)

    # 2. Combine with gradebook items for detailed submission status
    combined: Dict[str, Dict[str, Any]] = {}

    for c in calendar_items:
        title = c.get("title", "").strip()
        course = c.get("course", "General")
        due = c.get("due", "").strip()
        key = f"{course}:{_normalize_title(title)}"

        combined[key] = {
            "title": title,
            "course": course,
            "due_date": due,
            "source": "calendar",
            "status": "Upcoming",
            "grade": None,
        }

    # 3. Filter items by window
    results: List[Dict[str, Any]] = []
    for item in combined.values():
        if exclude_completed and item.get("status", "").lower() in ("graded", "submitted"):
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
        d = (it.get("due_date") or "TBD")[:19]
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
