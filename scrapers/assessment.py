"""
Assessment & Quiz Inspector Engine
Provides high-speed REST Fast-Path inspection of Blackboard Ultra assessments,
quizzes, tests, and student attempt rules (<150ms), with encapsulated Playwright fallback.
Guarantees read-only safety without clicking start triggers or starting timers.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from playwright.async_api import Page

from core.config import BLACKBOARD_BASE, SESSION_DIR, load_courses
from core.async_engine import AdaptiveDOM, AsyncSessionManager, EngineConfig
from scrapers.outline import get_cookie_header, _api_get

logger = logging.getLogger("blackboard.scrapers.assessment")


def _format_elapsed_time(minutes: int) -> str:
    """Converts elapsed minutes into human-readable duration."""
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    rem_mins = minutes % 60
    if hours < 24:
        return f"{hours}h {rem_mins}m ago" if rem_mins else f"{hours}h ago"
    days = hours // 24
    rem_hours = hours % 24
    return f"{days}d {rem_hours}h ago" if rem_hours else f"{days}d ago"


def resolve_assessment_course(
    content_id: str,
    courses: Dict[str, str],
    cookie_header: str,
) -> Optional[str]:
    """
    Auto-discovers the owning course ID for a given content_id
    by probing active configured courses.
    """
    for cid in courses.keys():
        url = f"{BLACKBOARD_BASE}/learn/api/public/v1/courses/{cid}/contents/{content_id}"
        data = _api_get(url, cookie_header, timeout=3.5)
        if data and not data.get("_http_status") and data.get("id") == content_id:
            return cid
    return None


def inspect_assessment_api(
    content_id: str,
    course_id: Optional[str] = None,
    courses: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    High-speed REST Fast-Path assessment inspection (<150ms).
    Retrieves full metadata, grading policy, attempt limits, scoring model,
    proctoring requirements, and student attempt history.
    """
    cookie_header = get_cookie_header()
    if not cookie_header:
        logger.debug("No active cookie header found for assessment inspection.")
        return None

    if courses is None:
        courses = load_courses()

    # 1. Auto-resolve parent course if not provided
    if not course_id:
        course_id = resolve_assessment_course(content_id, courses, cookie_header)
        if not course_id:
            logger.debug(f"Could not auto-resolve course for content_id {content_id}")
            return None

    course_name = courses.get(course_id, course_id)

    # 2. Fetch Content Item definition
    content_url = f"{BLACKBOARD_BASE}/learn/api/public/v1/courses/{course_id}/contents/{content_id}"
    content_data = _api_get(content_url, cookie_header)
    if not content_data or content_data.get("_http_status"):
        return content_data  # Pass through error dict for fallback handling

    title = content_data.get("title", "Untitled Assessment")
    description = content_data.get("description", "").strip()
    handler = content_data.get("contentHandler", {})
    handler_id = handler.get("id", "")
    assessment_id = handler.get("assessmentId", "")
    grade_col_id = handler.get("gradeColumnId", "")

    # Proctoring & Password
    proctoring = handler.get("proctoring", {})
    is_proctored = bool(
        proctoring.get("secureBrowserRequiredToTake")
        or proctoring.get("webcamRequired")
        or handler.get("password", {}).get("enabled")
    )
    is_late_disallowed = bool(handler.get("isLateAttemptCreationDisallowed", False))

    points_possible = 0.0
    attempts_allowed = 1
    scoring_model = "Last"
    due_date = ""
    time_limit_minutes = handler.get("timeLimit") or 0

    # 3. Fetch Gradebook Column details if available
    attempts_list: List[Dict[str, Any]] = []
    if grade_col_id:
        col_url = f"{BLACKBOARD_BASE}/learn/api/public/v1/courses/{course_id}/gradebook/columns/{grade_col_id}"
        col_data = _api_get(col_url, cookie_header)
        if col_data and not col_data.get("_http_status"):
            points_possible = col_data.get("score", {}).get("possible", 0.0)
            grading_info = col_data.get("grading", {})
            attempts_allowed = grading_info.get("attemptsAllowed", 1)
            scoring_model = grading_info.get("scoringModel", "Last")
            due_date = grading_info.get("due", "")
            if not time_limit_minutes:
                time_limit_minutes = grading_info.get("timeLimit", 0)

        # 4. Fetch Student Attempts
        att_url = f"{BLACKBOARD_BASE}/learn/api/public/v1/courses/{course_id}/gradebook/columns/{grade_col_id}/attempts"
        att_data = _api_get(att_url, cookie_header)
        if att_data and not att_data.get("_http_status"):
            now = datetime.now(timezone.utc)
            for attempt_raw in att_data.get("results", []):
                created_str = attempt_raw.get("created", "")
                elapsed_min = 0
                if created_str:
                    try:
                        c_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                        elapsed_min = int((now - c_dt).total_seconds() / 60)
                    except Exception:
                        pass

                is_overdue = bool(time_limit_minutes and elapsed_min > time_limit_minutes and attempt_raw.get("status") == "InProgress")

                attempts_list.append({
                    "attempt_id": attempt_raw.get("id"),
                    "status": attempt_raw.get("status", "Unknown"),
                    "created": created_str,
                    "submitted": attempt_raw.get("attemptDate"),
                    "elapsed_minutes": elapsed_min,
                    "is_overdue": is_overdue,
                    "score": attempt_raw.get("score"),
                })

    is_timed = bool(time_limit_minutes > 0)
    launcher_url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/outline/assessment/{content_id}/overview"

    return {
        "content_id": content_id,
        "assessment_id": assessment_id,
        "grade_column_id": grade_col_id,
        "course_id": course_id,
        "course_name": course_name,
        "title": title,
        "description": description,
        "content_handler": handler_id,
        "points_possible": points_possible,
        "attempts_allowed": attempts_allowed,
        "attempts_used": len(attempts_list),
        "scoring_model": scoring_model,
        "is_timed": is_timed,
        "time_limit_minutes": time_limit_minutes,
        "is_proctored": is_proctored,
        "is_late_disallowed": is_late_disallowed,
        "due_date": due_date,
        "launcher_url": launcher_url,
        "attempts": attempts_list,
    }


