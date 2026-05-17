from datetime import datetime
import re
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir
from scrapers.base import _navigate_and_check_page

def scrape_activity(page: Page) -> list[dict]:
    """Scrape the main activity stream."""
    url = f"{BLACKBOARD_BASE}/ultra/stream"
    print("🌊 Scraping Activity Stream...")

    if not _navigate_and_check_page(page, url):
        return []

    try:
        page.wait_for_selector(".stream-item, .empty-state", timeout=15_000)
    except PlaywrightTimeout:
        print("   ⚠️  Timed out waiting for activity stream.")
        return []

    stream_data = page.evaluate("""() => {
        const items = [];
        document.querySelectorAll('.stream-item').forEach(el => {
            const getVal = (selector) => {
                const node = el.querySelector(selector);
                return node ? node.innerText.trim() : "";
            };

            const textBlob = (el.innerText || "").replace(/\\s+/g, " ").trim();
            
            items.push({
                context: getVal('.context'),
                title: getVal('.title'),
                message: getVal('.message'),
                date: getVal('.date, .datetime'),
                text_blob: textBlob
            });
        });
        return items;
    }""")

    normalized = []
    for item in stream_data:
        title = (item.get("title") or "").strip()
        message = (item.get("message") or "").strip()
        context = (item.get("context") or "").strip()
        text_blob = item.get("text_blob") or ""

        if not title:
            if message:
                title = message.split("\n")[0][:120]
            elif context:
                title = f"Update in {context}"
            else:
                title = "Activity update"

        due_date = ""
        match = re.search(r"(?:due|deadline)[:\\s]+([^\\n|•]+)", text_blob, re.IGNORECASE)
        if match:
            due_date = match.group(1).strip()

        normalized.append(
            {
                "context": context,
                "course": context,
                "title": title,
                "message": message,
                "date": (item.get("date") or "").strip(),
                "due_date": due_date,
            }
        )

    print(f"   ✅ Extracted {len(normalized)} activity items.")
    return normalized

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
            ctx = item['context'] or 'System'
            lines.append(f"### {item['title']} ({ctx})")
            if item['message']:
                lines.append(f"> {item['message']}")
            lines.append(f"_{item['date']}_")
            lines.append("")
            
    filepath.write_text("\n".join(lines))
