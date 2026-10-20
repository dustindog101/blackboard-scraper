"""Gradebook command: per-course overall grade plus Gradable Items, from the shared status model.

REST only on the happy path (`core.gradebook`); the browser scraper is used only for courses whose REST
call failed for a reason other than an expired session, and its session is created lazily.
"""
import asyncio
import logging
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.config import BLACKBOARD_BASE, load_courses
from core.async_engine import AdaptiveDOM
from core.gradebook import (
    EXPIRED, GRADED, NOT_SUBMITTED, UNAVAILABLE, UNKNOWN, SUBMITTED, EXEMPT,
    course_label, course_title, fetch_gradebooks, public_view, status_text,
)
from core.output import ensure_output_dir
from core.time import format_local, parse_datetime, to_iso

logger = logging.getLogger("blackboard.scrapers.grades")

EXPIRED_MESSAGE = "Session expired — run `bb login`"


class SessionExpired(Exception):
    pass


def _cookie() -> Optional[str]:
    from scrapers.quiz import _get_cookie_header
    return _get_cookie_header()


# ---------------------------------------------------------------------------
# Browser fallback (only for courses REST could not answer)
# ---------------------------------------------------------------------------

_UI_STATUS = (
    # Browser rows do not expose the student's attempt state; Needs Grading alone does not prove submission.
    ("needs grading", UNKNOWN), ("not submitted", NOT_SUBMITTED), ("missing", NOT_SUBMITTED),
    ("submitted", SUBMITTED), ("graded", GRADED), ("exempt", EXEMPT),
)