def format_assessment_cli(record: Dict[str, Any]) -> str:
    """Formats assessment record into clean terminal presentation."""
    title = record.get("title", "Untitled")
    cname = record.get("course_name", record.get("course_id", "Course"))
    pts = record.get("points_possible", 0)
    allowed = record.get("attempts_allowed", 1)
    used = record.get("attempts_used", 0)
    model = record.get("scoring_model", "Last")
    proctored = "🔒 Yes (Proctored / Password)" if record.get("is_proctored") else "🟢 No (Open Browser)"
    timed = f"⏱️ {record.get('time_limit_minutes')} mins" if record.get("is_timed") else "🟢 None (Untimed)"
    url = record.get("launcher_url", "")
    desc = record.get("description", "")

    lines = [
        "",
        "╔═══════════════════════════════════════════════════════════════════════════╗",
        f"║  📝 ASSESSMENT INSPECTOR: {title[:48]}",
        "╚═══════════════════════════════════════════════════════════════════════════╝",
        f"  • Course:           {cname}",
        f"  • Content ID:       {record.get('content_id')}  (Assessment ID: {record.get('assessment_id') or 'N/A'})",
        f"  • Points Possible:  {pts} pts",
        f"  • Attempts:         {used} of {allowed} used  (Scoring Model: {model})",
        f"  • Time Limit:       {timed}",
        f"  • Proctoring:       {proctored}",
        f"  • Direct Launch:    {url}",
    ]

    if desc:
        desc_clean = desc.replace("\n", " ")[:120]
        lines.append(f"  • Description:      {desc_clean}...")

    attempts = record.get("attempts", [])
    if attempts:
        lines.append("\n  📋 Attempt History:")
        for idx, att in enumerate(attempts, 1):
            status = att.get("status")
            status_badge = f"🟡 [{status}]" if status == "InProgress" else f"🟢 [{status}]"
            created = att.get("created", "")[:16].replace("T", " ")
            overdue_tag = " ⚠️ OVERDUE" if att.get("is_overdue") else ""
            lines.append(f"    [{idx}] ID: {att.get('attempt_id')} | Status: {status_badge} | Started: {created} ({att.get('elapsed_minutes')}m ago){overdue_tag}")
    else:
        lines.append("\n  📋 Attempt History: No prior attempts submitted.")

    lines.append("")
    return "\n".join(lines)


