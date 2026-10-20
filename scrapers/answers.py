"""`bb answers`: export, preview and save answers in an existing Assessment Attempt.

Never submits or finalizes. Only explicit --new-attempt can start an eligible attempt. Protocol notes: docs/specs/save-draft-answers-protocol.md.

Transport order mirrors login (ADR-0002): HTTP REST first; if HTTP has no persistence
(no XSRF token before dispatch, or an explicit 401 rejection), the same allowlisted PATCH is sent from the
installed browser, then the bundled Playwright browser (core.browser candidate order).
A write that was dispatched and failed ambiguously is never retried on any transport.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core import rest
from core.assessment_answers import (
    SCHEMA_VERSION, AnswersInputError, AnswersRefusal, PlannedEdit, build_export,
    check_snapshot_age, choice_letter, encode_given, fingerprint, parse_file, parse_sets,
    plan_changes, project_attempt, set_changes,
)
from core.config import BLACKBOARD_BASE, SESSION_DIR

MIN_WINDOW = timedelta(seconds=120)
_ID = r"_\d+_\d+"
_SAVE_PATH = re.compile(rf"/learn/api/v1/courses/{_ID}/gradebook/attempts/{_ID}/assessment/answers/{_ID}")
_BODY_KEYS = {"questionType", "givenAnswer"}
_ASSESSMENT_HANDLE = "resource/x-bb-assessment"

Progress = Callable[[str], None]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Identity binding (opaque, local-keyed; never a raw user ID in the file)
# ---------------------------------------------------------------------------

def _binding_key() -> bytes:
    path = SESSION_DIR / "answers-binding.key"
    try:
        return bytes.fromhex(path.read_text().strip())
    except (OSError, ValueError):
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key)
        return bytes.fromhex(key)


def account_binding(user_id: str) -> str:
    return hmac.new(_binding_key(), f"{BLACKBOARD_BASE}|{user_id}".encode(), hashlib.sha256).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _refuse_rest(exc: rest.RestError, what: str) -> AnswersRefusal:
    if exc.kind == "auth":
        return AnswersRefusal("auth", f"Blackboard session expired while reading {what}. Run: bb login")
    if exc.kind == "forbidden":
        return AnswersRefusal("forbidden", f"Blackboard denied access to {what}.")
    if exc.kind == "not_found":
        return AnswersRefusal("not_found", f"{what} was not found.")
    return AnswersRefusal("network", f"Could not read {what} ({exc}).")


def _read_json(path: str, timeout: float = 12.0) -> Any:
    """Origin-validated, redirect-blocked reads, including provider pagination URLs."""
    from core.session_state import user_agent
    url = path if urlsplit(path).scheme or path.startswith("//") else f"{BLACKBOARD_BASE}{path}"
    parsed, base = urlsplit(url), urlsplit(BLACKBOARD_BASE)
    if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc) or parsed.username or parsed.fragment:
        raise rest.RestError("unknown", detail="refusing a read outside the Blackboard origin")
    cookies = rest.cookie_header()
    if not cookies:
        raise rest.RestError("auth", 401, "no saved session")
    req = urllib.request.Request(url, method="GET", headers={
        "Cookie": cookies, "User-Agent": user_agent(), "Accept": "application/json"})
    try:
        data = json.loads(_open_no_redirect(req, timeout=timeout).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data
    except urllib.error.HTTPError as exc:
        raise rest.RestError({401: "auth", 403: "forbidden", 404: "not_found"}.get(exc.code, "unknown"),
                             exc.code, f"HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise rest.RestError("unknown", detail=str(exc)) from None


def _get(path: str, what: str, timeout: float = 12.0) -> Any:
    try:
        return _read_json(path, timeout=timeout)
    except rest.RestError as exc:
        raise _refuse_rest(exc, what) from None


def _get_pages(path: str, max_pages: int = 20) -> List[dict]:
    results = []
    for _ in range(max_pages):
        data = _read_json(path)
        results.extend(data["results"])
        path = (data.get("paging") or {}).get("nextPage")
        if not path:
            return results
    raise rest.RestError("unknown", detail="pagination incomplete")


def _safe_due(test: Dict[str, Any]) -> Optional[str]:
    settings = test.get("deploymentSettings")
    required = ("isPasswordRequired", "isSecureBrowserRequiredToTake", "isWebcamRequired",
                "isIpFilterEnabled", "isCompletionForced", "isBacktrackingProhibited")
    if not isinstance(settings, dict) or any(type(settings.get(k)) is not bool for k in required):
        raise AnswersRefusal("unsafe_settings", "Test safety settings are missing or unknown; refusing to save.")
    timer_keys = ("isTimeLimitEnabled", "isTimerEnabled", "isTimed")
    timer_unknown = any(k in settings and type(settings[k]) is not bool for k in timer_keys)
    if "timeLimit" in settings:
        limit = settings["timeLimit"]
        timer_unknown |= limit is not None and (type(limit) not in (int, float) or limit < 0)
        timed = bool(limit)
    else:
        # Observed on every untimed Ultra test in this account (25/25, 2026-10-20): no timeLimit key and a
        # timerCompletion enum. bb assignment already treats a present timeLimit as "timed". Neither signal → unknown.
        has_flag = any(settings.get(k) is False for k in timer_keys)
        timer_unknown |= not (has_flag or settings.get("timerCompletion") == "CONTINUAL")
        timed = False
    timed |= any(settings.get(k) is True for k in timer_keys)
    if timer_unknown:
        raise AnswersRefusal("unsafe_settings", "Test timer settings are unknown; refusing to save.")
    location = settings.get("restrictLocation")
    if location is not None and not isinstance(location, dict):
        raise AnswersRefusal("unsafe_settings", "Test location restrictions are unknown; refusing.")
    risky = [label for label, active in [
        ("timed", timed), ("password protected", settings[required[0]]),
        ("secure browser", settings[required[1]]), ("webcam/proctored", settings[required[2]]),
        ("IP restricted", settings[required[3]] or (location or {}).get("selectedRestrictType") not in (None, "NONE")),
        ("forced completion", settings[required[4]]), ("no backtracking", settings[required[5]])] if active]
    if risky:
        raise AnswersRefusal("unsafe_settings", "Refusing: this test is " + ", ".join(risky) + ". Answer it yourself in Blackboard.")
    column = test.get("gradingColumn")
    if not isinstance(column, dict) or "dueDate" not in column:
        raise AnswersRefusal("unknown_deadline", "Test due date is unknown; refusing to save.")
    due = column["dueDate"]
    if due is not None:
        try:
            dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("missing timezone")
        except (AttributeError, TypeError, ValueError):
            raise AnswersRefusal("unknown_deadline", "Test due date is unknown; refusing to save.") from None
    return due


def _test_from_content(content: dict) -> dict:
    return ((content.get("contentDetail") or {}).get("resource/x-bb-asmt-test-link") or {}).get("test") or {}


_URL_COURSE = re.compile(r"/courses/(_\d+_\d+)")
_URL_ITEM = re.compile(r"/(?:assessment|test|outline/assessment(?:/test)?)/(_\d+_\d+)|[?&](?:contentId|content_id)=(_\d+_\d+)")
_URL_COURSE_QUERY = re.compile(r"[?&](?:course_id|courseId)=(_\d+_\d+)")


def parse_target_url(target: str) -> Optional[tuple]:
    """(course_id, content_id) from a pasted Blackboard URL, None if TARGET is not a URL."""
    from urllib.parse import urlsplit
    base = urlsplit(BLACKBOARD_BASE)
    raw = target.strip()
    if re.match(re.escape(base.netloc) + r"/", raw, re.I):  # pasted without https://
        raw = f"{base.scheme}://{raw}"
    if not re.match(r"https?://", raw, re.I):
        return None
    parts = urlsplit(raw)
    if parts.netloc.lower() != base.netloc.lower():
        raise AnswersInputError(f"that URL is not on {base.netloc}")
    course = _URL_COURSE.search(parts.path) or _URL_COURSE_QUERY.search("?" + parts.query)
    item = _URL_ITEM.search(parts.path + ("?" + parts.query if parts.query else ""))
    if not course or not item:
        raise AnswersInputError("could not find a course and test ID in that URL; paste the test's page URL or use its title")
    return course.group(1), item.group(1) or item.group(2)


def resolve_target(target: str, course_ids: List[str], courses: Dict[str, str]) -> Dict[str, Any]:
    """Exactly one native Ultra test across the given courses, or an ambiguity refusal.

    TARGET may be a title (or part of it, e.g. "J#5"), a content or gradebook-column ID, or a pasted
    Blackboard URL (the course and test IDs are read from it; the host must be this Blackboard).
    """
    parsed = parse_target_url(target)
    if parsed:
        if course_ids and len(course_ids) < len(courses or {}) and parsed[0] not in course_ids:
            raise AnswersInputError("that URL is for a different course than -c; drop -c or fix it")
        course_ids, target = [parsed[0]], parsed[1]
    matches = []
    key = target.casefold().strip()
    for cid in course_ids:
        try:
            cols = _get_pages(f"/learn/api/public/v2/courses/{cid}/gradebook/columns", max_pages=20)
        except rest.RestError as exc:
            raise _refuse_rest(exc, f"gradebook for {courses.get(cid, cid)}") from None
        for col in cols:
            ids = (col.get("id"), col.get("contentId"))
            name = col.get("name") or ""
            if target in ids or key in name.casefold():
                matches.append({"course_id": cid, "course_name": courses.get(cid, cid), "title": name,
                                "column_id": col.get("id"), "assessment_id": col.get("contentId"),
                                "handle": col.get("scoreProviderHandle"),
                                "due": (col.get("grading") or {}).get("due"),
                                "exact": target in ids})
    exact = [m for m in matches if m["exact"]]
    if exact:
        matches = exact
    if not matches:
        raise AnswersRefusal("not_found", f"No gradable item matches '{target}'.")
    if len(matches) > 1:
        raise AnswersRefusal("ambiguous_target", f"'{target}' matches {len(matches)} items:",
                             matches=[{k: m[k] for k in ("title", "course_name", "assessment_id", "due")} for m in matches])
    m = matches[0]
    if m["handle"] != _ASSESSMENT_HANDLE or not m["assessment_id"]:
        raise AnswersRefusal("unsupported_item", f"'{m['title']}' is not a native Ultra test; only tests support saved answers.")
    return m


def load_inventory(match: Dict[str, Any], check_safety: bool = True, complete: bool = False) -> Dict[str, Any]:
    """Fresh read of user, test settings, eligible attempt and its questions.

    check_safety=False is for read-only views (show/export): they never write, and apply re-checks before dispatch.
    """
    cid, aid = match["course_id"], match["assessment_id"]
    me = _get("/learn/api/v1/users/me", "your profile")
    content = _get(f"/learn/api/v1/courses/{cid}/contents/{aid}", "the test")
    if check_safety:
        due = _safe_due(_test_from_content(content))
    else:
        due = (_test_from_content(content).get("gradingColumn") or {}).get("dueDate") or match.get("due")
    grades = _get(f"/learn/api/v1/courses/{cid}/gradebook/grades?userId={me['id']}&limit=200", "your grades")
    if complete and (not isinstance(grades.get("results"), list) or (grades.get("paging") or {}).get("nextPage")
                     or any(not isinstance(g, dict) for g in grades["results"])):
        raise AnswersRefusal("unknown_attempt_count", "Grade inventory incomplete; refusing to start.")
    grade = next((g for g in grades.get("results", []) if g.get("columnId") == match["column_id"]), None)
    attempts: List[Dict[str, Any]] = []
    if grade:
        data = _get(f"/learn/api/v1/courses/{cid}/gradebook/columns/{match['column_id']}/grades/{grade['id']}/attempts",
                    "your attempts")
        attempts = data.get("results", []) if isinstance(data, dict) else []
        if complete:
            if (not isinstance(data, dict) or not isinstance(data.get("results"), list)
                    or (data.get("paging") or {}).get("nextPage")
                    or any(not isinstance(entry, dict) for entry in data["results"])):
                raise AnswersRefusal("unknown_attempt_count", "Attempt inventory incomplete; refusing to start.")
            known = {grade.get("firstAttemptId"), grade.get("lastAttemptId")} - {None}
            if not known <= {a.get("id") for a in attempts}:
                raise AnswersRefusal("unknown_attempt_count", "Attempt inventory contradicts grade; refusing to start.")
        if not attempts and not complete:
            ids = {grade.get("firstAttemptId"), grade.get("lastAttemptId")} - {None}
            attempts = [{"id": i} for i in ids]
    details = []
    seen = set()
    for entry in attempts:
        attempt_id = entry.get("id")
        if not isinstance(attempt_id, str) or not re.fullmatch(_ID, attempt_id) or attempt_id in seen:
            raise AnswersRefusal("unknown_attempt_count", "Missing or duplicate attempt ID.")
        seen.add(attempt_id)
        detail = _get(f"/learn/api/v1/courses/{cid}/gradebook/attempts/{attempt_id}?expand=toolAttemptDetail", "the attempt")
        if complete and (detail.get("id") != attempt_id or detail.get("userId") != me["id"] or detail.get("courseId") != cid
                         or detail.get("status") not in {"IN_PROGRESS", "COMPLETED", "GRADED", "NEEDS_GRADING", "SUBMITTED"}):
            raise AnswersRefusal("unknown_attempt_count", "Attempt identity/status unknown; refusing to start.")
        details.append(detail)
    return {"me": me, "content": content, "due": due, "attempts": details}


def load_state(match: Dict[str, Any], attempt_id: Optional[str] = None, check_safety: bool = True) -> Dict[str, Any]:
    inventory = load_inventory(match, check_safety=check_safety)
    return state_from_inventory(match, inventory, attempt_id)


def state_from_inventory(match, inventory, attempt_id=None):
    cid, aid = match["course_id"], match["assessment_id"]
    me, content, due = inventory["me"], inventory["content"], inventory["due"]
    attempts = inventory["attempts"]
    if attempt_id:
        attempts = [a for a in attempts if a.get("id") == attempt_id]
        if not attempts:
            raise AnswersRefusal("attempt_not_found", f"Attempt {attempt_id} is not yours on this test.")

    in_progress = [a for a in attempts if a.get("status") == "IN_PROGRESS"
                   and a.get("userId") == me["id"] and a.get("courseId") == cid]
    if not in_progress:
        raise AnswersRefusal("no_active_attempt", "No in-progress attempt. Open the test in Blackboard first, "
                             "or explicitly use --new-attempt for an eligible untimed test.")
    if len(in_progress) > 1:
        raise AnswersRefusal("multiple_attempts", "More than one in-progress attempt. Pass --attempt ID.",
                             attempts=[a["id"] for a in in_progress])
    attempt = in_progress[0]
    projection = project_attempt(attempt)
    order = [q["question_attempt_id"] for q in projection["questions"]]
    return {
        "match": match, "user_id": me["id"], "due": due, "attempt": attempt, "projection": projection,
        "sha": fingerprint(projection["raw"], order),
        "target": {
            "origin": BLACKBOARD_BASE, "course_id": cid, "course_name": match["course_name"],
            "assessment_id": aid, "title": content.get("title") or match["title"],
            "attempt_id": attempt["id"], "account_binding": account_binding(me["id"]),
        },
    }


def reread_attempt(state: Dict[str, Any], timeout: float = 12.0) -> Dict[str, Any]:
    t = state["target"]
    detail = _get(f"/learn/api/v1/courses/{t['course_id']}/gradebook/attempts/{t['attempt_id']}?expand=toolAttemptDetail",
                  "the attempt", timeout=timeout)
    if detail.get("status") != "IN_PROGRESS":
        raise AnswersRefusal("attempt_not_in_progress", f"Attempt is now {detail.get('status')}; stopped.")
    return project_attempt(detail)


def time_left(due: Optional[str], now: Optional[datetime] = None) -> Optional[timedelta]:
    if not due:
        return None
    return datetime.fromisoformat(due.replace("Z", "+00:00")) - (now or _now())


def check_window(state: Dict[str, Any]) -> None:
    left = time_left(state["due"])
    if left is not None and left < MIN_WINDOW:
        raise AnswersRefusal("deadline", "Too close to (or past) the due time to save safely. Do it in Blackboard yourself.")


# ---------------------------------------------------------------------------
# Save transports (allowlisted PATCH only)
# ---------------------------------------------------------------------------

class NotDispatched(Exception):
    """No persistence: missing session/XSRF before dispatch, or explicit HTTP 401 rejection."""


class WriteFailed(Exception):
    """The request was sent and failed or its outcome is unknown. Never retried."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # rejected | unknown


