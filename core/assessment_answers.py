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
MATCHING = "matching"
SUPPORTED_TYPES = (SINGLE, MULTI, TRUE_FALSE, MATCHING)

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


def pair_token(prompt_id: str, answer_id: str) -> str:
    """Matching selection token: one prompt matched to one local answer ID (``a0``, ``a1``, ...)."""
    return f"{prompt_id}:{answer_id}"


def split_pair(token: str) -> tuple:
    prompt_id, _, answer_id = token.rpartition(":")
    return prompt_id, answer_id


def given_field(kind: str) -> str:
    """Provider body field that carries the answer for this public type."""
    return "givenAnswers" if kind == MATCHING else "givenAnswer"


def is_answered(q: Dict[str, Any]) -> bool:
    """Blackboard counts a matching question as answered only when every prompt has a match."""
    selected = q["selected_choice_ids"]
    return len(selected) == len(q["prompts"]) if q["type"] == MATCHING else bool(selected)


def matched_pairs(q: Dict[str, Any], tokens: List[str]) -> List[Dict[str, Any]]:
    """Per prompt, in prompt order: the prompt, and the chosen answer letter/text (None when unanswered)."""
    chosen = dict(split_pair(t) for t in tokens)
    letters = {c["id"]: (choice_letter(i), c["text"]) for i, c in enumerate(q["choices"])}
    return [{"prompt": p["text"], "letter": (letters[chosen[p["id"]]][0] if p["id"] in chosen else None),
             "answer": (letters[chosen[p["id"]]][1] if p["id"] in chosen else None)} for p in q["prompts"]]


def _valid_matching(question: Dict[str, Any]) -> bool:
    prompts, answers = question.get("prompts"), question.get("answers")
    if not isinstance(prompts, list) or not prompts or not isinstance(answers, list) or not answers:
        return False
    ids = [p.get("id") if isinstance(p, dict) else None for p in prompts]
    return (all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids)
            and all(isinstance(a, dict) for a in answers))


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
    if qtype == "matching" and _valid_matching(question):
        return MATCHING
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


def _raw_matching(qa: Dict[str, Any], prompt_ids: List[str], answer_ids: List[str]) -> List[str]:
    """Readback ``givenAnswers``: [] or null = nothing answered, else one int index (or null) per prompt."""
    given = qa.get("givenAnswers")
    if given is None or given == []:
        return []
    if (isinstance(given, list) and len(given) == len(prompt_ids)
            and all(g is None or (type(g) is int and 0 <= g < len(answer_ids)) for g in given)):
        return [pair_token(pid, answer_ids[g]) for pid, g in zip(prompt_ids, given) if g is not None]
    raise AnswersRefusal("unsupported_encoding", f"Unexpected matching answer encoding on {qa.get('id')}")


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
        prompts: List[Dict[str, str]] = []
        if kind == TRUE_FALSE:
            choices = [dict(c) for c in TF_CHOICES]
            limit = 1
        elif kind == MATCHING:
            # Answers carry no provider IDs: a0, a1, ... are positions in provider order (also the CLI letters).
            prompts = [{"id": p["id"], "text": clean_text((p.get("promptText") or {}).get("rawText"))}
                       for p in question["prompts"]]
            choices = [{"id": f"a{i}", "text": clean_text((a.get("answerText") or {}).get("rawText"))}
                       for i, a in enumerate(question["answers"])]
            limit = len(prompts)
        else:
            answers = question.get("answers") or []
            ids = [a["id"] for a in answers]
            if len(set(ids)) != len(ids):
                raise AnswersRefusal("unsupported_protocol", f"Duplicate choice IDs on question {number}")
            # CLI letters follow this provider order, not Blackboard's shuffled UI.
            choices = [{"id": a["id"], "text": clean_text((a.get("answerText") or {}).get("rawText"))}
                       for a in answers]
            limit = 1 if kind == SINGLE else (question.get("answerSelectionLimit") or len(choices))
        raw[qa_id] = qa
        if kind == MATCHING:
            selected = _raw_matching(qa, [p["id"] for p in prompts], [c["id"] for c in choices])
        else:
            selected = _raw_selected(qa, kind, [c["id"] for c in choices])
        questions.append({
            "number": number,
            "question_attempt_id": qa_id,
            "question_id": qa.get("questionId"),
            "type": kind,
            "prompt": prompt,
            "points": question.get("points"),
            "max_selections": limit,
            "choices": choices,
            **({"prompts": prompts} if kind == MATCHING else {}),
            "selected_choice_ids": selected,
        })
    return {"status": attempt.get("status"), "questions": questions, "unsupported": unsupported, "raw": raw}


