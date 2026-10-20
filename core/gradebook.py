"""The single Gradable Item status model.

`grades`, `due`, `briefing` and `assignments` all read gradebook state through this module, so a given
item has one status everywhere, including in-progress work that has not been submitted.
Everything here is GET-only and REST-only (ADR-0002, ADR-0003).

Per course it reads columns and the student's whole gradebook. Ambiguous submission claims require
targeted attempt metadata; discussions require the student's own published posts. Neither path reads answers.
"""
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

from core.time import parse_datetime, to_iso

GRADED, NEEDS_GRADING, SUBMITTED, NOT_SUBMITTED, IN_PROGRESS, EXEMPT, UNKNOWN = (
    "graded", "needs_grading", "submitted", "not_submitted", "in_progress", "exempt", "unknown",
)
STATUSES = (GRADED, NEEDS_GRADING, SUBMITTED, NOT_SUBMITTED, IN_PROGRESS, EXEMPT, UNKNOWN)
# Work the student has handed in (or that needs nothing more from them).
COMPLETED = frozenset({GRADED, NEEDS_GRADING, SUBMITTED, EXEMPT})

# More than ~6 simultaneous requests makes this server stall every burst by ~2 s (measured), so stay below it.
MAX_CONNECTIONS = 6
OFFICIAL_NAMES = ("overall grade", "weighted total", "total points earned", "total")

EXPIRED, FORBIDDEN, NOT_FOUND, UNAVAILABLE = "expired", "forbidden", "not_found", "unavailable"
_HTTP_ERRORS = {401: EXPIRED, 403: FORBIDDEN, 404: NOT_FOUND}

_user_lock = Lock()
_user_cache: Dict[str, str] = {}  # in memory only: a student ID is never written to disk


