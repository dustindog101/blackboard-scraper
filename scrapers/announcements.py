import asyncio
from datetime import datetime
from typing import Any, Dict, List

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir
from core.async_engine import AdaptiveDOM


async def scrape_announcements_async(course_id: str, page: Any) -> List[Dict[str, Any]]:
    """Async scraper for course announcements with Adaptive DOM."""
    courses = load_courses()
    name = courses.get(course_id, course_id)
    print(f"📢 Scraping announcements for {name}...")

    outline_url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/outline"
    try:
        await page.goto(outline_url, wait_until="domcontentloaded", timeout=20_000)
    except Exception as e:
        print(f"   ⚠️ Could not load outline for {name}: {e}")
        return []

    # Check if course is unavailable
    matched_sel, _ = await AdaptiveDOM.wait_for_any_selector(
        page,
        [
            ".js-course-announcement-tool",
            f"a[href*='{course_id}/announcements']",
            "div:has-text(\"You can't access this course right now\")",
        ],
        timeout=8_000,
    )

    if not matched_sel or "You can't access" in matched_sel:
        print("   ℹ️  Course is currently unavailable or closed.")
        return []

    # Click announcements navigation
    clicked = await AdaptiveDOM.safe_click(page, ".js-course-announcement-tool", timeout=3000)
    if not clicked:
        clicked = await AdaptiveDOM.safe_click(page, f"a[href*='{course_id}/announcements']", timeout=3000)

    if not clicked:
        try:
            url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/announcements"
            await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
        except Exception:
            pass

    # Wait for rows or empty state
    matched_data_sel, _ = await AdaptiveDOM.wait_for_any_selector(
        page,
        ["tr.announcement-item-row", ".no-announcements-msg", "div:has-text('No announcements')"],
        timeout=10_000,
    )

    if not matched_data_sel or "no-announcements" in matched_data_sel:
        print(f"   ℹ️  No announcements found for {name}.")
        return []

    # Adaptive scroll to load all lazy items
    await AdaptiveDOM.adaptive_infinite_scroll(page, "tr.announcement-item-row", max_scrolls=6, idle_wait_ms=300)

    announcements_data = await page.evaluate("""() => {
        const items = [];
        document.querySelectorAll('tr.announcement-item-row').forEach(row => {
            const titleEl = row.querySelector('.announcement-title-detail');
            const title = titleEl ? titleEl.innerText.trim() : "Untitled";

            const dateEl = row.querySelector('.announcement-status-column');
            const meta = dateEl ? dateEl.innerText.trim() : "";

            const headerEl = row.querySelector('.announcement-header');
            let body = "";
            if (headerEl) {
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

    print(f"   ✅ Extracted {len(announcements_data)} announcements from {name}.")
    return announcements_data


def scrape_announcements(course_id: str, page: Any) -> list[dict]:
    """Synchronous fallback wrapper."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # In existing running event loop
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, scrape_announcements_async(course_id, page)).result()
        return asyncio.run(scrape_announcements_async(course_id, page))
    except Exception:
        # Direct sync execution
        url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/announcements"
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        return []


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
