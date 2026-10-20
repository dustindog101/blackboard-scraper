"""Homepage activity stream: REST first (``/learn/api/v1/streams/ultra``), browser DOM only as fallback.

The stream endpoint is a POST, but it is a read-only "retrieve" call (``retrieveOnly``); nothing is
marked read, dismissed or changed.
"""
import asyncio
import html
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.config import BLACKBOARD_BASE, load_courses
from core.display import clip, fmt_when, parse_dt, short_course
from core.output import ensure_output_dir, status
from core.rest import RestError, post_json

logger = logging.getLogger("blackboard.scrapers.activity")

STREAM_PATH = "/learn/api/v1/streams/ultra"
STREAM_PROVIDERS = [
    "bb-nautilus", "bb_mygrades", "bb_disc", "bb_calendar", "bb-announcement", "bb_blog",
    "bb_Wiki", "bb_ach_stream", "bb_tel", "bb_deployment", "bb-evidence-collection",
]
MAX_PAGES = 4
DEFAULT_LIMIT = 40


def _snippet(raw_text: Optional[str]) -> str:
    """Strip HTML tags and entities from a stream snippet."""
    if not raw_text:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw_text))
    return re.sub(r"\s+", " ", text).strip()


def _grade_text(details: Dict[str, Any]) -> Optional[str]:
    score = details.get("displayGradeScore")
    if score is None:
        score = details.get("grade")
    points = details.get("pointsPossible")
    if score is None:
        return None
    try:
        if points:
            return f"{float(score):g}/{float(points):g} ({float(score) / float(points) * 100:.0f}%)"
        return f"{float(score):g}"
    except (TypeError, ValueError):
        return str(score)


def normalize_entry(entry: Dict[str, Any], courses: Dict[str, str], users: Dict[str, Any]) -> Dict[str, Any]:
    """One raw stream entry -> a flat record. Absent values are omitted, not blank."""
    provider = entry.get("providerId") or ""
    event = (entry.get("extraAttribs") or {}).get("event_type") or provider
    data = entry.get("itemSpecificData") or {}
    course_id = entry.get("se_courseId") or ""
    when = parse_dt(entry.get("se_timestamp"))
    due = parse_dt((data.get("notificationDetails") or {}).get("dueDate"))
    user = users.get(entry.get("se_userId") or "") or {}
    author = user.get("sx_name") if isinstance(user, dict) else None

    title = data.get("title") or ""
    message = _snippet(data.get("contentExtract"))
    category = "other"
    grade = None

    if provider == "bb_disc":
        category = "discussion"
        disc = data.get("discussionboardDetails") or {}
        title = disc.get("forumName") or disc.get("threadName") or "Discussion post"
        message = f"{author}: {message}" if author and message else (f"New response by {author}" if author else message)
    elif provider == "bb_mygrades" or event == "GB:GB_GRA_UPDATED":
        category = "grade"
        title = title or "Grade posted"
        details = data.get("gradeDetails") or {}
        grade = _grade_text(details)
        feedback = _snippet(((details.get("instructorFeedback") or {}).get("displayText")))
        message = " | ".join(p for p in (f"Feedback: {feedback}" if feedback else "",) if p)
    elif "AN_AVAIL" in str(event):
        category, title = "announcement", title or "Announcement"
    elif "CO_AVAIL" in str(event):
        category, title = "content", title or "New course content"
        message = ""
    elif "ATT_UPDATED" in str(event):
        category, title, message = "submission", title or "Submission updated", ""
    elif provider == "bb_blog":
        category, title = "blog", (data.get("blogDetails") or {}).get("blogTitle") or title or "Blog update"
    elif provider == "bb_Wiki":
        category, title = "wiki", (data.get("wikiDetails") or {}).get("wikiTitle") or title or "Wiki update"
    elif provider == "bb_ach_stream":
        category, title = "achievement", title or "Achievement earned"
    elif "CR_AVAIL" in str(event):
        category, title = "course", title or "Course available"
    elif "DUE" in str(event) or due is not None or data.get("assessmentStreamEntryDetails"):
        category, title = "due_date", title or "Due date"
    elif "GRA" in str(event):
        category = "grade"

    if category == "due_date":
        message = ""

    url = entry.get("se_rhs") or ""
    if url and not url.startswith("http"):
        url = f"{BLACKBOARD_BASE}{url}"
    record = {
        "id": entry.get("se_id"),
        "category": category,
        "course_id": course_id,
        "course": courses.get(course_id) or "General",
        "title": (title or "Course update").strip(),
        "date": fmt_when(when),
        "at": when.astimezone().isoformat() if when else None,
        "due_date": fmt_when(due),
        "due_at": due.astimezone().isoformat() if due else None,
        "grade": grade,
        "author": author,
        "message": message,
        "url": url,
    }
    return {k: v for k, v in record.items() if v not in (None, "")}