def fingerprint(raw_by_id: Dict[str, Any], order: List[str]) -> str:
    """sha256 over identity, type, raw prompt and every choice identity/text/order (matching: prompt IDs/text/order too)."""
    canon = []
    for qa_id in order:
        qa = raw_by_id[qa_id]
        q = qa.get("question") or {}
        entry = [
            qa_id, qa.get("questionId"), qa.get("questionType"), q.get("answerType"),
            q.get("answerSelectionLimit"), (q.get("questionText") or {}).get("rawText"),
            [[a.get("id"), (a.get("answerText") or {}).get("rawText")] for a in (q.get("answers") or [])],
        ]
        if qa.get("questionType") == "matching":  # only here, so other types keep their fingerprints
            entry.append([[p.get("id"), (p.get("promptText") or {}).get("rawText")] for p in q.get("prompts") or []])
        canon.append(entry)
    return hashlib.sha256(json.dumps(canon, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def encode_given(kind: str, choice_ids: List[str], selected: List[str], prompt_ids: Optional[List[str]] = None) -> Any:
    """Provider answer value for a desired selection (see ``given_field`` for the body key)."""
    if kind == TRUE_FALSE:
        return None if not selected else selected[0] == "true"
    if kind == MATCHING:
        chosen = dict(split_pair(t) for t in selected)
        return [choice_ids.index(chosen[pid]) if pid in chosen else None for pid in prompt_ids or []]
    wanted = set(selected)
    return [cid in wanted for cid in choice_ids]


def choice_letter(index: int) -> str:
    """Zero-based provider position as A..Z, AA..AZ, ... ."""
    label = ""
    index += 1
    while index:
        index, digit = divmod(index - 1, 26)
        label = chr(65 + digit) + label
    return label


def parse_sets(values: List[str]) -> Dict[int, List[str]]:
    """Parse local shorthand before reads or attempt creation; no text matching."""
    result = {}
    for value in values:
        match = re.fullmatch(r"\s*([1-9]\d*)\s*=\s*(.*?)\s*", value)
        if not match:
            raise AnswersInputError("--set needs N=A,B, N=True/False, N=1:B,2:A (matching), or N=clear")
        number, selection = int(match[1]), match[2].upper()
        if number in result:
            raise AnswersInputError(f"duplicate --set question {number}")
        tokens = [s.strip() for s in selection.split(",")]
        if not all(re.fullmatch(r"([1-9]\d*:)?[A-Z]+", s) for s in tokens) or len(set(tokens)) != len(tokens):
            raise AnswersInputError(f"question {number}: invalid or duplicate selection")
        result[number] = [] if tokens == ["CLEAR"] else tokens
    return result


def _matching_selection(number: int, tokens: List[str], q: Dict[str, Any], letters: Dict[str, str]) -> List[str]:
    """``1:B,2:A`` -> pair tokens. Prompt numbers and letters are positions in bb's list, never text."""
    selected, seen = [], set()
    for token in tokens:
        prompt_no, _, letter = token.rpartition(":")
        if not prompt_no:
            raise AnswersInputError(f"question {number}: matching needs prompt:letter pairs, like 1:B,2:A")
        if not 1 <= int(prompt_no) <= len(q["prompts"]):
            raise AnswersInputError(f"question {number}: no prompt {prompt_no} (use bb's prompt list)")
        if letter not in letters:
            raise AnswersInputError(f"question {number}: unknown letter {letter} (use bb's answer list)")
        if prompt_no in seen:
            raise AnswersInputError(f"question {number}: prompt {prompt_no} given twice")
        seen.add(prompt_no)
        selected.append(pair_token(q["prompts"][int(prompt_no) - 1]["id"], letters[letter]))
    return selected


def set_changes(parsed: Dict[int, List[str]], projection: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Translate CLI letters into the same ID-based changes consumed by plan_changes."""
    questions = {q["number"]: q for q in projection["questions"]}
    changes = []
    for number, tokens in parsed.items():
        q = questions.get(number)
        if q is None:
            raise AnswersInputError(f"question {number} is unsupported or absent; answer in Blackboard")
        letters = {choice_letter(i): c["id"] for i, c in enumerate(q["choices"])}
        if q["type"] == MATCHING:
            selected = _matching_selection(number, tokens, q, letters)
            changes.append({"question_attempt_id": q["question_attempt_id"],
                            "operation": "set" if tokens else "clear", "selected_choice_ids": selected})
            continue
        if q["type"] == TRUE_FALSE:
            letters.update(TRUE="true", T="true", FALSE="false", F="false")
        if any(token not in letters for token in tokens):
            raise AnswersInputError(f"question {number}: unknown letter (use bb's option list)")
        selected = [letters[token] for token in tokens]
        if len(set(selected)) != len(selected):
            raise AnswersInputError(f"question {number}: duplicate selection")
        changes.append({"question_attempt_id": q["question_attempt_id"],
                        "operation": "set" if tokens else "clear", "selected_choice_ids": selected})
    return changes


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
    first = pair_token(q["prompts"][0]["id"], q["choices"][0]["id"]) if q["type"] == MATCHING else q["choices"][0]["id"]
    return {"number": q["number"], "question_attempt_id": q["question_attempt_id"], "operation": "set",
            "selected_choice_ids": [first],
            "note": "set = the full list of choices to have selected; use operation clear (no ids) to blank it"
                    + ("; matching ids are promptId:answerId, at most one per prompt" if q["type"] == MATCHING else "")}


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
        valid = ([pair_token(p["id"], c["id"]) for p in q["prompts"] for c in q["choices"]]
                 if q["type"] == MATCHING else [c["id"] for c in q["choices"]])
        desired = [] if ch["operation"] == "clear" else list(ch["selected_choice_ids"])
        bad = [s for s in desired if s not in valid]
        if bad:
            raise AnswersInputError(f"question {q['number']}: unknown choice ID(s) {', '.join(bad)}")
        if q["type"] == MATCHING and len({split_pair(t)[0] for t in desired}) != len(desired):
            raise AnswersInputError(f"question {q['number']}: a prompt can have only one answer")
        if len(desired) > q["max_selections"]:
            raise AnswersInputError(f"question {q['number']} takes one answer" if q["max_selections"] == 1
                                    else f"question {q['number']}: at most {q['max_selections']} choices allowed")
        desired = [c for c in valid if c in desired]  # provider order
        now_sel = q["selected_choice_ids"]
        if set(now_sel) == set(desired):
            status = "unchanged"
        elif set(now_sel) != set(baseline[qa_id].get("selected_choice_ids") or []):
            conflicts.append(q["number"])
            continue
        else:
            status = "change"
        extra = {"prompts": q["prompts"], "choices": q["choices"]} if q["type"] == MATCHING else {}
        edits.append(PlannedEdit(q["number"], qa_id, q["type"], q["prompt"], q["choices"], now_sel, desired, status,
                                 extra=extra))
    if conflicts:
        raise AnswersRefusal("conflict", "Answers changed in Blackboard since export (questions "
                             + ", ".join(map(str, conflicts)) + "). Export again; nothing was saved.",
                             questions=conflicts)
    return sorted(edits, key=lambda e: e.number)