def _rows_to_gradebook(course_id: str, course_name: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Shape browser-scraped rows like a REST gradebook. No overall: the browser rows carry no totals."""
    items = []
    for row in rows:
        text = (row.get("status") or "").strip().lower()
        status = next((v for k, v in _UI_STATUS if k in text), UNKNOWN)
        nums = re.findall(r"\d+(?:\.\d+)?", row.get("grade") or "")
        score = float(nums[0]) if nums and status == GRADED else None
        possible = float(nums[1]) if len(nums) > 1 else None
        items.append({
            "name": row.get("name", "Untitled"), "category": "other", "due_at": to_iso(row.get("dueDate")),
            "status": status, "score": score, "possible": possible,
            "percent": round(score / possible * 100, 1) if score is not None and possible else None,
            "feedback": None, "_counts": False, "_column_id": None, "_content_id": None,
        })
    overall = {"source": "none", "label": None, "score": None, "possible": None, "percent": None,
               "letter": None, "graded": sum(i["status"] == GRADED for i in items), "total": len(items)}
    return {"course_id": course_id, "course_name": course_name, "overall": overall, "items": items, "excluded": []}


async def scrape_grades_playwright_async(course_id: str, page: Any) -> List[Dict[str, Any]]:
    """Playwright browser DOM fallback for grades table scraping."""
    url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/grades"
    courses = load_courses()
    name = courses.get(course_id, course_id)
    print(f"Scraping grades for {name} (Playwright fallback)...", file=sys.stderr)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except Exception as e:
        print(f"Navigation error for {name}: {e}", file=sys.stderr)
        return []

    # Wait for React/MUI table or empty state
    matched_sel, _ = await AdaptiveDOM.wait_for_any_selector(
        page,
        [
            "[data-testid^='course-student-grades-table-row']",
            "table[class*='grades-table']",
            "div:has-text(\"You can't access this course right now\")",
            "div:has-text('No grades posted')",
        ],
        timeout=10_000,
    )

    if not matched_sel or "You can't access" in matched_sel:
        print("Course is currently unavailable or closed.", file=sys.stderr)
        return []

    grades: List[Dict[str, Any]] = []

    # Extract current page items
    page_num = 1
    while True:
        items = await page.evaluate("""() => {
            const results = [];
            const rows = document.querySelectorAll('[data-testid^="course-student-grades-table-row"]');
            rows.forEach(row => {
                const cells = row.querySelectorAll('td');
                if (cells.length < 4) return;

                const nameContainer = cells[0].querySelector('[class*="itemNameContainer"], [class*="titleContainer"]');
                const name = nameContainer
                    ? nameContainer.innerText.trim().split('\\n')[0]
                    : cells[0].innerText.trim().split('\\n')[0];

                const dueDate = cells[1] ? cells[1].innerText.trim().replace(/\\n+/g, ' ') : '';
                const status = cells[2] ? cells[2].innerText.trim().replace(/\\n+/g, ' ') : '';

                const gradeCell = cells[3];
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

        # Check for Next Page button
        next_btn = page.locator('[aria-label="Next Page"]').first
        count = await next_btn.count()
        if count > 0 and await next_btn.is_visible() and not await next_btn.is_disabled():
            try:
                await next_btn.click()
                await asyncio.sleep(0.8)
                page_num += 1
            except Exception:
                break
        else:
            break

    print(f"Extracted {len(grades)} gradebook items from {name}.", file=sys.stderr)
    return grades


async def _browser_fallback(failed: Dict[str, str], page: Optional[Any]) -> Dict[str, Dict[str, Any]]:
    print(f"HTTP Gradebook API unavailable for {len(failed)} course(s); falling back to Playwright browser scraper...", file=sys.stderr)

    async def run(p: Any) -> Dict[str, Dict[str, Any]]:
        out = {}
        for cid, name in failed.items():
            out[cid] = _rows_to_gradebook(cid, name, await scrape_grades_playwright_async(cid, p))
        return out

    if page is not None:
        return await run(page)
    from core.async_engine import AsyncSessionManager, EngineConfig
    manager = AsyncSessionManager(EngineConfig(headless=True))
    await manager.initialize()
    try:
        async with manager.acquire_page() as p:
            return await run(p)
    finally:
        await manager.close()


async def fetch_gradebooks_async(courses: Dict[str, str], page: Optional[Any] = None) -> Dict[str, Dict[str, Any]]:
    """Gradebooks for all `courses`, concurrently, keyed by course id.

    Raises SessionExpired on a 401. Courses closed or hidden from students (403/404) come back as `{"error": ...}`.
    """
    cookie = _cookie()
    if not cookie:
        raise SessionExpired(EXPIRED_MESSAGE)
    results = await asyncio.to_thread(fetch_gradebooks, courses, cookie)
    if any(r.get("error") == EXPIRED for r in results.values()):
        raise SessionExpired(EXPIRED_MESSAGE)
    failed = {cid: courses[cid] for cid, r in results.items() if r.get("error") == UNAVAILABLE}
    if failed:
        results.update(await _browser_fallback(failed, page))
    return results


async def scrape_grades_async(course_id: str, page: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Legacy per-course list (Telegram, menubar): `name`, `dueDate`, `status`, `grade`, plus the new status fields."""
    name = load_courses().get(course_id, course_id)
    book = (await fetch_gradebooks_async({course_id: name}, page)).get(course_id, {})
    if book.get("error"):
        return []
    return [{
        "name": i["name"],
        "dueDate": format_local(parse_datetime(i["due_at"])) if i["due_at"] else "",
        "status": status_text(i["status"], i),
        "grade": f"{_num(i['score'])} / {_num(i['possible'])}" if i["score"] is not None and i["possible"] else "",
        "points_possible": i["possible"],
        "due_at": i["due_at"],
        "status_code": i["status"],
    } for i in book["items"]]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _num(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.2f}".rstrip("0").rstrip(".")


def _points(score: Optional[float], possible: Optional[float]) -> str:
    if score is not None and possible is not None:
        return f"{_num(score)} / {_num(possible)} pts"
    if possible is not None:
        return f"{_num(possible)} pts possible"
    if score is not None:
        return f"{_num(score)} pts earned; total unavailable"
    return "point total unavailable"


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def overall_line(overall: Dict[str, Any]) -> str:
    """`85 / 100 pts (official: Overall Grade) · 7 of 9 graded`."""
    counts = f"{overall['graded']} of {overall['total']} graded"
    source = overall["source"]
    if overall.get("unavailable"):
        return "grades unavailable (partial data)"
    if source == "none":
        return f"no grade yet · {counts}"
    points = _points(overall.get("score"), overall.get("possible"))
    label = f"official: {overall['label']}" if source == "official" and overall["label"] else source
    if source == "calculated":
        label = "calculated from graded points"
    return f"{points}  ({label}) · {counts}"


def _item_row(i: Dict[str, Any]) -> tuple:
    due = format_local(parse_datetime(i["due_at"])) if i["due_at"] else "no due date"
    return (due, _clip(i["name"], 40), _points(i.get("score"), i.get("possible")), status_text(i["status"], i))


def summary_lines(pairs: List[tuple]) -> List[str]:
    """One aligned line per course: `ECON 122  85 / 100 pts  official  22 of 31 graded`."""
    cells = []
    for label, o in pairs:
        if o.get("unavailable"):
            value, source = "grades unavailable", "partial"
        elif o["source"] == "none":
            value, source = "no grade yet", ""
        else:
            value = _points(o.get("score"), o.get("possible"))
            source = o["source"]
        cells.append((label, value, source, f"{o['graded']} of {o['total']} graded"))
    widths = [max(len(c[i]) for c in cells) for i in range(3)]
    return [f"{a:<{widths[0]}}  {b:<{widths[1]}}  {c:<{widths[2]}}  {d}".rstrip() for a, b, c, d in cells]


def format_grades_summary(books: List[Dict[str, Any]]) -> str:
    return "\n".join(summary_lines([(course_label(b["course_name"]), b["overall"]) for b in books]))


def format_grades_text(books: List[Dict[str, Any]], summary: bool = False, summary_only: bool = False) -> str:
    """Per-course detail with column widths shared across every course; optional summary table first."""
    if not books:
        return "No grades found."
    out: List[str] = []
    if summary or summary_only:
        out += ["GRADES", ""] + ["  " + line for line in format_grades_summary(books).splitlines()]
        if summary_only:
            return "\n".join(out)
        out.append("")
    all_rows = [_item_row(i) for b in books for i in b["items"]]
    widths = [max((len(r[c]) for r in all_rows), default=0) for c in range(4)]
    for book in books:
        out.append(f"{course_label(book['course_name'])}  {course_title(book['course_name'])}".rstrip())
        out.append(f"  {overall_line(book['overall'])}")
        items = book["items"]
        if not items:
            out += ["  (no gradebook items)", ""]
            continue
        for due, name, score, status in (_item_row(i) for i in items):
            out.append(f"  {due:<{widths[0]}}  {name:<{widths[1]}}  {score:>{widths[2]}}  {status}".rstrip())
        notes = []
        with_feedback = sum(1 for i in items if i["feedback"])
        if with_feedback:
            notes.append(f"{with_feedback} with instructor feedback (see --json)")
        if book["excluded"]:
            notes.append("not counted: " + ", ".join(f"{e['name']} ({e['reason']})" for e in book["excluded"][:3])
                         + (f" +{len(book['excluded']) - 3} more" if len(book["excluded"]) > 3 else ""))
        out += [f"  {n}" for n in notes] + [""]
    return "\n".join(out).rstrip()


def grades_json(books: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [public_view(b) for b in books]


def save_grades(book: Dict[str, Any], course_id: str):
    out_dir = ensure_output_dir("grades")
    filepath = out_dir / f"{course_id}.md"
    lines = [
        f"# Grades: {book['course_name']}",
        f"_Scraped: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "", f"**Overall:** {overall_line(book['overall'])}", "",
        "| Due | Item | Points | Status |", "|---|---|---|---|",
    ]
    for i in book["items"]:
        due = format_local(parse_datetime(i["due_at"])) if i["due_at"] else "no due date"
        points = _points(i.get("score"), i.get("possible"))
        lines.append(f"| {due} | {i['name'].replace('|', '-')} | {points} | {status_text(i['status'], i)} |")
    filepath.write_text("\n".join(lines))
    print(f"Saved to: {filepath.name}", file=sys.stderr)
