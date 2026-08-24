import asyncio
import json
import logging
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.config import BLACKBOARD_BASE, SESSION_DIR, load_courses
from core.output import ensure_output_dir
from core.async_engine import AdaptiveDOM

logger = logging.getLogger("blackboard.scrapers.calendar")


def get_cookie_header() -> Optional[str]:
    """Extract Cookie header string from .session/cookies.json."""
    cookie_file = SESSION_DIR / "cookies.json"
    if not cookie_file.exists():
        return None
    try:
        cookies_list = json.loads(cookie_file.read_text())
        return "; ".join([
            f"{c['name']}={c['value']}"
            for c in cookies_list
            if "blackboard.umbc.edu" in c.get("domain", "") or "umbc.edu" in c.get("domain", "")
        ])
    except Exception:
        return None


def _format_iso_datetime(iso_str: str) -> str:
    """Convert ISO UTC timestamp into clean readable local format (e.g. 8/30/26, 11:59 PM (EDT))."""
    if not iso_str:
        return "TBD"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%-m/%-d/%y, %-I:%M %p (%Z)")
    except Exception:
        return iso_str


def scrape_calendar_api(course_id: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """
    High-speed HTTP REST API calendar scraper (<150ms).
    Queries GET /learn/api/public/v1/calendars/items.
    Returns list of calendar items or None on failure.
    """
    cookie_header = get_cookie_header()
    if not cookie_header:
        return None

    headers = {
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    url = f"{BLACKBOARD_BASE}/learn/api/public/v1/calendars/items"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=7) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug(f"Calendar HTTP API error: {e}")
        return None

    raw_items = data.get("results", [])
    extracted: List[Dict[str, Any]] = []

    for item in raw_items:
        cal_id = item.get("calendarId")
        if course_id and cal_id and cal_id != course_id:
            continue

        title = item.get("title", "").strip()
        if not title:
            continue

        course_name = item.get("calendarName") or "General"
        iso_due = item.get("end") or item.get("start") or ""
        human_due = _format_iso_datetime(iso_due)

        extracted.append({
            "title": title,
            "course": course_name,
            "due": human_due,
            "due_date": human_due,
            "raw_due": iso_due,
            "id": item.get("id"),
            "type": item.get("type", "CalendarItem"),
        })

    return extracted


async def scrape_calendar_playwright_async(page: Any, course_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Playwright browser DOM fallback for calendar scraping."""
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

    # Switch to Due Dates view with robust multi-selector support
    await AdaptiveDOM.safe_click(
        page,
        "button.js-viewSwitch-deadline-button, button:has-text('Due Dates'), button[aria-label*='Due dates'], button[title*='Due dates']",
        timeout=6000,
    )

    matched_sel, _ = await AdaptiveDOM.wait_for_any_selector(
        page,
        [
            ".element-card.due-item",
            ".deadline-list",
            ".element-card-deadline",
            ".calendar-deadline-view",
            ".calendar-nothing-due",
            "div:has-text('Nothing due')",
        ],
        timeout=8_000,
    )

    calendar_data = []

    # 1. Primary: Extract from Due Dates / Deadline List view
    if matched_sel and "nothing-due" not in matched_sel:
        await AdaptiveDOM.adaptive_infinite_scroll(page, ".element-card.due-item", max_scrolls=8, idle_wait_ms=300)
        calendar_data = await page.evaluate("""() => {
            const results = [];
            const seen = new Set();
            document.querySelectorAll(".element-card.due-item, .element-card-deadline").forEach(el => {
                const fullText = el.innerText.trim();
                if (!fullText) return;

                const lines = fullText.split('\\n').map(l => l.trim()).filter(Boolean);
                let title = lines[0] || '';
                let course = '';
                let due = '';

                // Extract course from link or inline bullet
                const courseEl = el.querySelector("a[analytics-id*='openCourseOutline'], [class*='course-title'], .course-name");
                if (courseEl && !courseEl.classList.contains('js-title')) {
                    course = courseEl.innerText.trim();
                }

                for (let line of lines) {
                    if (line.toLowerCase().includes('due date:') || line.toLowerCase().includes('due:')) {
                        const parts = line.split(/[∙·•|]/);
                        due = parts[0].replace(/due\\s*date[:\\s]*/i, '').replace(/due[:\\s]*/i, '').trim();
                        if (parts.length > 1 && !course) {
                            course = parts[1].trim();
                        }
                    }
                }

                const titleEl = el.querySelector(".js-title, .element-details .name a, h3, h4");
                if (titleEl) {
                    title = titleEl.innerText.trim();
                }

                const key = `${course}:${title}:${due}`;
                if (title && !seen.has(key)) {
                    seen.add(key);
                    results.push({
                        title: title,
                        course: course || "General",
                        due: due || "TBD",
                        due_date: due || "TBD"
                    });
                }
            });
            return results;
        }""")

    # 2. Fallback: If Due Dates view returned 0 items, check Month View
    if not calendar_data:
        month_clicked = await AdaptiveDOM.safe_click(
            page,
            "button:has-text('Month'), [aria-label*='Month view'], [title*='Month view'], .js-calendar-month-button",
            timeout=4000,
        )
        if month_clicked:
            await asyncio.sleep(1.5)
            calendar_data = await page.evaluate("""() => {
                const results = [];
                const seen = new Set();
                document.querySelectorAll(".fc-event, .fc-day-grid-event").forEach(el => {
                    const titleEl = el.querySelector(".fc-title") || el;
                    const title = titleEl ? titleEl.innerText.trim() : "";
                    if (!title) return;

                    let date = "";
                    const dateCell = el.closest("[data-date]");
                    if (dateCell) {
                        date = dateCell.getAttribute("data-date") || "";
                    }

                    const key = `${title}:${date}`;
                    if (!seen.has(key)) {
                        seen.add(key);
                        results.push({
                            title: title,
                            course: "General",
                            due: date ? `${date}` : "TBD"
                        });
                    }
                });
                return results;
            }""")

    if not calendar_data:
        print("   ℹ️  No upcoming calendar deadlines found.")
        return []

    print(f"   ✅ Extracted {len(calendar_data)} calendar items.")
    return calendar_data


async def scrape_calendar_async(page: Optional[Any] = None, course_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Unified calendar scraper.
    Primary: Fast HTTP REST API endpoint (<150ms).
    Fallback: Playwright browser DOM scraper if API encounters an error or is blocked.
    """
    try:
        api_results = await asyncio.to_thread(scrape_calendar_api, course_id)
        if api_results is not None:
            return api_results
    except Exception as e:
        logger.debug(f"Calendar HTTP API exception: {e}")

    # Fallback path: Playwright browser scraper
    print("⚠️ HTTP Calendar API unavailable; falling back to Playwright browser scraper...", file=sys.stderr)
    if page:
        return await scrape_calendar_playwright_async(page, course_id)
    else:
        from core.async_engine import AsyncSessionManager, EngineConfig
        session_manager = AsyncSessionManager(EngineConfig(headless=True))
        await session_manager.initialize()
        try:
            async with session_manager.acquire_page() as p:
                return await scrape_calendar_playwright_async(p, course_id)
        finally:
            await session_manager.close()


def scrape_calendar(page: Any = None, course_id: Optional[str] = None) -> list[dict]:
    """Synchronous fallback wrapper."""
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
