import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir
from core.async_engine import AdaptiveDOM


async def scrape_activity_async(page: Any) -> List[Dict[str, Any]]:
    """Scrapes activity stream asynchronously with Adaptive DOM."""
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

    stream_data = await page.evaluate("""() => {
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
        match = re.search(r"(?:due|deadline)[:\s]+([^\n|•]+)", text_blob, re.IGNORECASE)
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


def scrape_activity(page: Any) -> list[dict]:
    """Synchronous fallback wrapper."""
    import asyncio
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
            ctx = item['context'] or 'System'
            lines.append(f"### {item['title']} ({ctx})")
            if item['message']:
                lines.append(f"> {item['message']}")
            lines.append(f"_{item['date']}_")
            lines.append("")

    filepath.write_text("\n".join(lines))
    print(f"   💾 Saved to: {filepath.name}")
