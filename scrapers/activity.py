import asyncio
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.config import BLACKBOARD_BASE
from core.output import ensure_output_dir
from core.async_engine import AdaptiveDOM


async def scrape_activity_async(page: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Scrapes activity stream asynchronously directly from Blackboard Ultra web UI with Adaptive DOM."""
    if page is None:
        from core.async_engine import AsyncSessionManager, EngineConfig
        session_manager = AsyncSessionManager(EngineConfig(headless=True))
        await session_manager.initialize()
        try:
            async with session_manager.acquire_page() as p:
                return await scrape_activity_async(p)
        finally:
            await session_manager.close()

    url = f"{BLACKBOARD_BASE}/ultra/stream"
    print("🌊 Scraping Activity Stream...")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except Exception as e:
        print(f"   ⚠️ Navigation error for activity stream: {e}")
        return []

    matched_sel, _ = await AdaptiveDOM.wait_for_any_selector(
        page,
        [".stream-item", ".empty-state", "div:has-text('No recent activity')"],
        timeout=12_000,
    )

    if not matched_sel or "empty-state" in matched_sel or "No recent activity" in matched_sel:
        print("   ℹ️  No stream activity items found.")
        return []

    # Adaptive infinite scroll to load all stream items as semester fills up
    await AdaptiveDOM.adaptive_infinite_scroll(page, ".stream-item", max_scrolls=12, idle_wait_ms=350)

    stream_data = await page.evaluate("""() => {
        const items = [];
        document.querySelectorAll('.stream-item').forEach(el => {
            const dateEl = el.querySelector('.date');
            const timeEl = el.querySelector('.time');
            const dateStr = dateEl ? dateEl.innerText.trim() : '';
            const timeStr = timeEl ? timeEl.innerText.trim() : '';
            const posted = [dateStr, timeStr].filter(Boolean).join(' • ');

            // Course Name
            let course = '';
            const courseEl = el.querySelector('a:not(.js-title-link)');
            if (courseEl && !courseEl.classList.contains('dismiss-button')) {
                course = courseEl.innerText.trim();
            }

            // Event / Item Title
            let title = '';
            const titleEl = el.querySelector('.js-title-link, [class*="title"], h3, h4');
            if (titleEl) {
                title = titleEl.innerText.trim();
            }

            const fullText = el.innerText ? el.innerText.trim() : '';

            // Due Date
            let due_date = '';
            const match = fullText.match(/Due\\s*Date[:\\s]*([^|\\n]+)/i);
            if (match) {
                due_date = match[1].replace(/Dismiss/i, '').trim();
            }

            items.push({
                course: course,
                context: course,
                title: title,
                date: posted,
                due_date: due_date,
                raw_text: fullText
            });
        });
        return items;
    }""")

    normalized = []
    for item in stream_data:
        raw = item.get("raw_text") or ""
        lines = [line.strip() for line in raw.split("\n") if line.strip()]

        course = item.get("course") or "General"
        date_str = item.get("date") or ""
        title = item.get("title") or ""
        due_date = item.get("due_date") or ""

        # Smart title fallback: find the line that contains the actual event
        if not title or re.match(r"^\d+\s*(?:hours?|days?|minutes?|secs?|am|pm)", title, re.IGNORECASE) or re.match(r"^[A-Za-z]{3}\s*\d+", title):
            title = ""
            for line in lines:
                line_lower = line.lower()
                # Skip timestamps, time-ago, course names, and dismiss buttons
                if re.match(r"^\d+\s*(?:hours?|days?|minutes?|secs?)", line, re.IGNORECASE):
                    continue
                if re.match(r"^\d+:\d+\s*[AP]M", line, re.IGNORECASE):
                    continue
                if re.match(r"^[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}", line):
                    continue
                if line in course or course in line:
                    continue
                if line_lower in ("dismiss", "unread", "read", "past due:", "due:"):
                    continue
                if line_lower.startswith("due date:"):
                    continue

                title = line
                break

        if not title:
            title = "Course Update"

        # Build clean message (only when real body text exists)
        message_parts = []
        for line in lines:
            line_clean = line.strip()
            line_lower = line_clean.lower()

            # Skip title, course, and dismiss button
            if line_clean == title or line_clean in course or course in line_clean:
                continue
            if line_lower in ("dismiss", "unread", "read"):
                continue
            if line_lower.startswith("due date:"):
                continue

            # Skip timestamp lines (time-ago or clock times)
            if re.match(r"^\d+\s*(?:hours?|days?|minutes?|secs?|weeks?|months?)", line_clean, re.IGNORECASE):
                continue
            if re.match(r"^\d{1,2}:\d{2}\s*[AP]M", line_clean, re.IGNORECASE):
                continue
            if re.match(r"^[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}", line_clean):
                continue
            if line_clean in date_str or date_str in line_clean:
                continue

            message_parts.append(line_clean)

        message = " | ".join(message_parts).strip()

        item_dict = {
            "course": course,
            "title": title,
            "date": date_str,
        }
        if due_date:
            item_dict["due_date"] = due_date
        if message:
            item_dict["message"] = message

        normalized.append(item_dict)

    print(f"   ✅ Extracted {len(normalized)} activity items.")
    return normalized


def scrape_activity(page: Any = None) -> list[dict]:
    """Synchronous fallback wrapper."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, scrape_activity_async(page)).result()
        return asyncio.run(scrape_activity_async(page))
    except Exception:
        return []


def save_activity(activity: list[dict]):
    out_dir = ensure_output_dir("activity")
    filepath = out_dir / "stream.md"

    lines = [
        "# Blackboard Activity Stream",
        f"_Scraped: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        ""
    ]

    if not activity:
        lines.append("_No recent activity found._")
    else:
        for item in activity:
            cname = item.get('course') or 'System'
            lines.append(f"### {item.get('title', 'Update')} ({cname})")
            if item.get('due_date'):
                lines.append(f"**Due:** {item['due_date']}")
            if item.get('message'):
                lines.append(f"> {item['message']}")
            if item.get('date'):
                lines.append(f"_{item['date']}_")
            lines.append("")

    filepath.write_text("\n".join(lines))
    print(f"   💾 Saved to: {filepath.name}")
