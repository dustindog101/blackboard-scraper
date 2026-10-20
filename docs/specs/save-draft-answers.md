# Save answers to an existing Assessment Attempt

Status: implementation exists on `feat/save-draft-answers`. No provider CAS was observed;
the student authorized fresh baseline re-reads instead (a weaker guard with a remaining
GET/PATCH race) and HTTP-first browser fallback. These deviations supersede the CAS
blocker/browser-write ban below; see [ADR-0006](../adr/0006-explicit-answer-save-without-submit.md)
and the [observed protocol](save-draft-answers-protocol.md). Sections after the implemented safeguards record the original proposal.

## Current shorthand and explicit retake extension

`--set N=A,B` (repeatable) translates provider-order letters to stable choice IDs in
exactly the same internal changes list as `--file`. True/False/T/F and `N=clear` are
accepted; number/letter/cardinality/type/duplicate validation runs before writes.
Read-only and preview/apply views show A., B., C. with the existing radio/checkbox
markers; unchanged preview/apply questions collapse. Letters follow bb's list, which
may differ from Blackboard's shuffled display; never match option text.
The baseline is fresh at runtime, with no file-age expiry; all fingerprint, baseline,
lock, readback and receipt guards remain shared. `--set` excludes `--file` / `--export`.

`--new-attempt` is a separate explicit mutation, authorized by the current request and
[ADR-0007](../adr/0007-explicit-untimed-attempt-creation.md). It supersedes the original
no-start invariant **only for this flag**, leaving `bb assignment --start-attempt`
unchanged. It never submits. Apply's `_safe_due`, complete known attempt count,
positive attempts remaining and the 120-second due window are rechecked immediately
before dispatch. Use an existing IN_PROGRESS attempt instead of starting again.
HTTP precedes the installed browser and optional bundled Playwright; only pre-dispatch
unavailability permits fallback. No dispatched start is retried, including 401/404.
GET must confirm exactly one new IN_PROGRESS attempt and ID; ambiguity exits 4.
Timer/access-code prompts refuse; the browser blocks submit and all unrelated writes.
Creation remains **UNVERIFIED live**; static JS inspection is evidence for its shape,
not provider acceptance. See the [protocol](save-draft-answers-protocol.md).
Combined start/save preflights against readable prior questions before spending an
attempt; without them start separately, then preview/save. New question changes may
prevent saving after creation (exit 4, created ID retained). File/attempt pinning cannot
be combined with new-attempt. No new schedules or unattended starts are introduced.

Verified answer types: multiple answer (select all); single choice (multiple choice
with one answer, encoded by Ultra as `multipleanswer` with `answerSelectionLimit: 1`);
and true/false (`eitherOr` / `true_false`). Essay, short answer, fill-in-the-blank,
matching, ordering, calculated, file response, and anything else are unsupported:
**answer in Blackboard**. Type support does not establish live acceptance of every clear case.

## Implemented review safeguards

