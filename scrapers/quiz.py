"""
Blackboard Ultra Assessment & Quiz Inspector.

Features:
- Dual-Engine Architecture: Sub-200ms HTTP REST API Fast-Path with Playwright Browser Fallback.
- Top-level Metadata: Total points, due dates, time limits, allowed attempts, attempt status.
- Deep Question Inspection: Question numbers, points, types (True/False, Multiple Choice,
  Multiple Answer, Essay / Short Answer, Matching, Fill-in-the-Blank), prompts, choices,
  and current student answers.
- Seamless Transition: Accepts direct Blackboard URLs, content IDs (from outline/assignments),
  or attempt IDs.
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
from typing import Any, Dict, List, Optional, Tuple

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

    Example URL:
    https://blackboard.umbc.edu/ultra/courses/_112155_1/assessment/_8836886_1/attempt/_35936988_1?courseId=_112155_1

    Returns dict with keys: 'course_id', 'assessment_id', 'attempt_id', 'url'
    """
    target = target.strip()
    result: Dict[str, Optional[str]] = {
        "course_id": default_course_id,
        "assessment_id": None,
        "attempt_id": None,
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

        return result

    # Check if target is a standalone attempt ID or content ID
    if re.match(r"^_\d+_\d+$", target):
        # We need course_id context if not provided
        result["assessment_id"] = target

    return result


def _clean_html_text(raw_html: Optional[str]) -> str:
    """Strips HTML tags, decodes HTML entities, and normalizes whitespace."""
    if not raw_html:
        return ""
    # Strip <style> and <script>
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
    # Replace block breaks with newlines
    text = re.sub(r"<(p|br|div|li|tr)[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities
    text = html.unescape(text)
    # Clean whitespace
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    cleaned = "\n".join(lines)
    # Strip trailing TinyMCE / text editor placeholders
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
) -> Optional[Dict[str, Any]]:
    """
    Executes high-speed REST queries to retrieve full assessment metadata & question payload.
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

    # 2. Resolve Assessment Title and Settings
    asmt_title = "Assessment"
    due_date = ""
    time_limit = ""
    max_points = ""
    attempts_allowed = "Unlimited"

    if assessment_id:
        content_info = _api_get(f"/learn/api/v1/courses/{course_id}/contents/{assessment_id}?expand=gradebookCategory", cookie_header)
        if content_info and "_http_status" not in content_info:
            asmt_title = content_info.get("title") or asmt_title
            content_detail = content_info.get("contentDetail", {})
            test_block = content_detail.get("resource/x-bb-asmt-test-link", {}).get("test", {})
            dep_settings = test_block.get("deploymentSettings", {})
            grading_col = test_block.get("gradingColumn", {})
            assessment_meta = test_block.get("assessment", {})

            if dep_settings.get("timeLimit"):
                time_limit = f"{dep_settings['timeLimit']} minutes"

            raw_attempts = dep_settings.get("attemptCount")
            if raw_attempts == -1:
                attempts_allowed = "Unlimited"
            elif raw_attempts is not None and raw_attempts > 0:
                attempts_allowed = f"{raw_attempts} attempts"

            raw_due = grading_col.get("dueDate")
            if raw_due:
                try:
                    # Format ISO to localized readable string
                    dt = datetime.fromisoformat(raw_due.replace("Z", "+00:00"))
                    due_date = dt.strftime("%Y-%m-%d %H:%M UTC")
                except Exception:
                    due_date = raw_due

            raw_possible = grading_col.get("possible") or assessment_meta.get("totalPoints")
            if raw_possible is not None:
                max_points = f"{int(raw_possible) if float(raw_possible).is_integer() else raw_possible} points"

    # 3. Locate Attempt ID if not directly provided
    if not attempt_id and assessment_id:
        me_info = _api_get("/learn/api/v1/users/me", cookie_header)
        user_id = me_info.get("id") if me_info and "_http_status" not in me_info else None

        # Look up gradebook column for this assessment
        cols = _api_get(f"/learn/api/public/v2/courses/{course_id}/gradebook/columns", cookie_header)
        if cols and "results" in cols:
            target_col = None
            for col in cols["results"]:
                if col.get("contentId") == assessment_id or col.get("name") == asmt_title:
                    target_col = col
                    break

            if target_col:
                col_id = target_col.get("id")
                if target_col.get("score", {}).get("possible") is not None:
                    max_points = f"{target_col['score']['possible']} points"
                if target_col.get("dueDate"):
                    due_date = target_col["dueDate"]

                if col_id and user_id:
                    grades = _api_get(f"/learn/api/v1/courses/{course_id}/gradebook/columns/{col_id}/grades?userId={user_id}", cookie_header)
                    if grades and "results" in grades and len(grades["results"]) > 0:
                        grade_id = grades["results"][0].get("id")
                        attempts_left = grades["results"][0].get("attemptsLeft")
                        if attempts_left == -1:
                            attempts_allowed = "Unlimited"
                        elif attempts_left is not None:
                            attempts_allowed = f"{attempts_left} attempts remaining"

                        if grade_id:
                            user_attempts = _api_get(
                                f"/learn/api/v1/courses/{course_id}/gradebook/columns/{col_id}/grades/{grade_id}/attempts",
                                cookie_header,
                            )
                            if user_attempts and "results" in user_attempts and len(user_attempts["results"]) > 0:
                                attempt_id = user_attempts["results"][0].get("id")

    if not attempt_id:
        return None

    # 4. Fetch Deep Attempt Questions & Answers via Gradebook Attempts API
    attempt_data = _api_get(
        f"/learn/api/v1/courses/{course_id}/gradebook/attempts/{attempt_id}?expand=toolAttemptDetail,alignedGoals",
        cookie_header,
    )
    if not attempt_data or "_http_status" in attempt_data:
        return None

    tool_details = attempt_data.get("toolAttemptDetail", {})
    assessment_detail = tool_details.get("resource/x-bb-assessment") or next(iter(tool_details.values()), {})

    attempt_status = attempt_data.get("status") or assessment_detail.get("status", "IN_PROGRESS")
    q_attempts = assessment_detail.get("questionAttempts", [])

    parsed_questions: List[Dict[str, Any]] = []
    total_calculated_points = 0.0

    for qa in q_attempts:
        q_num = qa.get("visibleQuestionNumber") or (len(parsed_questions) + 1)
        q_type_raw = qa.get("questionType", "")
        q_info = qa.get("question", {})
        
        q_points = q_info.get("points")
        if q_points is not None:
            try:
                total_calculated_points += float(q_points)
            except Exception:
                pass

        # Extract Question Text / Prompt
        q_text_obj = q_info.get("questionText", {})
        raw_prompt = q_text_obj.get("rawText") or q_text_obj.get("displayText") or ""
        clean_prompt = _clean_html_text(raw_prompt)

        # Extract Options / Choices
        choices: List[str] = []
        raw_answers = q_info.get("answers", [])
        if raw_answers:
            for ans in raw_answers:
                if isinstance(ans, dict):
                    ans_text = ans.get("answerText", {}).get("rawText") or ans.get("text") or str(ans)
                    choices.append(_clean_html_text(ans_text))
                elif isinstance(ans, str):
                    choices.append(_clean_html_text(ans))
        elif _map_question_type(q_type_raw) == "True / False":
            choices = ["True", "False"]

        # Extract Given / Selected Answer
        given_raw = qa.get("givenAnswer")
        given_text: Optional[str] = None
        if isinstance(given_raw, dict):
            given_text = _clean_html_text(given_raw.get("rawText") or given_raw.get("displayText"))
        elif isinstance(given_raw, str):
            given_text = _clean_html_text(given_raw)

        # Correct answer (if released / graded)
        correct_ans = qa.get("correctAnswer") or qa.get("correctAnswers")

        parsed_questions.append({
            "number": q_num,
            "label": f"Question {q_num}",
            "points": f"{q_points} Points" if q_points is not None else "",
            "points_value": q_points,
            "type": _map_question_type(q_type_raw),
            "prompt": clean_prompt,
            "choices": choices,
            "selected_answer": given_text,
            "correct_answer": correct_ans,
            "id": qa.get("id", ""),
            "question_id": qa.get("questionId", ""),
        })

    if not max_points and total_calculated_points > 0:
        max_points = f"{int(total_calculated_points) if total_calculated_points.is_integer() else total_calculated_points} points"

    return {
        "engine": "http_rest",
        "title": asmt_title,
        "course_name": course_name,
        "course_id": course_id,
        "assessment_id": assessment_id,
        "attempt_id": attempt_id,
        "status": attempt_status,
        "due_date": due_date,
        "max_points": max_points,
        "attempts_allowed": attempts_allowed,
        "time_limit": time_limit or "No time limit",
        "question_count": len(parsed_questions),
        "questions": parsed_questions,
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
) -> Optional[Dict[str, Any]]:
    """
    Playwright DOM inspector fallback for live assessment attempt pages.
    """
    courses = load_courses()
    course_name = courses.get(course_id, course_id or "Course Assessment")

    try:
        await page.goto(target_url, wait_until="networkidle", timeout=30_000)
    except Exception as e:
        logger.debug(f"Navigation warning: {e}")

    await asyncio.sleep(2.0)

    extracted = await page.evaluate("""() => {
        const pageTitle = document.querySelector('h1, h2.assessment-title, [class*="assessment-title"], [data-analytics-id*="assessment-title"]')?.innerText.trim() || document.title;
        const courseTitle = document.querySelector('header [class*="course-title"], .course-title-element, [data-analytics-id*="course-title"]')?.innerText.trim() || "";

        // Summary details
        let dueDate = "";
        let attemptsAllowed = "Unlimited";
        let maxPoints = "";
        let timeLimit = "";

        const detailsText = document.querySelector('.panel-content-info, [class*="assessment-summary"], [class*="assessment-details"], [role="complementary"]')?.innerText || document.body.innerText;

        const dueMatch = detailsText.match(/due\\s*date[:\\s]*([^\\n]+)/i);
        if (dueMatch) dueDate = dueMatch[1].trim();

        const attemptsMatch = detailsText.match(/attempts[:\\s]*([^\\n]+)/i);
        if (attemptsMatch) attemptsAllowed = attemptsMatch[1].trim();

        const pointsMatch = detailsText.match(/maximum\\s*points[:\\s]*([0-9.]+\\s*(?:points)?)/i) || detailsText.match(/([0-9.]+)\\s*points\\s*possible/i);
        if (pointsMatch) maxPoints = pointsMatch[1].trim();

        const timeMatch = detailsText.match(/time\\s*limit[:\\s]*([^\\n]+)/i);
        if (timeMatch) timeLimit = timeMatch[1].trim();

        // Extract Questions
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

            // Strip trailing Text Editor strings
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
            question_count: uniqueQuestions.length,
            questions: uniqueQuestions
        };
    }""")

    return {
        "engine": "playwright_browser",
        "title": extracted.get("title") or "Assessment",
        "course_name": extracted.get("course_name") or course_name,
        "course_id": course_id,
        "assessment_id": assessment_id,
        "attempt_id": attempt_id,
        "status": "IN_PROGRESS",
        "due_date": extracted.get("due_date", ""),
        "max_points": extracted.get("max_points", ""),
        "attempts_allowed": extracted.get("attempts", "Unlimited"),
        "time_limit": extracted.get("time_limit") or "No time limit",
        "question_count": extracted.get("question_count", len(extracted.get("questions", []))),
        "questions": extracted.get("questions", []),
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
) -> Dict[str, Any]:
    """
    Main asynchronous entrypoint to scrape assessment/quiz attempt metadata and questions.

    Args:
        target: Full Blackboard Ultra assessment URL or Content/Attempt ID.
        course_id: Blackboard Course ID (optional if present in URL or target).
        page: Optional existing Playwright Page tab.
        headless: Whether to run Playwright headless on fallback.
        force_browser: If True, bypasses HTTP fast-path and uses Playwright directly.

    Returns:
        Dict with keys:
        - title: str
        - course_name: str
        - course_id: str
        - assessment_id: Optional[str]
        - attempt_id: Optional[str]
        - status: str (e.g. 'IN_PROGRESS', 'COMPLETED', 'NOT_ATTEMPTED')
        - max_points: str (e.g. '100 points')
        - due_date: str
        - attempts_allowed: str
        - time_limit: str
        - question_count: int
        - questions: List[Dict[str, Any]]
    """
    parsed = parse_assessment_target(target, default_course_id=course_id)
    cid = parsed.get("course_id") or course_id or ""
    asmt_id = parsed.get("assessment_id")
    att_id = parsed.get("attempt_id")
    full_url = parsed.get("url")

    # If course_id is omitted, auto-discover which enrolled course contains this assessment
    if not cid and (asmt_id or target):
        cookie_header = _get_cookie_header()
        if cookie_header:
            courses = load_courses()
            for cand_cid in courses.keys():
                if asmt_id:
                    c_info = _api_get(f"/learn/api/v1/courses/{cand_cid}/contents/{asmt_id}", cookie_header)
                    if c_info and "_http_status" not in c_info:
                        cid = cand_cid
                        break
                cols = _api_get(f"/learn/api/public/v2/courses/{cand_cid}/gradebook/columns", cookie_header)
                if cols and "results" in cols:
                    for col in cols["results"]:
                        if target.lower() in col.get("name", "").lower() or (asmt_id and col.get("contentId") == asmt_id):
                            cid = cand_cid
                            asmt_id = col.get("contentId") or asmt_id
                            break
                if cid:
                    break

    # 1. Fast-Path HTTP REST API Engine (if not forced to browser and we have course_id)
    if not force_browser and cid and (att_id or asmt_id):
        try:
            http_result = _scrape_assessment_http(course_id=cid, assessment_id=asmt_id, attempt_id=att_id)
            if http_result and http_result.get("questions"):
                return http_result
        except Exception as e:
            logger.debug(f"HTTP REST assessment fast-path failed: {e}")

    # 2. Playwright Browser Fallback Engine
    # Construct target Ultra URL if we don't have one
    if not full_url:
        if cid and asmt_id and att_id:
            full_url = f"{BLACKBOARD_BASE}/ultra/courses/{cid}/assessment/{asmt_id}/attempt/{att_id}?courseId={cid}"
        elif cid and asmt_id:
            full_url = f"{BLACKBOARD_BASE}/ultra/courses/{cid}/outline/assessment/{asmt_id}"
        else:
            full_url = target

    if page is not None:
        result = await _scrape_assessment_playwright(
            full_url, page, course_id=cid, assessment_id=asmt_id, attempt_id=att_id
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
                full_url, p, course_id=cid, assessment_id=asmt_id, attempt_id=att_id
            )
            return result or {}
    finally:
        await session_mgr.close()


def scrape_assessment_attempt(
    target: str,
    course_id: Optional[str] = None,
    headless: bool = True,
    force_browser: bool = False,
) -> Dict[str, Any]:
    """Synchronous wrapper for scrape_assessment_attempt_async."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return asyncio.run_coroutine_threadsafe(
                scrape_assessment_attempt_async(target, course_id, headless=headless, force_browser=force_browser),
                loop
            ).result()
    except Exception:
        pass
    return asyncio.run(
        scrape_assessment_attempt_async(target, course_id, headless=headless, force_browser=force_browser)
    )