async def inspect_assessment_playwright_async(
    content_id: str,
    course_id: str,
    page: Page,
) -> Optional[Dict[str, Any]]:
    """
    Playwright Fallback for assessment inspection if REST API is blocked.
    Inspects assessment drawer DOM safely without clicking start triggers.
    """
    url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/outline"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except Exception as e:
        logger.debug(f"Playwright navigation failed: {e}")
        return None

    # Wait for outline and target content item
    target_sel = f"[data-analytics-id*='{content_id}'], [data-content-id*='{content_id}']"
    elem = page.locator(target_sel).first
    if not await elem.is_visible():
        return None

    # Open drawer safely
    await elem.click(timeout=3000)
    await AdaptiveDOM.wait_for_any_selector(page, ["bb-drawer", "aside[role='dialog']"], timeout=5000)

    # Scrape drawer data
    data = await page.evaluate("""() => {
        const drawer = document.querySelector('bb-drawer, aside[role="dialog"]') || document.body;
        const title = drawer.querySelector('h1, h2, [class*="title"]')?.innerText.trim() || '';
        const points = drawer.querySelector('[data-analytics-id*="points"], [class*="pointsPossible"]')?.innerText.trim() || '';
        const attempts = drawer.querySelector('[class*="attemptsDetail"], [data-analytics-id*="attempts"]')?.innerText.trim() || '';
        const due = drawer.querySelector('[data-analytics-id*="due-date"], [class*="dueDate"]')?.innerText.trim() || '';
        const desc = drawer.querySelector('bb-rich-text-viewer, [class*="assessmentDescription"]')?.innerText.trim() || '';
        const isTimed = !!drawer.querySelector('.time-limit-warning, [data-analytics-id*="time-limit"]');
        return { title, points, attempts, due, desc, isTimed };
    }""")

    # Close drawer safely
    await page.keyboard.press("Escape")

    return {
        "content_id": content_id,
        "course_id": course_id,
        "title": data.get("title") or "Assessment",
        "description": data.get("desc") or "",
        "points_possible": data.get("points") or "",
        "attempts_allowed": data.get("attempts") or "",
        "attempts_used": 0,
        "due_date": data.get("due") or "",
        "is_timed": data.get("isTimed", False),
        "attempts": [],
        "launcher_url": f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/outline/assessment/{content_id}/overview",
    }


