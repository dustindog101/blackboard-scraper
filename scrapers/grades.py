import re
from datetime import datetime
from pathlib import Path
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from core.config import BLACKBOARD_BASE, load_courses, SCRIPT_DIR
from core.output import ensure_output_dir
from scrapers.base import _navigate_and_check_page

def scrape_grades(course_id: str, page: Page) -> list[dict]:
    """Scrape grades from a Blackboard Ultra course page."""
    url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/grades"
    courses = load_courses()
    name = courses.get(course_id, course_id)
    print(f"🎓 Scraping grades for {name}...")

    if not _navigate_and_check_page(page, url):
        return []

    # Wait for React/MUI table to render — grades page is heavy
    try:
        page.wait_for_selector(
            "[data-testid^='course-student-grades-table-row']",
            timeout=20_000,
        )
    except PlaywrightTimeout:
        print("   ⚠️  Timed out waiting for grades table. Dumping debug HTML...")
        html = page.content()
        dbg_dir = ensure_output_dir(".")
        dbg = dbg_dir / f"debug_grades_{course_id}.html"
        dbg.write_text(html)
        print(f"   Debug saved: {dbg}")

    grades = []

    items = page.evaluate("""() => {
        const results = [];
        const rows = document.querySelectorAll('[data-testid^="course-student-grades-table-row"]');
        rows.forEach(row => {
            const cells = row.querySelectorAll('td');
            if (cells.length < 4) return;

            // Assignment name
            const nameContainer = cells[1].querySelector('[class*="itemNameContainer"], [class*="titleContainer"]');
            const name = nameContainer
                ? nameContainer.innerText.trim().split('\\n')[0]
                : cells[1].innerText.trim().split('\\n')[0];

            // Due date
            const dueDate = cells[2] ? cells[2].innerText.trim() : '';

            // Status
            const status = cells[3] ? cells[3].innerText.trim() : '';

            // Grade pill
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

    # Handle pagination - click "Next Page" button to get more grades
    page_num = 1
    while True:
        try:
            # Check if there's a next page button
            next_btn = page.locator('[aria-label="Next Page"]')
            if next_btn.is_visible():
                next_btn.click()
                page.wait_for_timeout(3000)
                page_num += 1
                
                # Wait for table to re-render
                page.wait_for_selector(
                    "[data-testid^='course-student-grades-table-row']",
                    timeout=10_000,
                )
                
                # Extract next page items
                more_items = page.evaluate("""() => {
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
                
                for item in more_items:
                    grades.append(item)
                
                print(f"   📄 Extracted page {page_num}: {len(more_items)} items")
            else:
                break
        except Exception as e:
            print(f"   ⚠️  No more pages or error: {e}")
            break

    print(f"   ✅ Extracted {len(grades)} graded items (total from {page_num} pages)")
    return grades

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
            # Escape pipes
            safe_name = g['name'].replace('|', '-')
            safe_due = g['dueDate'].replace('|', '-')
            safe_status = g['status'].replace('|', '-')
            safe_grade = g['grade'].replace('|', '-')
            lines.append(f"| {safe_name} | {safe_due} | {safe_status} | {safe_grade} |")

    filepath.write_text("\n".join(lines))
    print(f"   💾 Saved to: {filepath.relative_to(Path.cwd()) if filepath.is_absolute() else filepath}")