def _validate_write(path: str, body: Dict[str, Any]) -> str:
    if not _SAVE_PATH.fullmatch(path) or set(body) != _BODY_KEYS:
        raise AssertionError("refusing a write outside the answer-save allowlist")
    return json.dumps(body)


def http_patch(path: str, body: Dict[str, Any], dispatch=None) -> Dict[str, Any]:
    payload = _validate_write(path, body)
    cookies, token = rest.cookie_header(), rest.xsrf_token()
    if not cookies or not token:
        raise NotDispatched("no saved session or XSRF token")
    from core.session_state import user_agent
    req = urllib.request.Request(
        f"{BLACKBOARD_BASE}{path}", data=payload.encode(), method="PATCH",
        headers={"Cookie": cookies, "User-Agent": user_agent(), "Accept": "application/json",
                 "Content-Type": "application/json;charset=UTF-8", "x-blackboard-xsrf": token,
                 "Origin": BLACKBOARD_BASE, "Referer": f"{BLACKBOARD_BASE}/ultra/stream"})
    if dispatch:
        dispatch()
    try:
        return json.loads(_open_no_redirect(req).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise NotDispatched("session rejected (401)") from None
        raise WriteFailed("unknown" if exc.code >= 500 or exc.code == 429 else "rejected", f"HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise WriteFailed("unknown", str(exc)) from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never follow (or forward cookies on) a redirect
        return None


def _open_no_redirect(req: urllib.request.Request, timeout: float = 15.0) -> bytes:
    """Network seam for writes (tests replace this)."""
    opener = urllib.request.build_opener(_NoRedirect)
    with rest._SLOTS, opener.open(req, timeout=timeout) as resp:
        return resp.read()


class BrowserTransport:
    """Sends the same allowlisted PATCH from inside a browser that holds the session.

    Uses core.browser's candidate order: the installed browser first, bundled Playwright last.
    """

    def __init__(self, progress: Progress):
        self.progress = progress
        self._pw = self._ctx = self._page = None
        self.label = "browser"

    def _open(self) -> None:
        if self._page:
            return
        try:
            from playwright.sync_api import sync_playwright
            from core.browser import launch_persistent_context
            self._pw = sync_playwright().start()
            self._ctx, spec = launch_persistent_context(self._pw, SESSION_DIR, headless=True,
                                                       **getattr(self, "launch_options", {}))
            self.label = f"browser ({spec.get('name') or spec.get('channel') or 'playwright'})"
            cookie_file = SESSION_DIR / "cookies.json"
            if cookie_file.exists():
                try:
                    self._ctx.add_cookies(json.loads(cookie_file.read_text()))
                except Exception:
                    pass
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            self._page.goto(f"{BLACKBOARD_BASE}/ultra/stream", wait_until="domcontentloaded", timeout=30000)
            if "/ultra" not in self._page.url:
                raise NotDispatched("browser session is not signed in (run: bb login)")
        except NotDispatched:
            raise
        except Exception as exc:
            raise NotDispatched(f"browser unavailable: {type(exc).__name__}") from None

    def _token(self) -> Optional[str]:
        for c in self._ctx.cookies(BLACKBOARD_BASE):
            if c["name"] == "XSRF-TOKEN":
                return c["value"]
        for c in self._ctx.cookies(BLACKBOARD_BASE):
            m = c["name"] == "BbRouter" and re.search(r"xsrf:([0-9a-fA-F-]+)", c["value"])
            if m:
                return m.group(1)
        return None

    def patch(self, path: str, body: Dict[str, Any], dispatch=None) -> Dict[str, Any]:
        payload = _validate_write(path, body)
        self._open()
        token = self._token()
        if not token:
            raise NotDispatched("no XSRF token in browser session")
        if dispatch:
            dispatch()
        try:
            return self._send(path, payload, token)
        except (WriteFailed, KeyboardInterrupt):
            raise
        except Exception as exc:
            raise WriteFailed("unknown", f"Browser response failed: {type(exc).__name__}") from None

    def _send(self, path, payload, token):
        res = self._page.evaluate(
            """async ([path, payload, token]) => {
                try {
                  const r = await fetch(path, {method: 'PATCH', body: payload, redirect: 'manual', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json;charset=UTF-8', 'Accept': 'application/json', 'x-blackboard-xsrf': token}});
                  return {status: r.status, text: await r.text()};
                } catch (e) { return {status: -1, text: String(e)}; }
            }""", [f"{BLACKBOARD_BASE}{path}", payload, token])  # absolute: Ultra sets a CDN <base href>
        status = res.get("status")
        if status == 200:
            return json.loads(res["text"])
        if status == 401:
            raise WriteFailed("rejected", "browser session rejected (401)")
        raise WriteFailed("unknown" if status in (-1, 0, 429) or (status or 0) >= 500 else "rejected", f"HTTP {status}")

    def close(self) -> None:
        try:
            if self._ctx:
                self._ctx.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


class Saver:
    """HTTP first; browser fallback only for no-XSRF/session or 401, before any persistence."""

    def __init__(self, progress: Progress, http=http_patch, browser_factory=BrowserTransport):
        self.progress = progress
        self.http = http
        self.browser_factory = browser_factory
        self.browser = None
        self.used = "http"
        self.http_persisted = False
        self.dispatch_count = 0
        self.before_dispatch = lambda: None

    def _dispatch(self):
        self.before_dispatch()
        self.dispatch_count += 1

    def patch(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if self.used == "http":
            try:
                if self.http is http_patch:
                    return self.http(path, body, dispatch=self._dispatch)
                self._dispatch()
                return self.http(path, body)
            except NotDispatched as why:
                if self.http_persisted:
                    raise WriteFailed("rejected", f"HTTP unavailable after an earlier save: {why}") from None
                self.progress(f"HTTP save unavailable ({why}); using the browser instead.")
                self.used = "browser"
        if self.browser is None:
            self.browser = self.browser_factory(self.progress)
        if isinstance(self.browser, BrowserTransport):
            result = self.browser.patch(path, body, dispatch=self._dispatch)
        else:
            self._dispatch()
            result = self.browser.patch(path, body)
        self.used = getattr(self.browser, "label", "browser")
        return result

    def close(self) -> None:
        if self.browser:
            self.browser.close()


# ---------------------------------------------------------------------------
# Local lock (one writer per attempt on this machine)
# ---------------------------------------------------------------------------

class AttemptLock:
    """Held OS lock; the stable lock inode is never unlinked, even after a crash."""

    def __init__(self, attempt_id: str):
        self.path = SESSION_DIR / "locks" / f"answers-{attempt_id}.lock"
        self.handle = None

    def __enter__(self):
        from core.session_state import _try_lock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        if not _try_lock(self.handle):
            self.handle.close()
            raise AnswersRefusal("locked", "Another bb answers run is saving to this attempt. Wait for it to finish.")
        return self

    def __exit__(self, *exc):
        from core.session_state import _unlock
        _unlock(self.handle)
        self.handle.close()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def export_doc(state: Dict[str, Any]) -> Dict[str, Any]:
    return build_export(state["target"], state["projection"], _now())


def _check_binding(doc: Dict[str, Any], state: Dict[str, Any]) -> None:
    t, f = state["target"], doc["target"]
    for key in ("course_id", "assessment_id", "attempt_id"):
        if f.get(key) != t[key]:
            raise AnswersRefusal("target_mismatch", f"File is for a different {key.replace('_', ' ')}. Export again.")
    if f.get("origin") not in (None, BLACKBOARD_BASE):
        raise AnswersRefusal("target_mismatch", "File is for a different Blackboard site.")
    if f["account_binding"] != t["account_binding"]:
        raise AnswersRefusal("account_mismatch", "File was exported by a different Blackboard account or machine. Export again.")


def prepare(doc: Dict[str, Any], state: Dict[str, Any]) -> List[PlannedEdit]:
    _check_binding(doc, state)
    if not doc.get("_runtime_baseline"):
        check_snapshot_age(doc, _now())
    return plan_changes(doc, state["projection"], state["sha"])


def _definition(proj):
    return fingerprint(proj["raw"], [q["question_attempt_id"] for q in proj["questions"]])


def _confirm_question(state, edit):
    """At most three reads with 1+2 seconds of backoff; never replay a write."""
    for index in range(3):
        try:
            proj = reread_attempt(state, timeout=0.6)
            current = {q["question_attempt_id"]: q for q in proj["questions"]}
            if _definition(proj) != state["sha"]:
                return False
            if edit.question_attempt_id in current and set(current[edit.question_attempt_id]["selected_choice_ids"]) == set(edit.after):
                return True
        except Exception:
            pass
        if index < 2:
            time.sleep(index + 1)
    return False


def apply(doc: Dict[str, Any], state: Dict[str, Any], progress: Progress,
          saver: Optional[Saver] = None, cancel_event=None) -> Dict[str, Any]:
    """Save, reconcile each question, then verify all requests in one final snapshot."""
    edits = prepare(doc, state)
    cancel_event = cancel_event or threading.Event()
    t = state["target"]
    saver = saver or Saver(progress)
    stop_reason = None
    def guard():
        if cancel_event.is_set():
            raise AnswersRefusal("cancelled", "Cancelled; no further answers will be sent.")
        check_window(state)
        content = _get(f"/learn/api/v1/courses/{t['course_id']}/contents/{t['assessment_id']}", "the test settings")
        state["due"] = _safe_due(_test_from_content(content))
        check_window(state)
        proj = reread_attempt(state)
        if _definition(proj) != state["sha"]:
            raise AnswersRefusal("stale_snapshot", "The questions or choices changed since export. Export again.")
        check_window(state)
        if cancel_event.is_set():
            raise AnswersRefusal("cancelled", "Cancelled; no further answers will be sent.")
        return proj
    saver.before_dispatch = guard
    try:
        with AttemptLock(t["attempt_id"]):
            try:
                for i, e in enumerate(edits, start=1):
                    proj = guard()
                    fresh = {q["question_attempt_id"]: q for q in proj["questions"]}
                    q = fresh[e.question_attempt_id]
                    now_sel = q["selected_choice_ids"]
                    if set(now_sel) == set(e.after):
                        e.outcome = "unchanged"
                        continue
                    if set(now_sel) != set(e.before):
                        raise AnswersRefusal("conflict", "Answer changed in Blackboard during this run; left alone.")
                    raw = proj["raw"][e.question_attempt_id]
                    body = {"questionType": raw["questionType"],
                            "givenAnswer": encode_given(q["type"], [c["id"] for c in q["choices"]], e.after)}
                    # The transport calls this again after browser startup, immediately before sending.
                    def dispatch_guard():
                        latest = guard()
                        latest_q = next(q for q in latest["questions"] if q["question_attempt_id"] == e.question_attempt_id)
                        if set(latest_q["selected_choice_ids"]) != set(now_sel):
                            raise AnswersRefusal("conflict", "Answer changed before dispatch; stopped.")
                    saver.before_dispatch = dispatch_guard
                    save_index = sum(x.status == "change" for x in edits[:i])
                    save_total = sum(x.status == "change" for x in edits)
                    progress(f"Saving question {e.number} ({save_index}/{save_total})…")
                    try:
                        resp = saver.patch(f"/learn/api/v1/courses/{t['course_id']}/gradebook/attempts/"
                                           f"{t['attempt_id']}/assessment/answers/{e.question_attempt_id}", body)
                        if not isinstance(resp, dict) or resp.get("attemptStatus") != "IN_PROGRESS" or "givenAnswer" not in resp or resp["givenAnswer"] != body["givenAnswer"]:
                            raise WriteFailed("unknown", "Unexpected answer-save response")
                    except (NotDispatched, AnswersRefusal):
                        raise
                    except Exception as exc:
                        why = exc if isinstance(exc, WriteFailed) else WriteFailed("unknown", f"Save response failed: {type(exc).__name__}")
                        e.outcome, e.detail = "unknown", str(why)
                        _confirm_question(state, e)  # GET reconciliation even for malformed responses
                        stop_reason = why.kind
                        break
                    if not _confirm_question(state, e):
                        e.outcome, e.detail = "unknown", "Blackboard did not confirm persistence; stopped"
                        stop_reason = "unknown"
                        break
                    e.outcome = "saved_verified"
                    if saver.used == "http":
                        saver.http_persisted = True
            except (AnswersRefusal, NotDispatched) as why:
                if not saver.dispatch_count:
                    if isinstance(why, NotDispatched):
                        raise AnswersRefusal("transport_unavailable", str(why)) from None
                    raise
                e.outcome = "conflict" if getattr(why, "code", None) == "conflict" else "not_saved"
                e.detail, stop_reason = str(why), getattr(why, "code", "transport_unavailable")
            except KeyboardInterrupt:
                cancel_event.set()
                if not saver.dispatch_count:
                    raise AnswersRefusal("cancelled", "Cancelled before any answer was sent.") from None
                if e.outcome is None:
                    e.outcome, e.detail = "unknown", "Interrupted after dispatch; check Blackboard"
                stop_reason = "cancelled"
            except Exception as exc:
                if not saver.dispatch_count:
                    raise AnswersRefusal("read_failed", f"Could not validate state: {type(exc).__name__}") from None
                e.outcome, e.detail = "unknown", f"Reconciliation failed: {type(exc).__name__}"
                stop_reason = "unknown"
            if stop_reason:
                for pending in edits:
                    if pending.outcome is None:
                        pending.outcome, pending.detail = "not_saved", "Stopped before this answer was sent"
            final = _final_readback(state, edits)
    finally:
        try:
            saver.close()
        except Exception as exc:
            progress(f"Transport cleanup failed: {type(exc).__name__}")
    dispatched = bool(saver.dispatch_count)
    outcome = _overall(edits, stop_reason, final)
    if not edits and final.get("ok") and final.get("status") == "IN_PROGRESS":
        outcome = "unchanged"
    result = _result(state, edits, outcome, final, transport=saver.used if dispatched else None, stop_reason=stop_reason)
    result["dispatched"] = dispatched
    return result


def _final_readback(state: Dict[str, Any], edits: List[PlannedEdit]) -> Dict[str, Any]:
    t = state["target"]
    try:
        detail = _get(f"/learn/api/v1/courses/{t['course_id']}/gradebook/attempts/{t['attempt_id']}?expand=toolAttemptDetail", "the final attempt")
        proj = project_attempt(detail)
        current = {q["question_attempt_id"]: q["selected_choice_ids"] for q in proj["questions"]}
        valid = detail.get("status") == "IN_PROGRESS" and _definition(proj) == state["sha"]
        touched = {e.question_attempt_id for e in edits}
        outside = [q["number"] for q in state["projection"]["questions"]
                   if q["question_attempt_id"] not in touched and
                   (q["question_attempt_id"] not in current or set(current[q["question_attempt_id"]]) != set(q["selected_choice_ids"]))]
        for e in edits:
            matches = valid and e.question_attempt_id in current and set(current[e.question_attempt_id]) == set(e.after)
            if not matches and e.outcome in ("saved_verified", "unchanged", None):
                e.outcome, e.detail = "unknown", "Requested answer absent or changed in the final snapshot"
        return {"ok": True, "status": detail.get("status"), "outside_changes": outside,
                "observed_at": _now().isoformat(timespec="seconds"), "current": current,
                "answered": sum(bool(q["selected_choice_ids"]) for q in proj["questions"]), "total": len(proj["questions"])}
    except Exception as exc:
        for e in edits:
            if e.outcome in ("saved_verified", "unchanged", None):
                e.outcome, e.detail = "unknown", "Final snapshot unavailable"
        return {"ok": False, "error": str(exc)}


def _overall(edits: List[PlannedEdit], stop_reason: Optional[str], final: Dict[str, Any]) -> str:
    outcomes = {e.outcome for e in edits}
    if not final.get("ok") or final.get("status") != "IN_PROGRESS" or "unknown" in outcomes:
        return "unknown"
    if stop_reason or final.get("outside_changes") or outcomes - {"saved_verified", "unchanged"}:
        return "partial" if "saved_verified" in outcomes else "refused"
    return "unchanged" if outcomes <= {"unchanged"} else "saved_verified"


def _result(state, edits, outcome, final, transport, stop_reason=None) -> Dict[str, Any]:
    t = state["target"]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "answers_result",
        "outcome": outcome,
        "stop_reason": stop_reason,
        "transport": transport,
        "concurrency": "baseline re-read before each save (Blackboard offers no conditional write)",
        "target": {k: t[k] for k in ("course_name", "title", "course_id", "assessment_id", "attempt_id")},
        "due": state["due"],
        "attempt_status_after": final.get("status") if final and final.get("ok") else None,
        "answered_after": (f"{final['answered']}/{final['total']}" if final and final.get("ok") else None),
        "outside_changes": (final or {}).get("outside_changes") or [],
        "submitted": False,
        "questions": [_edit_json(e) for e in edits],
    }


def _edit_json(e: PlannedEdit) -> Dict[str, Any]:
    text = {c["id"]: c["text"] for c in e.choices}
    return {"number": e.number, "question_attempt_id": e.question_attempt_id, "type": e.kind,
            "status": e.status, "outcome": e.outcome or ("preview" if e.status == "change" else "unchanged"),
            "detail": e.detail, "before": [text[i] for i in e.before], "after": [text[i] for i in e.after],
            "after_choice_ids": e.after}


def preview(doc: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    edits = prepare(doc, state)
    res = _result(state, edits, "preview", None, transport=None)
    res["attempt_status_after"] = None
    return res


# ---------------------------------------------------------------------------
# Safe file output
# ---------------------------------------------------------------------------

def validate_destination(path: Path, overwrite: bool = False) -> None:
    if path.is_symlink():
        raise AnswersInputError(f"{path} is a symlink; refusing to write")
    if path.exists() and not overwrite:
        raise AnswersInputError(f"{path} already exists (pass --overwrite to replace it)")
    if path.exists() and not path.is_file():
        raise AnswersInputError(f"{path} is not a regular file")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.close(fd)
        os.unlink(tmp)
    except OSError as exc:
        raise AnswersInputError(f"cannot write {path}: {exc}") from None


def write_private(path: Path, text: str, overwrite: bool = False) -> None:
    validate_destination(path, overwrite)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            fh.write(text)
        if path.is_symlink():
            raise AnswersInputError(f"{path} is a symlink; refusing to write")
        if path.exists() and not overwrite:
            raise AnswersInputError(f"{path} already exists (pass --overwrite to replace it)")
        os.replace(tmp, path)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# Human output
# ---------------------------------------------------------------------------

def _when(due: Optional[str]) -> str:
    if not due:
        return "No due date"
    dt = datetime.fromisoformat(due.replace("Z", "+00:00")).astimezone()
    stamp = dt.strftime("%a %b %-d, %-I:%M %p %Z") if sys.platform != "win32" else dt.strftime("%a %b %d, %I:%M %p")
    left = time_left(due)
    if left is None:
        return f"Due {stamp}"
    if left.total_seconds() <= 0:
        return f"Due {stamp} (past due)"
    mins = int(left.total_seconds() // 60)
    span = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"
    return f"Due {stamp} ({span} left)"


def _course(name: str) -> str:
    m = re.match(r"([A-Z]{2,5}\s?\d{3}[A-Z]?)", name or "")
    return m.group(1) if m else (name or "")


def _header(target: Dict[str, Any], due: Optional[str], status: str) -> List[str]:
    return [target["title"], f"{_course(target['course_name'])} · {status} · {_when(due)}", ""]


def _wrap(text: str, indent: str, width: int = 88) -> List[str]:
    import textwrap
    lines = []
    for para in (text or "").splitlines() or [""]:
        lines += textwrap.wrap(para, width - len(indent), break_long_words=False) or [""]
    return [indent + ln for ln in lines]


def _hint(q: Dict[str, Any]) -> str:
    if q["type"] == "multiple_answer" and "select all" not in q["prompt"].lower():
        return "  (select all that apply)"
    return ""


def _hang(first: str, text: str, indent: str, width: int = 88) -> List[str]:
    """First line starts with ``first``; continuation lines align under the text."""
    lines = _wrap(text, " " * len(first), width)
    lines[0] = first + lines[0][len(first):]
    return [ln if i == 0 else indent + ln[len(first):] if len(indent) >= len(first) else ln for i, ln in enumerate(lines)]


def _box(q: Dict[str, Any], on: bool) -> str:
    """[x] for select-all questions, (•) for pick-one, so the two read differently at a glance."""
    if q["type"] == "multiple_answer":
        return "[x]" if on else "[ ]"
    return "(•)" if on else "( )"


def _short(text: str, width: int = 58) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= width else one[: width - 1].rstrip() + "…"


def _clock() -> str:
    now = datetime.now().astimezone()
    return now.strftime("%-I:%M %p %Z") if sys.platform != "win32" else now.strftime("%I:%M %p").lstrip("0")


def _unsupported_lines(unsupported: List[Dict[str, Any]]) -> List[str]:
    return [f"{u['number']:>2}. Answer in Blackboard (bb can't save {u['provider_type']}): {_short(u['prompt'], 50)}"
            for u in unsupported]


def format_current(state: Dict[str, Any], started: bool = False) -> str:
    proj = state["projection"]
    qs = proj["questions"]
    answered = sum(1 for q in qs if q["selected_choice_ids"])
    out = _header(state["target"], state["due"], "Attempt open")
    out.append(f"{answered} of {len(qs)} answered (saved in Blackboard, not submitted)")
    out.append("")
    for q in qs:
        out += _hang(f"{q['number']:>2}. ", q["prompt"] + _hint(q), "    ")
        for i, c in enumerate(q["choices"]):
            out += _hang(f"     {_box(q, c['id'] in q['selected_choice_ids'])} {choice_letter(i)}. ", c["text"], "            ")
        out.append("")
    if proj["unsupported"]:
        out += _unsupported_lines(proj["unsupported"]) + [""]
    out.append("New attempt started; no answers changed. Nothing submitted." if started else "Read only; nothing was changed.")
    return "\n".join(out)


_TAGS = {"preview": "will change", "saved_verified": "saved ✓", "unchanged": "already set",
         "acknowledged": "sent, not confirmed", "unknown": "NOT CONFIRMED: check it in Blackboard",
         "not_saved": "not saved", "conflict": "CHANGED IN BLACKBOARD: left alone"}


def format_result(res: Dict[str, Any], questions: Dict[str, Dict[str, Any]], next_cmd: str = "", verbose: bool = False) -> str:
    """Before/after view for preview and apply. Unchanged questions collapse to one line."""
    t, outcome = res["target"], res["outcome"]
    items = res["questions"]
    changed = [i for i in items if i["status"] == "change"]
    saved = [i for i in items if i["outcome"] == "saved_verified"]
    same = [i for i in items if i["outcome"] == "unchanged"]
    bad = [i for i in items if i["outcome"] in ("unknown", "not_saved", "conflict", "acknowledged")]
    nothing_to_do = not changed and outcome in ("preview", "unchanged")
    status = "Already up to date" if nothing_to_do else {
        "preview": "Preview", "saved_verified": "Saved, not submitted", "partial": "PARTLY SAVED",
        "unknown": "SAVE NOT CONFIRMED", "refused": "Not saved"}.get(outcome, outcome)
    out = _header(t, res["due"], status)
    if bad:
        nums = ", ".join(str(i["number"]) for i in bad)
        out += [f"⚠ {len(bad)} answer{'s' if len(bad) != 1 else ''} NOT confirmed (question{'s' if len(bad) != 1 else ''} {nums}). "
                "Check in Blackboard before retrying; nothing was submitted.", ""]
    if res.get("outside_changes"):
        out += ["⚠ Other answers changed in Blackboard during this run (questions "
                + ", ".join(map(str, res["outside_changes"])) + "); they were left as they are.", ""]
    for item in items:
        q = questions[item["question_attempt_id"]]
        tag = _TAGS.get(item["outcome"], item["outcome"])
        if item["outcome"] == "unchanged" and not verbose:
            out.append(f"{q['number']:>2}. {_short(q['prompt'])}".ljust(66) + f"  {tag}")
            continue
        before = set(q["selected_choice_ids"])
        after = set(item["after_choice_ids"])
        out += _hang(f"{q['number']:>2}. ", q["prompt"] + _hint(q), "    ")
        out.append(f"    → {tag}" + (f" ({item['detail']})" if item.get("detail") else ""))
        for i, c in enumerate(q["choices"]):
            was, now = c["id"] in before, c["id"] in after
            sign = "+" if now and not was else "-" if was and not now else " "
            out += _hang(f"   {sign} {_box(q, now)} {choice_letter(i)}. ", c["text"], "            ")
        out.append("")
    if out[-1] != "":
        out.append("")
    if nothing_to_do:
        out.append(f"Nothing to save: all {len(items)} answers already match your selections. Blackboard was not changed.")
        return "\n".join(out)
    if outcome == "preview":
        out.append(f"Preview only; nothing saved yet. {len(changed)} to change, {len(items) - len(changed)} already set.")
        out.append(f"To save {'this answer' if len(changed) == 1 else f'these {len(changed)} answers'} "
                   "(stays unsubmitted; saved answers may be visible to course staff):")
        if next_cmd:
            out.append(f"  {next_cmd} --apply")
        return "\n".join(out)
    line = f"Saved {len(saved)} answer{'s' if len(saved) != 1 else ''}"
    line += f" ({len(same)} already set)." if same else "."
    if res["attempt_status_after"]:
        state_word = "still open" if res["attempt_status_after"] == "IN_PROGRESS" else res["attempt_status_after"].replace("_", " ").lower()
        answered = (res.get("answered_after") or "").replace("/", " of ")
        line += f" Checked in Blackboard at {_clock()}: attempt {state_word}, {answered} answered."
    out.append(line)
    if res["attempt_status_after"] == "IN_PROGRESS":
        out.append("NOT SUBMITTED. Open the test in Blackboard to review and submit.")
    else:
        out.append(f"This command sent no submit action. Blackboard now reports {res['attempt_status_after'] or 'UNKNOWN'}.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI entry (called from main.py; returns the process exit code)
# ---------------------------------------------------------------------------

EXIT_OK, EXIT_INPUT, EXIT_REFUSED, EXIT_PARTIAL = 0, 2, 3, 4


def run_cli(args, courses: Dict[str, str], target_cids: List[str], cancel_event=None) -> int:
    as_json = bool(getattr(args, "json", False))
    start_receipt = None
    def progress(msg: str) -> None:  # stderr only, so --json stdout stays one document
        print(msg, file=sys.stderr)

    def emit(payload: Dict[str, Any], human: str, code: int) -> int:
        if start_receipt:
            payload["new_attempt"] = start_receipt
            if start_receipt["outcome"] == "created_verified":
                human = f"Started attempt {start_receipt['attempt_id']}; confirmed IN_PROGRESS by GET. Nothing submitted.\n\n" + human
        text = json.dumps(payload, indent=None if getattr(args, "compact", False) else 2, ensure_ascii=False)
        if getattr(args, "out", None):
            try:
                write_private(Path(args.out), text + "\n", overwrite=getattr(args, "overwrite", False))
                progress(f"Result written to {args.out}")
            except (AnswersInputError, OSError) as exc:
                progress(f"Could not write receipt to {args.out}: {exc}")
        print(text if as_json else human)
        return code

    def fail(code: int, kind: str, message: str, **details: Any) -> int:
        payload = {"schema_version": SCHEMA_VERSION, "kind": "answers_result", "outcome": "unknown" if code == EXIT_PARTIAL else "refused" if code == EXIT_REFUSED else "invalid_input",
                   "error": kind, "message": message, "submitted": False, **({"details": details} if details else {})}
        if start_receipt:
            payload["new_attempt"] = start_receipt
            if getattr(args, "out", None):
                try:
                    write_private(Path(args.out), json.dumps(payload, indent=2) + "\n", overwrite=getattr(args, "overwrite", False))
                except (AnswersInputError, OSError) as exc:
                    progress(f"Could not write receipt to {args.out}: {exc}")
        if as_json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"bb answers: {message}", file=sys.stderr)
            matches = details.get("matches", [])
            width = max((len(m["title"]) for m in matches), default=0)
            for n, m in enumerate(matches, start=1):
                due = f"  {_when(m['due'])}" if m.get("due") else "  no due date"
                print(f"  {n}. {m['title'].ljust(width)}  {m['assessment_id']}{due}", file=sys.stderr)
            if matches:
                pick = next((m for m in matches if m.get("due")), matches[0])
                course_code = _course(pick["course_name"]).replace(" ", "")
                print(f"Re-run with the ID of the one you want, e.g.:\n  bb answers {pick['assessment_id']} -c {course_code}", file=sys.stderr)
            for hint in details.get("hints", []):
                print(f"  {hint}", file=sys.stderr)
        return code

    target_arg = f'"{args.target}"' if not args.target.startswith("_") else args.target
    base_cmd = f"bb answers {target_arg}" + (f" -c {args.course}" if getattr(args, "course", None) else "")
    try:
        sets = getattr(args, "set", None) or []
        new_attempt = bool(getattr(args, "new_attempt", False))
        parsed_sets = parse_sets(sets)
        if sets and (args.file or args.export):
            raise AnswersInputError("--set cannot be combined with --file or --export")
        if new_attempt and (args.file or getattr(args, "attempt", None)):
            raise AnswersInputError("--new-attempt cannot be combined with --file or --attempt; use --set for a fresh attempt")
        if args.apply and not (args.file or sets):
            return fail(EXIT_INPUT, "invalid_input", "--apply only works with --file or --set.", hints=[
                f"1. {base_cmd} --export answers.json   (write a template)",
                "2. edit the \"changes\" list in answers.json",
                f"3. {base_cmd} --file answers.json          (preview)",
                f"4. {base_cmd} --file answers.json --apply  (save; never submits)"])
        if args.export and args.file:
            raise AnswersInputError("use either --export or --file, not both")
        if args.export:
            validate_destination(Path(args.export), getattr(args, "overwrite", False))
        if getattr(args, "out", None):
            validate_destination(Path(args.out), getattr(args, "overwrite", False))
        doc = None
        if args.file:
            path = Path(args.file)
            try:
                doc = parse_file(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError) as exc:
                raise AnswersInputError(f"cannot read {path}: {exc}") from None
        cids = target_cids or ([doc["target"]["course_id"]] if doc else []) or list(courses)
        if len(cids) > 3 and not args.target.startswith("_") and not parse_target_url(args.target):
            codes = ", ".join(sorted({_course(n).replace(" ", "") for n in courses.values()})[:5])
            raise AnswersInputError(f"name the course with -c, e.g. -c {codes.split(', ')[0]} (yours: {codes})")

        match = resolve_target(args.target, cids, courses)
        progress(f"Reading {match['title']} from Blackboard…")
        if new_attempt:
            from scrapers.answer_attempts import ensure_attempt
            state, start_receipt = ensure_attempt(match, progress, parsed=parsed_sets, cancel_event=cancel_event)
        else:
            state = load_state(match, attempt_id=getattr(args, "attempt", None) or (doc["target"]["attempt_id"] if doc else None),
                               check_safety=doc is not None or bool(sets))
        if sets:
            doc = export_doc(state)
            doc["_runtime_baseline"] = True  # never loaded from a file; no file-age expiry
            doc["changes"] = set_changes(parsed_sets, state["projection"])
            prepare(doc, state)  # same cardinality/baseline/fingerprint validation as file mode

        if args.export:
            exported = export_doc(state)
            write_private(Path(args.export), json.dumps(exported, indent=2, ensure_ascii=False) + "\n",
                          overwrite=getattr(args, "overwrite", False))
            n = len(exported["snapshot"]["questions"])
            until = datetime.fromisoformat(exported["snapshot"]["expires_at"]).strftime("%-I:%M %p" if sys.platform != "win32" else "%I:%M %p")
            unsup = (f"\n{len(exported['unsupported'])} question(s) must be answered in Blackboard itself."
                     if exported["unsupported"] else "")
            human = (f"Exported {n} questions to {args.export}. No answers were changed.{unsup}\n"
                     f"Next: add entries to \"changes\" (see \"example_change\"), then preview. "
                     f"The file is valid until {until}; export again after that.\n"
                     f"  {base_cmd} --file {args.export}")
            payload = {"schema_version": SCHEMA_VERSION, "kind": "answers_export", "outcome": "exported",
                       "path": str(args.export), "questions": n, "unsupported": len(exported["unsupported"]),
                       "expires_at": exported["snapshot"]["expires_at"], "submitted": False}
            return emit(payload, human, EXIT_OK)

        if doc is None:
            payload = {"schema_version": SCHEMA_VERSION, "kind": "answers_current", "outcome": "read",
                       "target": {k: state["target"][k] for k in ("course_name", "title", "course_id", "assessment_id", "attempt_id")},
                       "due": state["due"], "questions": state["projection"]["questions"],
                       "unsupported": state["projection"]["unsupported"], "submitted": False}
            current_text = format_current(state, started=bool(start_receipt and start_receipt["dispatched"]))
            return emit(payload, current_text, EXIT_OK)

        by_id = {q["question_attempt_id"]: q for q in state["projection"]["questions"]}
        if not args.apply:
            import shlex
            res = preview(doc, state)
            next_cmd = base_cmd + (" " + " ".join("--set " + shlex.quote(value) for value in sets) if sets else f" --file {shlex.quote(args.file)}")
            return emit(res, format_result(res, by_id, next_cmd=next_cmd), EXIT_OK)

        res = apply(doc, state, progress, cancel_event=cancel_event)
        code = EXIT_OK if res["outcome"] in ("saved_verified", "unchanged") else (EXIT_PARTIAL if res["dispatched"] else EXIT_REFUSED)
        if code != EXIT_OK and start_receipt and start_receipt["dispatched"]:
            code = EXIT_PARTIAL
        return emit(res, format_result(res, by_id), code)
    except AnswersInputError as exc:
        return fail(EXIT_PARTIAL if start_receipt and start_receipt["dispatched"] else EXIT_INPUT, "invalid_input", str(exc))
    except AnswersRefusal as exc:
        started = (start_receipt and start_receipt["dispatched"]) or (exc.details.get("start") or {}).get("dispatched")
        return fail(EXIT_PARTIAL if started else EXIT_REFUSED, exc.code, str(exc), **exc.details)
    except Exception as exc:
        from scrapers.answer_attempts import StartUnknown
        if isinstance(exc, StartUnknown):
            start_receipt = exc.receipt
            return fail(EXIT_PARTIAL, "start_unknown", str(exc))
        if start_receipt and start_receipt["dispatched"]:
            return fail(EXIT_PARTIAL, "after_start_failed", f"Attempt started; subsequent operation failed ({type(exc).__name__}). Check Blackboard.")
        raise


def input_error(args, message: str) -> int:
    """One versioned JSON error for main.py course-resolution failures, too."""
    if getattr(args, "json", False):
        print(json.dumps({"schema_version": SCHEMA_VERSION, "kind": "answers_result",
                          "outcome": "invalid_input", "error": "invalid_input",
                          "message": message, "submitted": False}))
    else:
        print(f"bb answers: {message}", file=sys.stderr)
    return EXIT_INPUT