async def inspect_assessment(
    content_id: str,
    course_id: Optional[str] = None,
    headless: bool = True,
    cdp_url: Optional[str] = None,
    courses: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Dual-engine assessment inspector.
    Attempts high-speed REST Fast-Path first (<150ms). If REST returns HTTP 403 Forbidden
    or restricted access, automatically fails over to Playwright Fallback.
    """
    if courses is None:
        courses = load_courses()

    # 1. High-speed REST Fast-Path
    res = inspect_assessment_api(content_id, course_id=course_id, courses=courses)
    if res and not res.get("_http_status"):
        return res

    # 2. Check if failure warrants Playwright Fallback
    is_blocked = res and res.get("_http_status") in (401, 403)
    if is_blocked or res is None:
        logger.info(f"REST Assessment API returned {res.get('_http_status') if res else 'None'}; falling back to Playwright...")
        target_course = course_id
        if not target_course and courses:
            # Probe courses in browser
            target_course = list(courses.keys())[0]

        if target_course:
            session_manager = AsyncSessionManager(EngineConfig(headless=headless, cdp_url=cdp_url))
            await session_manager.initialize()
            try:
                async with session_manager.acquire_page() as page:
                    return await inspect_assessment_playwright_async(content_id, target_course, page)
            finally:
                await session_manager.close()

    return None


def get_in_progress_attempts_api(
    courses: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """
    High-speed REST check for active, unsubmitted student attempts across courses.
    Queries gradebook columns and attempts to flag items in 'InProgress' status.
    """
    cookie_header = get_cookie_header()
    if not cookie_header:
        return []

    if courses is None:
        courses = load_courses()

    in_progress: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for cid, cname in courses.items():
        cols_url = f"{BLACKBOARD_BASE}/learn/api/public/v2/courses/{cid}/gradebook/columns"
        cols_data = _api_get(cols_url, cookie_header, timeout=4.0)
        if not cols_data or cols_data.get("_http_status"):
            continue

        for col in cols_data.get("results", []):
            col_id = col.get("id")
            content_id = col.get("contentId")
            title = col.get("name", "Untitled")
            due_str = col.get("grading", {}).get("due", "")
            time_limit = col.get("grading", {}).get("timeLimit", 0)

            # Check attempts on column
            att_url = f"{BLACKBOARD_BASE}/learn/api/public/v1/courses/{cid}/gradebook/columns/{col_id}/attempts"
            att_data = _api_get(att_url, cookie_header, timeout=3.5)
            if not att_data or att_data.get("_http_status"):
                continue

            for attempt_item in att_data.get("results", []):
                if attempt_item.get("status") == "InProgress":
                    created_str = attempt_item.get("created", "")
                    elapsed_min = 0
                    if created_str:
                        try:
                            c_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                            elapsed_min = int((now - c_dt).total_seconds() / 60)
                        except Exception:
                            pass

                    is_overdue = bool(time_limit and elapsed_min > time_limit)
                    launcher_url = (
                        f"{BLACKBOARD_BASE}/ultra/courses/{cid}/outline/assessment/{content_id}/overview"
                        if content_id
                        else f"{BLACKBOARD_BASE}/ultra/courses/{cid}/grades"
                    )

                    in_progress.append({
                        "course_id": cid,
                        "course_name": cname,
                        "title": title,
                        "content_id": content_id,
                        "column_id": col_id,
                        "attempt_id": attempt_item.get("id"),
                        "status": "InProgress",
                        "created": created_str,
                        "elapsed_minutes": elapsed_min,
                        "elapsed_time_human": _format_elapsed_time(elapsed_min),
                        "time_limit_minutes": time_limit,
                        "is_overdue": is_overdue,
                        "due_date": due_str,
                        "launcher_url": launcher_url,
                    })

    return in_progress


def format_in_progress_alert_cli(open_attempts: List[Dict[str, Any]]) -> str:
    """Formats top-level urgent alert banner for in-progress attempts."""
    if not open_attempts:
        return ""

    lines = [
        "",
        "╔═══════════════════════════════════════════════════════════════════════════╗",
        f"║  🚨 IN-PROGRESS / UNFINISHED ATTEMPTS DETECTED ({len(open_attempts)})",
        "╚═══════════════════════════════════════════════════════════════════════════╝",
    ]

    for attempt in open_attempts:
        cname = attempt.get("course_name") or attempt.get("course") or attempt.get("course_id") or "Course"
        title = attempt.get("title")
        elapsed = attempt.get("elapsed_time_human") or "Active attempt"
        att_id = attempt.get("attempt_id") or "Active"
        url = attempt.get("launcher_url") or ""
        overdue_note = " ⚠️ [TIME LIMIT EXCEEDED / OVERDUE]" if attempt.get("is_overdue") else ""

        lines.append(f"  ⚠️  [{cname}] {title}{overdue_note}")
        lines.append(f"      • Started:        {elapsed} (Attempt ID: {att_id})")
        lines.append(f"      • Action Required: Test has not been submitted!")
        lines.append(f"      • Resume URL:      {url}\n")

    return "\n".join(lines)