# ============================================================================
# 5. CLI Output Formatting & File Persistence
# ============================================================================

def format_assessment_attempt_cli(data: Dict[str, Any]) -> str:
    """Renders structured assessment questions and metadata as a rich CLI string."""
    if not data:
        return "⚠️ No assessment data found."

    title = data.get("title", "Assessment")
    cname = data.get("course_name", data.get("course_id", "Course"))
    status = data.get("status", "IN_PROGRESS")
    due = data.get("due_date", "No due date specified")
    max_pts = data.get("max_points", "N/A")
    attempts = data.get("attempts_allowed", "Unlimited")
    time_lim = data.get("time_limit", "No time limit")
    q_count = data.get("question_count", len(data.get("questions", [])))

    engine_badge = "⚡ [REST Fast-Path]" if data.get("engine") == "http_rest" else "🌐 [Playwright Fallback]"

    lines = [
        f"\n📝 {title} • {cname} {engine_badge}",
        "━" * 68,
        f"  📊 Status:           {status}",
        f"  🎯 Maximum Points:   {max_pts}",
        f"  ⏰ Assessment Due:   {due}",
        f"  ⏱️ Time Limit:       {time_lim}",
        f"  🔄 Attempts:         {attempts}",
        f"  ❓ Total Questions:  {q_count}",
        "━" * 68,
        "",
    ]

    questions = data.get("questions", [])
    if not questions:
        lines.append("  (No questions found or assessment has not been started)")
        return "\n".join(lines)

    for q in questions:
        num = q.get("number", "?")
        q_type = q.get("type", "Question")
        pts = f" ({q['points']})" if q.get("points") else ""
        lines.append(f"【Q{num}】 {q_type}{pts}")
        
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

        if selected and not choices:
            lines.append(f"  Your Response:\n    > {selected}")

        if q.get("correct_answer"):
            lines.append(f"  ✅ Correct Answer: {q['correct_answer']}")

        lines.append("")

    return "\n".join(lines)


