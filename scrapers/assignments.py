import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir
from core.async_engine import AdaptiveDOM
from core.gradebook import GradebookError, EXPIRED, FORBIDDEN, NOT_FOUND, UNAVAILABLE, MAX_CONNECTIONS, course_label, default_api_get, due_iso, is_calculated, item_status, status_text, student_records
from core.time import format_local, parse_datetime, to_iso
from scrapers.quiz import _api_get, _get_cookie_header, _clean_html_text

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger("blackboard.scrapers.assignments")



def scrape_course_assignments_http(course_id: str) -> Optional[List[Dict[str, Any]]]:
    """
    High-speed HTTP REST API scraper for course assignments and gradable items.
    Returns list of assignments in < 150ms without launching a browser.
    """
    cookie_header = _get_cookie_header()
    if not cookie_header:
        raise GradebookError(EXPIRED)

    # 1. Fetch gradebook columns
    cols = _api_get(f"/learn/api/public/v2/courses/{course_id}/gradebook/columns", cookie_header)
    if cols and cols.get("_http_status"):
        raise GradebookError({401: EXPIRED, 403: FORBIDDEN, 404: NOT_FOUND}.get(cols["_http_status"], UNAVAILABLE))
    if not cols or "results" not in cols:
        return None

    # Content details (one request per item) and the student's records are independent: fetch them together.
    # core.rest caps requests in flight (the server stalls above ~6).
    content_ids = [c["contentId"] for c in cols["results"] if c.get("contentId") and not is_calculated(c)]

    def _detail(content_id: str):
        return default_api_get(f"/learn/api/v1/courses/{course_id}/contents/{content_id}?expand=gradebookCategory", cookie_header)

    with ThreadPoolExecutor(max_workers=MAX_CONNECTIONS) as pool:
        records_future = pool.submit(student_records, course_id, cols["results"], cookie_header)
        details = dict(zip(content_ids, pool.map(_detail, content_ids)))
        status_inputs = records_future.result()
        if status_inputs.get("error"):
            raise GradebookError(status_inputs["error"])

    assignments: List[Dict[str, Any]] = []

    for c in cols["results"]:
        name = c.get("name", "")
        if is_calculated(c):  # totals are never items
            continue

        col_id = c.get("id")
        content_id = c.get("contentId")
        possible = c.get("score", {}).get("possible")
        handler = c.get("scoreProviderHandle", "")
        due_raw = c.get("dueDate") or (c.get("grading") or {}).get("due")

        item_type = "Assignment"
        if "forum" in handler or "discussion" in handler:
            item_type = "Discussion Board"
        elif "test" in handler or "assessment" in handler:
            item_type = "Quiz / Test"

        due_formatted = ""
        if due_raw:
            try:
                dt = datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
                due_formatted = dt.strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                due_formatted = due_raw

        instructions = ""
        is_timed = False
        attempts = ""

        # If content_id exists, inspect content detail
        if content_id:
            c_info = details.get(content_id)
            if c_info and "_http_status" not in c_info:
                cdetail = c_info.get("contentDetail", {})
                test_block = cdetail.get("resource/x-bb-asmt-test-link", {}).get("test", {})
                asmt_meta = test_block.get("assessment", {})
                dep_settings = test_block.get("deploymentSettings", {})

                subtype = asmt_meta.get("subtype") or test_block.get("deployedAssessmentType")
                if subtype:
                    if subtype.lower() == "assignment":
                        item_type = "Assignment"
                    elif subtype.lower() == "test":
                        item_type = "Quiz / Test"
                    else:
                        item_type = subtype

                raw_inst = (
                    asmt_meta.get("instructions", {}).get("rawText")
                    or asmt_meta.get("instructions", {}).get("displayText")
                    or asmt_meta.get("description", {}).get("rawText")
                    or asmt_meta.get("description", {}).get("displayText")
                    or ""
                )
                instructions = _clean_html_text(raw_inst)

                if dep_settings.get("timeLimit"):
                    is_timed = True

                raw_attempts = dep_settings.get("attemptCount")
                if raw_attempts == -1:
                    attempts = "Unlimited"
                elif raw_attempts is not None and raw_attempts > 0:
                    attempts = f"{raw_attempts} attempt{'s' if raw_attempts > 1 else ''}"

                if not due_formatted:
                    raw_c_due = c_info.get("genericReadOnlyData", {}).get("dueDate")
                    if raw_c_due:
                        try:
                            dt = datetime.fromisoformat(raw_c_due.replace("Z", "+00:00"))
                            due_formatted = dt.strftime("%Y-%m-%d %H:%M UTC")
                        except Exception:
                            due_formatted = raw_c_due

        # One shared status model (core.gradebook); `submission_status` keeps the raw gradebook value.
        records = status_inputs["records"]
        record = (records or {}).get(col_id)
        status = (record or {}).get("status") or "NOT_ATTEMPTED"
        model_status = item_status(c, record, status_inputs["posts"].get(col_id),
                                   status_inputs.get("attempts", {}).get(col_id),
                                   attempts_checked=col_id in status_inputs.get("attempts", {})) if records is not None else "unknown"

        assignments.append({
            "id": content_id or col_id,
            "content_id": content_id,
            "column_id": col_id,
            "title": name,
            "item_type": item_type,
            "due_date": due_formatted,
            "points_possible": f"{possible} points" if possible is not None else "",
            "due_at": due_iso(c) or to_iso(due_formatted),
            "submission_status": status,
            "status": model_status,
            "attempts": attempts,
            "is_timed_test": is_timed,
            "instructions": instructions,
            "attachments": [],
        })

    return assignments