The implementation now revalidates full question definitions and fresh choice order
immediately before each dispatch, checks each PATCH with bounded GET polling before
the next, and verifies all requested values together in a final IN_PROGRESS snapshot.
A held fcntl/msvcrt lock covers reconciliation; no PID-file expiry or unlink is used.
Safety metadata fails closed, deadline/settings are refreshed before dispatch, GETs
block redirects/cross-origin pagination, and cooperative cancellation stops further
writes while retaining a receipt. Only missing session/XSRF before dispatch or explicit
HTTP 401 rejection (no persistence) can trigger browser fallback, and never after HTTP
has persisted another answer. Output/input validation and exit-code details are in the
[CLI reference](../CLI_REFERENCE.md#bb-answers-target--c-course).

## Outcome and current evidence

A student asks an agent to enter chosen options in Blackboard, leave them saved, and
let the student review and submit in Blackboard. Saving answers is a remote mutation
even though it does not submit. It must require an explicit save instruction.

Source inspection at `cb8312f` and installed `bb assignment --help` on 2026-10-20 show:

- `main.py` exposes assignment inspection and guarded attempt initiation, no answer writer.
- `scrapers/quiz.py::_scrape_assessment_http` is GET-only; existing Question Attempts
  expose IDs, prompts, choices, and selected answers. Choice IDs and structured answer
  values are lost in the human-oriented projection; do not write from its text alone.
- Positional numbering includes presentation blocks; `visibleQuestionNumber` can repeat.
- `core/rest.py` has GET and POST helpers. Its POST helper serves read-only retrieve
  endpoints; it does not establish a safe answer-save protocol.
- [Issue #15](https://github.com/dustindog101/blackboard-scraper/issues/15) concerns
  inspecting submissions/drafts and explicitly excludes updating them. This is separate.

The answer-save endpoint, request method/body, version controls, and response semantics
are **unverified**. This spec intentionally does not invent an endpoint. Discovery is
the first implementation gate. Existing ADR-0003/0005 read defaults and Attempt
Initiation Guards stay intact; document the new explicit mutation in an additional ADR.

## Scope and invariants

First release: native Ultra untimed Quiz / Test, one existing, editable, current-user
Assessment Attempt with explicit `IN_PROGRESS` state. Support single-choice,
multiple-answer, and true/false questions after each provider encoding is verified.

1. Never create, start, resume through a state-changing action, submit, finalize,
   complete, grade, or delete an Assessment Attempt. No `--submit` mode exists.
2. Read/preview/export cause no remote writes. `--apply` is the only mutation switch.
3. Reject timed, proctored, locked, access-code-controlled, or unknown-safety attempts,
   including already active timed attempts. Never bypass restrictions.
4. Never choose a different attempt after export. Never use the first/latest attempt
   as a substitute for resolving an unambiguous, editable attempt owned by this user.
5. All requested edits validate before the first write. Omitted questions stay unchanged.
6. A save counts as verified only after fresh independent readback of exact structured
   values and the attempt's still-in-progress state. HTTP success or a UI tick is insufficient.
7. Stop on conflict, unknown persistence, or unexpected state. Never blindly replay writes,
   auto-submit, start a replacement attempt, or automatically undo a partial save.

Out of scope: essay/rich text, fill-in blanks, matching, ordering, calculated questions,
file uploads, assignment dropboxes, discussions, Original-course assessments, LTI/external
tools, answer generation, correctness checks, and automatic scheduled filling. Report
unsupported types without inferring a format. Additional types require explicit specs/tests.

Blackboard may autosubmit on a deadline or an instructor may change state. The promise
is that **this command never submits**, plus an observed final status/time; it cannot
promise indefinite draft retention. Refuse at/past deadline or an imminent configured
auto-submit cutoff; unknown cutoff/editability safety metadata blocks apply. A conservative
minimum remaining window (initially 120 seconds) must be checked before every write;
it does not prove a batch will finish. No late-save override in v1.

## Proposed CLI (not available yet)

```sh
# Export a snapshot/template; does not create an attempt or change answers.
bb answers "Library Research Module" -c ENGL --export answers.json

# Edit changes[] in the file, then show the exact before/after without saving.
bb answers "Library Research Module" -c ENGL --file answers.json

# Explicitly save only those changes; verify and leave in Blackboard for review.
bb answers "Library Research Module" -c ENGL --file answers.json --apply
```

- Reuse target resolution, but require exactly one course/item; ambiguity is an error
  listing candidates. Explicit IDs are supported. No bulk/all-course apply.
- Export and file modes are mutually exclusive. `--apply` requires file mode; unknown
  flags and incompatible `--start-attempt`, `--force-start`, browser/CDP flags, or inspection
  views error before network mutation. No implicit apply through legacy flags.
- Accept `--attempt ID` to choose among multiple eligible active attempts; ownership,
  course/item relation, and eligibility must still be proven. Export pins that choice.
- Human preview lists course, item, attempt, local-zone deadline, question wording,
  before/after options and changed/unchanged counts. It states “Preview only; nothing saved.”
  It must say saved answers may be visible to course staff; do not call them private drafts.
- Apply's explicit flag supplies the CLI confirmation; do not introduce mandatory stdin
  prompts for agents. The calling agent still follows the user's authorization scope.
- `--json` prints exactly one versioned JSON result, including on errors; progress to stderr.
  `--out` writes the same result atomically; keep template and result paths distinct.
- Reject conflicting flags, duplicate JSON keys, malformed/oversize files, unknown schema
  versions/fields and duplicate changes. No eval, YAML constructors, arbitrary URLs, raw
  HTTP payloads, student IDs, tokens, or API method fields in the file.

## Snapshot and change contract

Illustrative synthetic structure; implementation must publish a complete JSON Schema.
Export uses fresh provider data, not saved course snapshots or `school.db`.

```json
{
  "schema_version": 1,
  "target": {
    "origin": "https://blackboard.example.edu",
    "course_id": "course-example",
    "assessment_id": "assessment-example",
    "attempt_id": "attempt-example",
    "account_binding": "opaque-local-account-digest"
  },
  "snapshot": {
    "observed_at": "2026-10-20T10:00:00-04:00",
    "expires_at": "2026-10-20T10:15:00-04:00",
    "definition_sha256": "example-only-not-a-real-digest",
    "questions": [
      {
        "question_attempt_id": "qa-example",
        "question_id": "q-example",
        "type": "multiple_answer",
        "prompt": "Which resources apply?",
        "choices": [
          {"id": "choice-a", "text": "Books"},
          {"id": "choice-b", "text": "Articles"}
        ],
        "selected_choice_ids": [],
        "revision": "provider-version-if-supported"
      }
    ]
  },
  "changes": [
    {
      "question_attempt_id": "qa-example",
      "operation": "set",
      "selected_choice_ids": ["choice-a", "choice-b"]
    }
  ]
}
```

- Export `changes: []`; export itself cannot schedule any answer edits. Non-supported
  blocks may be described separately, never become actionable change records.
- Account binding is a locally keyed digest, never a raw student identifier or reusable
  credential. Resolve current `/users/me` identity afresh and compare; fail on missing
  local binding key or account switch. Do not send the digest to arbitrary hosts.
- Treat input as untrusted. Validate origin against configured Blackboard origin and all
  IDs against fresh provider data. Do not construct arbitrary API paths from input URLs.
- Recompute definition fingerprint over full raw question identity/type, prompt and all
  choice identities/text/order (including media identity, not stripped text alone).
  Snapshot is a consistency aid, not authentication or a signed authorization.
- Enforce 15-minute snapshot expiry (future/invalid timestamps fail), then recheck the
  actual definition/settings regardless of timestamp. Export again after expiration.
- Stable provider choice IDs are required, or a verified equivalent stable mapping.
  True/false uses verified provider values mapped to stable IDs, never an assumed array order.
  No indexing by letters, item position, points, fuzzy text, or substring matching.
- Single-choice/true-false `set` selects exactly one valid choice; multi-answer selects
  one or more unique valid choices (respect provider cardinality constraints). Use an
  explicit `operation: "clear"` with no selections to clear a response. `set: []` is invalid.
- `set` replaces the entire selected set for that one Question Attempt, not an additive
  toggle. Compare multiple-answer selections as sets; write using verified provider order.
- Duplicate option text, shuffled answers, presentation-only blocks and identical question
  prompts must remain unambiguous through IDs. Missing/malformed provider IDs fail closed.
- Unsupported/unknown answer encoding or incomplete pagination prevents apply; never
  reinterpret booleans, omitted answers, zero, null, and an empty selection as equivalent
  until the adapter proves their meaning. Hidden feedback/correct answers are not needed.

## Preflight, save, and reconciliation

1. Parse/validate the entire local input; establish current authenticated user and exact
   target/attempt. Use identity-verified, origin/path-filtered session cookies. Do not auto
   login or request Duo during apply. Obtain required anti-CSRF state without logging it.
2. Acquire a bounded local per-origin/account/attempt lock shared across CLI processes.
   Locks need crash recovery and owner checks. This does not lock browser tabs or other hosts.
3. GET current attempt, complete questions/answers, settings, permissions, deadline and
   provider revisions. Distinguish 401, 403, 404, conflict, network and schema failures.
   Resolve server clock skew conservatively from provider Date/time metadata; uncertain
   near-cutoff timing refuses apply. Terminal/unknown status or ownership mismatch refuses.
4. Validate every edit and fingerprint. Preserve untouched question answers. An empty
   changes list returns `unchanged`, with zero writes. For each edit, if current equals
   desired, report `unchanged`; otherwise current must equal the exported baseline or
   refuse the batch before writing. This makes rerunning a partly completed file safe
   only when verified persisted changes already equal desired and all other baselines match.
5. Use a dedicated answer-save transport with an allowlist of verified routes/methods/
   request fields. It must not be able to call submission/attempt-creation endpoints or
   send status/finished/submitted/scoring fields. Block cross-origin redirects and credential
   forwarding. Never serialize a whole attempt back to Blackboard.
6. Require provider-enforced conditional writes (ETag/revision or an equivalent verified
   mechanism) to prevent lost updates. Use the provider revision from the immediate
   pre-write read; baseline checks alone do not close the race. If the deployment lacks
   this capability, return `unsupported_concurrency` and block apply in v1. Do not silently
   fall back to unguarded REST or browser autosave. Review any weaker alternative separately.
7. Prefer an atomic conditional batch only if verified by the provider protocol. Otherwise
   save deterministically one question at a time with per-write state/version/cutoff checks.
   Document the provider's actual atomicity boundary. Validate all local edits up front;
   a mid-batch provider failure may still leave partial saves.
8. Read back each changed response independently, bypassing local caches. Use bounded
   read-only polling if persistence is asynchronous (max 3 reads / 5 seconds with backoff).
   Acknowledgment is not persistence. Do not retry a write after timeout, disconnect,
   429, 5xx, malformed response or interrupt; stop writing and reconcile via GET only.
9. Final fresh read verifies the same attempt is IN_PROGRESS, no submitted/completed
   timestamp, requested answers persisted, and untouched answers unchanged. Report any
   outside edit as a conflict, without overwriting it. If final state changed externally,
   report that observed status; never claim “left unsubmitted” or blame the command without evidence.
10. Release owned lock and print a bounded receipt. Cancellation between writes stops
    further writes; if readback cannot run, outcome is unknown for in-flight questions.
    No automatic rollback: it could overwrite later browser edits and is another mutation.

An initial read followed by a conditional write still cannot prevent the instructor
changing a deadline/state unless the provider validates editability atomically; discover
and test that server guarantee. If it cannot be established, apply remains blocked.

## Transport discovery gate and browser policy

Use an authorized disposable, untimed, non-final practice attempt, or sanitized traffic
supplied from an explicitly authorized student save. Never probe guessed write endpoints
against a real graded assignment. Do not submit the practice attempt for this work.

Record redacted method/path shape, minimal request schema, response/ack semantics,
independent GET encoding, conditional-write support, multi-answer/true-false/clear behavior,
permission/deadline enforcement, and request scope (one answer vs. whole attempt).
Inspect the normal UI's network behavior only in the authorized environment. No raw HAR,
cookies, student identity, actual course questions/answers or private URLs in the PR.

REST Fast-Path remains preferred. **No browser mutation fallback in v1**: autosave races,
implicit starts, and inability to enforce conditional writes would weaken the contract.
If REST saving is unavailable or schema/concurrency is unproven, return a useful unsupported
result and retain the local guide. Read-only fallback must not enter a route that creates
or resumes an attempt as a side effect. Review a future browser adapter separately.

## Results, persistence, and exit status

Result fields: `schema_version`, `mode`, `outcome`, target IDs, `observed_at`,
`attempt_status_before`, `attempt_status_after`, `submitted_at`, `submission_action_sent`
(always false), changed/unchanged counts and one record per requested question with
`saved_verified | unchanged | conflict | failed | unknown | not_attempted` plus reason.
`outcome` is `preview | saved_verified | unchanged | refused | partial | unknown`.
“saved_verified” requires all requested values verified and the final state guard passing.
Preview and refusal leave `attempt_status_after` null unless freshly read; never invent it.

Exit 0: successful preview, fully verified save, or unchanged. Exit 2: invalid local input
(argparse compatible). Exit 3: auth/access/eligibility refusal, conflict or unsupported
protocol before writes. Exit 4: partial/unknown result after any write was dispatched.
Do not treat a partial outcome as success even if some answers persisted.

Human success example: “Saved 3 answers; 2 unchanged. Blackboard reports in progress at
10:04 AM EDT. Nothing submitted by this command. Review in Blackboard.” Unknown/partial
output explicitly names affected questions and tells the student to inspect before retrying.
Use a trusted, constructed Blackboard review URL only if its route is verified read-only.

Sensitive exports/receipts go only to explicit user paths, atomic write, owner-only
permissions where supported, Windows ACL best effort documented, no clobber by default,
reject symlink destinations. Never persist cookies, CSRF tokens, passwords, raw user IDs,
provider bodies or debug response dumps. Do not add exports to git or school trackers
automatically. An explicit receipt need not repeat full question or answer text.

## Acceptance and edge-case matrix

Offline tests must observe all outgoing requests, assert zero submission/start requests,
and cover the boundary between validation, remote writes, and readback.

| Case | Required outcome |
| --- | --- |
| Export/preview/default inspection/legacy CLI | Zero remote mutations; existing output contracts unchanged |
| Unique eligible untimed attempt; single/multi/TF | Exact persisted selections, unchanged attempt state, fresh readback |
| No active attempt; multiple active attempts | Refuse; explicit eligible attempt ID resolves only the latter |
| Wrong account, cross-course ID, arbitrary origin/path, redirect | Refuse; no credential leakage or mutation |
| Timed/proctored/locked/LTI/unknown metadata | Refuse even with existing attempt or force-start flags |
| Submitted/graded/deleted attempt; instructor closes access | Refuse or stop partial batch; no replacement attempt |
| Due/past due/near cutoff, clock skew, deadline changes | Conservative refusal; mid-batch stop/reconcile |
| Presentation blocks, repeated visible numbers/prompts | Correct stable Question Attempt IDs only |
| Shuffled/removed choices, changed prompt/media/definition | Stale snapshot refusal; export again |
| Duplicate labels, duplicate/missing IDs, unknown answer type | No guess; unsupported/refused |
| False vs missing/null, plural/singular answer encodings | Verified explicit semantics; no wrong clear/select |
| Invalid choice, duplicate selection/change, wrong cardinality | Entire batch validation fails before writes |
| Omitted question, explicit clear, repeated apply/no-op | Omitted unchanged; clear explicit; verified no-op zero writes |
| Browser/other-host edit before or during apply | Baseline/conditional-write conflict; never overwrite |
| Local simultaneous CLI/stale lock/crash | Serialize or refuse; safe lock recovery, no duplicate writes |
| One answer saved, next fails/429/timeout/5xx | Stop; GET reconciliation; partial/unknown receipt; no write replay |
| Async save/ack without persistence/readback denied | Bounded polling; unknown rather than false success |
| Attempt auto-submits or status changes externally | Report observed state; never claim unsubmitted success |
| SIGINT/process termination/output disk full | Stop further writes; preserve truthful available receipt, no rollback |
| Malformed/duplicate-key JSON, version mismatch, expired/future snapshot | Input/refusal failure with zero writes |
| `--json`, stderr diagnostics, file overwrite/symlink | One JSON document; safe exports; no secrets |
| Provider lacks CAS or atomic editability checks | Apply blocked; no browser fallback that weakens safeguards |

Release also requires an explicitly authorized practice save proving persisted UI-visible
answers after reload, IN_PROGRESS state, unchanged attempt count and untouched answers,
and no start/submit calls in sanitized request evidence. Test each supported question type
and explicit clear. Offline mocks alone cannot establish the provider contract.

## Review decisions before implementation

- Confirm the proposed CLI/schema and initial restricted question-type scope.
- Confirm fail-closed conditional-write/editability requirements; inability to verify these
  is a blocker, not permission to weaken them during implementation.
- Confirm a permitted disposable practice target. No live mutation is authorized by
  this documentation PR or its merge. Today's graded assignment is not a test fixture.
- Review the implementation plan in [save-draft-answers-plan.md](save-draft-answers-plan.md).