class GradebookError(Exception):
    """Carries one of EXPIRED / FORBIDDEN / NOT_FOUND / UNAVAILABLE."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


_STATUS_TEXT = {
    GRADED: "graded", NEEDS_GRADING: "submitted (awaiting grade)", SUBMITTED: "submitted",
    NOT_SUBMITTED: "not submitted", IN_PROGRESS: "in progress (not submitted)", EXEMPT: "exempt", UNKNOWN: "status unknown",
}


def status_text(status: str, item: Optional[Dict[str, Any]] = None) -> str:
    """Student-facing wording for terminal output. JSON keeps the enum values."""
    if status == GRADED and item is not None and not item.get("possible"):
        return "graded (no points)"
    return _STATUS_TEXT.get(status, "status unknown")


def default_api_get(path: str, cookie: str) -> Optional[Dict[str, Any]]:
    """GET through the shared REST client (it uses the saved session; `cookie` only keys caches)."""
    from core.rest import RestError, get_json
    try:
        return get_json(path)
    except RestError as exc:
        return {"_http_status": exc.status} if exc.status else None


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

_LABEL_RE = re.compile(r"^\s*([A-Za-z]{2,6})\s*[-_]?\s*(\d{2,3}[A-Za-z]?)(?![0-9A-Za-z])")


def course_label(name: Optional[str]) -> str:
    """`IS 410 Intro To Database Design (03.1520) FA2026` -> `IS 410`; falls back to the trimmed name."""
    if not name:
        return "Course"
    m = _LABEL_RE.match(name)
    if m:
        return f"{m.group(1).upper()} {m.group(2).upper()}"
    return re.sub(r"\s*\([^)]*\)\s*", " ", name).strip()[:12] or name


def course_title(name: Optional[str]) -> str:
    """`IS 410 Intro To Database Design (03.1520) FA2026` -> `Intro To Database Design`."""
    if not name:
        return ""
    text = re.sub(r"\s*\([^)]*\)", "", name)
    text = re.sub(r"\s*\b(?:FA|SP|SU|WI)\d{4}\b", "", text)
    m = _LABEL_RE.match(text)
    return (text[m.end():] if m else text).strip()


# ---------------------------------------------------------------------------
# Raw API plumbing
# ---------------------------------------------------------------------------

def _unwrap(resp: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if resp is None:
        raise GradebookError(UNAVAILABLE)
    status = resp.get("_http_status")
    if status:
        raise GradebookError(_HTTP_ERRORS.get(status, UNAVAILABLE))
    return resp


def _get_all(path: str, cookie: str, api_get: Callable) -> List[Dict[str, Any]]:
    """GET a collection, following `paging.nextPage` (bounded)."""
    results: List[Dict[str, Any]] = []
    for _ in range(10):
        data = _unwrap(api_get(path, cookie))
        results.extend(data.get("results") or [])
        path = (data.get("paging") or {}).get("nextPage")
        if not path:
            break
    if path:
        raise GradebookError(UNAVAILABLE)  # a truncated collection cannot establish completion
    return results


def current_user_id(cookie: str, api_get: Callable = default_api_get) -> str:
    with _user_lock:
        cached = _user_cache.get(cookie)
    if cached:
        return cached
    me = _unwrap(api_get("/learn/api/public/v1/users/me", cookie))
    user_id = me.get("id")
    if not user_id:
        raise GradebookError(UNAVAILABLE)
    with _user_lock:
        _user_cache[cookie] = user_id
    return user_id


# ---------------------------------------------------------------------------
# Status model
# ---------------------------------------------------------------------------

def column_kind(column: Dict[str, Any]) -> str:
    handler = (column.get("scoreProviderHandle") or "").lower()
    if "forum" in handler or "discussion" in handler:
        return "discussion"
    if "test" in handler or "assessment" in handler or "asmt" in handler:
        return "assessment"
    if "assignment" in handler:
        return "assignment"
    if (column.get("grading") or {}).get("type", "").lower() == "manual" or not handler:
        return "manual"
    return "other"


def is_calculated(column: Dict[str, Any]) -> bool:
    return (column.get("grading") or {}).get("type", "").lower() == "calculated"


def _norm_status(raw: Any) -> str:
    return re.sub(r"[^A-Z]", "", str(raw or "").upper())


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _record_score(record: Dict[str, Any]) -> Optional[float]:
    score = _number(record.get("score"))
    if score is None:
        score = _number((record.get("displayGrade") or {}).get("score"))
    return score


def _possible(column: Dict[str, Any]) -> Optional[float]:
    return _number((column.get("score") or {}).get("possible"))


def _percent(score: Optional[float], possible: Optional[float]) -> Optional[float]:
    return round(score / possible * 100, 1) if score is not None and possible else None


def needs_post_check(column: Dict[str, Any], record: Dict[str, Any]) -> bool:
    """A discussion marked as handed in cannot be believed without seeing our own posts (#22)."""
    return column_kind(column) == "discussion" and _norm_status(record.get("status")) in (
        "NEEDSGRADING", "COMPLETED", "ATTEMPTED", "SUBMITTED"
    )


def needs_attempt_check(column: Dict[str, Any], record: Dict[str, Any]) -> bool:
    """Aggregate gradebook states describe grading, not whether the student handed in an attempt."""
    if column_kind(column) not in ("assignment", "assessment") or record.get("exempt"):
        return False
    raw = _norm_status(record.get("status"))
    return raw in ("NEEDSGRADING", "COMPLETED", "ATTEMPTED", "SUBMITTED", "GRADED")


def _attempt_status(attempts: Optional[List[Dict[str, Any]]], raw: str) -> str:
    if attempts is None:
        return UNKNOWN
    if not attempts:
        return NOT_SUBMITTED
    states = {_norm_status(a.get("status")) for a in attempts}
    # Do not assume the API ordered attempts or hide an active draft behind an older final attempt.
    if "INPROGRESS" in states:
        return IN_PROGRESS
    final = {"NEEDSGRADING", "COMPLETED", "SUBMITTED", "GRADED"}
    not_started = {"NOTATTEMPTED", "NOTSTARTED", "UNSUBMITTED"}
    if states - final - not_started:
        return UNKNOWN
    if states & not_started:
        return NOT_SUBMITTED
    if states & final:
        return NEEDS_GRADING if raw == "NEEDSGRADING" else SUBMITTED
    return NOT_SUBMITTED


def item_status(column: Dict[str, Any], record: Optional[Dict[str, Any]], posts: Optional[int] = None,
                attempts: Optional[List[Dict[str, Any]]] = None, *, attempts_checked: bool = False) -> str:
    """Reconcile one gradebook column with the student's record into a single status.

    `posts` verifies our own discussion posts; `attempts` verifies assessment/assignment state.
    None means not verified. Attempt existence, draft content and timestamps alone never prove submission.
    """
    kind = column_kind(column)
    submittable = kind in ("assignment", "assessment", "discussion")
    if record is None:
        return NOT_SUBMITTED if submittable else UNKNOWN
    if record.get("exempt"):
        return EXEMPT
    raw = _norm_status(record.get("status"))
    has_score = _record_score(record) is not None
    if raw == "GRADED" and has_score:
        if attempts is None:
            return UNKNOWN if attempts_checked else GRADED
        if not attempts:
            return GRADED  # an instructor can post a grade for work handed in outside an online attempt
        state = _attempt_status(attempts, raw)
        return GRADED if state in COMPLETED else state
    if needs_attempt_check(column, record):
        return _attempt_status(attempts, raw)
    if needs_post_check(column, record):
        if posts is None:
            return GRADED if raw == "GRADED" and has_score else UNKNOWN
        if posts == 0:
            return NOT_SUBMITTED
    if raw == "GRADED" or (has_score and raw not in ("NEEDSGRADING", "INPROGRESS")):
        if has_score:
            return GRADED
        return UNKNOWN
    if raw == "NEEDSGRADING":
        return NEEDS_GRADING if kind == "discussion" and posts else UNKNOWN
    if raw in ("COMPLETED", "ATTEMPTED", "SUBMITTED"):
        return SUBMITTED if kind == "discussion" and posts else UNKNOWN
    if raw == "INPROGRESS":
        return IN_PROGRESS if submittable else UNKNOWN
    if raw in ("NOTATTEMPTED", ""):
        return NOT_SUBMITTED if submittable else UNKNOWN
    return UNKNOWN


def due_iso(column: Dict[str, Any]) -> Optional[str]:
    """Top-level `dueDate`, then `grading.due` (the two places this tenant puts deadlines)."""
    return to_iso(column.get("dueDate")) or to_iso((column.get("grading") or {}).get("due"))


def _feedback(record: Dict[str, Any]) -> Optional[str]:
    fb = record.get("feedback")
    if isinstance(fb, dict):
        fb = fb.get("displayText") or fb.get("rawText")
    if not fb:
        return None
    from scrapers.quiz import _clean_html_text
    return _clean_html_text(str(fb)) or None


# ---------------------------------------------------------------------------
# Reported course total (letters are never inferred)
# ---------------------------------------------------------------------------

_LETTER_RE = re.compile(r"^[A-F][+-]?$")


def _pick_official(columns: List[Dict[str, Any]], records: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The course's own total: a Calculated column the student has a visible value for."""
    visible = [c for c in columns if is_calculated(c)
               and (c.get("externalGrade") or (c.get("name") or "").strip().lower() in OFFICIAL_NAMES)
               and c.get("id") in records
               and (_record_score(records[c["id"]]) is not None or (records[c["id"]].get("displayGrade") or {}).get("text"))]
    if not visible:
        return None

    def rank(c: Dict[str, Any]):
        name = (c.get("name") or "").strip().lower()
        by_name = OFFICIAL_NAMES.index(name) if name in OFFICIAL_NAMES else len(OFFICIAL_NAMES)
        return (not c.get("externalGrade"), by_name)

    return min(visible, key=rank)


def _official_overall(column: Dict[str, Any], record: Dict[str, Any], schemas: List[Dict[str, Any]],
                      graded: int, total: int) -> Dict[str, Any]:
    display = record.get("displayGrade") or {}
    score = _record_score(record)
    # The student's own `displayGrade.possible` is the points graded so far; the column's is the whole course.
    possible = _number(display.get("possible")) or _possible(column)
    text = display.get("text")
    scale = str(display.get("scaleType") or "").lower()
    display_score = _number(display.get("score"))
    if scale == "percent" and display_score is not None:
        # displayGrade is already scaled; top-level record.score may still be raw points.
        percent: Optional[float] = round(display_score, 1)
        score = _number(record.get("score"))
        possible = _possible(column) if score is not None else None
    else:
        percent = _percent(score, possible)
    letter = text if isinstance(text, str) and _LETTER_RE.match(text.strip()) else None
    return {"source": "official", "label": column.get("name"), "score": score, "possible": possible,
            "percent": percent, "letter": letter, "graded": graded, "total": total}


def _calculated_overall(items: List[Dict[str, Any]], schemas: List[Dict[str, Any]], total: int) -> Dict[str, Any]:
    counted = [i for i in items if i["_counts"] and i["_graded"] and i["score"] is not None]
    score = round(sum(i["score"] for i in counted), 2)
    possible = round(sum(i["possible"] for i in counted), 2)
    percent = _percent(score, possible) if counted else None
    return {"source": "calculated" if counted else "none", "label": "Calculated" if counted else None,
            "score": score if counted else None, "possible": possible if counted else None,
            "percent": percent, "letter": None, "graded": len(counted), "total": total}


def build_gradebook(course_id: str, course_name: str, columns: List[Dict[str, Any]], records: Optional[List[Dict[str, Any]]],
                    schemas: List[Dict[str, Any]], posts: Optional[Dict[str, Optional[int]]] = None,
                    attempts: Optional[Dict[str, Optional[List[Dict[str, Any]]]]] = None) -> Dict[str, Any]:
    """Join columns with the student's records into the documented `grades --json` shape (plus `_`-prefixed helpers).

    `records=None` means the student's gradebook could not be read: every status is then `unknown`.
    """
    by_column = {r.get("columnId"): r for r in records or [] if r.get("columnId")}
    posts = posts or {}
    attempts = attempts or {}
    items: List[Dict[str, Any]] = []
    excluded: List[Dict[str, str]] = []

    for column in columns:
        if is_calculated(column):
            continue  # totals are never items
        cid = column.get("id")
        record = by_column.get(cid)
        status = item_status(column, record, posts.get(cid), attempts.get(cid),
                             attempts_checked=cid in attempts) if records is not None else UNKNOWN
        score = _record_score(record) if record else None
        possible = _possible(column)
        if status == EXEMPT:
            score = None
        counts = status != EXEMPT and column.get("includeInCalculations", True) is not False and bool(possible)
        if status != EXEMPT and not counts:
            reason = "not included in the course total" if column.get("includeInCalculations") is False else "no points possible"
            excluded.append({"name": column.get("name"), "reason": reason})
        elif status == EXEMPT:
            excluded.append({"name": column.get("name"), "reason": "exempt"})
        items.append({
            "name": (column.get("name") or "Untitled").strip(),
            "category": column_kind(column),
            "due_at": due_iso(column),
            "status": status,
            "score": score,
            "possible": possible,
            "percent": _percent(score, possible),
            "feedback": _feedback(record) if record and score is not None else None,
            "_counts": counts,
            "_graded": status == GRADED or bool(record and _norm_status(record.get("status")) == "GRADED" and score is not None),
            "_column_id": cid,
            "_content_id": column.get("contentId"),
        })

    # Dated items first (soonest first), undated last; stable by name.
    def sort_key(i: Dict[str, Any]):
        dt = parse_datetime(i["due_at"])
        return (dt is None, dt.timestamp() if dt else 0, i["name"].lower())

    items.sort(key=sort_key)
    total = sum(1 for i in items if i["_counts"])
    official_col = _pick_official(columns, by_column)
    graded = sum(1 for i in items if i["_counts"] and i["_graded"])
    overall = (_official_overall(official_col, by_column[official_col["id"]], schemas, graded, total)
               if official_col else _calculated_overall(items, schemas, total))
    return {"course_id": course_id, "course_name": course_name, "overall": overall, "items": items, "excluded": excluded}


def public_view(gradebook: Dict[str, Any]) -> Dict[str, Any]:
    """The documented JSON shape: drops `_` helper keys."""
    return {
        "course_id": gradebook["course_id"],
        "course_name": gradebook["course_name"],
        "overall": gradebook["overall"],
        "items": [{k: v for k, v in i.items() if not k.startswith("_")} for i in gradebook["items"]],
        "excluded": gradebook["excluded"],
        **({"records_error": gradebook["records_error"]} if gradebook.get("records_error") else {}),
    }


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _count_own_posts(course_id: str, discussion_id: str, user_id: str, cookie: str, api_get: Callable) -> Optional[int]:
    try:
        data = _unwrap(api_get(f"/learn/api/public/v1/courses/{course_id}/discussions/{discussion_id}/messages?limit=200", cookie))
    except GradebookError:
        return None
    if (data.get("paging") or {}).get("nextPage"):
        return None  # more posts than we looked at: do not guess
    own = [m for m in data.get("results", []) if m.get("userId") == user_id]
    if any(_norm_status(m.get("status")) not in ("PUBLISHED", "DRAFT", "DELETED") for m in own):
        return None
    return sum(1 for m in own if _norm_status(m.get("status")) == "PUBLISHED")


def _verify_discussions(course_id: str, columns: List[Dict[str, Any]], records: List[Dict[str, Any]],
                        user_id: str, cookie: str, api_get: Callable) -> Dict[str, Optional[int]]:
    by_column = {r.get("columnId"): r for r in records}
    pending = [c["id"] for c in columns if c.get("id") in by_column and needs_post_check(c, by_column[c["id"]])]
    if not pending:
        return {}
    try:
        discussions = _get_all(f"/learn/api/public/v1/courses/{course_id}/discussions", cookie, api_get)
    except GradebookError:
        return {}
    topic = {d.get("gradebookColumnId"): d.get("id") for d in discussions}
    wanted = {cid: topic[cid] for cid in pending if topic.get(cid)}
    if not wanted:
        return {}
    with ThreadPoolExecutor(max_workers=min(3, len(wanted))) as pool:
        counts = pool.map(lambda cid: _count_own_posts(course_id, wanted[cid], user_id, cookie, api_get), wanted)
        return dict(zip(wanted, counts))


def _read_attempts(course_id: str, column_id: str, user_id: str, cookie: str,
                   api_get: Callable) -> Optional[List[Dict[str, Any]]]:
    """Read metadata only, with identity checks at both grade and attempt boundaries."""
    try:
        grades = _get_all(f"/learn/api/v1/courses/{course_id}/gradebook/columns/{column_id}/grades?userId={user_id}",
                          cookie, api_get)
        if not grades or any(g.get("userId") != user_id or g.get("columnId") != column_id
                             or g.get("courseId") != course_id or not g.get("id") for g in grades):
            return None
        attempts = []
        for grade in grades:
            rows = _get_all(f"/learn/api/v1/courses/{course_id}/gradebook/columns/{column_id}/grades/{grade['id']}/attempts",
                            cookie, api_get)
            if any(a.get("userId") != user_id or a.get("courseId") != course_id
                   or a.get("gradeId") != grade["id"] for a in rows):
                return None
            attempts.extend(rows)
        return attempts
    except GradebookError as exc:
        if exc.kind == EXPIRED:
            raise
        return None


def _verify_attempts(course_id: str, columns: List[Dict[str, Any]], records: List[Dict[str, Any]],
                     user_id: str, cookie: str, api_get: Callable) -> Dict[str, Optional[List[Dict[str, Any]]]]:
    by_column = {r.get("columnId"): r for r in records}
    pending = [c["id"] for c in columns if c.get("id") in by_column and needs_attempt_check(c, by_column[c["id"]])]
    if not pending:
        return {}
    with ThreadPoolExecutor(max_workers=min(3, len(pending))) as pool:
        attempts = pool.map(lambda cid: _read_attempts(course_id, cid, user_id, cookie, api_get), pending)
        return dict(zip(pending, attempts))


def student_records(course_id: str, columns: List[Dict[str, Any]], cookie: str,
                    api_get: Callable = default_api_get) -> Dict[str, Any]:
    """One course's status inputs for callers that already hold the columns (`assignments`).

    Returns `{"records": {column_id: record} | None, "posts": {...}}`; records are None when unreadable.
    """
    try:
        user_id = current_user_id(cookie, api_get)
        records = _get_all(f"/learn/api/public/v2/courses/{course_id}/gradebook/users/{user_id}", cookie, api_get)
        attempts = _verify_attempts(course_id, columns, records, user_id, cookie, api_get)
    except GradebookError as exc:
        return {"records": None, "posts": {}, "attempts": {}, "error": exc.kind}
    return {
        "records": {r.get("columnId"): r for r in records},
        "posts": _verify_discussions(course_id, columns, records, user_id, cookie, api_get),
        "attempts": attempts,
    }


def fetch_gradebooks(courses: Dict[str, str], cookie: str, api_get: Callable = default_api_get,
                     verify: bool = True) -> Dict[str, Any]:
    """Fetch every course concurrently. Value per course id: a gradebook dict, or `{"error": kind}`."""
    if not courses:
        return {}
    ids = list(courses)
    with ThreadPoolExecutor(max_workers=min(MAX_CONNECTIONS, 2 * len(ids) + 1)) as pool:
        # Identity first: it warms DNS/TLS so the burst below does not stall on a cold resolver.
        me = pool.submit(current_user_id, cookie, api_get)
        try:
            user_id = me.result()
        except GradebookError as e:
            return {cid: {"error": e.kind} for cid in ids}
        cols = {cid: pool.submit(_get_all, f"/learn/api/public/v2/courses/{cid}/gradebook/columns", cookie, api_get) for cid in ids}
        users = {cid: pool.submit(_get_all, f"/learn/api/public/v2/courses/{cid}/gradebook/users/{user_id}", cookie, api_get) for cid in ids}

        def finish(cid: str) -> Dict[str, Any]:
            try:
                columns = cols[cid].result()
            except GradebookError as e:
                return {"error": e.kind}
            records: Optional[List[Dict[str, Any]]]
            records_error = None
            try:
                records = users[cid].result()
            except GradebookError as e:
                if e.kind == EXPIRED:
                    return {"error": EXPIRED}
                records = None
                records_error = e.kind
            posts = _verify_discussions(cid, columns, records, user_id, cookie, api_get) if verify and records else {}
            try:
                attempts = _verify_attempts(cid, columns, records, user_id, cookie, api_get) if verify and records else {}
            except GradebookError as exc:
                return {"error": exc.kind}
            book = build_gradebook(cid, courses[cid], columns, records, [], posts, attempts)
            if records_error:
                book["records_error"] = records_error
                book["overall"]["unavailable"] = True
            return book

        with ThreadPoolExecutor(max_workers=len(ids)) as finishers:
            return dict(zip(ids, finishers.map(finish, ids)))


def first_error(results: Dict[str, Any], kind: str) -> bool:
    return any(isinstance(r, dict) and r.get("error") == kind for r in results.values())
