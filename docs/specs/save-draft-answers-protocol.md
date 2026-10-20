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
an explicit false isTimeLimitEnabled/isTimerEnabled/isTimed flag or the observed
CONTINUAL timerCompletion enum, with no contradictory or unknown timer flag.
Other timerCompletion enums without an explicit no-timer flag are unknown. A dueDate field must
be present; null means no due date. Settings and the remaining deadline window are
rechecked immediately before dispatch. All GETs validate the origin and block redirects.

Post-dispatch Playwright/JSON/schema failures become unknown, reconcile by GET, stop
the batch and emit one receipt (exit 4). Pre-dispatch refusals/conflicts use exit 3.
Ctrl-C/cancellation prevents further dispatches while allowing final reconciliation.
Validate --out before writes; a later file failure is reported on stderr without
replacing the real stdout receipt or exit code. Atomic private files use mkstemp in
the destination directory, fchmod 0600 and os.replace, refusing symlink targets.

## Shorthand (offline-verified implementation)

`--set N=A,B` translates provider-order letters into the existing ID-based `changes[]`.
True/False/T/F and `N=clear` are supported. Letters follow bb's list, which may differ
from Blackboard's shuffled display; matching by option text is never allowed. All
number/letter/type/cardinality/duplicate checks run before any answer write; retakes
preflight against readable prior questions before start and validate the new runtime
list again afterward. If prior questions are unavailable, start separately, then
preview/save. This avoids spending an attempt on input that cannot be validated.
A fresh runtime baseline bypasses file-age expiry; baseline/fingerprint guards,
per-question GETs, OS lock and final verification are the same save implementation.

Verified answer types: multiple answer (select all); single choice (multiple choice
with one answer, encoded as `multipleanswer` with `answerSelectionLimit: 1`); true/false
(`eitherOr` / `true_false`). Essay, short answer, fill-in-the-blank, matching, ordering,
calculated, file response, and anything else are unsupported: **answer in Blackboard**.
These encodings were observed by the original save implementation; exhaustive live
clear behavior remains unverified.

## Attempt creation: static source observed, transport UNVERIFIED live

On 2026-10-20 GET-only inspection of the deployed public Ultra build
`uiv4001.2.0-rel.35_ca6d19d` found this call chain:

- `app-99500fbee07467ea.js`: assessment service `launchAttempt` builds
  `scoreProviderHandle: Test`, `status: InProgress`, and
  `toolAttemptDetail["resource/x-bb-assessment"]: {type: Test, password: undefined}`.
  JSON serialization omits the undefined password. The assessment type enum's Test
  value is `"Test"`.
- The gradebook column model's `launchAttempt` sends POST to its own column URL plus
  `/attempts` (group attempts use a different path and are excluded).
- `15059-99500fbee07467ea.js`, module `293183`, also exposes `createAttempt` through
  the POST request class for `/v1/courses/%courseId%/gradebook/columns/%columnId%/attempts`.
- GET-only content inspection observed `deploymentSettings.attemptCount` as the
  attempts-allowed setting. `bb assignment` already reads that provider field.

