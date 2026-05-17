import time
from datetime import datetime
from pathlib import Path
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir
from scrapers.base import _navigate_and_check_page

def scrape_calendar(page: Page, course_id: str = None) -> list[dict]:
    """Scrape calendar due dates via infinite scroll bypass."""
    url = f"{BLACKBOARD_BASE}/ultra/calendar"
    if course_id:
        courses = load_courses()
        name = courses.get(course_id, course_id)
        print(f"📅 Scraping calendar for {name}...")
    else:
        print("📅 Scraping globally aggregated calendar due dates...")

    if not _navigate_and_check_page(page, url):
        return []

    # Switch to Due Dates view
    try:
        due_dates_btn = page.locator("button[aria-label='Due dates view']")
        if due_dates_btn.is_visible():
            due_dates_btn.click()
            page.wait_for_timeout(2000)
    except Exception as e:
        print(f"   ⚠️ Could not switch to Due Dates view: {e}")

    try:
        page.wait_for_selector(".element-card.due-item, .calendar-nothing-due", state="attached", timeout=15_000)
    except PlaywrightTimeout:
        print("   ⚠️  Timed out waiting for calendar to load.")
        return []

    # If course_id provided, open the filter panel and select it
    if course_id:
        try:
            filter_btn = page.locator("button[aria-label*='Filter']")
            if filter_btn.is_visible():
                filter_btn.click()
                page.wait_for_selector(".facet-list", timeout=5000)
                
                # Check the specific course checkbox
                checkbox = page.locator(f"input[value='{course_id}']")
                if checkbox.is_visible():
                    checkbox.check()
                    page.wait_for_timeout(2000) # give it time to filter
                # Close filter
                close_btn = page.locator("button.close-panel")
                if close_btn.is_visible():
                    close_btn.click()
        except Exception as e:
            print(f"   ⚠️ Could not filter calendar for {course_id}: {e}")

    # Infinite scroll to load all future items
    last_count = 0
    scroll_attempts = 0
    max_scrolls = 20
    
    while scroll_attempts < max_scrolls:
        items = page.locator(".element-card.due-item")
        count = items.count()
        
        if count == last_count:
            # Scroll the .scroll-container element instead of window
            page.evaluate("""() => {
                const scrollContainer = document.querySelector('.scroll-container');
                if (scrollContainer) scrollContainer.scrollTop = scrollContainer.scrollHeight;
            }""")
            page.wait_for_timeout(1000)
            new_count = page.locator(".element-card.due-item").count()
            if new_count == count:
                break
        last_count = count
        scroll_attempts += 1

    calendar_data = page.evaluate("""() => {
        const results = [];
        const items = document.querySelectorAll(".element-card.due-item");
        items.forEach(el => {
            const titleEl = el.querySelector(".element-details .name a");
            const dateEl = el.querySelector(".element-details .content > span:first-child");
            const courseEl = el.querySelector(".element-details .content a[analytics-id*='openCourseOutline']");
            
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
    print(f"   💾 Saved to: {filepath.relative_to(Path.cwd()) if filepath.is_absolute() else filepath}")
