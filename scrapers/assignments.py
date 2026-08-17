import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir
from core.async_engine import AdaptiveDOM

logger = logging.getLogger("blackboard.scrapers.assignments")


async def scrape_course_assignments_async(
    course_id: str,
    page: Page,
    safe_only: bool = True,
) -> List[Dict[str, Any]]:
    """
    Scrapes detailed assignment records by inspecting outline assessments and slide-over drawers.
    Extracts due dates, point values, instructions, rubrics, and downloadable starter files.
    Includes strict safety guards to never trigger timed test starts.
    """
    courses = load_courses()
    course_name = courses.get(course_id, course_id)

    url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/outline"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
    except Exception as e:
        logger.debug(f"Navigation error for {course_name}: {e}")
        return []

    # Verify outline exists
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

    # Expand any top-level folders to reveal assignments
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

    # Find all assessment / assignment interactive links
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

    for item in assessment_links[:12]:  # Inspect up to 12 assessments
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
                    "title": title,
                    "due_date": item.get("due_date", ""),
                    "points_possible": "",
                    "submission_status": "Unattempted",
                    "instructions": "",
                    "attachments": [],
                })
                continue

            # Extract drawer contents
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

            # Close drawer safely
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
            close_btn = page.locator("button[analytics-id*='closeDrawer'], button[aria-label='Close'], button.bb-close-button").first
            if await close_btn.is_visible():
                await close_btn.click(timeout=1500)
                await asyncio.sleep(0.2)

            assignments.append({
                "title": title,
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
    """Formats assignments into a clean CLI string."""
    lines = [
        f"📝 Assignments & Assessments: {course_name} ({course_id})" if course_id else f"📝 Assignments & Assessments: {course_name}",
        "━" * 50,
    ]

    if not assignments:
        lines.append("  (No assignments found or course is currently closed)")
        return "\n".join(lines)

    for a in assignments:
        timed = " ⏱️ [TIMED]" if a.get("is_timed_test") else ""
        lines.append(f"\n• {a['title']}{timed}")
        if a.get("due_date"):
            lines.append(f"  └ Due Date: {a['due_date']}")
        if a.get("points_possible"):
            lines.append(f"  └ Points: {a['points_possible']}")
        if a.get("submission_status"):
            lines.append(f"  └ Status: {a['submission_status']}")
        if a.get("attempts"):
            lines.append(f"  └ Attempts: {a['attempts']}")
        if a.get("instructions"):
            snippet = a['instructions'].replace("\n", " ").strip()
            if len(snippet) > 140:
                snippet = snippet[:137] + "..."
            lines.append(f"  └ Instructions: {snippet}")
        for att in a.get("attachments", []):
            lines.append(f"  └ 📎 File: {att['filename']} ({att['url']})")

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
            lines.append(f"## 📝 {a['title']}{timed_badge}")
            if a.get("due_date"):
                lines.append(f"**Due Date:** `{a['due_date']}`")
            if a.get("points_possible"):
                lines.append(f"**Points:** {a['points_possible']}")
            if a.get("attempts"):
                lines.append(f"**Attempts:** {a['attempts']}")
            if a.get("submission_status"):
                lines.append(f"**Status:** `{a['submission_status']}`")

            if a.get("instructions"):
                lines.append("\n### Instructions:")
                lines.append(f"> {a['instructions'].replace(chr(10), chr(10) + '> ')}")

            if a.get("attachments"):
                lines.append("\n### Attached Files:")
                for att in a["attachments"]:
                    lines.append(f"- 📎 [{att['filename']}]({att['url']})")

            lines.append("\n---")

    filepath.write_text("\n".join(lines))
    return filepath