Public static sources (no session material):
[app bundle](https://ultra.content.blackboardcdn.com/ultra/uiv4001.2.0-rel.35_ca6d19d/app-99500fbee07467ea.js),
[API bundle](https://ultra.content.blackboardcdn.com/ultra/uiv4001.2.0-rel.35_ca6d19d/15059-99500fbee07467ea.js).
No live POST was used to discover or test this path. The inferred minimal serialized
body, now behind a separate strict allowlist in `scrapers/answer_attempts.py`, is:

```http
POST /learn/api/v1/courses/_100_1/gradebook/columns/_200_1/attempts
Content-Type: application/json;charset=UTF-8
```

```json
{
  "scoreProviderHandle": "resource/x-bb-assessment",
  "status": "IN_PROGRESS",
  "toolAttemptDetail": {"resource/x-bb-assessment": {"type": "Test"}}
}
```

Cookies/XSRF use the same origin-scoped session as answer saves. No extra body fields,
query strings, alternate routes, redirects, submit/finalize or whole-attempt updates
are allowed. The HTTP endpoint/body, deployed permission behavior, response shape,
UI selectors and installed/bundled browser creation all remain **UNVERIFIED live**.
The browser's actual serialized request may differ; extra fields are refused until
reviewed rather than widening the allowlist automatically.

Preconditions: `_safe_due`, complete known current-user attempt inventory, positive
`attemptsAllowed - existing count` (provider `attemptCount` maps to attemptsAllowed),
and at least 120 seconds before due. Unknown/contradictory counts and unlimited
sentinels refuse. An existing IN_PROGRESS attempt is reported and reused. Recheck
under an assessment lock immediately before dispatch. Missing timeLimit with the
observed CONTINUAL timer enum is accepted by the shared safety check; unknown enums
or contradictory enabled flags refuse.

Transport order: HTTP → installed browser → optional bundled Playwright through
`core.browser`. Only missing session/XSRF **before** dispatch permits fallback. Unlike
answer PATCH's observed 401 exception, **no dispatched start may fall back or retry**,
including 401, 403, 404, redirect, timeout, 429, 5xx or malformed replies. Browser
mode clicks one exact `Start new attempt` / `Start attempt` button, refuses timer or
access-code prompts and intercepts all writes: only one exact creation POST can pass.
It never confirms additional prompts or clicks Submit.

After either transport, at most three GET inventory reads (1+2 seconds backoff) must
show all old IDs plus exactly one new IN_PROGRESS attempt, with matching user/course
and no second active attempt. Print that ID. An acknowledgment alone proves nothing.
A lost acknowledgment may reconcile successfully; ambiguous/missing results exit 4,
state that a start was dispatched, and instruct inspection before retrying. Creation
receipts survive later save failures. No rollback or cleanup mutation is sent.
The local lock and GET confirmation do not provide server idempotency or prevent
another host/browser consuming an attempt concurrently. See [ADR-0007](../adr/0007-explicit-untimed-attempt-creation.md).

## Live acceptance procedure (not run during implementation)

Use only an explicitly authorized disposable untimed practice test before its due
cutoff, with known attempts remaining. Substitute its exact content ID and course
for `PRACTICE_ID` and `DEMO100`; do not use a graded test as a probe.

```bash
# Read-only baseline: record attempt IDs/count; confirm settings and remaining attempts.
bb assignment PRACTICE_ID -c DEMO100 --full --json
# Parse-only; zero dispatch:
bb answers PRACTICE_ID -c DEMO100 --new-attempt --help
# MUTATION: run once, only with explicit start authorization.
bb answers PRACTICE_ID -c DEMO100 --new-attempt --json
# Independent reads; confirm reported ID, IN_PROGRESS, one added attempt, no submission.
bb answers PRACTICE_ID -c DEMO100 --json
bb assignment PRACTICE_ID -c DEMO100 --full --json
# Preview chosen synthetic practice selections using bb's current letters.
bb answers PRACTICE_ID -c DEMO100 --set 1=A --set 2=A,B --set 3=False
# MUTATION: only after explicit save authorization; no new start is needed here.
bb answers PRACTICE_ID -c DEMO100 --set 1=A --set 2=A,B --set 3=False --apply --json
```

For a separately authorized retake with readable prior questions and no active attempt,
combined acceptance is exactly:

```bash
bb answers PRACTICE_ID -c DEMO100 --new-attempt --set 1=A --set 2=A,B --set 3=False --apply --json
```

Use actual question numbers/types from that disposable practice target. Retain only
sanitized request shapes and counts. Confirm persisted selections in Blackboard after
reload and record the transport actually exercised. HTTP success does not accept the
browser path. Do not re-run new-attempt to force another transport; browser acceptance
requires a separately authorized eligible practice start with HTTP unavailable before
dispatch. If any start exits 4, inspect first; never retry automatically. Never submit.
