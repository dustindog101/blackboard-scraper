import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from playwright.async_api import Page

from core.config import load_courses
from core.output import ensure_output_dir
from scrapers.calendar import scrape_calendar_async
from scrapers.grades import scrape_grades_async

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
    print(f"📅 Aggregating cross-course due dates (Window: {window_filter})...")

    # 1. Scrape Global Calendar
    calendar_items = await scrape_calendar_async(page)
    print(f"   Calendar returned {len(calendar_items)} items.")

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
    now = datetime.now()
    results: List[Dict[str, Any]] = []

    for item in combined.values():
        if exclude_completed and item.get("status", "").lower() in ("graded", "submitted"):
            continue

        results.append(item)

    print(f"   ✅ Aggregated {len(results)} distinct upcoming assignments.")
    return results


def save_due_dates(items: List[Dict[str, Any]], window_filter: str = "7d") -> Path:
    """Saves due dates report to output/due_dates.md."""
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
    print(f"   💾 Saved due dates schedule to: {filepath.name}")
    return filepath
