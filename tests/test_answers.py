"""Offline contract tests for draft answer saves; all provider data is synthetic.

The fake provider observes every GET/PATCH and rejects all routes except the
explicit read routes and per-question answer-save route. No browser or socket is
allowed to escape these tests.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import socket
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from core import assessment_answers as model, rest
from scrapers import answers

NOW = datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc)
ORIGIN = "https://blackboard.example.invalid"
CID, AID, COL, ATT, USER, GRADE = [f"_{n}_1" for n in range(910001, 910007)]
SINGLE, MULTI, TF, ESSAY = [f"_{n}_1" for n in range(920001, 920005)]
PATH = f"/learn/api/v1/courses/{CID}/gradebook/attempts/{ATT}/assessment/answers/{SINGLE}"
BODY = {"questionType": "multipleanswer", "givenAnswer": [True, False, False, False]}


def choice_question(qa_id, limit=1, selected=None):
    return {
        "id": qa_id, "questionId": f"question-{qa_id}", "questionType": "multipleanswer",
        "visibleQuestionNumber": 1, "givenAnswer": selected,
        "question": {
            "questionText": {"rawText": "<p>Choose a synthetic option.</p>"},
            "answerSelectionLimit": limit, "points": 1,
            # Deliberately nonalphabetical, with duplicate text: mapping must use IDs.
            "answers": [{"id": cid, "answerText": {"rawText": "Same label"}}
                        for cid in ["choice-c", "choice-a", "choice-d", "choice-b"]],
        },
    }


def raw_attempt():
    return {
        "id": ATT, "userId": USER, "courseId": CID, "status": "IN_PROGRESS",
        "toolAttemptDetail": {"resource/x-bb-assessment": {"questionAttempts": [
            {"questionType": "presentation", "question": {"questionText": {"rawText": "Instructions"}}},
            choice_question(SINGLE),
            {"questionType": "presentation"},
            choice_question(MULTI, 3),
            {"id": TF, "questionId": "synthetic-tf", "questionType": "eitherOr", "givenAnswer": None,
             "visibleQuestionNumber": 1,
             "question": {"answerType": "true_false", "questionText": {"rawText": "Synthetic true/false?"}}},
            {"id": ESSAY, "questionId": "synthetic-essay", "questionType": "essay",
             "question": {"questionText": {"rawText": "Unsupported synthetic essay"}}},
        ]}},
    }


def blocks(attempt):
    return attempt["toolAttemptDetail"]["resource/x-bb-assessment"]["questionAttempts"]


def change(qa_id=SINGLE, selections=None, operation="set"):
    return {"question_attempt_id": qa_id, "operation": operation,
            "selected_choice_ids": ["choice-a"] if selections is None else selections}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected network/browser operation in offline answer tests")

    monkeypatch.setattr(rest, "get_json", forbidden)
    monkeypatch.setattr(rest, "get_pages", forbidden)
    monkeypatch.setattr(answers, "_open_no_redirect", forbidden)
    monkeypatch.setattr(answers.BrowserTransport, "_open", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(answers, "SESSION_DIR", tmp_path / "session")
    monkeypatch.setattr(answers, "BLACKBOARD_BASE", ORIGIN)
    monkeypatch.setattr(answers, "_now", lambda: NOW)
    monkeypatch.setattr(answers.time, "sleep", lambda *_: None)
    monkeypatch.setattr(rest, "cookie_header", lambda: "synthetic-cookie=offline")
    monkeypatch.setattr(rest, "xsrf_token", lambda: "synthetic-xsrf")
    from core import session_state
    monkeypatch.setattr(session_state, "user_agent", lambda: "offline-answer-tests")


class Provider:
    def __init__(self):
        self.attempt = raw_attempt()
        self.calls = []
        self.patch_count = 0
        self.read_count = 0
        self.fail_patch = None
        self.persist = True
        self.on_read = None
        self.response_status = "IN_PROGRESS"
        self.columns = [{"id": COL, "contentId": AID, "name": "Synthetic practice",
                         "scoreProviderHandle": "resource/x-bb-assessment",
                         "grading": {"due": (NOW + timedelta(hours=1)).isoformat()}}]
        self.settings = {"timeLimit": 0, "isPasswordRequired": False,
                         "isSecureBrowserRequiredToTake": False, "isWebcamRequired": False,
                         "isIpFilterEnabled": False, "isCompletionForced": False,
                         "isBacktrackingProhibited": False}
        self.attempts = [{"id": ATT}]

    def get_pages(self, path, **kwargs):
        self.calls.append(("GET", path, None))
        assert path == f"/learn/api/public/v2/courses/{CID}/gradebook/columns"
        return deepcopy(self.columns)

    def get_json(self, path, **kwargs):
        self.calls.append(("GET", path, None))
        prefix = f"/learn/api/v1/courses/{CID}"
        if path == "/learn/api/v1/users/me":
            return {"id": USER}
        if path == f"{prefix}/contents/{AID}":
            return {"title": "Synthetic practice", "contentDetail": {"resource/x-bb-asmt-test-link": {
                "test": {"deploymentSettings": deepcopy(self.settings),
                         "gradingColumn": {"dueDate": self.columns[0]["grading"]["due"]}}}}}
        if path == f"{prefix}/gradebook/grades?userId={USER}&limit=200":
            return {"results": [{"id": GRADE, "columnId": COL}]}
        if path == f"{prefix}/gradebook/columns/{COL}/grades/{GRADE}/attempts":
            return {"results": deepcopy(self.attempts)}
        assert path == f"{prefix}/gradebook/attempts/{ATT}?expand=toolAttemptDetail", path
        self.read_count += 1
        if self.on_read:
            self.on_read(self)
        return deepcopy(self.attempt)

    def patch(self, request, **kwargs):
        assert request.method == "PATCH"
        assert request.full_url.startswith(ORIGIN + "/")
        path = request.full_url.removeprefix(ORIGIN)
        body = json.loads(request.data)
        answers._validate_write(path, body)
        self.calls.append(("PATCH", path, body))
        self.patch_count += 1
        if self.patch_count == self.fail_patch:
            raise urllib.error.HTTPError(request.full_url, 503, "synthetic unavailable", {}, None)
        qa_id = path.rsplit("/", 1)[1]
        qa = next(q for q in blocks(self.attempt) if q.get("id") == qa_id)
        if self.persist:
            qa["givenAnswer"] = deepcopy(body["givenAnswer"])
        return json.dumps({"givenAnswer": body["givenAnswer"], "attemptStatus": self.response_status}).encode()

    @property
    def writes(self):
        return [(path, body) for method, path, body in self.calls if method == "PATCH"]

    def assert_safe_routes(self):
        for method, path, body in self.calls:
            assert "submit" not in path.lower()
            assert "start" not in path.lower()
            assert method in ("GET", "PATCH")
            if method == "PATCH":
                assert "/assessment/answers/" in path
                assert not path.endswith("/attempts")
                assert set(body) == {"questionType", "givenAnswer"}


@pytest.fixture
def provider(monkeypatch):
    fake = Provider()
    monkeypatch.setattr(answers, "_read_json", fake.get_json)
    monkeypatch.setattr(answers, "_get_pages", fake.get_pages)
    monkeypatch.setattr(answers, "_open_no_redirect", fake.patch)
    yield fake
    fake.assert_safe_routes()


@pytest.fixture
def state(provider):
    match = answers.resolve_target("Synthetic practice", [CID], {CID: "TEST100 Synthetic course"})
    return answers.load_state(match)


@pytest.fixture
def doc(state):
    result = model.build_export(deepcopy(state["target"]), deepcopy(state["projection"]), NOW)
    result["changes"] = [change(), change(MULTI, ["choice-b", "choice-c"]), change(TF, ["false"])]
    return result


def args(**overrides):
    values = dict(target="Synthetic practice", apply=False, file=None, export=None,
                  json=True, compact=False, out=None, overwrite=False, attempt=None)
    values.update(overrides)
    return SimpleNamespace(**values)


def cli(doc, tmp_path, **overrides):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(doc))
    return answers.run_cli(args(file=str(path), **overrides), {CID: "TEST100 Synthetic course"}, [CID])


def assert_one_json(capsys):
    output = capsys.readouterr()
    text = output.out.strip()
    result, end = json.JSONDecoder().raw_decode(text)
    assert end == len(text), "stdout must contain exactly one JSON document"
    assert isinstance(result, dict)
    assert result["schema_version"] == 1
    return result


def test_projection_types_numbering_and_unsupported():
    result = model.project_attempt(raw_attempt())
    assert result["status"] == "IN_PROGRESS"
    assert [q["number"] for q in result["questions"]] == [1, 2, 3]
    assert [q["type"] for q in result["questions"]] == [model.SINGLE, model.MULTI, model.TRUE_FALSE]
    assert [q["max_selections"] for q in result["questions"]] == [1, 3, 1]
    assert result["unsupported"] == [{"number": 4, "question_attempt_id": ESSAY,
                                       "provider_type": "essay", "prompt": "Unsupported synthetic essay"}]
    assert list(result["raw"]) == [SINGLE, MULTI, TF]
    assert result["questions"][0]["prompt"] == "Choose a synthetic option."


@pytest.mark.parametrize("mutation", ["duplicate_attempt", "missing_attempt", "duplicate_choice"])
def test_projection_refuses_bad_ids(mutation):
    raw = raw_attempt()
    qs = blocks(raw)
    if mutation == "duplicate_attempt":
        qs[3]["id"] = SINGLE
    elif mutation == "missing_attempt":
        del qs[1]["id"]
    else:
        qs[1]["question"]["answers"][1]["id"] = "choice-c"
    with pytest.raises(model.AnswersRefusal, match="[Dd]uplicate|[Mm]issing"):
        model.project_attempt(raw)



def test_projection_refuses_missing_question_id():
    raw = raw_attempt()
    del blocks(raw)[1]["questionId"]
    with pytest.raises(model.AnswersRefusal):
        model.project_attempt(raw)


@pytest.mark.parametrize("given", [[], [True], [1, 0, 0, 0], "choice-a", False, {}, [True, False, False, None]])
def test_projection_refuses_unknown_multiple_encodings(given):
    raw = raw_attempt()
    blocks(raw)[1]["givenAnswer"] = given
    with pytest.raises(model.AnswersRefusal) as exc:
        model.project_attempt(raw)
    assert exc.value.code == "unsupported_encoding"


@pytest.mark.parametrize("given,selected", [(True, ["true"]), (False, ["false"]), (None, [])])
def test_true_false_false_is_not_unanswered(given, selected):
    raw = raw_attempt()
    blocks(raw)[4]["givenAnswer"] = given
    assert model.project_attempt(raw)["questions"][2]["selected_choice_ids"] == selected


@pytest.mark.parametrize("given", [0, 1, "false", [], [False], {}])
def test_projection_refuses_unknown_tf_encodings(given):
    raw = raw_attempt()
    blocks(raw)[4]["givenAnswer"] = given
    with pytest.raises(model.AnswersRefusal) as exc:
        model.project_attempt(raw)
    assert exc.value.code == "unsupported_encoding"


@pytest.mark.parametrize("kind,selected,expected", [
    (model.SINGLE, ["choice-a"], [False, True, False, False]),
    (model.MULTI, ["choice-b", "choice-c"], [True, False, False, True]),
    (model.MULTI, [], [False, False, False, False]),
    (model.TRUE_FALSE, ["true"], True), (model.TRUE_FALSE, ["false"], False),
    (model.TRUE_FALSE, [], None),
])
def test_encode_given_uses_provider_order(kind, selected, expected):
    actual = model.encode_given(kind, ["choice-c", "choice-a", "choice-d", "choice-b"], selected)
    assert actual == expected
    assert type(actual) is type(expected)


@pytest.mark.parametrize("mutation", ["prompt", "choice_order", "choice_id", "media", "type"])
def test_fingerprint_covers_definition(mutation):
    raw = raw_attempt()
    before = model.project_attempt(raw)
    original = model.fingerprint(before["raw"], [SINGLE, MULTI, TF])
    q = blocks(raw)[1]["question"]
    if mutation == "prompt":
        q["questionText"]["rawText"] += " Changed"
    elif mutation == "choice_order":
        q["answers"].reverse()
    elif mutation == "choice_id":
        q["answers"][0]["id"] = "replacement"
    elif mutation == "media":
        q["questionText"]["rawText"] += '<img src="synthetic-media-2.png">'
    else:
        q["answerSelectionLimit"] = 3
    after = model.project_attempt(raw)
    assert model.fingerprint(after["raw"], [SINGLE, MULTI, TF]) != original


def test_fingerprint_excludes_current_selection():
    raw = raw_attempt()
    p = model.project_attempt(raw)
    before = model.fingerprint(p["raw"], [SINGLE, MULTI, TF])
    blocks(raw)[1]["givenAnswer"] = [True, False, False, False]
    p = model.project_attempt(raw)
    assert model.fingerprint(p["raw"], [SINGLE, MULTI, TF]) == before


def test_export_round_trip_is_empty_and_expires_in_fifteen_minutes(state):
    exported = model.build_export(state["target"], state["projection"], NOW)
    assert exported["changes"] == []
    assert model.parse_file(json.dumps(exported)) == exported
    snap = exported["snapshot"]
    assert datetime.fromisoformat(snap["expires_at"]) - datetime.fromisoformat(snap["observed_at"]) == timedelta(minutes=15)
    assert USER not in json.dumps(exported)


@pytest.mark.parametrize("text", ['{"schema_version":1,"schema_version":1}',
                                   '{"target":{"course_id":"x","course_id":"y"}}'])
def test_parse_refuses_duplicate_json_keys(text):
    with pytest.raises(model.AnswersInputError, match="duplicate JSON key"):
        model.parse_file(text)


@pytest.mark.parametrize("where", ["file", "target", "snapshot", "change"])
def test_parse_refuses_unknown_fields(doc, where):
    obj = {"file": doc, "target": doc["target"], "snapshot": doc["snapshot"], "change": doc["changes"][0]}[where]
    obj["unexpected"] = "no"
    with pytest.raises(model.AnswersInputError, match="unknown field"):
        model.parse_file(json.dumps(doc))


@pytest.mark.parametrize("version", [0, 2, "1", None])
def test_parse_refuses_wrong_version(doc, version):
    doc["schema_version"] = version
    with pytest.raises(model.AnswersInputError, match="schema_version"):
        model.parse_file(json.dumps(doc))


@pytest.mark.parametrize("edit", [change(selections=[]), change(operation="clear"),
                                  change(selections=["choice-a", "choice-a"]), change(operation="toggle")])
def test_parse_refuses_invalid_change(doc, edit):
    doc["changes"] = [edit]
    with pytest.raises(model.AnswersInputError):
        model.parse_file(json.dumps(doc))


def test_parse_refuses_duplicate_question_changes(doc):
    doc["changes"].append(deepcopy(doc["changes"][0]))
    with pytest.raises(model.AnswersInputError, match="more than once"):
        model.parse_file(json.dumps(doc))


@pytest.mark.parametrize("text", ["{", "[]", "null", "x" * (model.MAX_FILE_BYTES + 1)])
def test_parse_refuses_malformed_or_oversize(text):
    with pytest.raises(model.AnswersInputError):
        model.parse_file(text)


def test_plan_stale_fingerprint(doc, state):
    with pytest.raises(model.AnswersRefusal) as exc:
        model.plan_changes(doc, state["projection"], "changed-definition")
    assert exc.value.code == "stale_snapshot"


@pytest.mark.parametrize("edit", [change(selections=["unknown"]), change(selections=["choice-a", "choice-b"]),
                                  change(ESSAY, ["choice-a"]), change("_929999_1", ["choice-a"])])
def test_plan_invalid_selection(doc, state, edit):
    doc["changes"] = [edit]
    with pytest.raises(model.AnswersInputError):
        model.plan_changes(doc, state["projection"], state["sha"])


def test_plan_unchanged_desired_allows_safe_rerun(doc, state):
    state["projection"]["questions"][0]["selected_choice_ids"] = ["choice-a"]
    edits = model.plan_changes(doc, state["projection"], state["sha"])
    assert edits[0].status == "unchanged"
    assert edits[1].after == ["choice-c", "choice-b"]


def test_plan_conflict_when_provider_differs_from_baseline(doc, state):
    state["projection"]["questions"][0]["selected_choice_ids"] = ["choice-d"]
    with pytest.raises(model.AnswersRefusal) as exc:
        model.plan_changes(doc, state["projection"], state["sha"])
    assert exc.value.code == "conflict"


@pytest.mark.parametrize("seconds", [0, 899])
def test_snapshot_valid_before_expiry(doc, seconds):
    model.check_snapshot_age(doc, NOW + timedelta(seconds=seconds))


@pytest.mark.parametrize("seconds", [900, 901, 3600])
def test_snapshot_expired(doc, seconds):
    with pytest.raises(model.AnswersRefusal) as exc:
        model.check_snapshot_age(doc, NOW + timedelta(seconds=seconds))
    assert exc.value.code == "stale_snapshot"


@pytest.mark.parametrize("value", ["bad", "2026-10-20T16:00:00", None])
def test_snapshot_invalid_timestamp(doc, value):
    doc["snapshot"]["observed_at"] = value
    with pytest.raises(model.AnswersInputError):
        model.check_snapshot_age(doc, NOW)


def test_snapshot_far_future_refused(doc):
    doc["snapshot"]["observed_at"] = (NOW + timedelta(minutes=3)).isoformat()
    doc["snapshot"]["expires_at"] = (NOW + timedelta(minutes=18)).isoformat()
    with pytest.raises(model.AnswersRefusal):
        model.check_snapshot_age(doc, NOW)



def test_snapshot_near_future_refused(doc):
    doc["snapshot"]["observed_at"] = (NOW + timedelta(seconds=30)).isoformat()
    doc["snapshot"]["expires_at"] = (NOW + timedelta(minutes=15, seconds=30)).isoformat()
    with pytest.raises(model.AnswersRefusal):
        model.check_snapshot_age(doc, NOW)


@pytest.mark.parametrize("path", [
    f"/learn/api/v1/courses/{CID}/gradebook/attempts", PATH + "/submit", PATH + "?submit=true",
    f"/learn/api/v1/courses/{CID}/gradebook/attempts/{ATT}", "/submit", ORIGIN + PATH,
    "https://other.example.invalid" + PATH, PATH.replace(ATT, "../attempt"),
])
def test_validate_write_refuses_non_answer_routes(path):
    with pytest.raises(AssertionError):
        answers._validate_write(path, BODY)



def test_validate_write_refuses_trailing_newline():
    with pytest.raises(AssertionError):
        answers._validate_write(PATH + "\n", BODY)


@pytest.mark.parametrize("key", ["status", "submitted", "finished", "score", "method"])
def test_validate_write_refuses_extra_body_keys(key):
    with pytest.raises(AssertionError):
        answers._validate_write(PATH, {**BODY, key: True})


def test_validate_write_accepts_exact_body():
    assert json.loads(answers._validate_write(PATH, BODY)) == BODY


def test_saver_falls_back_only_when_not_dispatched():
    calls = []

    def http(path, body):
        calls.append("http")
        raise answers.NotDispatched("synthetic unavailable")

    class Browser:
        label = "synthetic-browser"

        def __init__(self, progress):
            calls.append("browser-open")

        def patch(self, path, body):
            calls.append("browser-patch")
            assert (path, body) == (PATH, BODY)
            return {"attemptStatus": "IN_PROGRESS"}

        def close(self):
            calls.append("browser-close")

    saver = answers.Saver(lambda _: None, http=http, browser_factory=Browser)
    assert saver.patch(PATH, BODY)["attemptStatus"] == "IN_PROGRESS"
    saver.patch(PATH, BODY)
    saver.close()
    assert calls == ["http", "browser-open", "browser-patch", "browser-patch", "browser-close"]


@pytest.mark.parametrize("kind", ["unknown", "rejected"])
def test_saver_never_retries_dispatched_failure(kind):
    calls = []

    def http(*_):
        calls.append("http")
        raise answers.WriteFailed(kind, "synthetic failure")

    def browser(*_):
        pytest.fail("WriteFailed must never dispatch a browser retry")

    saver = answers.Saver(lambda _: None, http=http, browser_factory=browser)
    with pytest.raises(answers.WriteFailed):
        saver.patch(PATH, BODY)
    saver.close()
    assert calls == ["http"]


def test_apply_saves_all_types_with_exact_bodies_and_readback(doc, state, provider):
    result = answers.apply(doc, state, lambda _: None)
    assert result["outcome"] == "saved_verified"
    assert result["attempt_status_after"] == "IN_PROGRESS"
    assert result["submitted"] is False
    assert [q["outcome"] for q in result["questions"]] == ["saved_verified"] * 3
    assert provider.writes == [
        (PATH, {"questionType": "multipleanswer", "givenAnswer": [False, True, False, False]}),
        (PATH.replace(SINGLE, MULTI), {"questionType": "multipleanswer", "givenAnswer": [True, False, False, True]}),
        (PATH.replace(SINGLE, TF), {"questionType": "eitherOr", "givenAnswer": False}),
    ]
    methods = [method for method, path, _ in provider.calls if "/gradebook/attempts/" in path]
    assert methods == ["GET", "GET", "GET", "PATCH", "GET", "GET", "GET", "PATCH", "GET", "GET", "GET", "PATCH", "GET", "GET"]
    with answers.AttemptLock(ATT):
        pass  # stable inode remains, but the OS lock is released


@pytest.mark.parametrize("qa_id,given,expected", [
    (SINGLE, [True, False, False, False], [False] * 4), (TF, True, None),
])
def test_apply_explicit_clear(qa_id, given, expected, provider):
    next(q for q in blocks(provider.attempt) if q.get("id") == qa_id)["givenAnswer"] = given
    match = answers.resolve_target(AID, [CID], {CID: "TEST100 Synthetic course"})
    state = answers.load_state(match)
    doc = answers.export_doc(state)
    doc["changes"] = [{"question_attempt_id": qa_id, "operation": "clear"}]
    result = answers.apply(doc, state, lambda _: None)
    assert result["outcome"] == "saved_verified"
    assert len(provider.writes) == 1
    assert provider.writes[0][1]["givenAnswer"] == expected


def test_apply_mid_batch_5xx_stops_without_retry_and_cli_exit4(doc, provider, tmp_path, capsys):
    provider.fail_patch = 2
    assert cli(doc, tmp_path, apply=True) == 4
    result = assert_one_json(capsys)
    assert result["outcome"] in ("unknown", "partial")
    assert provider.patch_count == 2
    assert [q["outcome"] for q in result["questions"][:2]] == ["saved_verified", "unknown"]
    assert provider.writes[-1][0].endswith(MULTI)


def test_apply_readback_mismatch_is_unknown(doc, state, provider):
    provider.persist = False
    result = answers.apply(doc, state, lambda _: None)
    assert result["outcome"] == "unknown"
    assert result["questions"][0]["outcome"] == "unknown"
    assert provider.patch_count == 1
    assert provider.read_count == 7  # initial, two dispatch guards, three polls, final


def test_apply_response_completed_stops_after_one_patch(doc, state, provider):
    provider.response_status = "COMPLETED"
    result = answers.apply(doc, state, lambda _: None)
    assert result["outcome"] in ("unknown", "partial")
    assert provider.patch_count == 1
    assert result["questions"][0]["outcome"] == "unknown"


def test_apply_completed_between_writes_returns_exit4(doc, provider, tmp_path, capsys):
    def complete_after_first_save(fake):
        if fake.patch_count:
            fake.attempt["status"] = "COMPLETED"

    provider.on_read = complete_after_first_save
    code = cli(doc, tmp_path, apply=True)
    result = assert_one_json(capsys)
    assert provider.patch_count == 1
    assert code == 4
    assert result["outcome"] in ("unknown", "partial")
    assert result["attempt_status_after"] == "COMPLETED"


@pytest.mark.parametrize("noop", ["empty", "already_saved"])
def test_apply_noop_zero_writes(doc, state, provider, noop):
    if noop == "empty":
        doc["changes"] = []
    else:
        doc["changes"] = [change(selections=[], operation="clear")]
    result = answers.apply(doc, state, lambda _: None)
    assert result["outcome"] == "unchanged"
    assert provider.writes == []


@pytest.mark.parametrize("seconds", [119, 0, -1])
def test_apply_near_deadline_refuses_before_any_write(doc, state, provider, seconds):
    state["due"] = (NOW + timedelta(seconds=seconds)).isoformat()
    with pytest.raises(model.AnswersRefusal) as exc:
        answers.apply(doc, state, lambda _: None)
    assert exc.value.code == "deadline"
    assert provider.writes == []


def test_apply_live_pid_lock_refused(doc, state, provider):
    with answers.AttemptLock(ATT) as lock:
        owner = lock.path.read_text()
        with pytest.raises(model.AnswersRefusal) as exc:
            answers.apply(doc, state, lambda _: None)
        assert exc.value.code == "locked"
        assert lock.path.read_text() == owner
    assert provider.writes == []



def test_old_lock_with_live_pid_remains_held(doc, state, provider):
    with answers.AttemptLock(ATT) as lock:
        lock.path.write_text(f"{os.getpid()} {answers.time.time() - 601}")
        with pytest.raises(model.AnswersRefusal) as exc:
            answers.apply(doc, state, lambda _: None)
        assert exc.value.code == "locked"
    assert provider.writes == []


def test_apply_per_question_conflict_stops_without_overwrite(doc, state, provider):
    def external_edit(fake):
        if fake.patch_count:
            blocks(fake.attempt)[3]["givenAnswer"] = [False, True, False, False]

    provider.on_read = external_edit
    result = answers.apply(doc, state, lambda _: None)
    assert result["outcome"] == "partial"
    assert provider.patch_count == 1
    assert result["questions"][1]["outcome"] == "conflict"



def test_final_readback_detects_outside_edit_of_omitted_question(doc, state, provider):
    doc["changes"] = [change()]

    def edit_omitted(fake):
        if fake.patch_count:
            blocks(fake.attempt)[3]["givenAnswer"] = [True, False, False, False]

    provider.on_read = edit_omitted
    result = answers.apply(doc, state, lambda _: None)
    assert provider.patch_count == 1
    assert result["outcome"] in ("unknown", "partial")


@pytest.mark.parametrize("key,value", [("timeLimit", 30), ("isPasswordRequired", True),
                                       ("isSecureBrowserRequiredToTake", True), ("isWebcamRequired", True)])
def test_load_state_refuses_restricted_tests(provider, key, value):
    provider.settings[key] = value
    match = answers.resolve_target(AID, [CID], {CID: "TEST100 Synthetic course"})
    with pytest.raises(model.AnswersRefusal) as exc:
        answers.load_state(match)
    assert exc.value.code == "unsafe_settings"
    assert provider.writes == []


@pytest.mark.parametrize("status", ["COMPLETED", "NEEDS_GRADING", "GRADED", None])
def test_load_state_refuses_terminal_or_unknown_status(provider, status):
    provider.attempt["status"] = status
    match = answers.resolve_target(AID, [CID], {CID: "TEST100 Synthetic course"})
    with pytest.raises(model.AnswersRefusal) as exc:
        answers.load_state(match)
    assert exc.value.code == "no_active_attempt"


def test_resolve_target_ambiguous_and_exact_id(provider):
    provider.columns.append({**deepcopy(provider.columns[0]), "id": "_919001_1", "contentId": "_919002_1"})
    with pytest.raises(model.AnswersRefusal) as exc:
        answers.resolve_target("Synthetic", [CID], {CID: "TEST100"})
    assert exc.value.code == "ambiguous_target"
    assert answers.resolve_target(AID, [CID], {CID: "TEST100"})["assessment_id"] == AID


def test_preview_and_human_formatting_are_read_only(doc, state, provider):
    current = answers.format_current(state)
    result = answers.preview(doc, state)
    text = answers.format_result(result, {q["question_attempt_id"]: q for q in state["projection"]["questions"]})
    assert "Read only" in current
    assert "Preview only; nothing saved" in text
    assert "course staff" in text
    assert result["attempt_status_after"] is None
    assert provider.writes == []


def test_write_private_mode_and_no_clobber(tmp_path):
    path = tmp_path / "nested" / "export.json"
    answers.write_private(path, "first")
    assert path.read_text() == "first"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(model.AnswersInputError, match="already exists"):
        answers.write_private(path, "second")
    assert path.read_text() == "first"
    answers.write_private(path, "replacement", overwrite=True)
    assert path.read_text() == "replacement"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("dangling", [False, True])
@pytest.mark.parametrize("overwrite", [False, True])
def test_write_private_refuses_symlinks(tmp_path, dangling, overwrite):
    target = tmp_path / "target"
    if not dangling:
        target.write_text("untouched")
    path = tmp_path / "link"
    path.symlink_to(target)
    with pytest.raises(model.AnswersInputError, match="symlink"):
        answers.write_private(path, "bad", overwrite=overwrite)
    assert path.is_symlink()
    assert not target.exists() if dangling else target.read_text() == "untouched"


def test_cli_apply_without_file_exits2_and_emits_one_json(capsys):
    assert answers.run_cli(args(apply=True), {}, []) == 2
    result = assert_one_json(capsys)
    assert result["error"] == "invalid_input"


@pytest.mark.parametrize("mode", ["read", "preview", "apply", "export", "refusal"])
def test_cli_json_is_one_document(mode, doc, provider, tmp_path, capsys):
    if mode == "read":
        code = answers.run_cli(args(), {CID: "TEST100 Synthetic course"}, [CID])
    elif mode == "export":
        code = answers.run_cli(args(export=str(tmp_path / "export.json")), {CID: "TEST100 Synthetic course"}, [CID])
    else:
        if mode == "refusal":
            provider.columns[0]["grading"]["due"] = (NOW + timedelta(seconds=60)).isoformat()
        code = cli(doc, tmp_path, apply=mode in ("apply", "refusal"))
    result = assert_one_json(capsys)
    assert code == (3 if mode == "refusal" else 0)
    assert result["submitted"] is False
    if mode != "apply":
        assert provider.writes == []

# Adversarial review regressions: all fixtures below remain strictly offline.
@pytest.mark.parametrize("read_number", [2, 3, 5])
def test_fresh_choice_reordering_stops_before_wrong_patch(doc, state, provider, read_number):
    def reorder(fake):
        if fake.read_count == read_number:
            blocks(fake.attempt)[1]["question"]["answers"].reverse()
    provider.on_read = reorder
    if read_number < 5:
        with pytest.raises(model.AnswersRefusal, match="questions or choices"):
            answers.apply(doc, state, lambda _: None)
        assert provider.patch_count == 0
    else:
        result = answers.apply(doc, state, lambda _: None)
        assert provider.patch_count == 1
        assert result["outcome"] == "unknown"


@pytest.mark.parametrize("which", ["initially_unchanged", "previously_verified", "missing_clear"])
def test_all_requested_answers_share_final_snapshot(doc, state, provider, which):
    if which == "initially_unchanged":
        doc["changes"][0] = change(operation="clear", selections=[])
    if which == "missing_clear":
        doc["changes"] = [change(operation="clear", selections=[])]
    # One requested clear: initial + guard + final. Three changes: initial +
    # three (baseline, immediate pre-dispatch, polling) + final.
    final_read = 3 if which == "missing_clear" else (9 if which == "initially_unchanged" else 11)
    def drift(fake):
        if fake.read_count == final_read:
            if which == "missing_clear":
                blocks(fake.attempt).pop(1)
            else:
                blocks(fake.attempt)[1]["givenAnswer"] = [True, False, False, False]
    provider.on_read = drift
    result = answers.apply(doc, state, lambda _: None)
    assert result["outcome"] == "unknown"
    assert result["questions"][0]["outcome"] == "unknown"


def test_per_question_poll_must_finish_before_next_patch(doc, state, provider):
    provider.persist = False
    result = answers.apply(doc, state, lambda _: None)
    assert provider.patch_count == 1
    assert result["outcome"] == "unknown"
    assert [q["outcome"] for q in result["questions"]] == ["unknown", "not_saved", "not_saved"]


@pytest.mark.parametrize("failure", [401, 403, 429, 500, 503, "timeout"])
def test_real_http_fallback_boundary(failure, monkeypatch):
    def failed(req, **kwargs):
        if failure == "timeout":
            raise TimeoutError("synthetic")
        raise urllib.error.HTTPError(req.full_url, failure, "synthetic", {}, None)
    monkeypatch.setattr(answers, "_open_no_redirect", failed)
    calls = []
    class Browser:
        def __init__(self, progress):
            calls.append("open")
        def patch(self, *args):
            calls.append("patch")
            return {"givenAnswer": BODY["givenAnswer"], "attemptStatus": "IN_PROGRESS"}
    saver = answers.Saver(lambda _: None, browser_factory=Browser)
    if failure == 401:
        saver.patch(PATH, BODY)
        assert calls == ["open", "patch"]
    else:
        with pytest.raises(answers.WriteFailed):
            saver.patch(PATH, BODY)
        assert calls == []


def test_no_browser_fallback_after_http_has_persisted(monkeypatch):
    def rejected(req, **kwargs):
        raise urllib.error.HTTPError(req.full_url, 401, "synthetic", {}, None)
    monkeypatch.setattr(answers, "_open_no_redirect", rejected)
    saver = answers.Saver(lambda _: None, browser_factory=lambda _: pytest.fail("already persisted"))
    saver.http_persisted = True
    with pytest.raises(answers.WriteFailed):
        saver.patch(PATH, BODY)


@pytest.mark.parametrize("when", ["before_first", "after_first", "during_guard"])
def test_cooperative_cancel_prevents_further_dispatches(doc, state, provider, when):
    import threading
    cancel = threading.Event()
    if when == "before_first":
        cancel.set()
    def interrupt(fake):
        if (when == "after_first" and fake.patch_count) or (when == "during_guard" and fake.read_count == 3):
            cancel.set()
    provider.on_read = interrupt
    if when in ("before_first", "during_guard"):
        with pytest.raises(model.AnswersRefusal) as exc:
            answers.apply(doc, state, lambda _: None, cancel_event=cancel)
        assert exc.value.code == "cancelled"
        assert not provider.writes
    else:
        result = answers.apply(doc, state, lambda _: None, cancel_event=cancel)
        assert provider.patch_count == 1
        assert result["questions"][0]["outcome"] == "saved_verified"
        assert result["stop_reason"] == "cancelled"
        assert result["dispatched"] is True


def test_lock_held_through_final_reconciliation(doc, state, provider):
    old_read = provider.on_read
    def check_lock(fake):
        if fake.patch_count:
            with pytest.raises(model.AnswersRefusal, match="Another bb answers"):
                with answers.AttemptLock(ATT):
                    pytest.fail("lock released before readback")
        if old_read:
            old_read(fake)
    provider.on_read = check_lock
    assert answers.apply(doc, state, lambda _: None)["outcome"] == "saved_verified"
    with answers.AttemptLock(ATT):
        pass


def test_abandoned_pid_file_does_not_block_os_lock(monkeypatch):
    path = answers.SESSION_DIR / "locks" / f"answers-{ATT}.lock"
    path.parent.mkdir(parents=True)
    path.write_text("12345678 0")
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("no PID probe permitted"))
    with answers.AttemptLock(ATT):
        assert path.exists()
    assert path.exists()


@pytest.mark.parametrize("key", ["isPasswordRequired", "isSecureBrowserRequiredToTake", "isWebcamRequired",
                                  "isIpFilterEnabled", "isCompletionForced", "isBacktrackingProhibited"])
@pytest.mark.parametrize("bad", ["missing", None, "false", 0])
def test_missing_or_unknown_safety_fields_refuse(provider, key, bad):
    if bad == "missing":
        provider.settings.pop(key)
    else:
        provider.settings[key] = bad
    match = answers.resolve_target(AID, [CID], {CID: "TEST100"})
    with pytest.raises(model.AnswersRefusal) as exc:
        answers.load_state(match)
    assert exc.value.code == "unsafe_settings"


@pytest.mark.parametrize("timer,allowed", [({}, False), ({"timerCompletion": "CONTINUAL"}, True),
                                           ({"timerCompletion": None}, False),
                                           ({"timerCompletion": "CONTINUAL", "isTimed": True}, False),
                                           ({"isTimeLimitEnabled": False}, True),
                                           ({"isTimerEnabled": False, "isTimed": True}, False),
                                           ({"isTimerEnabled": None}, False)])
def test_absent_time_limit_requires_explicit_no_timer(provider, timer, allowed):
    provider.settings.pop("timeLimit")
    provider.settings.update(timer)
    match = answers.resolve_target(AID, [CID], {CID: "TEST100"})
    if allowed:
        answers.load_state(match)
    else:
        with pytest.raises(model.AnswersRefusal):
            answers.load_state(match)


@pytest.mark.parametrize("column,allowed", [({}, False), ({"dueDate": None}, True),
                                           ({"dueDate": "bad"}, False), ({"dueDate": False}, False)])
def test_null_due_date_is_distinct_from_unknown(provider, column, allowed):
    test = {"deploymentSettings": provider.settings, "gradingColumn": column}
    if allowed:
        assert answers._safe_due(test) is None
    else:
        with pytest.raises(model.AnswersRefusal) as exc:
            answers._safe_due(test)
        assert exc.value.code == "unknown_deadline"


def test_deadline_rechecked_immediately_before_dispatch(doc, state, provider, monkeypatch):
    old = provider.get_json
    settings_reads = 0
    def get(path, **kwargs):
        nonlocal settings_reads
        if "/contents/" in path:
            settings_reads += 1
            if settings_reads == 2:
                provider.columns[0]["grading"]["due"] = (NOW + timedelta(seconds=119)).isoformat()
        return old(path, **kwargs)
    # The second content read is in the transport dispatch guard.
    monkeypatch.setattr(answers, "_read_json", get)
    with pytest.raises(model.AnswersRefusal) as exc:
        answers.apply(doc, state, lambda _: None)
    assert exc.value.code == "deadline"
    assert provider.patch_count == 0


@pytest.mark.parametrize("response", [b"not-json", b"[]", b"null", b"{}", b'{"attemptStatus":"IN_PROGRESS"}'])
def test_post_dispatch_bad_response_one_receipt_exit4(doc, provider, monkeypatch, tmp_path, capsys, response):
    original = provider.patch
    def malformed(request, **kwargs):
        original(request, **kwargs)
        return response
    monkeypatch.setattr(answers, "_open_no_redirect", malformed)
    assert cli(doc, tmp_path, apply=True) == 4
    result = assert_one_json(capsys)
    assert result["outcome"] == "unknown"
    assert provider.patch_count == 1
    assert provider.read_count >= 5  # GET reconciliation and final snapshot


def test_browser_playwright_error_after_dispatch_normalized(doc, state, provider, monkeypatch):
    monkeypatch.setattr(rest, "xsrf_token", lambda: None)
    class Page:
        def evaluate(self, *args):
            raise RuntimeError("synthetic Playwright page closed")
    browser = answers.BrowserTransport(lambda _: None)
    browser._page = Page()
    monkeypatch.setattr(browser, "_open", lambda: None)
    monkeypatch.setattr(browser, "_token", lambda: "synthetic")
    saver = answers.Saver(lambda _: None, browser_factory=lambda _: browser)
    result = answers.apply(doc, state, lambda _: None, saver=saver)
    assert result["dispatched"] is True
    assert result["outcome"] == "unknown"
    assert result["stop_reason"] == "unknown"


@pytest.mark.parametrize("path", ["https://evil.invalid/steal", "//evil.invalid/steal",
                                  ORIGIN + ".evil.invalid/steal", "https://user@blackboard.example.invalid/steal"])
def test_reads_reject_other_origins_before_network(path):
    with pytest.raises(rest.RestError, match="outside"):
        answers._read_json(path)


def test_redirecting_get_uses_no_redirect_opener(monkeypatch):
    seen = []
    def open_request(req, **kwargs):
        seen.append(req)
        assert req.method == "GET"
        raise urllib.error.HTTPError(req.full_url, 302, "redirect", {"Location": "https://evil.invalid"}, None)
    monkeypatch.setattr(answers, "_open_no_redirect", open_request)
    with pytest.raises(rest.RestError, match="HTTP 302"):
        answers._read_json("/learn/api/v1/users/me")
    assert len(seen) == 1
    assert answers._NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.invalid") is None


def test_cross_origin_pagination_refused(monkeypatch):
    def open_request(req, **kwargs):
        assert req.full_url == ORIGIN + "/columns"
        return b'{"results":[],"paging":{"nextPage":"https://evil.invalid/steal"}}'
    monkeypatch.setattr(answers, "_open_no_redirect", open_request)
    with pytest.raises(rest.RestError, match="outside"):
        answers._get_pages("/columns")


@pytest.mark.parametrize("destination", ["existing", "symlink", "parent_file"])
def test_out_rejected_before_any_patch(doc, provider, tmp_path, capsys, destination):
    path = tmp_path / "receipt"
    if destination == "existing":
        path.write_text("untouched")
    elif destination == "symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        path.write_text("parent")
        path = path / "child"
    assert cli(doc, tmp_path, apply=True, out=str(path)) == 2
    assert assert_one_json(capsys)["error"] == "invalid_input"
    assert not provider.writes


def test_receipt_disk_failure_preserves_save_result(doc, provider, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(answers, "write_private", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
    assert cli(doc, tmp_path, apply=True, out=str(tmp_path / "receipt")) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["outcome"] == "saved_verified"
    assert "Could not write receipt" in output.err
    assert "disk full" in output.err
    assert provider.patch_count == 3


@pytest.mark.parametrize("reason", ["conflict", "no_transport"])
def test_pre_dispatch_failures_exit3(doc, provider, tmp_path, capsys, monkeypatch, reason):
    if reason == "conflict":
        def edit(fake):
            if fake.read_count >= 2:
                blocks(fake.attempt)[1]["givenAnswer"] = [True, False, False, False]
        provider.on_read = edit
    else:
        monkeypatch.setattr(rest, "xsrf_token", lambda: None)
        def unavailable(self):
            raise answers.NotDispatched("synthetic unavailable")
        monkeypatch.setattr(answers.BrowserTransport, "_open", unavailable)
    assert cli(doc, tmp_path, apply=True) == 3
    assert assert_one_json(capsys)["outcome"] == "refused"
    assert not provider.writes


def test_private_writer_uses_exclusive_unique_temp(doc, tmp_path):
    path = tmp_path / "receipt"
    predictable = tmp_path / f".receipt.{os.getpid()}.tmp"
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    predictable.symlink_to(victim)
    answers.write_private(path, "safe")
    assert victim.read_text() == "untouched"
    assert path.read_text() == "safe"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("record", [None, [], "bad", {}, {"question_attempt_id": 1, "selected_choice_ids": []},
                                    {"question_attempt_id": SINGLE, "selected_choice_ids": "bad"},
                                    {"question_attempt_id": SINGLE, "selected_choice_ids": [1]}])
def test_invalid_nested_snapshot_records_are_input_errors(doc, record):
    doc["snapshot"]["questions"] = [record]
    with pytest.raises(model.AnswersInputError):
        model.parse_file(json.dumps(doc))


def test_invalid_utf8_is_one_json_input_error(tmp_path, capsys):
    path = tmp_path / "invalid.json"
    path.write_bytes(b"\xff")
    assert answers.run_cli(args(file=str(path)), {}, []) == 2
    assert assert_one_json(capsys)["error"] == "invalid_input"


@pytest.mark.parametrize("status", ["COMPLETED", "GRADED", None, "IN_PROGRESS"])
def test_human_footer_matches_final_status(doc, state, status):
    result = answers.preview(doc, state)
    result["outcome"] = "unknown"
    result["attempt_status_after"] = status
    text = answers.format_result(result, {q["question_attempt_id"]: q for q in state["projection"]["questions"]})
    if status == "IN_PROGRESS":
        assert text.endswith("NOT SUBMITTED. Open the test in Blackboard to review and submit.")
    else:
        assert "NOT SUBMITTED" not in text
        assert text.endswith(f"This command sent no submit action. Blackboard now reports {status or 'UNKNOWN'}.")


def test_ctrl_c_main_dispatch_stops_thread_and_keeps_receipt(doc, provider, tmp_path, monkeypatch, capsys):
    import asyncio
    import signal
    import threading
    import main
    path = tmp_path / "changes.json"
    path.write_text(json.dumps(doc))
    parsed = main._parse_args(["answers", AID, "-c", CID, "--json", "--file", str(path), "--apply"])
    monkeypatch.setattr(main, "load_courses", lambda: {CID: "TEST100 Synthetic course"})
    handler = None
    restored = []
    previous = object()
    def signal_handler(sig, callback):
        nonlocal handler
        if callback is previous:
            restored.append(sig)
        else:
            handler = callback
        return previous
    monkeypatch.setattr(signal, "signal", signal_handler)
    original = provider.patch
    def interrupt_after_patch(request, **kwargs):
        assert threading.current_thread() is not threading.main_thread()
        result = original(request, **kwargs)
        handler(signal.SIGINT, None)
        return result
    monkeypatch.setattr(answers, "_open_no_redirect", interrupt_after_patch)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(main.main_async(parsed))
    assert exc.value.code == 4
    result = assert_one_json(capsys)
    assert provider.patch_count == 1
    assert result["questions"][0]["outcome"] == "saved_verified"
    assert result["stop_reason"] == "cancelled"
    assert restored == [signal.SIGINT]


def test_async_cancellation_waits_for_receipt(doc, provider, tmp_path, monkeypatch, capsys):
    import asyncio
    import threading
    import main
    path = tmp_path / "changes.json"
    path.write_text(json.dumps(doc))
    parsed = main._parse_args(["answers", AID, "-c", CID, "--json", "--file", str(path), "--apply"])
    monkeypatch.setattr(main, "load_courses", lambda: {CID: "TEST100 Synthetic course"})
    entered = threading.Event()
    release = threading.Event()
    original = provider.patch
    def delayed(request, **kwargs):
        result = original(request, **kwargs)
        entered.set()
        assert release.wait(3), "test cancellation did not release transport"
        return result
    monkeypatch.setattr(answers, "_open_no_redirect", delayed)
    async def scenario():
        # main_async intentionally exits with SystemExit; contain that inside its task.
        async def run():
            try:
                await main.main_async(parsed)
            except SystemExit as exc:
                return exc.code
        task = asyncio.create_task(run())
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)  # cancellation handler sets the cooperative event
        release.set()
        return await task
    assert asyncio.run(scenario()) == 4
    result = assert_one_json(capsys)
    assert provider.patch_count == 1
    assert result["stop_reason"] == "cancelled"
    assert result["questions"][0]["outcome"] == "saved_verified"


def test_unexpected_post_dispatch_read_shape_still_emits_receipt(doc, provider, tmp_path, capsys):
    def malformed(fake):
        if fake.patch_count:
            fake.attempt["toolAttemptDetail"] = []
    provider.on_read = malformed
    assert cli(doc, tmp_path, apply=True) == 4
    result = assert_one_json(capsys)
    assert result["outcome"] == "unknown"
    assert provider.patch_count == 1


def test_reconciliation_polls_with_bounded_read_timeouts(doc, state, provider, monkeypatch):
    original = provider.get_json
    timeouts = []
    def get(path, **kwargs):
        if "gradebook/attempts/" in path and provider.patch_count and kwargs.get("timeout") == 0.6:
            timeouts.append(kwargs["timeout"])
        return original(path, **kwargs)
    monkeypatch.setattr(answers, "_read_json", get)
    provider.persist = False
    assert answers.apply(doc, state, lambda _: None)["outcome"] == "unknown"
    assert timeouts == [0.6, 0.6, 0.6]


def test_read_only_views_skip_safety_gate_but_writes_do_not(provider):
    provider.settings.pop("timeLimit")  # unknown timer: refuse for writes, still readable
    match = answers.resolve_target(AID, [CID], {CID: "TEST100"})
    state = answers.load_state(match, check_safety=False)
    assert state["projection"]["questions"]
    with pytest.raises(model.AnswersRefusal):
        answers.load_state(match)
    assert provider.writes == []
