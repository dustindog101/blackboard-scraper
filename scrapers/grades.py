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

logger = logging.getLogger("blackboard.scrapers.grades")


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


def _format_grade_due(iso_str: str) -> str:
    """Format ISO UTC timestamp into clean date string (e.g. 8/30/26)."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%-m/%-d/%y")
    except Exception:
        return iso_str


def scrape_grades_api(course_id: str) -> Optional[List[Dict[str, Any]]]:
    """
    High-speed HTTP REST API gradebook scraper (<150ms).
    Queries GET /learn/api/public/v2/courses/{course_id}/gradebook/columns.
    """
    cookie_header = get_cookie_header()
    if not cookie_header:
        return None

    headers = {
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    url = f"{BLACKBOARD_BASE}/learn/api/public/v2/courses/{course_id}/gradebook/columns"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=7) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            # Course is closed or unavailable -> return empty list immediately
            return []
        logger.debug(f"Gradebook HTTP {e.code} for {course_id}: {e}")
        return None
    except Exception as e:
        logger.debug(f"Gradebook network error for {course_id}: {e}")
        return None

    raw_cols = data.get("results", [])
    extracted: List[Dict[str, Any]] = []

    for col in raw_cols:
        name = col.get("name", "Untitled").strip()
        # Skip internal calculation containers if course has actual items
        if name.lower() in ("overall grade", "total", "weighted total") and len(raw_cols) > 1:
            continue

        due_iso = col.get("grading", {}).get("due") or ""
        due_formatted = _format_grade_due(due_iso)
        pts = col.get("score", {}).get("possible")

        extracted.append({
            "name": name,
            "dueDate": due_formatted,
            "status": "Unopened",
            "grade": "--",
            "points_possible": pts,
        })

    return extracted


async def scrape_grades_playwright_async(course_id: str, page: Any) -> List[Dict[str, Any]]:
    """Playwright browser DOM fallback for grades table scraping."""
    url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/grades"
    courses = load_courses()
    name = courses.get(course_id, course_id)
    print(f"🎓 Scraping grades for {name} (Playwright fallback)...")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except Exception as e:
        print(f"   ⚠️ Navigation error for {name}: {e}")
        return []

    # Wait for React/MUI table or empty state
    matched_sel, _ = await AdaptiveDOM.wait_for_any_selector(
        page,
        [
            "[data-testid^='course-student-grades-table-row']",
            "table[class*='grades-table']",
            "div:has-text(\"You can't access this course right now\")",
            "div:has-text('No grades posted')",
        ],
        timeout=10_000,
    )

    if not matched_sel or "You can't access" in matched_sel:
        print("   ℹ️  Course is currently unavailable or closed.")
        return []

    grades: List[Dict[str, Any]] = []

    # Extract current page items
    page_num = 1
    while True:
        items = await page.evaluate("""() => {
            const results = [];
            const rows = document.querySelectorAll('[data-testid^="course-student-grades-table-row"]');
            rows.forEach(row => {
                const cells = row.querySelectorAll('td');
                if (cells.length < 4) return;

                const nameContainer = cells[0].querySelector('[class*="itemNameContainer"], [class*="titleContainer"]');
                const name = nameContainer
                    ? nameContainer.innerText.trim().split('\\n')[0]
                    : cells[0].innerText.trim().split('\\n')[0];

                const dueDate = cells[1] ? cells[1].innerText.trim().replace(/\\n+/g, ' ') : '';
                const status = cells[2] ? cells[2].innerText.trim().replace(/\\n+/g, ' ') : '';

                const gradeCell = cells[3];
                let grade = '';
                if (gradeCell) {
                    const pill = gradeCell.querySelector('[class*="readonlyPill"], [class*="pill"]');
                    grade = pill ? pill.innerText.trim().replace(/\\n+/g, ' ') : gradeCell.innerText.trim().replace(/\\n+/g, ' ');
                }

                if (name) results.push({ name, dueDate, status, grade });
            });
            return results;
        }""")

        for item in items:
            grades.append(item)

        # Check for Next Page button
        next_btn = page.locator('[aria-label="Next Page"]').first
        count = await next_btn.count()
        if count > 0 and await next_btn.is_visible() and not await next_btn.is_disabled():
            try:
                await next_btn.click()
                await asyncio.sleep(0.8)
                page_num += 1
            except Exception:
                break
        else:
            break

    print(f"   ✅ Extracted {len(grades)} graded items from {name}.")
    return grades


async def scrape_grades_async(course_id: str, page: Optional[Any] = None) -> List[Dict[str, Any]]:
    """
    Unified gradebook scraper.
    Primary: Fast HTTP REST API endpoint (<150ms).
    Fallback: Playwright browser DOM scraper if API fails.
    """
    courses = load_courses()
    name = courses.get(course_id, course_id)

    try:
        api_results = await asyncio.to_thread(scrape_grades_api, course_id)
        if api_results is not None:
            return api_results
    except Exception as e:
        logger.debug(f"Gradebook HTTP API exception for {name}: {e}")

    # Fallback path: Playwright browser scraper
    print(f"⚠️ HTTP Gradebook API unavailable for {name}; falling back to Playwright browser scraper...", file=sys.stderr)
    if page:
        return await scrape_grades_playwright_async(course_id, page)
    else:
        from core.async_engine import AsyncSessionManager, EngineConfig
        session_manager = AsyncSessionManager(EngineConfig(headless=True))
        await session_manager.initialize()
        try:
            async with session_manager.acquire_page() as p:
                return await scrape_grades_playwright_async(course_id, p)
        finally:
            await session_manager.close()


def scrape_grades(course_id: str, page: Any) -> list[dict]:
    """Synchronous fallback wrapper."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, scrape_grades_async(course_id, page)).result()
        return asyncio.run(scrape_grades_async(course_id, page))
    except Exception:
        return []


def save_grades(grades: list[dict], course_id: str):
    out_dir = ensure_output_dir("grades")
    courses = load_courses()
    name = courses.get(course_id, course_id)
    filepath = out_dir / f"{course_id}.md"

    lines = [
        f"# Grades: {name}",
        f"_Course ID: {course_id}_",
        f"_Scraped: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "", "---", "",
    ]

    if not grades:
        lines.append("_No grades found._")
    else:
        lines.append("| Assignment | Due Date | Status | Grade |")
        lines.append("|---|---|---|---|")
        for g in grades:
            safe_name = g['name'].replace('|', '-')
            safe_due = g['dueDate'].replace('|', '-')
            safe_status = g['status'].replace('|', '-')
            safe_grade = g['grade'].replace('|', '-')
            lines.append(f"| {safe_name} | {safe_due} | {safe_status} | {safe_grade} |")

    filepath.write_text("\n".join(lines))
    print(f"   💾 Saved to: {filepath.name}")
