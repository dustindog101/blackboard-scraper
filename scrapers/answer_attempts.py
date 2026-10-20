"""Explicit untimed attempt creation. Separate allowlist from the answer PATCH adapter.

HTTP route and body observed live from the Ultra UI (2026-10-20, 201 Created). No submit route.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.request
from copy import deepcopy
from urllib.parse import urlsplit

from core import rest
from core.assessment_answers import AnswersInputError, AnswersRefusal, plan_changes, project_attempt, set_changes
from scrapers import answers as a

_START_PATH = re.compile(rf"/learn/api/v1/courses/{a._ID}/gradebook/columns/{a._ID}/attempts")
# Exact body the Ultra UI sent for "Start attempt 2" (observed live 2026-10-20, HTTP 201 with the new attempt).
_START_BODY = {"stagedAttemptGrades": [],
               "scoreProviderHandle": {"name": "resource/x-bb-assessment", "iconClass": "test",
                                       "contentHandlers": ["resource/x-bb-asmt-test-link"],
                                       "needsGradingIcon": "grades", "needsReconcilingIcon": "grades"},
               "status": "IN_PROGRESS",
               "toolAttemptDetail": {"resource/x-bb-assessment": {"type": "Test"}}}


class StartUnknown(RuntimeError):
    def __init__(self, receipt):
        super().__init__(receipt["message"])
        self.receipt = receipt


def validate_start(path, body):
    if not _START_PATH.fullmatch(path) or body != _START_BODY:
        raise AnswersInputError("Refusing non-allowlisted attempt creation request")
    return json.dumps(body, separators=(",", ":"))


def http_start(path, body, dispatch):
    payload = validate_start(path, body)
    cookies, token = rest.cookie_header(), rest.xsrf_token()
    if not cookies or not token:
        raise a.NotDispatched("no saved session or XSRF token before start")
    from core.session_state import user_agent
    request = urllib.request.Request(a.BLACKBOARD_BASE + path, data=payload.encode(), method="POST", headers={
        "Cookie": cookies, "x-blackboard-xsrf": token, "User-Agent": user_agent(),
        "Content-Type": "application/json;charset=UTF-8", "Accept": "application/json",
        "Origin": a.BLACKBOARD_BASE, "Referer": a.BLACKBOARD_BASE + "/ultra/stream"})
    dispatch()
    # Every failure after dispatch, including 401/404, is reconciled by GET only.
    return json.loads(a._open_no_redirect(request).decode("utf-8"))


class BrowserStarter(a.BrowserTransport):
    """One exact start-button click; all non-GET requests blocked except one start POST."""

    launch_options = {"service_workers": "block"}  # service-worker traffic bypasses Playwright routes

    def start(self, match, path, body, dispatch):
        validate_start(path, body)
        self._open()  # shared installed-browser -> optional bundled Playwright launcher
        page = self._page
        armed = False
        sent = False
        blocked = None  # exceptions raised inside a Playwright route callback are swallowed; re-raise after the click

        def intercept(route):
            nonlocal sent, blocked
            request = route.request
            if request.method in {"GET", "HEAD", "OPTIONS"}:
                route.continue_()
                return
            if armed and not sent and request.method == "POST" and request.url == a.BLACKBOARD_BASE + path:
                try:
                    validate_start(path, request.post_data_json)
                    dispatch()  # fresh safety/count/deadline check immediately before dispatch
                except BaseException as exc:
                    blocked = exc
                    route.abort()
                    return
                sent = True
                route.continue_()
                return
            route.abort()  # includes every submit/finalize/autosave/other start route

        self._ctx.route("**/*", intercept)
        try:
            # Observed: the details page lives at /assessment/{id}/overview (the /outline/... form 404s).
            page.goto(f"{a.BLACKBOARD_BASE}/ultra/courses/{match['course_id']}/assessment/{match['assessment_id']}"
                      f"/overview?courseId={match['course_id']}", wait_until="domcontentloaded", timeout=30000)
            if (urlsplit(page.url).scheme, urlsplit(page.url).netloc) != (urlsplit(a.BLACKBOARD_BASE).scheme, urlsplit(a.BLACKBOARD_BASE).netloc):
                raise AnswersRefusal("auth", "Assessment page left Blackboard; run bb login.")
            # Observed: an <a> labelled "Start attempt 2" (role link), not a button.
            label = re.compile(r"^\s*Start (?:new )?attempt(?: \d+)?\s*$", re.I)
            button = page.get_by_role("link", name=label).or_(page.get_by_role("button", name=label))
            button.first.wait_for(state="visible", timeout=10000)
            self._check_prompts(page)
            if button.count() != 1:
                raise AnswersRefusal("ambiguous_start", "Expected one Start attempt button; refusing.")
            armed = True
            try:
                with page.expect_response(lambda response: response.request.method == "POST"
                                          and response.url == a.BLACKBOARD_BASE + path,
                                          timeout=10000):
                    button.click(timeout=5000)
                    # A warning may appear instead of dispatch. Never confirm it.
                    self._check_prompts(page)
            except Exception:
                if blocked is not None:  # aborted by our own guard: report its reason, not a Playwright timeout
                    raise blocked from None
                raise
            if blocked is not None:
                raise blocked
            if not sent:
                raise AnswersRefusal("start_not_sent", "No allowlisted start request observed; nothing retried. Check Blackboard.")
        finally:
            armed = False
            # Keep the disarmed interceptor until close: a delayed UI request must
            # stay blocked, including requests after a click timeout or refusal.

    @staticmethod
    def _check_prompts(page):
        warning = page.locator(".time-limit-warning, [data-analytics-id*='time-limit'], input[type='password'], "
                               "input[name*='accessCode' i], input[name*='password' i]")
        for index in range(warning.count()):
            if warning.nth(index).is_visible():
                raise AnswersRefusal("unsafe_prompt", "Time-limit warning or access-code prompt; refusing to start.")
        text = page.get_by_text(re.compile(r"(?:this (?:test|assessment) (?:has a time limit|is timed))|(?:enter|provide).*(?:access code|assessment password)", re.I))
        for index in range(text.count()):
            if text.nth(index).is_visible():
                raise AnswersRefusal("unsafe_prompt", "Time-limit warning or access-code prompt; refusing to start.")


def _eligible(match):
    try:
        inventory = a.load_inventory(match, complete=True)  # _safe_due is shared with answer apply
    except (KeyError, TypeError, ValueError, AttributeError):
        raise AnswersRefusal("unknown_attempt_count", "Test or attempt metadata is unknown; refusing to start.") from None
    a.check_window(inventory)
    active = [att for att in inventory["attempts"] if att["status"] == "IN_PROGRESS"]
    if active:
        return inventory, active
    settings = a._test_from_content(inventory["content"])["deploymentSettings"]
    # Ultra calls this attemptCount. Accept the explicit attemptsAllowed spelling too;
    # contradictory values, unlimited sentinels and unknown counts fail closed.
    values = [settings[k] for k in ("attemptsAllowed", "attemptCount") if k in settings]
    if not values or any(type(v) is not int or v < 1 for v in values) or len(set(values)) != 1:
        raise AnswersRefusal("unknown_attempt_count", "Attempts allowed is unknown; refusing to start.")
    remaining = values[0] - len(inventory["attempts"])
    if remaining <= 0:
        raise AnswersRefusal("no_attempts_remaining", "No attempts remaining; refusing to start.")
    return inventory, []


def _preflight_sets(parsed, inventory):
    if not parsed:
        return
    # Retakes have a previous question list. Validate the entire shorthand before
    # spending an attempt; if it is hidden/unavailable, require a separate start.
    candidates = inventory["attempts"]
    if not candidates:
        raise AnswersRefusal("questions_unavailable", "Cannot validate --set before starting. Start with --new-attempt alone, then preview --set.")
    latest = max(candidates, key=lambda att: att.get("attemptDate") or att.get("creationDate") or "")
    projection = project_attempt(latest)
    doc = {"snapshot": {"definition_sha256": a._definition(projection), "questions": projection["questions"]},
           "changes": set_changes(parsed, projection)}
    plan_changes(doc, projection, a._definition(projection))
    return question_shape(projection)


def question_shape(projection):
    """Question identity, type and choice IDs/text in provider order. Equal across attempts of the same test
    (verified live on two J#5 attempts); used so --set letters validated before a retake mean the same choices after it."""
    return [(q["question_id"], q["type"], [(c["id"], c["text"]) for c in q["choices"]]) for q in projection["questions"]]


def ensure_attempt(match, progress, parsed=None, cancel_event=None, http=None, browser_factory=None):
    """Return (fresh state, start receipt). Never retry a dispatched start."""
    http = http or http_start
    browser_factory = browser_factory or BrowserStarter
    cancel_event = cancel_event or threading.Event()
    sent = False
    used = "http"
    browser = None
    receipt = {"requested": True, "outcome": "unknown", "dispatched": False, "attempt_id": None,
               "transport": None, "submitted": False}
    # Serialize starts by assessment (the future attempt ID is not known yet).
    with a.AttemptLock("start-" + match["course_id"] + "-" + match["assessment_id"]):
        before, active = _eligible(match)
        if active:
            state = a.state_from_inventory(match, before)
            progress(f"Attempt {state['target']['attempt_id']} is already IN_PROGRESS; using it. No new attempt started.")
            return state, {**receipt, "outcome": "existing", "attempt_id": state["target"]["attempt_id"]}
        shape = _preflight_sets(parsed, before)
        old_ids = {att["id"] for att in before["attempts"]}
        path = f"/learn/api/v1/courses/{match['course_id']}/gradebook/columns/{match['column_id']}/attempts"
        validate_start(path, _START_BODY)

        def dispatch():
            nonlocal sent
            if sent:
                raise AnswersRefusal("already_dispatched", "Start already dispatched; refusing a retry.")
            fresh, active_now = _eligible(match)
            if active_now or fresh["me"]["id"] != before["me"]["id"] or {att["id"] for att in fresh["attempts"]} != old_ids:
                raise AnswersRefusal("attempts_changed", "Attempt inventory/account changed before start; refusing.")
            _preflight_sets(parsed, fresh)
            if cancel_event.is_set():
                raise AnswersRefusal("cancelled", "Cancelled before starting an attempt.")
            sent = True
            receipt.update(dispatched=True, transport=used)

        failure = None
        try:
            try:
                http(path, deepcopy(_START_BODY), dispatch)
            except a.NotDispatched as exc:
                if sent:
                    raise RuntimeError("Start dispatched; no fallback") from exc
                progress(f"HTTP start unavailable ({exc}); using installed browser, then optional bundled Playwright.")
                used = "browser"
                browser = browser_factory(progress)
                browser.start(match, path, deepcopy(_START_BODY), dispatch)
                used = getattr(browser, "label", "browser")
                receipt["transport"] = used
        except BaseException as exc:
            if not sent:
                if isinstance(exc, (AnswersRefusal, AnswersInputError, KeyboardInterrupt)):
                    raise
                raise AnswersRefusal("transport_unavailable", f"No start dispatched ({type(exc).__name__}).") from None
            failure = type(exc).__name__
        finally:
            if browser:
                browser.close()
        if not sent:
            raise AnswersRefusal("start_not_sent", "No start dispatched.")
        # Bounded read-only reconciliation. Never repeat the POST or button click.
        for index in range(3):
            try:
                after = a.load_inventory(match, check_safety=False, complete=True)
                new = [att for att in after["attempts"] if att["id"] not in old_ids]
                active_after = [att for att in after["attempts"] if att["status"] == "IN_PROGRESS"]
                ids_after = {att["id"] for att in after["attempts"]}
                if (after["me"]["id"] == before["me"]["id"] and len(new) == len(active_after) == 1
                        and new[0]["id"] == active_after[0]["id"] and old_ids <= ids_after):
                    state = a.state_from_inventory(match, after, new[0]["id"])
                    receipt.update(outcome="created_verified", attempt_id=new[0]["id"], transport=used)
                    if shape is not None and question_shape(state["projection"]) != shape:
                        receipt["questions_changed"] = True
                        raise AnswersRefusal("questions_changed_after_start",
                                             f"Started attempt {new[0]['id']}, but its questions differ from the previous attempt, "
                                             "so no answers were saved. Preview again with --set, then --apply.",
                                             start=dict(receipt))
                    if failure:
                        receipt["acknowledgment_error"] = failure
                    progress(f"Started attempt {new[0]['id']}; GET confirms exactly one new IN_PROGRESS attempt. Nothing submitted.")
                    return state, receipt
                if new or active_after:
                    break  # contradictory result, not propagation lag
            except AnswersRefusal as exc:
                if exc.code == "questions_changed_after_start":
                    raise  # attempt is confirmed; only the answer mapping is unsafe
            except Exception:
                pass
            if index < 2:
                a.time.sleep(index + 1)
        receipt["message"] = "Start dispatched, but exactly one new IN_PROGRESS attempt could not be confirmed. Nothing retried or submitted. Inspect Blackboard before retrying."
        raise StartUnknown(receipt)
