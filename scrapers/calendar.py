import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir
from core.async_engine import AdaptiveDOM


async def scrape_calendar_async(page: Any, course_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Scrapes calendar due dates asynchronously."""
    url = f"{BLACKBOARD_BASE}/ultra/calendar"
    if course_id:
        courses = load_courses()
        name = courses.get(course_id, course_id)
        print(f"📅 Scraping calendar for {name}...")
    else:
        print("📅 Scraping globally aggregated calendar due dates...")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except Exception as e:
        print(f"   ⚠️ Navigation error for calendar: {e}")
        return []

    # Switch to Due Dates view
    await AdaptiveDOM.safe_click(page, "button[aria-label='Due dates view']", timeout=3000)

    matched_sel, _ = await AdaptiveDOM.wait_for_any_selector(
        page,
        [".element-card.due-item", ".calendar-nothing-due", "div:has-text('Nothing due')"],
        timeout=12_000,
    )

    if not matched_sel or "nothing-due" in matched_sel:
        print("   ℹ️  No upcoming calendar deadlines found.")
        return []

    # Adaptive scroll to load full calendar
    await AdaptiveDOM.adaptive_infinite_scroll(page, ".element-card.due-item", max_scrolls=8, idle_wait_ms=300)

    calendar_data = await page.evaluate("""() => {
        const results = [];
        const items = document.querySelectorAll(".element-card.due-item");
        items.forEach(el => {
            const titleEl = el.querySelector(".element-details .name a, .js-title");
            const dateEl = el.querySelector(".element-details .content > span:first-child");
            const courseEl = el.querySelector(".element-details .content a[analytics-id*='openCourseOutline'], [class*='course-title']");

            if (titleEl) {
                results.push({
                    "title": titleEl.innerText.trim(),
                    "course": courseEl ? courseEl.innerText.trim() : "Unknown Course",
                    "due": dateEl ? dateEl.innerText.replace("Due date:", "").trim() : "Unknown Date"
                });
            }
        });
        return results;
    }""")

    print(f"   ✅ Extracted {len(calendar_data)} calendar items.")
    return calendar_data


def scrape_calendar(page: Any, course_id: Optional[str] = None) -> list[dict]:
    """Synchronous fallback wrapper."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, scrape_calendar_async(page, course_id)).result()
        return asyncio.run(scrape_calendar_async(page, course_id))
    except Exception:
        return []


def save_calendar(calendar: list[dict], course_id: str = None):
    out_dir = ensure_output_dir("calendar")
    filename = f"{course_id}.md" if course_id else "due_dates.md"
    filepath = out_dir / filename

    title = f"Calendar: {course_id}" if course_id else "Global Calendar Due Dates"

    lines = [
        f"# {title}",
        f"_Scraped: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        ""
    ]

    if not calendar:
        lines.append("_No upcoming due dates found._")
    else:
        for item in calendar:
            lines.append(f"### {item['title']}")
            if not course_id and item['course']:
                lines.append(f"**Course:** {item['course']}")
            lines.append(f"**Due:** _{item['due']}_")
            lines.append("")

    filepath.write_text("\n".join(lines))
    print(f"   💾 Saved to: {filepath.name}")
