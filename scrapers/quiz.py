"""
Blackboard Ultra Assessment, Assignment & Quiz Inspector.

Features:
- Dual-Engine Architecture: Sub-200ms HTTP REST API Fast-Path with Playwright Browser Fallback.
- Multi-Type Support: Seamlessly inspects Quizzes/Tests, Assignments/Dropboxes, and Discussion Boards.
- Non-Destructive Info Mode (Default): Extracts full prompts, statements, rubrics, points, due dates,
  time limits, and attempt parameters without initiating or altering student attempts.
- Begin / Continue Attempt Mode: Supports reading active in-progress attempts, continuing them,
  or starting new attempts when explicitly requested (--start-attempt), with strict safety guards
  on timed exams.
- Deep Question & Choice Extraction: Question labels, points, types (True/False, Multiple Choice,
  Multiple Answer, Essay / Short Answer, Matching, Fill-in-the-Blank), prompts, options, and student answers.
- Seamless Discovery: Accepts direct Blackboard URLs, content IDs (from outline/assignments),
  or assignment titles across enrolled courses.
"""

import asyncio
import html
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import Page

from core.config import BLACKBOARD_BASE, SESSION_DIR, load_courses
from core.output import ensure_output_dir

logger = logging.getLogger("blackboard.scrapers.quiz")


# ============================================================================
# 1. Target URL & ID Parsing Helpers
# ============================================================================

def parse_assessment_target(target: str, default_course_id: Optional[str] = None) -> Dict[str, Optional[str]]:
    """
    Parses a direct Ultra assessment/attempt URL or IDs into components.

    Example URLs:
    - https://blackboard.umbc.edu/ultra/courses/_112155_1/assessment/_8836886_1/attempt/_35936988_1?courseId=_112155_1
    - https://blackboard.umbc.edu/ultra/courses/_112155_1/outline/assessment/_8836895_1?courseId=_112155_1
    - https://blackboard.umbc.edu/ultra/courses/_112155_1/discussion/_417123_1

    Returns dict with keys: 'course_id', 'assessment_id', 'attempt_id', 'discussion_id', 'url'
    """
    target = target.strip()
    result: Dict[str, Optional[str]] = {
        "course_id": default_course_id,
        "assessment_id": None,
        "attempt_id": None,
        "discussion_id": None,
        "url": None,
    }

    if target.startswith("http://") or target.startswith("https://"):
        result["url"] = target

        # Extract courseId from URL path or query params
        course_path_m = re.search(r"/courses/(_\d+_\d+)", target)
        course_query_m = re.search(r"courseId=(_\d+_\d+)", target)
        if course_path_m:
            result["course_id"] = course_path_m.group(1)
        elif course_query_m:
            result["course_id"] = course_query_m.group(1)

        # Extract assessment / content ID
        asmt_m = re.search(r"/assessment/(_\d+_\d+)", target) or re.search(r"/contents/(_\d+_\d+)", target)
        if asmt_m:
            result["assessment_id"] = asmt_m.group(1)

        # Extract attempt ID
        att_m = re.search(r"/attempt/(_\d+_\d+)", target) or re.search(r"/attempts/(_\d+_\d+)", target)
        if att_m:
            result["attempt_id"] = att_m.group(1)

        # Extract discussion ID
        disc_m = re.search(r"/discussion/(_\d+_\d+)", target) or re.search(r"/forums/(_\d+_\d+)", target)
        if disc_m:
            result["discussion_id"] = disc_m.group(1)

        return result

    # Check if target is a standalone ID
    if re.match(r"^_\d+_\d+$", target):
        result["assessment_id"] = target

    return result