async def scrape_course_assignments_async(
    course_id: str,
    page: Optional['Page'] = None,
    safe_only: bool = True,
) -> List[Dict[str, Any]]:
    """
    Scrapes detailed assignment records for a course.
    Uses HTTP REST Fast-Path primary (<150ms) with Playwright DOM crawler fallback.
    """
    # 1. Try HTTP REST Fast-Path first
    http_assignments = scrape_course_assignments_http(course_id)
    if http_assignments is not None:
        return http_assignments

    if page is None:
        return []

    # 2. Playwright Browser Fallback
    courses = load_courses()
    course_name = courses.get(course_id, course_id)

    url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/outline"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
    except Exception as e:
        logger.debug(f"Navigation error for {course_name}: {e}")
        return []

    matched_sel, _ = await AdaptiveDOM.wait_for_any_selector(
        page,
        [
            "div.course-outline-tree",
            "bb-course-outline",
            "div[role='tree']",
            "div:has-text(\"You can't access this course right now\")",
            "div:has-text(\"Course is not currently available\")",
        ],
        timeout=10_000,
    )

    if not matched_sel or "You can't access" in matched_sel or "not currently available" in matched_sel:
        return []

    expand_buttons = page.locator("button[aria-expanded='false']")
    count = await expand_buttons.count()
    for idx in range(min(count, 8)):
        try:
            btn = expand_buttons.nth(idx)
            if await btn.is_visible():
                await btn.click(timeout=1500)
                await asyncio.sleep(0.1)
        except Exception:
            continue

    assessment_links = await page.evaluate("""() => {
        const results = [];
        const seen = new Set();

        document.querySelectorAll('bb-content-item, div[role="treeitem"], div.element-details, li.item, li.clearfix').forEach(el => {
            const html = el.outerHTML.toLowerCase();
            const isAssessment = html.includes('assignment') || html.includes('assessment') ||
                                 html.includes('quiz') || html.includes('test') || html.includes('duedate') ||
                                 html.includes('project') || html.includes('homework') || html.includes('lab');
            if (!isAssessment) return;

            const linkEl = el.querySelector('a, button, [role="button"], span.title');
            const title = el.querySelector('h3, h4, span.title, [class*="itemName"]')?.innerText.trim() ||
                          linkEl?.innerText.trim() || el.innerText.trim().split('\\n')[0];
            const analyticsId = el.getAttribute('data-analytics-id') || el.getAttribute('data-content-id') || el.id || title;

            if (title && !seen.has(analyticsId)) {
                seen.add(analyticsId);
                let dueDate = '';
                const dueEl = el.querySelector('[class*="dueDate"], [class*="due-date"]');
                if (dueEl) {
                    dueDate = dueEl.innerText.replace(/due\\s*date[:\\s]*/i, '').trim();
                }

                results.push({
                    title: title,
                    content_id: analyticsId,
                    due_date: dueDate,
                });
            }
        });
        return results;
    }""")

    if not assessment_links:
        return []

    assignments: List[Dict[str, Any]] = []

    for item in assessment_links[:12]:
        title = item["title"]
        try:
            item_locator = page.locator(f"text={title}").first
            if not await item_locator.is_visible():
                item_locator = page.locator(f"[data-analytics-id='{item['content_id']}']").first

            if not await item_locator.is_visible():
                continue

            await item_locator.click(timeout=3000)

            drawer_sel, _ = await AdaptiveDOM.wait_for_any_selector(
                page,
                [
                    "bb-drawer",
                    "aside[role='dialog']",
                    "div.panel-content",
                    "div[class*='assessmentDetails']",
                    ".time-limit-warning",
                ],
                timeout=5000,
            )

            if not drawer_sel:
                assignments.append({
                    "id": item.get("content_id", ""),
                    "content_id": item.get("content_id", ""),
                    "title": title,
                    "item_type": "Assignment",
                    "due_date": item.get("due_date", ""),
                    "points_possible": "",
                    "submission_status": "Unattempted",
                    "instructions": "",
                    "attachments": [],
                })
                continue

            drawer_data = await page.evaluate("""() => {
                const drawer = document.querySelector('bb-drawer, aside[role="dialog"], div.panel-content') || document.body;

                let points = '';
                const pointsEl = drawer.querySelector('[data-analytics-id*="points"], [class*="pointsPossible"], [class*="score-pill"]');
                if (pointsEl) points = pointsEl.innerText.trim();

                let due = '';
                const dueEl = drawer.querySelector('[data-analytics-id*="due-date"], [class*="dueDate"], .due-date-value');
                if (dueEl) due = dueEl.innerText.replace(/due\\s*date[:\\s]*/i, '').trim();

                let status = 'Unattempted';
                let attempts = '';
                const attemptsEl = drawer.querySelector('[class*="attemptsDetail"], [data-analytics-id*="attempts"]');
                if (attemptsEl) attempts = attemptsEl.innerText.trim();

                const isTimed = !!drawer.querySelector('.time-limit-warning, [data-analytics-id*="time-limit"], span:has-text("time limit")');

                let instructions = '';
                const instEl = drawer.querySelector('bb-rich-text-viewer, div.details-instructions, [class*="assessmentDescription"]');
                if (instEl) instructions = instEl.innerText.trim();

                const attachments = [];
                drawer.querySelectorAll('a[data-analytics-id*="file-download"], bb-attachment-item a, a[href*="bbcswebdav"]').forEach(a => {
                    const fname = a.innerText.trim();
                    const url = a.href;
                    if (fname && url) {
                        attachments.push({ filename: fname, url: url });
                    }
                });

                return {
                    points,
                    due,
                    status,
                    attempts,
                    is_timed: isTimed,
                    instructions,
                    attachments
                };
            }""")

            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
            close_btn = page.locator("button[analytics-id*='closeDrawer'], button[aria-label='Close'], button.bb-close-button").first
            if await close_btn.is_visible():
                await close_btn.click(timeout=1500)
                await asyncio.sleep(0.2)

            assignments.append({
                "id": item.get("content_id", ""),
                "content_id": item.get("content_id", ""),
                "title": title,
                "item_type": "Quiz / Test" if drawer_data.get("is_timed") else "Assignment",
                "due_date": drawer_data.get("due") or item.get("due_date", ""),
                "points_possible": drawer_data.get("points", ""),
                "submission_status": drawer_data.get("status", "Unattempted"),
                "attempts": drawer_data.get("attempts", ""),
                "is_timed_test": drawer_data.get("is_timed", False),
                "instructions": drawer_data.get("instructions", ""),
                "attachments": drawer_data.get("attachments", []),
            })

        except Exception as e:
            logger.debug(f"Could not read drawer for {title}: {e}")
            await page.keyboard.press("Escape")
            continue

    return assignments


