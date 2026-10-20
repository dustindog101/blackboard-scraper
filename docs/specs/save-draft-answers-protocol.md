# Answer-save protocol

Status: observed live on 2026-10-20; implemented on `feat/save-draft-answers`.
All examples below are synthetic. No credentials, student content or live IDs are recorded.

## Observed

Selecting an option in the Ultra UI produced a per-Question Attempt save. Reproducing
the request with plain HTTP returned 200; independent reads confirmed persistence.

```http
PATCH /learn/api/v1/courses/_100_1/gradebook/attempts/_300_1/assessment/answers/_400_1
Content-Type: application/json;charset=UTF-8
```

Authentication requires session cookies and `x-blackboard-xsrf`, taken from
`XSRF-TOKEN` or the `xsrf:` value in `BbRouter`. Values are deliberately omitted.
The UI sends a question copy, `questionIndex`, `pageIndex` and `isVisibleGivenFilter`.
The minimal body below was also accepted and persisted:

```json
{"questionType": "multipleanswer", "givenAnswer": [false, true, false]}
```

- `multipleanswer`: `givenAnswer` is a boolean list parallel to `question.answers[]`.
  Choice IDs such as `new_<uuid>` were stable across reads. With
  `answerSelectionLimit: 1`, the UI renders radio buttons (single choice).
- `eitherOr` with `question.answerType: "true_false"`: `givenAnswer` is `true` or
  `false`; an unanswered response is `null`. False is an answer, not an empty value.
- The response echoes `givenAnswer`, reports `attemptStatus: "IN_PROGRESS"`, and
  includes `saveTimes[]` and `unansweredAnswerableQuestions`. Acknowledgment alone
  is not independent persistence evidence.

Synthetic true/false save body:

```json
{"questionType": "eitherOr", "givenAnswer": false}
```

## Implementation mapping and transport

`core/assessment_answers.py` maps provider IDs to `selected_choice_ids`. Single-choice
and multiple-answer sets become boolean lists in provider choice order; true/false uses
the local IDs `"true"` and `"false"`. Explicit `clear` encodes all-false for
`multipleanswer` and `null` for true/false. These mappings do not establish support
for other question types or constitute live acceptance of every clear case.

`scrapers/answers.py` reads the existing attempt using
`GET /learn/api/v1/courses/{cid}/gradebook/attempts/{attemptId}?expand=toolAttemptDetail`.
It validates the whole change set, re-reads the current selection immediately before
each PATCH, recomputes the full definition fingerprint (including choice order), and
encodes the answer from that fresh definition. A mismatch refuses/stops. After each
PATCH, it GETs that question until confirmed (maximum three reads, 1+2 seconds backoff,
0.6-second per-read timeouts, about five seconds total), before sending another PATCH.
One final snapshot independently verifies every requested question/value, including
unchanged and previously verified answers, presence, definition and IN_PROGRESS status.
The fcntl/msvcrt OS lock stays held through reconciliation and final verification;
lock files are never unlinked and no PID liveness probes are used.
Writes are sequential per question, not an atomic batch; earlier saves can persist if
a later save fails. Bounded GET polling can reconcile persistence without replaying writes.

Transport follows the student's required order, consistent with ADR-0002 and login:

1. Direct HTTP using the saved session and XSRF token.
2. Only before HTTP has persisted any answer, when session/XSRF is missing before
   dispatch or HTTP returns 401, send
   the same allowlisted PATCH through same-origin `fetch()` in the installed browser.
3. Browser launch follows `core.browser` candidate order, with bundled Playwright
   Chromium as the optional last resort. Browser selection occurs before dispatch;
   it is not a second write attempt after a failed browser request.

Only the exact answer route and `questionType` / `givenAnswer` body fields are allowed.
There are no UI option clicks, attempt-start or submission calls in the save adapter.
HTTP 401 is an explicit Blackboard rejection with no persistence, rather than an
ambiguous write failure; it is the sole post-dispatch exception allowing fallback.
HTTP 403, 429, 5xx, timeouts, redirects and malformed replies never allow fallback.
Other dispatched-then-failed writes are never retried on another transport. Timeouts, 429,
5xx or malformed responses stop writes and require read-only reconciliation. No
automatic rollback occurs.

## Known limitation: no verified conditional write

No ETag, revision or conditional-write mechanism was observed. This is not proof that
Blackboard has none. The branch compares against the exported baseline and re-reads
immediately before each PATCH, refusing a mismatch unless the desired answer is already
present. A local attempt lock coordinates CLI writers on this machine only.

These checks are **weaker than provider-enforced compare-and-swap (CAS)**: a browser
or another host can edit between the last GET and PATCH, and that edit can be overwritten.
Readback cannot close this race. The student explicitly authorized proceeding with this
limitation. [ADR-0006](../adr/0006-explicit-answer-save-without-submit.md) records the
deviation from the original spec's CAS gate and browser-write ban.

## Unverified

Essay, fill-in, matching and other encodings remain unsupported. CAS and server-side
atomic editability guarantees remain unverified. Behavior after the due date was not
tested; apply refuses when less than 120 seconds remain, including past-due attempts.
This local cutoff is not proof of server deadline enforcement or indefinite draft retention.
The command never submits, but Blackboard or an instructor may change attempt state.

## Safety metadata and receipts

Recognized restriction booleans must be explicitly present. A missing timeLimit needs
an explicit false isTimeLimitEnabled/isTimerEnabled/isTimed flag and no contradictory
or unknown flag; timerCompletion CONTINUAL alone is insufficient. A dueDate field must
be present; null means no due date. Settings and the remaining deadline window are
rechecked immediately before dispatch. All GETs validate the origin and block redirects.

Post-dispatch Playwright/JSON/schema failures become unknown, reconcile by GET, stop
the batch and emit one receipt (exit 4). Pre-dispatch refusals/conflicts use exit 3.
Ctrl-C/cancellation prevents further dispatches while allowing final reconciliation.
Validate --out before writes; a later file failure is reported on stderr without
replacing the real stdout receipt or exit code. Atomic private files use mkstemp in
the destination directory, fchmod 0600 and os.replace, refusing symlink targets.
