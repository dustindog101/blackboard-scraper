import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir
from core.async_engine import AdaptiveDOM

logger = logging.getLogger("blackboard.scrapers.grades")


async def scrape_grades_async(course_id: str, page: Any) -> List[Dict[str, Any]]:
    """Scrapes grades from a Blackboard Ultra course page with Adaptive DOM."""
    url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/grades"
    courses = load_courses()
    name = courses.get(course_id, course_id)
    print(f"🎓 Scraping grades for {name}...")

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
        print(f"   ℹ️  Course is currently unavailable or closed.")
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

                const nameContainer = cells[1].querySelector('[class*="itemNameContainer"], [class*="titleContainer"]');
                const name = nameContainer
                    ? nameContainer.innerText.trim().split('\\n')[0]
                    : cells[1].innerText.trim().split('\\n')[0];

                const dueDate = cells[2] ? cells[2].innerText.trim() : '';
                const status = cells[3] ? cells[3].innerText.trim() : '';

                const gradeCell = cells[4];
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
