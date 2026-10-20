"""Pure model for saving answers to an existing Assessment Attempt (no network).

See docs/specs/save-draft-answers.md and docs/specs/save-draft-answers-protocol.md.
Everything here is deterministic: projecting a provider attempt payload into the public
snapshot, fingerprinting it, validating a changes file and planning per-question edits.
Network I/O lives in scrapers/answers.py.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import unescape
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1
SNAPSHOT_TTL = timedelta(minutes=15)
MAX_FILE_BYTES = 512 * 1024

SINGLE = "single_choice"
MULTI = "multiple_answer"
TRUE_FALSE = "true_false"
SUPPORTED_TYPES = (SINGLE, MULTI, TRUE_FALSE)

# Verified live 2026-10-20 (ENGL100 J#5): True/False is questionType eitherOr with
# answerType true_false and a bare boolean givenAnswer (None = unanswered).
TF_CHOICES = [{"id": "true", "text": "True"}, {"id": "false", "text": "False"}]

_TOP_KEYS = {"schema_version", "target", "snapshot", "changes", "unsupported", "example_change"}
_TARGET_KEYS = {"origin", "course_id", "assessment_id", "attempt_id", "account_binding",
                "course_name", "title"}
_SNAPSHOT_KEYS = {"observed_at", "expires_at", "definition_sha256", "questions"}
_CHANGE_KEYS = {"question_attempt_id", "operation", "selected_choice_ids", "number", "note"}


class AnswersInputError(ValueError):
    """Invalid local input (exit 2). Raised before any network write."""


class AnswersRefusal(RuntimeError):
    """The remote state does not allow a safe save (exit 3). ``code`` is machine readable."""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details


def clean_text(raw: Optional[str]) -> str:
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", raw or "", flags=re.I)
    text = unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"[ \t ]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def question_kind(qa: Dict[str, Any]) -> Optional[str]:
    """Public type for a raw question attempt, or None when saving it is unsupported."""
    qtype = qa.get("questionType")
    question = qa.get("question") or {}
    if qtype == "multipleanswer":
        answers = question.get("answers") or []
        if not answers or any(not isinstance(a, dict) or not a.get("id") for a in answers):
            return None
        return SINGLE if question.get("answerSelectionLimit") == 1 else MULTI
    if qtype == "eitherOr" and question.get("answerType") == "true_false":
        return TRUE_FALSE
    return None


def _raw_selected(qa: Dict[str, Any], kind: str, choice_ids: List[str]) -> List[str]:
    given = qa.get("givenAnswer")
    if kind == TRUE_FALSE:
        if given is None:
            return []
        if isinstance(given, bool):
            return ["true" if given else "false"]
        raise AnswersRefusal("unsupported_encoding", f"Unexpected True/False answer encoding on {qa.get('id')}")
    if given is None:
        return []
    if isinstance(given, list) and len(given) == len(choice_ids) and all(isinstance(x, bool) for x in given):
        return [cid for cid, flag in zip(choice_ids, given) if flag]
    raise AnswersRefusal("unsupported_encoding", f"Unexpected multiple-answer encoding on {qa.get('id')}")


def project_attempt(attempt: Dict[str, Any]) -> Dict[str, Any]:
    """Split a raw gradebook attempt into supported questions and unsupported blocks.

    Returns {"status", "questions": [...public...], "unsupported": [...], "raw": {qa_id: qa}}.
    Presentation blocks are skipped; "number" counts answerable questions in display order,
    which is what a student sees ("1 of 10"), not the reused visibleQuestionNumber.
    """
    details = attempt.get("toolAttemptDetail") or {}
    detail = details.get("resource/x-bb-assessment") or {}
    qas = detail.get("questionAttempts")
    if not isinstance(qas, list):
        raise AnswersRefusal("unsupported_protocol", "Attempt has no question list (not a native Ultra test?)")
    questions, unsupported, raw = [], [], {}
    seen_ids = set()
    number = 0
    for position, qa in enumerate(qas, start=1):
        if qa.get("questionType") == "presentation":
            continue
        number += 1
        qa_id = qa.get("id")
        if not qa_id or qa_id in seen_ids or not qa.get("questionId"):
            raise AnswersRefusal("unsupported_protocol", f"Missing or duplicate question ID at block {position}")
        seen_ids.add(qa_id)
        question = qa.get("question") or {}
        prompt = clean_text((question.get("questionText") or {}).get("rawText"))
        kind = question_kind(qa)
        if kind is None:
            unsupported.append({"number": number, "question_attempt_id": qa_id,
                                "provider_type": qa.get("questionType"), "prompt": prompt})
            continue
        if kind == TRUE_FALSE:
            choices = [dict(c) for c in TF_CHOICES]
            limit = 1
        else:
            answers = question.get("answers") or []
            ids = [a["id"] for a in answers]
            if len(set(ids)) != len(ids):
                raise AnswersRefusal("unsupported_protocol", f"Duplicate choice IDs on question {number}")
            # No A/B/C labels: Blackboard may shuffle display order per student.
            choices = [{"id": a["id"], "text": clean_text((a.get("answerText") or {}).get("rawText"))}
                       for a in answers]
            limit = 1 if kind == SINGLE else (question.get("answerSelectionLimit") or len(choices))
        raw[qa_id] = qa
        questions.append({
            "number": number,
            "question_attempt_id": qa_id,
            "question_id": qa.get("questionId"),
            "type": kind,
            "prompt": prompt,
            "points": question.get("points"),
            "max_selections": limit,
            "choices": choices,
            "selected_choice_ids": _raw_selected(qa, kind, [c["id"] for c in choices]),
        })
    return {"status": attempt.get("status"), "questions": questions, "unsupported": unsupported, "raw": raw}


def fingerprint(raw_by_id: Dict[str, Any], order: List[str]) -> str:
    """sha256 over identity, type, raw prompt and every choice identity/text/order."""
    canon = []
    for qa_id in order:
        qa = raw_by_id[qa_id]
        q = qa.get("question") or {}
        canon.append([
            qa_id, qa.get("questionId"), qa.get("questionType"), q.get("answerType"),
            q.get("answerSelectionLimit"), (q.get("questionText") or {}).get("rawText"),
            [[a.get("id"), (a.get("answerText") or {}).get("rawText")] for a in (q.get("answers") or [])],
        ])
    return hashlib.sha256(json.dumps(canon, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def encode_given(kind: str, choice_ids: List[str], selected: List[str]) -> Any:
    """Provider givenAnswer value for a desired selection (verified encodings only)."""
    if kind == TRUE_FALSE:
        return None if not selected else selected[0] == "true"
    wanted = set(selected)
    return [cid in wanted for cid in choice_ids]


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def build_export(target: Dict[str, Any], projection: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    order = [q["question_attempt_id"] for q in projection["questions"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "target": target,
        "snapshot": {
            "observed_at": _iso(now),
            "expires_at": _iso(now + SNAPSHOT_TTL),
            "definition_sha256": fingerprint(projection["raw"], order),
            "questions": projection["questions"],
        },
        "unsupported": projection["unsupported"],
        # Ignored on read. Shows the shape of one entry; copy it into "changes" and edit.
        "example_change": _example(projection["questions"]),
        "changes": [],
    }


def _example(questions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not questions:
        return None
    q = questions[0]
    return {"number": q["number"], "question_attempt_id": q["question_attempt_id"], "operation": "set",
            "selected_choice_ids": [q["choices"][0]["id"]],
            "note": "set = the full list of choices to have selected; use operation clear (no ids) to blank it"}


def _no_dupes(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise AnswersInputError(f"duplicate JSON key {k!r}")
        out[k] = v
    return out


def _check_keys(obj: Any, allowed: set, where: str) -> None:
    if not isinstance(obj, dict):
        raise AnswersInputError(f"{where} must be an object")
    extra = set(obj) - allowed
    if extra:
        raise AnswersInputError(f"unknown field(s) in {where}: {', '.join(sorted(extra))}")


def parse_file(text: str) -> Dict[str, Any]:
    """Strictly parse an answers file (exported template with changes filled in)."""
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise AnswersInputError("answers file is too large")
    try:
        doc = json.loads(text, object_pairs_hook=_no_dupes)
    except json.JSONDecodeError as exc:
        raise AnswersInputError(f"answers file is not valid JSON: {exc}") from None
    _check_keys(doc, _TOP_KEYS, "file")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise AnswersInputError(f"unsupported schema_version {doc.get('schema_version')!r} (expected {SCHEMA_VERSION})")
    _check_keys(doc.get("target"), _TARGET_KEYS, "target")
    _check_keys(doc.get("snapshot"), _SNAPSHOT_KEYS, "snapshot")
    for key in ("course_id", "assessment_id", "attempt_id", "account_binding"):
        if not isinstance(doc["target"].get(key), str) or not doc["target"][key]:
            raise AnswersInputError(f"target.{key} is missing")
    records = doc["snapshot"].get("questions")
    if not isinstance(records, list):
        raise AnswersInputError("snapshot.questions must be a list")
    seen_questions = set()
    for q in records:
        if (not isinstance(q, dict) or not isinstance(q.get("question_attempt_id"), str)
                or not q["question_attempt_id"] or not isinstance(q.get("selected_choice_ids"), list)
                or not all(isinstance(cid, str) for cid in q["selected_choice_ids"])):
            raise AnswersInputError("invalid snapshot.questions record")
        if q["question_attempt_id"] in seen_questions:
            raise AnswersInputError("duplicate snapshot.questions record")
        seen_questions.add(q["question_attempt_id"])
    changes = doc.get("changes")
    if not isinstance(changes, list):
        raise AnswersInputError("changes must be a list")
    seen = set()
    for i, ch in enumerate(changes, start=1):
        _check_keys(ch, _CHANGE_KEYS, f"changes[{i}]")
        qa_id = ch.get("question_attempt_id")
        if not isinstance(qa_id, str) or not qa_id:
            raise AnswersInputError(f"changes[{i}].question_attempt_id is missing")
        if qa_id in seen:
            raise AnswersInputError(f"question {qa_id} appears more than once in changes")
        seen.add(qa_id)
        op = ch.get("operation")
        sel = ch.get("selected_choice_ids")
        if op == "set":
            if not isinstance(sel, list) or not sel or not all(isinstance(s, str) for s in sel):
                raise AnswersInputError(f"changes[{i}]: 'set' needs a non-empty selected_choice_ids list (use operation 'clear' to clear)")
            if len(set(sel)) != len(sel):
                raise AnswersInputError(f"changes[{i}]: duplicate choice IDs")
        elif op == "clear":
            if sel not in (None, []):
                raise AnswersInputError(f"changes[{i}]: 'clear' takes no selected_choice_ids")
        else:
            raise AnswersInputError(f"changes[{i}].operation must be 'set' or 'clear'")
    return doc


def parse_time(value: str, where: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise AnswersInputError(f"{where} is not an ISO timestamp") from None
    if dt.tzinfo is None:
        raise AnswersInputError(f"{where} has no timezone")
    return dt


def check_snapshot_age(doc: Dict[str, Any], now: datetime) -> None:
    observed = parse_time(doc["snapshot"].get("observed_at"), "snapshot.observed_at")
    expires = parse_time(doc["snapshot"].get("expires_at"), "snapshot.expires_at")
    if observed > now or expires - observed > SNAPSHOT_TTL:
        raise AnswersRefusal("stale_snapshot", "Snapshot timestamps are invalid. Export again.")
    if now >= expires:
        raise AnswersRefusal("stale_snapshot", "That export is more than 15 minutes old. Export again with --export, then copy your changes over.")


@dataclass
class PlannedEdit:
    number: int
    question_attempt_id: str
    kind: str
    prompt: str
    choices: List[Dict[str, str]]
    before: List[str]
    after: List[str]
    status: str  # change | unchanged
    outcome: Optional[str] = None
    detail: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def plan_changes(doc: Dict[str, Any], fresh: Dict[str, Any], fresh_sha: str) -> List[PlannedEdit]:
    """Validate every change against fresh provider state. Raises before any write."""
    if doc["snapshot"].get("definition_sha256") != fresh_sha:
        raise AnswersRefusal("stale_snapshot", "The questions or choices changed since export. Export again.")
    baseline = {q["question_attempt_id"]: q for q in doc["snapshot"].get("questions") or []}
    current = {q["question_attempt_id"]: q for q in fresh["questions"]}
    unsupported = {u["question_attempt_id"] for u in fresh["unsupported"]}
    edits: List[PlannedEdit] = []
    conflicts = []
    for ch in doc["changes"]:
        qa_id = ch["question_attempt_id"]
        if qa_id in unsupported:
            raise AnswersInputError(f"question {qa_id} has an unsupported type and cannot be saved")
        q = current.get(qa_id)
        if q is None or qa_id not in baseline:
            raise AnswersInputError(f"question {qa_id} is not in this attempt")
        valid = [c["id"] for c in q["choices"]]
        desired = [] if ch["operation"] == "clear" else list(ch["selected_choice_ids"])
        bad = [s for s in desired if s not in valid]
        if bad:
            raise AnswersInputError(f"question {q['number']}: unknown choice ID(s) {', '.join(bad)}")
        if len(desired) > q["max_selections"]:
            raise AnswersInputError(f"question {q['number']}: at most {q['max_selections']} choice(s) allowed")
        desired = [c for c in valid if c in desired]  # provider order
        now_sel = q["selected_choice_ids"]
        if set(now_sel) == set(desired):
            status = "unchanged"
        elif set(now_sel) != set(baseline[qa_id].get("selected_choice_ids") or []):
            conflicts.append(q["number"])
            continue
        else:
            status = "change"
        edits.append(PlannedEdit(q["number"], qa_id, q["type"], q["prompt"], q["choices"], now_sel, desired, status))
    if conflicts:
        raise AnswersRefusal("conflict", "Answers changed in Blackboard since export (questions "
                             + ", ".join(map(str, conflicts)) + "). Export again; nothing was saved.",
                             questions=conflicts)
    return sorted(edits, key=lambda e: e.number)
