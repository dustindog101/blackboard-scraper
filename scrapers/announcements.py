from core.output import status as print
import asyncio
import html
import json
import logging
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.config import BLACKBOARD_BASE, SESSION_DIR, load_courses
from core.output import ensure_output_dir
from core.async_engine import AdaptiveDOM

logger = logging.getLogger("blackboard.scrapers.announcements")


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


def _clean_html_body(raw_html: str) -> str:
    """Convert Blackboard HTML announcement body into clean, formatted plaintext."""
    if not raw_html:
        return ""
    text = re.sub(r"</p>|</li>|<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _format_created_meta(iso_str: str) -> str:
    """Format ISO timestamp into clean human readable date."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%B %-d, %Y at %-I:%M %p")
    except Exception:
        return iso_str


def scrape_announcements_api(course_id: str) -> Optional[List[Dict[str, Any]]]:
    """
    High-speed HTTP REST API announcement scraper (<150ms).
    Queries GET /learn/api/public/v1/courses/{course_id}/announcements.
    """
    cookie_header = get_cookie_header()
    if not cookie_header:
        return None

    headers = {
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    url = f"{BLACKBOARD_BASE}/learn/api/public/v1/courses/{course_id}/announcements"
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
        logger.debug(f"Announcements HTTP {e.code} for {course_id}: {e}")
        return None
    except Exception as e:
        logger.debug(f"Announcements network error for {course_id}: {e}")
        return None

    raw_items = data.get("results", [])
    extracted: List[Dict[str, Any]] = []

    for item in raw_items:
        title = item.get("title", "Untitled").strip()
        raw_body = item.get("body", "")
        body = _clean_html_body(raw_body)
        created_str = item.get("created") or item.get("modified") or ""
        meta = _format_created_meta(created_str)

        is_unread = item.get("unread") if isinstance(item.get("unread"), bool) else None

        extracted.append({
            "title": title,
            "meta": meta,
            "body": body,
            "unread": is_unread,
            "created": created_str,
            "modified": item.get("modified"),
        })

    return filter_announcements(extracted)


async def scrape_announcements_playwright_async(course_id: str, page: Any) -> List[Dict[str, Any]]:
    """Playwright browser DOM fallback for course announcements."""
    courses = load_courses()
    name = courses.get(course_id, course_id)
    print(f"📢 Scraping announcements for {name} (Playwright fallback)...")

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


async def scrape_announcements_async(course_id: str, page: Optional[Any] = None, verify_unread: bool = False) -> List[Dict[str, Any]]:
    """
    Unified announcements scraper.
    Primary: Fast HTTP REST API endpoint (<150ms).
    Fallback: Playwright browser DOM scraper if API fails.
    """
    courses = load_courses()
    name = courses.get(course_id, course_id)

    api_results = None
    try:
        api_results = await asyncio.to_thread(scrape_announcements_api, course_id)
        if api_results is not None and not verify_unread:
            return api_results
        if api_results is not None and verify_unread and not api_results:
            return []
    except Exception as e:
        logger.debug(f"Announcements HTTP API exception for {name}: {e}")

    # Fallback path: Playwright browser scraper
    print(f"⚠️ Announcement API data or read state unavailable for {name}; falling back to Playwright browser scraper...", file=sys.stderr)
    def reconcile(browser_rows):
        if api_results is None:
            return filter_announcements(browser_rows)
        title_counts = {r["title"]: sum(b["title"] == r["title"] for b in browser_rows) for r in browser_rows}
        for row in api_results:
            matches = [b for b in browser_rows if b["title"] == row["title"]]
            if len(matches) == 1 and title_counts[row["title"]] == 1 and sum(
                    a["title"] == row["title"] for a in api_results) == 1:
                row["unread"] = matches[0].get("unread")
        if any(r.get("unread") is None for r in api_results) and verify_unread:
            print("⚠️ Some announcement read states could not be verified; --unread excludes unknown states.", file=sys.stderr)
        return filter_announcements(api_results)

    if page:
        return reconcile(await scrape_announcements_playwright_async(course_id, page))
    else:
        from core.async_engine import AsyncSessionManager, EngineConfig
        session_manager = AsyncSessionManager(EngineConfig(headless=True))
        await session_manager.initialize()
        try:
            async with session_manager.acquire_page() as p:
                return reconcile(await scrape_announcements_playwright_async(course_id, p))
        finally:
            await session_manager.close()


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


def filter_announcements(items, unread=False, since=None, limit=None, now=None):
    from core.time import parse_datetime
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    threshold = None
    if since:
        match = re.fullmatch(r'(\d+)(d|w)', since)
        if match:
            threshold = now - timedelta(days=int(match[1]) * (7 if match[2] == 'w' else 1))
        elif re.fullmatch(r'\d{4}-\d{2}-\d{2}', since):
            threshold = parse_datetime(since)
        if threshold is None:
            raise ValueError('--since must be Nd, Nw, or YYYY-MM-DD')
    if limit is not None and limit < 0:
        raise ValueError('--limit must be nonnegative')
    rows = [dict(item) for item in items if not unread or item.get('unread') is True]
    if threshold:
        rows = [r for r in rows if parse_datetime(r.get('created') or r.get('meta')) and
                parse_datetime(r.get('created') or r.get('meta')) >= threshold]
    rows.sort(key=lambda r: parse_datetime(r.get('created') or r.get('meta')) or
              datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows[:limit] if limit is not None else rows


def announcement_label(item, now=None, term_start=None):
    from core.time import parse_datetime
    now = now or datetime.now(timezone.utc)
    created = parse_datetime(item.get('created') or item.get('meta'))
    if created:
        seconds = max(0, (now - created).total_seconds())
        age = f'{int(seconds // 86400)}d ago' if seconds >= 86400 else \
            f'{int(seconds // 3600)}h ago' if seconds >= 3600 else 'just now'
    else:
        age = 'date unknown'
    older = ' (older term)' if created and term_start and created < term_start else ''
    prefix = '🆕 [UNREAD] ' if item.get('unread') is True else ''
    unknown = ' [read state unknown]' if item.get('unread') is None else ''
    return f"{prefix}{item['title']} ({age}){older}{unknown}"