def format_assignments_summary(assignments: List[Dict[str, Any]], course_name: str, course_id: str = "") -> str:
    """One aligned table per course: due time (local, with zone), type, points, status, title."""
    from core.display import align, clip

    count = len(assignments)
    lines = [f"{course_label(course_name)}  ({count} item{'s' if count != 1 else ''})"]
    if not assignments:
        lines.append("  none found (or the course is closed)")
        return "\n".join(lines)

    far = datetime.max.replace(tzinfo=timezone.utc)
    ordered = sorted(assignments, key=lambda a: parse_datetime(a.get("due_at")) or far)
    rows = []
    for a in ordered:
        due_dt = parse_datetime(a.get("due_at"))
        due = format_local(due_dt) if due_dt else "no due date"
        points = str(a.get("points_possible") or "").replace(" points", "").replace(".0", "")
        timed = " (timed)" if a.get("is_timed_test") else ""
        rows.append((due, a.get("item_type", "Assignment"), points, status_text(a.get("status", "unknown")), clip(a["title"], 60) + timed))
    lines.extend(align(rows))
    return "\n".join(lines)


def save_assignments(assignments: List[Dict[str, Any]], course_id: str) -> Path:
    """Saves assignments markdown report to output/assignments/<course_id>.md."""
    out_dir = ensure_output_dir("assignments")
    filepath = out_dir / f"{course_id}.md"

    courses = load_courses()
    course_name = courses.get(course_id, course_id)

    lines = [
        f"# Assignments: {course_name}",
        f"_Course ID: {course_id}_",
        f"_Scraped: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "", "---", ""
    ]

    if not assignments:
        lines.append("_No assignments found or course is unavailable._")
    else:
        for a in assignments:
            timed_badge = " ⏱️ [TIMED TEST]" if a.get("is_timed_test") else ""
            item_type = a.get("item_type", "Assignment")
            item_id = a.get("id") or a.get("content_id") or ""
            lines.append(f"## 📝 {a['title']} [{item_type}]{timed_badge}")
            if item_id:
                lines.append(f"**ID:** `{item_id}`")
            if a.get("due_at") or a.get("due_date"):
                due = format_local(parse_datetime(a.get("due_at"))) if a.get("due_at") else a["due_date"]
                lines.append(f"**Due Date:** `{due}`")
            if a.get("points_possible"):
                lines.append(f"**Points:** {a['points_possible']}")
            if a.get("attempts"):
                lines.append(f"**Attempts:** {a['attempts']}")
            if a.get("status") or a.get("submission_status"):
                lines.append(f"**Status:** `{a['status'].replace('_', ' ') if a.get('status') else a['submission_status']}`")

            if a.get("instructions"):
                lines.append("\n### Instructions / Prompt:")
                lines.append(f"> {a['instructions'].replace(chr(10), chr(10) + '> ')}")

            if a.get("attachments"):
                lines.append("\n### Attached Files:")
                for att in a["attachments"]:
                    lines.append(f"- 📎 [{att['filename']}]({att['url']})")

            lines.append("\n---")

    filepath.write_text("\n".join(lines))
    return filepath
