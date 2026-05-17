from datetime import datetime
from pathlib import Path
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir
from scrapers.base import _navigate_and_check_page

def scrape_announcements(course_id: str, page: Page) -> list[dict]:
    """Scrape course announcements."""
    url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/announcements"
    courses = load_courses()
    name = courses.get(course_id, course_id)
    print(f"📢 Scraping announcements for {name}...")

    # Blackboard's Angular SPA requires navigation from the course outline —
    # direct URL navigation to /announcements doesn't trigger the Angular route state.
    outline_url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/outline"
    if not _navigate_and_check_page(page, outline_url):
        return []
    page.wait_for_timeout(4000)

    # Click the specific Announcements link in the left nav
    try:
        page.locator(".js-course-announcement-tool").click()
    except Exception:
        # Fall back: try href-based locator
        try:
            page.locator(f"a[href*='{course_id}/announcements']").first.click()
        except Exception:
            page.goto(url, wait_until="domcontentloaded", timeout=15_000)

    # Wait for the Angular panel to mount and render announcement rows
    try:
        page.wait_for_selector(
            "tr.announcement-item-row, .no-announcements-msg",
            state="attached",
            timeout=20_000,
        )
    except PlaywrightTimeout:
        print("   ⚠️  Timed out waiting for announcements to load.")
        return []

    # Scroll to load all lazy items
    last_height = page.evaluate("document.body.scrollHeight")
    while True:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

    announcements_data = page.evaluate("""() => {
        const items = [];
        document.querySelectorAll('tr.announcement-item-row').forEach(row => {
            const titleEl = row.querySelector('.announcement-title-detail');
            const title = titleEl ? titleEl.innerText.trim() : "Untitled";

            const dateEl = row.querySelector('.announcement-status-column');
            const meta = dateEl ? dateEl.innerText.trim() : "";

            // Body preview is inside .announcement-header but excludes the title
            const headerEl = row.querySelector('.announcement-header');
            let body = "";
            if (headerEl) {
                // Remove title text from header to get just the body snippet
                const clone = headerEl.cloneNode(true);
                const titleNode = clone.querySelector('.announcement-title-detail');
                if (titleNode) titleNode.remove();
                body = clone.innerText.trim();
            }

            const isUnread = !row.classList.contains('is-read');

            items.push({ title, meta, body, unread: isUnread });
        });
        return items;
    }""")

    print(f"   ✅ Extracted {len(announcements_data)} announcements.")
    return announcements_data

def save_announcements(data: list[dict], course_id: str):
    out_dir = ensure_output_dir("announcements")
    filepath = out_dir / f"{course_id}.md"

    courses = load_courses()
    course_name = courses.get(course_id, course_id)

    lines = [
        f"# Announcements: {course_name}",
        f"_Scraped: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "", "---", ""
    ]

    if not data:
        lines.append("_No announcements found._")
    else:
        for ann in data:
            status = "🟢 [UNREAD]" if ann.get('unread') else "⚪ [READ]"
            lines.append(f"## {status} {ann['title']}")
            if ann['meta']:
                lines.append(f"_{ann['meta']}_")
            lines.append("")

            formatted_body = ann['body'].replace('\n', '\n> ')
            if formatted_body:
                lines.append(f"> {formatted_body}")
            else:
                lines.append("> _(No content)_")
            lines.append("\n---")

    filepath.write_text("\n".join(lines))