def tidy(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Newest-first list without repeats: identical entries merge, and a discussion thread's many
    responses collapse into one row (``count`` responses, newest message)."""
    items = sorted(items, key=lambda i: i.get("at") or "", reverse=True)
    graded = {(i["course_id"], i["title"]) for i in items if i["category"] == "grade" and i.get("grade")}
    out: List[Dict[str, Any]] = []
    seen: Dict[tuple, Dict[str, Any]] = {}
    for item in items:
        if item["category"] == "grade" and not item.get("grade") and (item.get("course_id"), item["title"]) in graded:
            continue
        key = (item["category"], item.get("course_id"), item["title"],
               "" if item["category"] == "discussion" else item.get("due_at", ""), item.get("grade", ""))
        if key in seen:
            if item["category"] == "discussion":
                seen[key]["count"] = seen[key].get("count", 1) + 1
            continue
        seen[key] = item
        out.append(item)
    return out


def fetch_activity_rest(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Newest-first activity from the Ultra stream endpoint. Raises RestError."""
    providers = {
        p: {"sp_provider": p, "sp_newest": -1, "sp_oldest": 9007199254740992, "sp_refreshDate": 0}
        for p in STREAM_PROVIDERS
    }
    raw: Dict[str, Dict[str, Any]] = {}
    courses: Dict[str, str] = dict(load_courses())
    users: Dict[str, Any] = {}

    for _ in range(MAX_PAGES):
        page = post_json(STREAM_PATH, {
            "providers": providers, "forOverview": False, "retrieveOnly": True, "flushCache": False,
        })
        extras = page.get("sv_extras") or {}
        for course in extras.get("sx_courses", []):
            if course.get("id") and course.get("name"):
                courses[course["id"]] = course["name"]
        for user in extras.get("sx_users", []):
            if user.get("sx_id"):
                users[user["sx_id"]] = user
        added = 0
        for entry in page.get("sv_streamEntries", []):
            if entry.get("se_id") and entry["se_id"] not in raw:
                raw[entry["se_id"]] = entry
                added += 1
        returned = page.get("sv_providers") or []
        if not page.get("sv_moreData") or not added or not returned:
            break
        providers = {p["sp_provider"]: p for p in returned}

    items = tidy([normalize_entry(e, courses, users) for e in raw.values()])
    return items[:limit] if limit else items


def format_activity(items: List[Dict[str, Any]], total: int, source: str = "REST") -> str:
    """Grouped-by-course text: newest first within a course, one line per item, no mid-word cuts."""
    if not items:
        return "No recent activity."
    now = datetime.now(timezone.utc).astimezone()
    by_course: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        by_course.setdefault(short_course(item.get("course")), []).append(item)
    shown = f"{len(items)} of {total}" if total > len(items) else str(len(items))
    lines = [f"Activity: {shown} items across {len(by_course)} course{'s' if len(by_course) != 1 else ''} ({source}), newest first"]
    for label in sorted(by_course, key=lambda c: (c == "General", c)):
        lines.append("")
        lines.append(f"{label}  ({len(by_course[label])})")
        for item in by_course[label]:
            when = fmt_when(parse_dt(item.get("at")), now) or item.get("date") or "no date"
            what = item.get("category", "other").replace("_", " ")
            extra = []
            if item.get("grade"):
                extra.append(item["grade"])
            if item.get("due_date"):
                extra.append(f"due {item['due_date']}")
            tail = f"  [{'; '.join(extra)}]" if extra else ""
            if item.get("count", 1) > 1:
                tail = f"  [{item['count']} responses]"
            lines.append(f"  {when:<26}{what:<14}{item['title']}{tail}")
            if item.get("message") and item["message"] != item["title"]:
                lines.append(f"      {clip(item['message'], 150)}")
    if total > len(items):
        lines.append("")
        lines.append(f"Showing the newest {len(items)}; use --limit N for more.")
    return "\n".join(lines)


async def scrape_activity_dom_async(page: Any) -> List[Dict[str, Any]]:
    """Browser DOM fallback: scrapes /ultra/stream when the REST stream endpoint is unavailable."""
    from core.async_engine import AdaptiveDOM

    url = f"{BLACKBOARD_BASE}/ultra/stream"

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except Exception as e:
        status(f"Could not open the activity stream: {e}")
        return []

    matched_sel, _ = await AdaptiveDOM.wait_for_any_selector(
        page,
        [".stream-item", ".empty-state", "div:has-text('No recent activity')"],
        timeout=12_000,
    )

    if not matched_sel or "empty-state" in matched_sel or "No recent activity" in matched_sel:
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

    return normalized


async def scrape_activity_async(page: Optional[Any] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Activity stream for briefing and the CLI. REST first; the browser only if REST is unavailable.
    Auth failures are not hidden behind a browser attempt: they raise RestError for the CLI to report.
    """
    try:
        return await asyncio.to_thread(fetch_activity_rest, limit)
    except RestError as exc:
        if exc.kind == "auth":
            raise
        status(f"Activity REST unavailable ({exc.kind}); reading the stream in the browser instead.")
    if page is not None:
        return await scrape_activity_dom_async(page)
    from core.async_engine import AsyncSessionManager, EngineConfig
    session_manager = AsyncSessionManager(EngineConfig(headless=True))
    await session_manager.initialize()
    try:
        async with session_manager.acquire_page() as p:
            return await scrape_activity_dom_async(p)
    finally:
        await session_manager.close()


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
    status(f"Saved activity to {filepath}")