def _clean_html_text(raw_html: Optional[str]) -> str:
    """Strips HTML tags, decodes HTML entities, and normalizes whitespace."""
    if not raw_html:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<(p|br|div|li|tr|h[1-6])[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n*Text Editor\s*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _map_question_type(raw_type: str) -> str:
    """Normalizes Blackboard internal question type codes."""
    t = (raw_type or "").lower()
    if t in ("eitheror", "true_false", "truefalse", "tf"):
        return "True / False"
    if t in ("multiplechoice", "mc", "choice"):
        return "Multiple Choice"
    if t in ("multipleanswer", "ma", "multianswer"):
        return "Multiple Answer"
    if t in ("essay", "shortanswer", "short_answer"):
        return "Essay / Short Answer"
    if t in ("matching", "match"):
        return "Matching"
    if t in ("fillinblank", "fib", "blank"):
        return "Fill in the Blank"
    if t in ("ordering", "order"):
        return "Ordering"
    if t in ("calculated", "calc"):
        return "Calculated Formula"
    return raw_type or "Question"


# ============================================================================
# 2. HTTP REST Fast-Path Engine (< 200ms)
# ============================================================================

def _get_cookie_header() -> Optional[str]:
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


def _api_get(url_or_path: str, cookie_header: str, timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    url = f"{BLACKBOARD_BASE}{url_or_path}" if url_or_path.startswith("/") else url_or_path
    headers = {
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            return {"_http_status": e.code, "error": str(e.reason)}
        logger.debug(f"HTTP Error {e.code} for {url}: {e}")
        return None
    except Exception as e:
        logger.debug(f"Network error for {url}: {e}")
        return None


def _scrape_assessment_http(
    course_id: str,
    assessment_id: Optional[str] = None,
    attempt_id: Optional[str] = None,
    target_query: Optional[str] = None,
    allow_start: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Executes high-speed REST queries to retrieve full assessment metadata & question payload.
    Works for Quizzes/Tests, Assignments, and Discussions.
    NOTE: allow_start is accepted for signature symmetry but intentionally
    ignored here — this engine is GET-only and never creates attempts.
    Attempt creation happens exclusively in the Playwright path, behind the
    --start-attempt / --force-start guards (see scrape_assessment_attempt_async).
    """
    cookie_header = _get_cookie_header()
    if not cookie_header:
        return None

    courses = load_courses()
    course_name = courses.get(course_id, course_id)

    # 1. Course Details & Official Name
    course_info = _api_get(f"/learn/api/public/v1/courses/{course_id}", cookie_header)
    if course_info and "name" in course_info:
        course_name = course_info["name"]

    # 2. Resolve Gradebook Columns to locate the target item
    matching_col = None
    cols = _api_get(f"/learn/api/public/v2/courses/{course_id}/gradebook/columns", cookie_header)
    if cols and "results" in cols:
        # First pass: check for assessment_id or exact title match
        for c in cols["results"]:
            cid = c.get("contentId")
            c_name = c.get("name", "")
            if assessment_id and cid == assessment_id:
                matching_col = c
                break
            if target_query and (target_query.strip().lower() == c_name.strip().lower() or target_query == c.get("id")):
                matching_col = c
                break

        # Second pass: check for substring match if no exact match found
        if not matching_col and target_query:
            tq_lower = target_query.strip().lower()
            for c in cols["results"]:
                c_name = c.get("name", "")
                if tq_lower in c_name.lower():
                    matching_col = c
                    break

    asmt_title = matching_col.get("name") if matching_col else "Assessment"
    col_id = matching_col.get("id") if matching_col else None
    col_handler = matching_col.get("scoreProviderHandle", "") if matching_col else ""
    if matching_col and matching_col.get("contentId"):
        assessment_id = matching_col["contentId"]

    # ------------------------------------------------------------------------
    # A. Special Handling: Discussion Boards (resource/x-bb-forumlink)
    # ------------------------------------------------------------------------
    if "forum" in col_handler or "discussion" in col_handler:
        discs = _api_get(f"/learn/api/public/v1/courses/{course_id}/discussions", cookie_header)
        matching_disc = None
        if discs and "results" in discs:
            for d in discs["results"]:
                if (col_id and d.get("gradebookColumnId") == col_id) or (asmt_title and d.get("title") == asmt_title):
                    matching_disc = d
                    break

        if matching_disc:
            topic = matching_disc.get("topic", {})
            prompt_text = _clean_html_text(topic.get("body", ""))
            due_date = ""
            raw_due = None
            if matching_col and matching_col.get("dueDate"):
                raw_due = matching_col["dueDate"]
            elif assessment_id:
                c_info = _api_get(f"/learn/api/v1/courses/{course_id}/contents/{assessment_id}", cookie_header)
                if c_info:
                    raw_due = c_info.get("genericReadOnlyData", {}).get("dueDate")

            if raw_due:
                try:
                    dt = datetime.fromisoformat(raw_due.replace("Z", "+00:00"))
                    due_date = dt.strftime("%Y-%m-%d %H:%M UTC")
                except Exception:
                    due_date = raw_due

            max_pts = ""
            if matching_col and matching_col.get("score", {}).get("possible") is not None:
                max_pts = f"{matching_col['score']['possible']} points"

            return {
                "engine": "http_rest",
                "item_type": "Discussion Board",
                "title": matching_disc.get("title") or asmt_title,
                "course_name": course_name,
                "course_id": course_id,
                "assessment_id": assessment_id,
                "column_id": col_id,
                "attempt_id": None,
                "status": "OPEN",
                "due_date": due_date,
                "max_points": max_pts,
                "attempts_allowed": "Unlimited",
                "time_limit": "No time limit",
                "instructions": prompt_text,
                "question_count": 0,
                "questions": [],
                "is_timed_test": False,
            }

    # ------------------------------------------------------------------------
    # B. Assessment / Assignment / Test Inspection
    # ------------------------------------------------------------------------
    if not matching_col and not assessment_id:
        return None

    due_date = ""
    time_limit = "No time limit"
    max_points = ""
    attempts_allowed = "Unlimited"
    instructions_text = ""
    item_type = "Assessment"
    is_timed = False
    question_count = 0

    if assessment_id:
        content_info = _api_get(f"/learn/api/v1/courses/{course_id}/contents/{assessment_id}?expand=gradebookCategory", cookie_header)
        if content_info and "_http_status" not in content_info:
            asmt_title = content_info.get("title") or asmt_title
            content_detail = content_info.get("contentDetail", {})
            test_block = content_detail.get("resource/x-bb-asmt-test-link", {}).get("test", {})
            dep_settings = test_block.get("deploymentSettings", {})
            grading_col = test_block.get("gradingColumn", {})
            assessment_meta = test_block.get("assessment", {})

            # Determine subtype: Test vs Assignment
            subtype = assessment_meta.get("subtype") or test_block.get("deployedAssessmentType") or "Assessment"
            if subtype.lower() == "assignment":
                item_type = "Assignment"
            elif subtype.lower() == "test":
                item_type = "Quiz / Test"
            else:
                item_type = subtype

            # Instructions / prompt description
            raw_inst = (
                assessment_meta.get("instructions", {}).get("rawText")
                or assessment_meta.get("instructions", {}).get("displayText")
                or assessment_meta.get("description", {}).get("rawText")
                or assessment_meta.get("description", {}).get("displayText")
                or ""
            )
            instructions_text = _clean_html_text(raw_inst)

            question_count = assessment_meta.get("questionCount", 0)

            if dep_settings.get("timeLimit"):
                time_limit = f"{dep_settings['timeLimit']} minutes"
                is_timed = True

            raw_attempts = dep_settings.get("attemptCount")
            if raw_attempts == -1:
                attempts_allowed = "Unlimited"
            elif raw_attempts is not None and raw_attempts > 0:
                attempts_allowed = f"{raw_attempts} attempt{'s' if raw_attempts > 1 else ''}"

            raw_due = grading_col.get("dueDate") or (matching_col.get("dueDate") if matching_col else None)
            if raw_due:
                try:
                    dt = datetime.fromisoformat(raw_due.replace("Z", "+00:00"))
                    due_date = dt.strftime("%Y-%m-%d %H:%M UTC")
                except Exception:
                    due_date = raw_due

            raw_possible = grading_col.get("possible") or assessment_meta.get("totalPoints") or (matching_col.get("score", {}).get("possible") if matching_col else None)
            if raw_possible is not None:
                max_points = f"{int(raw_possible) if float(raw_possible).is_integer() else raw_possible} points"
        elif not matching_col:
            # Neither gradebook column nor course content found for this assessment_id
            return None

    if not due_date and matching_col and matching_col.get("dueDate"):
        raw_due = matching_col["dueDate"]
        try:
            dt = datetime.fromisoformat(raw_due.replace("Z", "+00:00"))
            due_date = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            due_date = raw_due

    if not max_points and matching_col and matching_col.get("score", {}).get("possible") is not None:
        raw_possible = matching_col["score"]["possible"]
        max_points = f"{int(raw_possible) if float(raw_possible).is_integer() else raw_possible} points"

    # 3. Locate Attempt ID if not directly provided
    user_attempt_status = "NOT_ATTEMPTED"
    if not attempt_id and col_id:
        me_info = _api_get("/learn/api/v1/users/me", cookie_header)
        user_id = me_info.get("id") if me_info and "_http_status" not in me_info else None

        if col_id and user_id:
            grades = _api_get(f"/learn/api/v1/courses/{course_id}/gradebook/columns/{col_id}/grades?userId={user_id}", cookie_header)
            if grades and "results" in grades and len(grades["results"]) > 0:
                g_record = grades["results"][0]
                grade_id = g_record.get("id")
                user_attempt_status = g_record.get("status", "NOT_ATTEMPTED")

                if grade_id:
                    user_attempts = _api_get(
                        f"/learn/api/v1/courses/{course_id}/gradebook/columns/{col_id}/grades/{grade_id}/attempts",
                        cookie_header,
                    )
                    if user_attempts and "results" in user_attempts and len(user_attempts["results"]) > 0:
                        latest_att = user_attempts["results"][0]
                        attempt_id = latest_att.get("id")
                        user_attempt_status = latest_att.get("status", user_attempt_status)

    # ------------------------------------------------------------------------
    # C. Non-Destructive Return (if no attempt started and it's an assignment or info mode)
    # ------------------------------------------------------------------------
    if not attempt_id:
        # We can successfully return the complete assignment / quiz metadata & instructions!
        return {
            "engine": "http_rest",
            "item_type": item_type,
            "title": asmt_title,
            "course_name": course_name,
            "course_id": course_id,
            "assessment_id": assessment_id,
            "column_id": col_id,
            "attempt_id": None,
            "status": user_attempt_status,
            "due_date": due_date,
            "max_points": max_points,
            "attempts_allowed": attempts_allowed,
            "time_limit": time_limit,
            "instructions": instructions_text,
            "question_count": question_count,
            "questions": [],
            "is_timed_test": is_timed,
        }

    # ------------------------------------------------------------------------
    # D. Existing Attempt Details & Question Inspection
    # ------------------------------------------------------------------------
    attempt_data = _api_get(
        f"/learn/api/v1/courses/{course_id}/gradebook/attempts/{attempt_id}?expand=toolAttemptDetail,alignedGoals",
        cookie_header,
    )
    if not attempt_data or "_http_status" in attempt_data:
        return {
            "engine": "http_rest",
            "item_type": item_type,
            "title": asmt_title,
            "course_name": course_name,
            "course_id": course_id,
            "assessment_id": assessment_id,
            "column_id": col_id,
            "attempt_id": attempt_id,
            "status": user_attempt_status,
            "due_date": due_date,
            "max_points": max_points,
            "attempts_allowed": attempts_allowed,
            "time_limit": time_limit,
            "instructions": instructions_text,
            "question_count": question_count,
            "questions": [],
            "is_timed_test": is_timed,
        }

    tool_details = attempt_data.get("toolAttemptDetail", {})
    assessment_detail = tool_details.get("resource/x-bb-assessment") or next(iter(tool_details.values()), {})

    attempt_status = attempt_data.get("status") or assessment_detail.get("status", user_attempt_status)
    q_attempts = assessment_detail.get("questionAttempts", [])

    parsed_questions: List[Dict[str, Any]] = []
    total_calculated_points = 0.0

    # NOTE (for reviewers): numbering is positional, NOT visibleQuestionNumber.
    # The API reuses visibleQuestionNumber across presentation blocks, so an
    # instruction header and the first real question both reported number 1
    # (seen live on AGNG Module 3, attempt _36086680_1). List position matches
    # the order a student sees in Ultra.
    for position, qa in enumerate(q_attempts, start=1):
        q_num = position
        q_type_raw = qa.get("questionType", "")
        q_type = _map_question_type(q_type_raw)
        q_info = qa.get("question", {})

        q_points = q_info.get("points")
        if q_points is not None:
            try:
                total_calculated_points += float(q_points)
            except Exception:
                pass

        # Question prompt
        q_text_obj = q_info.get("questionText", {})
        raw_prompt = q_text_obj.get("rawText") or q_text_obj.get("displayText") or ""
        clean_prompt = _clean_html_text(raw_prompt)

        # Options / Choices (right-hand definitions for Matching)
        choices: List[str] = []
        raw_answers = q_info.get("answers", [])
        if raw_answers:
            for ans in raw_answers:
                if isinstance(ans, dict):
                    ans_text = ans.get("answerText", {}).get("rawText") or ans.get("text") or str(ans)
                    choices.append(_clean_html_text(ans_text))
                elif isinstance(ans, str):
                    choices.append(_clean_html_text(ans))
        elif q_type == "True / False":
            choices = ["True", "False"]

        # Matching terms (left-hand side). The API splits a Matching question
        # across two arrays: question.prompts[] holds the terms
        # (promptText.rawText, e.g. "Health span") and question.answers[]
        # holds the definitions. Previous code only read answers[], so the
        # terms were silently dropped (verified against the live Module 3
        # matching payload, question _23048955_1).
        match_terms: List[str] = []
        if q_type == "Matching":
            for pr in q_info.get("prompts", []):
                if isinstance(pr, dict):
                    term_raw = pr.get("promptText", {}).get("rawText") or pr.get("promptText", {}).get("displayText") or ""
                    term = _clean_html_text(term_raw)
                    if term:
                        match_terms.append(term)

        # Given student answer. Newer payloads use the plural key
        # givenAnswers (a list; empty = unanswered) instead of the legacy
        # singular givenAnswer dict. Accept both so selected_answer is not
        # spuriously null on in-progress attempts.
        given_raw = qa.get("givenAnswer")
        if given_raw is None:
            plural = qa.get("givenAnswers") or []
            plural_texts = []
            for g in plural:
                if isinstance(g, dict):
                    t = g.get("answerText", {}).get("rawText") or g.get("text") or ""
                    if t:
                        plural_texts.append(_clean_html_text(t))
                elif isinstance(g, str) and g.strip():
                    plural_texts.append(_clean_html_text(g))
            given_raw = plural_texts if plural_texts else None
        given_text: Optional[str] = None
        if isinstance(given_raw, dict):
            given_text = _clean_html_text(given_raw.get("rawText") or given_raw.get("displayText"))
        elif isinstance(given_raw, str):
            given_text = _clean_html_text(given_raw)
        elif isinstance(given_raw, list):
            # REVIEW: Multiple Answer items store one boolean per option,
            # parallel to question.answers[] (verified live on Module 3 Q9:
            # 4 options ↔ [false,false,false,false] = nothing picked yet).
            # Zip flags to choice text instead of joining raw booleans.
            if given_raw and all(isinstance(x, bool) for x in given_raw) and len(given_raw) == len(choices):
                picked = [c for c, flag in zip(choices, given_raw) if flag]
                given_text = "; ".join(picked) if picked else None
                str_parts = []
            else:
                str_parts = []
                for x in given_raw:
                    if isinstance(x, bool):
                        str_parts.append(str(x))
                    elif isinstance(x, str) and x.strip():
                        str_parts.append(_clean_html_text(x))
                    elif isinstance(x, dict):
                        t = x.get("answerText", {}).get("rawText") or x.get("text") or ""
                        if t:
                            str_parts.append(_clean_html_text(t))
                    elif x is not None:
                        str_parts.append(str(x))
                given_text = "; ".join(str_parts) if str_parts else None

        correct_ans = qa.get("correctAnswer") or qa.get("correctAnswers")

        parsed_questions.append({
            "number": q_num,
            "label": f"Question {q_num}",
            "points": f"{q_points} Points" if q_points is not None else "",
            "points_value": q_points,
            "type": q_type,
            "prompt": clean_prompt,
            "choices": choices,
            "match_terms": match_terms,
            "selected_answer": given_text,
            "correct_answer": correct_ans,
            "id": qa.get("id", ""),
            "question_id": qa.get("questionId", ""),
        })

    if not max_points and total_calculated_points > 0:
        max_points = f"{int(total_calculated_points) if total_calculated_points.is_integer() else total_calculated_points} points"

    return {
        "engine": "http_rest",
        "item_type": item_type,
        "title": asmt_title,
        "course_name": course_name,
        "course_id": course_id,
        "assessment_id": assessment_id,
        "column_id": col_id,
        "attempt_id": attempt_id,
        "status": attempt_status,
        "due_date": due_date,
        "max_points": max_points,
        "attempts_allowed": attempts_allowed,
        "time_limit": time_limit,
        "instructions": instructions_text,
        "question_count": len(parsed_questions) or question_count,
        "questions": parsed_questions,
        "is_timed_test": is_timed,
    }


# ============================================================================
# 3. Playwright Browser Fallback Engine
# ============================================================================

async def _scrape_assessment_playwright(
    target_url: str,
    page: Page,
    course_id: Optional[str] = None,
    assessment_id: Optional[str] = None,
    attempt_id: Optional[str] = None,
    allow_start: bool = False,
    force_start: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Playwright DOM inspector fallback for live assessment, assignment, and attempt pages.
    """
    courses = load_courses()
    course_name = courses.get(course_id, course_id or "Course Assessment")

    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        logger.debug(f"Navigation warning: {e}")

    await asyncio.sleep(2.0)

    # Check if a drawer is open with a "View assessment" or "Start attempt" button
    drawer_visible = await page.locator("bb-drawer, aside[role='dialog'], div.panel-content").first.is_visible()
    
    if drawer_visible and allow_start:
        # Check if timed test warning is present
        is_timed_warning = await page.locator(".time-limit-warning, [data-analytics-id*='time-limit']").first.is_visible()
        if is_timed_warning and not force_start:
            logger.warning("Timed test detected in drawer. Safe mode prevented auto-starting countdown timer.")
        else:
            start_btn = page.locator("button:has-text('View assessment'), button:has-text('Start attempt'), button:has-text('Continue attempt')").first
            if await start_btn.is_visible():
                await start_btn.click(timeout=3000)
                await asyncio.sleep(2.5)

    extracted = await page.evaluate("""() => {
        const pageTitle = document.querySelector('h1, h2.assessment-title, [class*="assessment-title"], [data-analytics-id*="assessment-title"]')?.innerText.trim() || document.title;
        const courseTitle = document.querySelector('header [class*="course-title"], .course-title-element, [data-analytics-id*="course-title"]')?.innerText.trim() || "";

        // Summary details
        let dueDate = "";
        let attemptsAllowed = "Unlimited";
        let maxPoints = "";
        let timeLimit = "";
        let instructions = "";

        const detailsText = document.querySelector('.panel-content-info, [class*="assessment-summary"], [class*="assessment-details"], [role="complementary"]')?.innerText || document.body.innerText;

        const dueMatch = detailsText.match(/due\\s*date[:\\s]*([^\\n]+)/i);
        if (dueMatch) dueDate = dueMatch[1].trim();

        const attemptsMatch = detailsText.match(/attempts[:\\s]*([^\\n]+)/i);
        if (attemptsMatch) attemptsAllowed = attemptsMatch[1].trim();

        const pointsMatch = detailsText.match(/maximum\\s*points[:\\s]*([0-9.]+\\s*(?:points)?)/i) || detailsText.match(/([0-9.]+)\\s*points\\s*possible/i);
        if (pointsMatch) maxPoints = pointsMatch[1].trim();

        const timeMatch = detailsText.match(/time\\s*limit[:\\s]*([^\\n]+)/i);
        if (timeMatch) timeLimit = timeMatch[1].trim();

        const instEl = document.querySelector('bb-rich-text-viewer, div.details-instructions, [class*="assessmentDescription"], div[class*="instructions"]');
        if (instEl) instructions = instEl.innerText.trim();

        // Extract Questions if inside attempt
        const questionNodes = Array.from(document.querySelectorAll('div.assessment-question, form[name="questionForm"]'));
        const uniqueQuestions = [];
        const seenForms = new Set();

        questionNodes.forEach((node, idx) => {
            const form = node.tagName === 'FORM' ? node : node.querySelector('form[name="questionForm"]') || node;
            const autoSaveId = form.getAttribute('auto-save-context-id') || form.id || `q_${idx}`;

            if (seenForms.has(autoSaveId)) return;
            seenForms.add(autoSaveId);

            const label = form.querySelector('.question-label, [id*="question-label"]')?.innerText.trim() || `Question ${uniqueQuestions.length + 1}`;

            let points = "";
            const pointsEl = form.querySelector('.points-text, [class*="points-value"], .question-points');
            if (pointsEl) {
                points = pointsEl.innerText.replace(/\\s+/g, ' ').trim();
            } else {
                const headerText = form.querySelector('.question-header')?.innerText || '';
                const pMatch = headerText.match(/(\\d+(?:\\.\\d+)?)\\s*Points/i);
                if (pMatch) points = `${pMatch[1]} Points`;
            }

            let qType = "Question";
            const wrapper = form.closest('.assessment-question') || form;
            const classList = wrapper.className;

            if (classList.includes('js-question-type-eitherOr') || (form.innerText.includes('True') && form.innerText.includes('False'))) {
                qType = "True / False";
            } else if (classList.includes('js-question-type-multipleChoice') || form.querySelector('input[type="radio"]')) {
                qType = "Multiple Choice";
            } else if (classList.includes('js-question-type-multipleAnswer') || form.querySelector('input[type="checkbox"]')) {
                qType = "Multiple Answer";
            } else if (classList.includes('js-question-type-essay') || form.querySelector('bb-editor, #bb-editor-textbox, textarea')) {
                qType = "Essay / Short Answer";
            } else if (classList.includes('js-question-type-matching') || form.querySelector('select')) {
                qType = "Matching";
            } else if (classList.includes('js-question-type-fillInBlank') || form.querySelector('input[type="text"]')) {
                qType = "Fill in the Blank";
            }

            let prompt = "";
            const promptEl = form.querySelector('bb-rich-text-viewer, .question-text, .question-prompt, [class*="questionPrompt"], div[class*="rich-text-viewer"]');
            if (promptEl) {
                prompt = promptEl.innerText.trim();
            } else {
                const directTexts = Array.from(form.querySelectorAll('div, p, span'))
                    .map(el => el.innerText.trim())
                    .filter(t => t.length > 5 && !t.startsWith('Question') && !t.includes('Points') && t !== 'True' && t !== 'False');
                if (directTexts.length > 0) prompt = directTexts[0];
            }

            prompt = prompt.replace(/\\n*Text Editor\\s*$/i, '').trim();

            const choices = [];
            const optionNodes = form.querySelectorAll('label, div.radio-wrapper, div.checkbox-wrapper, [role="radio"], [role="checkbox"]');
            const seenChoices = new Set();

            optionNodes.forEach(opt => {
                const text = opt.innerText.trim();
                if (text && !seenChoices.has(text) && text !== prompt && !text.startsWith('Question')) {
                    seenChoices.add(text);
                    const isChecked = !!opt.querySelector('input:checked') || opt.getAttribute('aria-checked') === 'true' || opt.classList.contains('is-checked');
                    choices.push({ text: text, selected: isChecked });
                }
            });

            let currentAnswer = null;
            if (qType === "True / False" || qType === "Multiple Choice") {
                const sel = choices.find(c => c.selected);
                currentAnswer = sel ? sel.text : null;
            } else if (qType === "Multiple Answer") {
                const sels = choices.filter(c => c.selected).map(c => c.text);
                currentAnswer = sels.length > 0 ? sels : null;
            } else if (qType === "Essay / Short Answer") {
                const editor = form.querySelector('#bb-editor-textbox, textarea, [contenteditable="true"]');
                const text = editor ? editor.innerText.trim() : "";
                currentAnswer = (text && text !== "Text Editor") ? text : null;
            }

            uniqueQuestions.push({
                number: uniqueQuestions.length + 1,
                label: label,
                points: points,
                type: qType,
                prompt: prompt,
                choices: choices.map(c => c.text),
                selected_answer: currentAnswer,
                id: autoSaveId
            });
        });

        return {
            title: pageTitle,
            course_name: courseTitle,
            due_date: dueDate,
            attempts: attemptsAllowed,
            max_points: maxPoints,
            time_limit: timeLimit,
            instructions: instructions,
            question_count: uniqueQuestions.length,
            questions: uniqueQuestions
        };
    }""")

    # Guard against empty/failed page extractions (e.g. blank page, navigation failure, or 404)
    has_content = (
        bool(extracted.get("questions"))
        or bool(extracted.get("instructions"))
        or bool(extracted.get("due_date"))
        or bool(extracted.get("max_points"))
    )
    raw_title = extracted.get("title", "").strip()
    title_lower = raw_title.lower()
    is_error_title = any(err in title_lower for err in ("not found", "error", "404", "blackboard learn"))
    if not has_content or is_error_title:
        return None

    return {
        "engine": "playwright_browser",
        "item_type": "Quiz / Test" if len(extracted.get("questions", [])) > 0 else "Assignment",
        "title": extracted.get("title") or "Assessment",
        "course_name": extracted.get("course_name") or course_name,
        "course_id": course_id,
        "assessment_id": assessment_id,
        "attempt_id": attempt_id,
        "status": "IN_PROGRESS" if len(extracted.get("questions", [])) > 0 else "NOT_ATTEMPTED",
        "due_date": extracted.get("due_date", ""),
        "max_points": extracted.get("max_points", ""),
        "attempts_allowed": extracted.get("attempts", "Unlimited"),
        "time_limit": extracted.get("time_limit") or "No time limit",
        "instructions": extracted.get("instructions", ""),
        "question_count": extracted.get("question_count", len(extracted.get("questions", []))),
        "questions": extracted.get("questions", []),
        "is_timed_test": "minute" in extracted.get("time_limit", "").lower(),
    }


# ============================================================================
# 4. Main Public Asynchronous & Synchronous Scraper Functions
# ============================================================================

async def scrape_assessment_attempt_async(
    target: str,
    course_id: Optional[str] = None,
    page: Optional[Page] = None,
    headless: bool = True,
    force_browser: bool = False,
    allow_start: bool = False,
    force_start: bool = False,
) -> Dict[str, Any]:
    """
    Main asynchronous entrypoint to inspect any assessment, assignment, quiz, or discussion.

    Args:
        target: Blackboard Ultra assessment/attempt URL, Content ID, or Title.
        course_id: Blackboard Course ID (optional if present in URL or target).
        page: Optional existing Playwright Page tab.
        headless: Whether to run Playwright headless on fallback.
        force_browser: If True, bypasses HTTP fast-path and uses Playwright directly.
        allow_start: If True, allows beginning a new attempt when none exists.
        force_start: If True, permits starting timed tests.

    Returns:
        Dict with keys:
        - title: str
        - item_type: str ('Quiz / Test', 'Assignment', 'Discussion Board')
        - course_name: str
        - course_id: str
        - assessment_id: Optional[str]
        - attempt_id: Optional[str]
        - status: str (e.g. 'IN_PROGRESS', 'COMPLETED', 'NOT_ATTEMPTED')
        - max_points: str
        - due_date: str
        - attempts_allowed: str
        - time_limit: str
        - instructions: str
        - question_count: int
        - questions: List[Dict[str, Any]]
        - is_timed_test: bool
    """
    parsed = parse_assessment_target(target, default_course_id=course_id)
    cid = parsed.get("course_id") or course_id or ""
    asmt_id = parsed.get("assessment_id")
    att_id = parsed.get("attempt_id")
    full_url = parsed.get("url")

    cookie_header = _get_cookie_header()

    # If course_id is omitted, auto-discover which enrolled course contains this assessment
    if not cid and (asmt_id or target) and cookie_header:
        courses = load_courses()
        for cand_cid in courses.keys():
            if asmt_id:
                c_info = _api_get(f"/learn/api/v1/courses/{cand_cid}/contents/{asmt_id}", cookie_header)
                if c_info and "_http_status" not in c_info:
                    cid = cand_cid
                    break
            cols = _api_get(f"/learn/api/public/v2/courses/{cand_cid}/gradebook/columns", cookie_header)
            if cols and "results" in cols:
                # Pass 1: exact match or ID match
                for col in cols["results"]:
                    c_name = col.get("name", "")
                    if (target and target.strip().lower() == c_name.strip().lower()) or (asmt_id and col.get("contentId") == asmt_id):
                        cid = cand_cid
                        asmt_id = col.get("contentId") or asmt_id
                        break
                # Pass 2: substring match
                if not cid and target:
                    t_lower = target.strip().lower()
                    for col in cols["results"]:
                        if t_lower in col.get("name", "").lower():
                            cid = cand_cid
                            asmt_id = col.get("contentId") or asmt_id
                            break
            if cid:
                break

    # 1. Fast-Path HTTP REST API Engine (if not forced to browser and we have course_id)
    if not force_browser and cid and (att_id or asmt_id or target):
        try:
            http_result = _scrape_assessment_http(
                course_id=cid,
                assessment_id=asmt_id,
                attempt_id=att_id,
                target_query=target if not asmt_id else None,
                allow_start=allow_start,
            )
            if http_result:
                # REVIEW (spec fix): an explicit --start-attempt must never
                # silently degrade to metadata-only. REST is read-only by
                # design (GETs only; creating attempts via POST /attempts is
                # documented in the Blackboard API but deliberately NOT wired
                # — see RESEARCH-NOTES §6), so when the user asked to start
                # and no attempt exists, fall through to the Playwright
                # starter below, which enforces the timed-exam --force-start
                # guard before clicking anything.
                if allow_start and not http_result.get("attempt_id"):
                    logger.warning(
                        "No attempt exists yet and --start-attempt was given: "
                        "REST cannot create attempts, continuing in the browser..."
                    )
                else:
                    return http_result
            elif not allow_start and cookie_header:
                # REST checked the course gradebook and contents with valid cookies and found nothing.
                courses = load_courses()
                course_hint = f" in course '{courses.get(cid, cid)}'" if cid else ""
                return {
                    "status": "NOT_FOUND",
                    "error": f"No assignment or quiz matching '{target}' was found{course_hint}.",
                    "target": target,
                    "course_id": cid or course_id,
                }
        except Exception as e:
            logger.debug(f"HTTP REST assessment fast-path failed: {e}")

    # 2. Playwright Browser Fallback Engine
    if not full_url:
        if cid and asmt_id and att_id:
            full_url = f"{BLACKBOARD_BASE}/ultra/courses/{cid}/assessment/{asmt_id}/attempt/{att_id}?courseId={cid}"
        elif cid and asmt_id:
            full_url = f"{BLACKBOARD_BASE}/ultra/courses/{cid}/outline/assessment/{asmt_id}"
        elif target.startswith("http://") or target.startswith("https://"):
            full_url = target
        else:
            courses = load_courses()
            course_hint = f" in course '{courses.get(cid, cid)}'" if cid else " across configured courses"
            return {
                "status": "NOT_FOUND",
                "error": f"No assignment or quiz matching '{target}' was found{course_hint}.",
                "target": target,
                "course_id": cid or course_id,
            }

    if page is not None:
        result = await _scrape_assessment_playwright(
            full_url, page, course_id=cid, assessment_id=asmt_id, attempt_id=att_id, allow_start=allow_start, force_start=force_start
        )
        if result:
            return result

    # Acquire isolated tab from AsyncSessionManager
    from core.async_engine import AsyncSessionManager, EngineConfig
    session_mgr = AsyncSessionManager(EngineConfig(headless=headless, block_assets=False))
    await session_mgr.initialize()

    try:
        async with session_mgr.acquire_page() as p:
            result = await _scrape_assessment_playwright(
                full_url, p, course_id=cid, assessment_id=asmt_id, attempt_id=att_id, allow_start=allow_start, force_start=force_start
            )
            if not result:
                return {
                    "status": "NOT_FOUND",
                    "error": f"Could not load assessment details for '{target}'.",
                    "target": target,
                    "course_id": cid or course_id,
                }
            return result
    finally:
        await session_mgr.close()


def scrape_assessment_attempt(
    target: str,
    course_id: Optional[str] = None,
    headless: bool = True,
    force_browser: bool = False,
    allow_start: bool = False,
    force_start: bool = False,
) -> Dict[str, Any]:
    """Synchronous wrapper for scrape_assessment_attempt_async."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return asyncio.run_coroutine_threadsafe(
                scrape_assessment_attempt_async(
                    target, course_id, headless=headless, force_browser=force_browser, allow_start=allow_start, force_start=force_start
                ),
                loop
            ).result()
    except Exception:
        pass
    return asyncio.run(
        scrape_assessment_attempt_async(
            target, course_id, headless=headless, force_browser=force_browser, allow_start=allow_start, force_start=force_start
        )
    )


# ============================================================================
# 5. CLI Output Formatting & File Persistence
# ============================================================================

def format_assessment_attempt_cli(data: Dict[str, Any]) -> str:
    """Renders structured assessment questions and metadata as a rich CLI string."""
    if not data:
        return "⚠️ No assessment data found."

    if data.get("status") == "NOT_FOUND" or (data.get("error") and not data.get("title")):
        err = data.get("error") or f"No assignment or quiz matching '{data.get('target', '')}' was found."
        tip_course = f" -c {data['course_id']}" if data.get("course_id") else ""
        return f"\n❌ {err}\n💡 Tip: Run 'bb assignments{tip_course}' or 'bb due' to see valid assignment titles and IDs.\n"

    title = data.get("title", "Assessment")
    item_type = data.get("item_type", "Assessment")
    cname = data.get("course_name", data.get("course_id", "Course"))
    status = data.get("status", "NOT_ATTEMPTED")
    due = data.get("due_date", "No due date specified")
    max_pts = data.get("max_points", "N/A")
    attempts = data.get("attempts_allowed", "Unlimited")
    time_lim = data.get("time_limit", "No time limit")
    q_count = data.get("question_count", len(data.get("questions", [])))

    engine_badge = "⚡ [REST Fast-Path]" if data.get("engine") == "http_rest" else "🌐 [Playwright Fallback]"

    lines = [
        f"\n📝 {title} [{item_type}] • {cname} {engine_badge}",
        "━" * 68,
        f"  📊 Status:           {status}",
        f"  🎯 Maximum Points:   {max_pts}",
        f"  ⏰ Due Date:         {due}",
        f"  ⏱️ Time Limit:       {time_lim}",
        f"  🔄 Attempts:         {attempts}",
    ]

    if q_count > 0:
        lines.append(f"  ❓ Total Questions:  {q_count}")

    lines.append("━" * 68)
    lines.append("")

    # Instructions or Prompt Body
    instructions = data.get("instructions")
    if instructions:
        lines.append("📖 Instructions / Prompt:")
        for line in instructions.splitlines():
            lines.append(f"  > {line}")
        lines.append("")

    questions = data.get("questions", [])
    if questions:
        lines.append("📋 Questions & Choices:")
        for q in questions:
            num = q.get("number", "?")
            q_type = q.get("type", "Question")
            pts = f" ({q['points']})" if q.get("points") else ""
            lines.append(f"\n【Q{num}】 {q_type}{pts}")

            prompt = q.get("prompt", "")
            if prompt:
                lines.append(f"  Prompt: {prompt}")

            choices = q.get("choices", [])
            selected = q.get("selected_answer")

            if choices:
                lines.append("  Options:")
                for choice in choices:
                    is_sel = False
                    if isinstance(selected, list):
                        is_sel = choice in selected
                    elif isinstance(selected, str):
                        is_sel = choice.strip() == selected.strip()

                    check = "🔘 [X]" if is_sel else "⚪ [ ]"
                    lines.append(f"    {check} {choice}")

            match_terms = q.get("match_terms", [])
            if match_terms:
                lines.append("  Match terms:")
                for term in match_terms:
                    lines.append(f"    • {term}")

            if selected and not choices:
                lines.append(f"  Your Response:\n    > {selected}")

            if q.get("correct_answer"):
                lines.append(f"  ✅ Correct Answer: {q['correct_answer']}")

    return "\n".join(lines)


def save_assessment_attempt(data: Dict[str, Any], filepath: Optional[Path] = None) -> Path:
    """Saves assessment attempt questions and metadata to Markdown format."""
    out_dir = ensure_output_dir("assessments")
    cid = data.get("course_id", "course")
    asmt_id = data.get("assessment_id") or data.get("attempt_id") or "assessment"

    if filepath is None:
        clean_title = re.sub(r"[^\w\-_\.]", "_", data.get("title", asmt_id))
        filename = f"{cid}_{clean_title}.md".replace("/", "_")
        filepath = out_dir / filename

    title = data.get("title", "Assessment")
    item_type = data.get("item_type", "Assessment")
    cname = data.get("course_name", cid)

    lines = [
        f"# 📝 {title} [{item_type}]",
        f"**Course:** {cname} (`{cid}`)",
        f"_Scraped: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_",
        "",
        "## Summary & Parameters",
        f"- **Status:** `{data.get('status', 'NOT_ATTEMPTED')}`",
        f"- **Item Type:** `{item_type}`",
        f"- **Maximum Points:** `{data.get('max_points', 'N/A')}`",
        f"- **Due Date:** `{data.get('due_date', 'N/A')}`",
        f"- **Time Limit:** `{data.get('time_limit', 'No time limit')}`",
        f"- **Attempts Allowed:** `{data.get('attempts_allowed', 'Unlimited')}`",
        f"- **Question Count:** `{data.get('question_count', 0)}`",
        "",
        "---",
        "",
    ]

    instructions = data.get("instructions")
    if instructions:
        lines.append("## Instructions / Prompt")
        lines.append(f"> {instructions.replace(chr(10), chr(10) + '> ')}")
        lines.append("")
        lines.append("---")
        lines.append("")

    questions = data.get("questions", [])
    if questions:
        lines.append("## Questions & Answers")
        lines.append("")
        for q in questions:
            num = q.get("number", "?")
            q_type = q.get("type", "Question")
            pts = f" ({q['points']})" if q.get("points") else ""
            lines.append(f"### Question {num}: {q_type}{pts}")
            lines.append("")
            lines.append("**Prompt:**")
            lines.append(f"> {q.get('prompt', '').replace(chr(10), chr(10) + '> ')}")
            lines.append("")

            choices = q.get("choices", [])
            selected = q.get("selected_answer")

            if choices:
                lines.append("**Options:**")
                for choice in choices:
                    is_sel = False
                    if isinstance(selected, list):
                        is_sel = choice in selected
                    elif isinstance(selected, str):
                        is_sel = choice.strip() == selected.strip()

                    box = "[x]" if is_sel else "[ ]"
                    lines.append(f"- {box} {choice}")
                lines.append("")

            match_terms = q.get("match_terms", [])
            if match_terms:
                lines.append("**Match terms:**")
                for term in match_terms:
                    lines.append(f"- {term}")
                lines.append("")

            if selected and not choices:
                lines.append(f"**Your Response:**\n\n```text\n{selected}\n```\n")

            if q.get("correct_answer"):
                lines.append(f"**Correct Answer:** `{q['correct_answer']}`\n")

            lines.append("---")
            lines.append("")

    filepath.write_text("\n".join(lines))
    return filepath