def save_assessment_attempt(data: Dict[str, Any], filepath: Optional[Path] = None) -> Path:
    """Saves assessment attempt questions and metadata to Markdown format."""
    out_dir = ensure_output_dir("assessments")
    cid = data.get("course_id", "course")
    asmt_id = data.get("assessment_id") or data.get("attempt_id") or "assessment"

    if filepath is None:
        filename = f"{cid}_{asmt_id}.md".replace("/", "_")
        filepath = out_dir / filename

    title = data.get("title", "Assessment")
    cname = data.get("course_name", cid)

    lines = [
        f"# 📝 {title}",
        f"**Course:** {cname} (`{cid}`)",
        f"_Scraped: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_",
        "",
        "## Summary & Parameters",
        f"- **Status:** `{data.get('status', 'IN_PROGRESS')}`",
        f"- **Maximum Points:** `{data.get('max_points', 'N/A')}`",
        f"- **Due Date:** `{data.get('due_date', 'N/A')}`",
        f"- **Time Limit:** `{data.get('time_limit', 'No time limit')}`",
        f"- **Attempts Allowed:** `{data.get('attempts_allowed', 'Unlimited')}`",
        f"- **Question Count:** `{data.get('question_count', 0)}`",
        "",
        "---",
        "",
        "## Questions & Answers",
        "",
    ]

    for q in data.get("questions", []):
        num = q.get("number", "?")
        q_type = q.get("type", "Question")
        pts = f" ({q['points']})" if q.get("points") else ""
        lines.append(f"### Question {num}: {q_type}{pts}")
        lines.append("")
        lines.append(f"**Prompt:**")
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

        if selected and not choices:
            lines.append(f"**Your Response:**\n\n```text\n{selected}\n```\n")

        if q.get("correct_answer"):
            lines.append(f"**Correct Answer:** `{q['correct_answer']}`\n")

        lines.append("---")
        lines.append("")

    filepath.write_text("\n".join(lines))
    return filepath
