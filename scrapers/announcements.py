"""Course announcements: REST Fast-Path, newest-first, filters, term marking, optional read-state check.

Blackboard's public announcements API has no read/unread field, so ``unread`` is ``None`` (unknown)
unless it was verified in the browser (``--unread``, issue #38). Age is never used to guess it.
"""
import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.config import BLACKBOARD_BASE, load_courses
from core.display import ago, clip, fmt_when, parse_dt
from core.gradebook import course_label
from core.output import ensure_output_dir, status
from core.rest import RestError, get_json, get_pages

logger = logging.getLogger("blackboard.scrapers.announcements")

PREVIEW_CHARS = 170


def _clean_html_body(raw_html: str) -> str:
    """Convert Blackboard HTML announcement body into clean, formatted plaintext."""
    if not raw_html:
        return ""
    text = re.sub(r"</p>|</li>|<br\s*/?>|</div>|</h[1-6]>", "\n", raw_html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def normalize_announcements(raw_items: List[dict]) -> List[Dict[str, Any]]:
    """Public-API announcement records -> stable dicts. ``unread`` is None: the API doesn't say."""
    out = []
    for item in raw_items:
        created = item.get("created") or item.get("modified") or ""
        out.append({
            "id": item.get("id"),
            "title": (item.get("title") or "Untitled").strip(),
            "created": created,
            "modified": item.get("modified"),
            "meta": fmt_when(parse_dt(created)),
            "body": _clean_html_body(item.get("body", "")),
            "unread": None,
        })
    return out


def parse_since(spec: str, now: Optional[datetime] = None) -> datetime:
    """'14d', '2w' or '2026-09-01' -> cutoff datetime. Raises ValueError for anything else."""
    now = now or datetime.now(timezone.utc)
    match = re.fullmatch(r"(\d+)([dw])", spec.strip().lower())
    if match:
        return now - timedelta(days=int(match[1]) * (7 if match[2] == "w" else 1))
    try:
        return datetime.strptime(spec.strip(), "%Y-%m-%d").astimezone()
    except ValueError:
        raise ValueError(f"--since must look like 14d, 2w or 2026-09-01 (got '{spec}')") from None


def sort_newest_first(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(items, key=lambda a: parse_dt(a.get("created")) or floor, reverse=True)


def filter_announcements(
    items: List[Dict[str, Any]],
    *,
    since: Optional[datetime] = None,
    unread: bool = False,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Newest first, then ``since``, ``unread`` (verified True only) and ``limit`` in that order."""
    rows = sort_newest_first(items)
    if since is not None:
        rows = [r for r in rows if (d := parse_dt(r.get("created"))) and d >= since]
    if unread:
        rows = [r for r in rows if r.get("unread") is True]
    return rows[:limit] if limit is not None else rows


def fetch_course_announcements(course_id: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """REST fetch for one course. Returns (items, error) where error is a RestError kind or None."""
    try:
        raw = get_pages(f"/learn/api/public/v1/courses/{course_id}/announcements")
    except RestError as exc:
        return [], exc.kind
    return sort_newest_first(normalize_announcements(raw)), None


def scrape_announcements_api(course_id: str) -> Optional[List[Dict[str, Any]]]:
    """Briefing's entry point: items newest first, [] when the course is closed to students, None when unknown."""
    items, error = fetch_course_announcements(course_id)
    return None if error in ("auth", "unknown") else items


def fetch_term_starts(course_ids: List[str]) -> Dict[str, Optional[datetime]]:
    """Start of each course's academic term (from /terms), or None when unknown."""
    from concurrent.futures import ThreadPoolExecutor

    def term_id(cid: str) -> Optional[str]:
        try:
            return get_json(f"/learn/api/public/v1/courses/{cid}").get("termId")
        except RestError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        terms_future = pool.submit(get_pages, "/learn/api/public/v1/terms")
        id_futures = {cid: pool.submit(term_id, cid) for cid in course_ids}
        try:
            terms = {t["id"]: parse_dt(((t.get("availability") or {}).get("duration") or {}).get("start"))
                     for t in terms_future.result()}
        except RestError:
            terms = {}
        return {cid: terms.get(f.result()) for cid, f in id_futures.items()}


def mark_older_term(items: List[Dict[str, Any]], term_start: Optional[datetime]) -> None:
    """Set ``older_term`` (True/False, or None when the term start is unknown) on every item."""
    for item in items:
        created = parse_dt(item.get("created"))
        item["older_term"] = None if term_start is None or created is None else created < term_start


def reconcile_read_state(rest_rows: List[Dict[str, Any]], browser_rows: List[Dict[str, Any]]) -> int:
    """Copy browser-verified ``unread`` onto REST rows joined by title. Returns how many stay unknown.

    Only titles that appear exactly once on each side are joined; duplicates stay unknown (#38).
    """
    for row in rest_rows:
        same_rest = [r for r in rest_rows if r["title"] == row["title"]]
        same_browser = [b for b in browser_rows if b.get("title", "").strip() == row["title"]]
        if len(same_rest) == 1 and len(same_browser) == 1 and isinstance(same_browser[0].get("unread"), bool):
            row["unread"] = same_browser[0]["unread"]
    return sum(1 for r in rest_rows if r.get("unread") is None)


async def verify_read_state(
    rows_by_course: Dict[str, List[Dict[str, Any]]], headless: bool = True, cdp_url: Optional[str] = None
) -> int:
    """Read-only browser check of read state for the given REST rows (mutated in place).

    Returns how many rows are still unknown (courses that failed or titles that could not be joined).
    The browser is only started here, never on the REST happy path.
    """
    from core.async_engine import AsyncCourseWorkerPool, AsyncSessionManager, EngineConfig

    manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp_url))
    await manager.initialize()
    try:
        async def worker(cid: str, _name: str, page: Any) -> int:
            return reconcile_read_state(rows_by_course[cid], await scrape_announcements_playwright_async(cid, page))

        results = await AsyncCourseWorkerPool(manager).execute_task_per_course({c: c for c in rows_by_course}, worker)
    finally:
        await manager.close()
    return sum(r if isinstance(r, int) else len(rows_by_course[c]) for c, r in results.items())


def format_announcements(
    groups: List[Dict[str, Any]], now: Optional[datetime] = None, preview_chars: int = PREVIEW_CHARS
) -> str:
    """Per-course text: short label, newest first, local time + age, a clipped preview of each body."""
    now = now or datetime.now(timezone.utc)
    lines: List[str] = []
    for group in sorted(groups, key=lambda g: course_label(g["course_name"])):
        items = group["announcements"]
        total = group.get("total", len(items))
        count = f"{len(items)} of {total}" if total != len(items) else f"{len(items)}"
        lines.append(f"{course_label(group['course_name'])}  ({count} announcement{'s' if total != 1 else ''})")
        if group.get("error"):
            lines.append(f"  not available: {_ERROR_TEXT.get(group['error'], group['error'])}")
        elif not items:
            lines.append("  none")
        for item in items:
            created = parse_dt(item.get("created"))
            flags = ("  [unread]" if item.get("unread") is True else "") + \
                    ("  (older term)" if item.get("older_term") else "")
            lines.append(f"  {fmt_when(created, now.astimezone()) or 'no date':<26}{ago(created, now):>9}  {item['title']}{flags}")
            if item.get("body"):
                lines.append(f"      {clip(item['body'], preview_chars)}")
        lines.append("")
    return "\n".join(lines).rstrip()


_ERROR_TEXT = {
    "auth": "session expired, run `bb login`",
    "forbidden": "not visible to students",
    "not_found": "course not found",
    "unknown": "network or server error, try again",
}


async def scrape_announcements_playwright_async(course_id: str, page: Any) -> List[Dict[str, Any]]:
    """Playwright browser DOM fallback for course announcements."""
    from core.async_engine import AdaptiveDOM

    courses = load_courses()
    name = courses.get(course_id, course_id)
    status(f"Reading announcements for {course_label(name)} in the browser...")

    outline_url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/outline"
    try:
        await page.goto(outline_url, wait_until="domcontentloaded", timeout=20_000)
    except Exception as e:
        status(f"  could not load {course_label(name)}: {e}")
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
        status(f"  {course_label(name)} is unavailable or closed.")
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

    return announcements_data


async def scrape_announcements_async(course_id: str, page: Optional[Any] = None) -> List[Dict[str, Any]]:
    """
    Compatibility entry point for briefing, Telegram and the menubar.
    REST Fast-Path first; the browser is used only when the API is unreachable.
    """
    items, error = await asyncio.to_thread(fetch_course_announcements, course_id)
    if error is None:
        return items
    if error in ("forbidden", "not_found"):
        return []
    name = load_courses().get(course_id, course_id)
    status(f"Announcements API unavailable for {course_label(name)} ({error}); using the browser.")
    if page:
        return await scrape_announcements_playwright_async(course_id, page)
    from core.async_engine import AsyncSessionManager, EngineConfig
    session_manager = AsyncSessionManager(EngineConfig(headless=True))
    await session_manager.initialize()
    try:
        async with session_manager.acquire_page() as p:
            return await scrape_announcements_playwright_async(course_id, p)
    finally:
        await session_manager.close()


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
            state = {True: "[UNREAD] ", False: "[read] "}.get(ann.get("unread"), "")
            lines.append(f"## {state}{ann['title']}")
            if ann.get('meta'):
                lines.append(f"_{ann['meta']}_")
            lines.append("")

            formatted_body = ann['body'].replace('\n', '\n> ')
            if formatted_body:
                lines.append(f"> {formatted_body}")
            else:
                lines.append("> _(No content)_")
            lines.append("\n---")

    filepath.write_text("\n".join(lines))
