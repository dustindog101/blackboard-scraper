import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from playwright.async_api import Page

from core.config import BLACKBOARD_BASE, SESSION_DIR, load_courses
from core.output import ensure_output_dir
from core.async_engine import AdaptiveDOM
from scrapers.quiz import _api_get, _get_cookie_header, _clean_html_text

logger = logging.getLogger("blackboard.scrapers.assignments")


def scrape_course_assignments_http(course_id: str) -> Optional[List[Dict[str, Any]]]:
    """
    High-speed HTTP REST API scraper for course assignments and gradable items.
    Returns list of assignments in < 150ms without launching a browser.
    """
    cookie_header = _get_cookie_header()
    if not cookie_header:
        return None

    # 1. Fetch gradebook columns
    cols = _api_get(f"/learn/api/public/v2/courses/{course_id}/gradebook/columns", cookie_header)
    if not cols or "results" not in cols:
        return None

    me = _api_get("/learn/api/v1/users/me", cookie_header)
    user_id = me.get("id") if me and "_http_status" not in me else None

    assignments: List[Dict[str, Any]] = []

    for c in cols["results"]:
        name = c.get("name", "")
        # Skip total score rollups
        if name in ("Overall Grade", "Weighted Total", "Total"):
            continue

        col_id = c.get("id")
        content_id = c.get("contentId")
        possible = c.get("score", {}).get("possible")
        handler = c.get("scoreProviderHandle", "")
        due_raw = c.get("dueDate")

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
            c_info = _api_get(f"/learn/api/v1/courses/{course_id}/contents/{content_id}?expand=gradebookCategory", cookie_header)
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

        # Check user grade / attempt status
        status = "NOT_ATTEMPTED"
        if user_id and col_id:
            grades = _api_get(f"/learn/api/v1/courses/{course_id}/gradebook/columns/{col_id}/grades?userId={user_id}", cookie_header)
            if grades and "results" in grades and len(grades["results"]) > 0:
                g = grades["results"][0]
                status = g.get("status", status)

        assignments.append({
            "id": content_id or col_id,
            "content_id": content_id,
            "column_id": col_id,
            "title": name,
            "item_type": item_type,
            "due_date": due_formatted,
            "points_possible": f"{possible} points" if possible is not None else "",
            "submission_status": status,
            "attempts": attempts,
            "is_timed_test": is_timed,
            "instructions": instructions,
            "attachments": [],
        })

    return assignments


async def scrape_course_assignments_async(
    course_id: str,
    page: Optional[Page] = None,
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
    """Formats assignments into a rich, structured CLI string with IDs and quick inspect hints."""
    header = f"📝 Assignments & Assessments: {course_name} ({course_id})" if course_id else f"📝 Assignments & Assessments: {course_name}"
    lines = [
        header,
        "━" * len(header),
    ]

    if not assignments:
        lines.append("  (No assignments found or course is currently closed)")
        return "\n".join(lines)

    for idx, a in enumerate(assignments, 1):
        item_type = a.get("item_type", "Assignment")
        timed = " ⏱️ [TIMED]" if a.get("is_timed_test") else ""
        item_id = a.get("id") or a.get("content_id") or a.get("column_id") or ""
        
        lines.append(f"\n{idx}. [{item_type}] {a['title']}{timed}")
        if item_id:
            lines.append(f"   ├ 🆔 Unique ID: {item_id}")
        if a.get("due_date"):
            lines.append(f"   ├ ⏰ Due Date:  {a['due_date']}")
        if a.get("points_possible"):
            lines.append(f"   ├ 🎯 Points:    {a['points_possible']}")
        if a.get("submission_status"):
            lines.append(f"   ├ 📊 Status:    {a['submission_status']}")
        if a.get("attempts"):
            lines.append(f"   ├ 🔄 Attempts:  {a['attempts']}")
        if a.get("instructions"):
            snippet = a['instructions'].replace("\n", " ").strip()
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            lines.append(f"   ├ 📖 Prompt:    {snippet}")
        for att in a.get("attachments", []):
            lines.append(f"   ├ 📎 File:      {att['filename']} ({att['url']})")
        if item_id:
            lines.append(f"   └ 💡 Inspect:   bb --assignment {item_id}")

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
            if a.get("due_date"):
                lines.append(f"**Due Date:** `{a['due_date']}`")
            if a.get("points_possible"):
                lines.append(f"**Points:** {a['points_possible']}")
            if a.get("attempts"):
                lines.append(f"**Attempts:** {a['attempts']}")
            if a.get("submission_status"):
                lines.append(f"**Status:** `{a['submission_status']}`")

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
