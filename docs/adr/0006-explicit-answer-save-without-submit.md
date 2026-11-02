# Explicit answer saving without submission

## Context

Students need to save chosen options in an existing Assessment Attempt, review them in
Blackboard, and submit themselves. ADR-0003/0005 keep inspection non-destructive;
answer saving is a separate, explicitly authorized remote mutation. Course staff may
see saved answers even before submission.

The [protocol observed on 2026-10-20](../specs/save-draft-answers-protocol.md) supports
per-question PATCH saves and independent readback. No conditional-write mechanism
was observed. The original [spec](../specs/save-draft-answers.md) required CAS and
prohibited browser mutations; the student explicitly authorized the weaker concurrency
guard and required HTTP-first browser fallback for this implementation.

## Decision

Add `bb answers` with read-only show/export/preview modes and an explicit
`--file FILE --apply` save mode. Calling agents must have the student's explicit save
request and always preview the exact changes first. CLI apply uses the flag as its
confirmation; it adds no mandatory stdin prompt.

- Save only supported single-choice, multiple-answer and true/false responses in the
  current user's existing, unambiguous `IN_PROGRESS` native Ultra test attempt.
- Never start (superseded for the explicit `--new-attempt` flag by ADR-0007), submit, finalize, grade or delete an attempt. Reject timed/proctored,
  password/secure-browser/IP-restricted, forced-completion and no-backtracking tests.
  Missing/unknown recognized safety fields or deadlines refuse; only explicit null
  means no due date. A missing time limit needs explicit no-timer fields. Recheck
  settings and the 120-second due window immediately before actual dispatch.
- Bind exports to the account/machine and exact target/attempt, expire snapshots after
  15 minutes, validate all edits before writing, and leave omitted questions alone.
- Allow only PATCH to the verified answer route with `questionType` and `givenAnswer`.
  Use HTTP first; only missing session/XSRF or HTTP 401 permits browser fallback.
  Send the same PATCH with same-origin `fetch()` using the installed browser first,
  then optional bundled Playwright through `core.browser` launch candidate order.
  HTTP 401 is an explicit rejection before persistence: the request was dispatched,
  but Blackboard did not save it. This is the sole post-dispatch fallback exception.
  Do not fall back after any HTTP answer persisted; 403/429/5xx, timeout, redirects,
  malformed replies and browser failures stop and reconcile by GET.
- Save sequentially, stop on conflict/failure, and independently read back requested
  answers and attempt status. Report partial/unknown outcomes; never auto-rollback.
  Do not auto-login or request Duo during a save.

## Consequences

This decision explicitly supersedes the original spec's CAS blocker and browser-write
ban for `bb answers`. Fresh baseline comparison immediately before each PATCH and a
local per-attempt lock reduce conflicts but **do not provide CAS**. Other browser/host
edits can race between GET and PATCH and be overwritten. No provider-enforced revision
guard is claimed; independent readback verifies observed persistence, not race prevention.

Existing inspection defaults and attempt-initiation guards remain intact. Saves are not
atomic across questions, and successful saves may remain after a later failure. The
command never submits; final observed status cannot guarantee future draft retention.
Other question types, CAS and behavior after the due date remain unverified. Offline
tests and implementation checks do not establish those provider guarantees.

## Review hardening

All command reads, including pagination and final readback, validate the configured
origin and block redirects before forwarding cookies. Cooperative Ctrl-C/cancellation
checks run immediately before actual HTTP/browser dispatch, including after browser
startup. Output destinations are validated before writes; a later receipt-file failure
preserves the real stdout result and save exit code. See the CLI reference for the
exit-2/3/4 contract and the explicit final-status footer. These changes retain the
existing human output except where the final status makes submission wording unsafe.

## Shorthand and explicit retakes

`--set` now creates the same internal ID-based changes list as file mode from a fresh
runtime baseline, without file-age expiry. Letters follow bb's provider-order list,
which may differ from Blackboard's shuffled display; no text matching is permitted.
No duplicate save pipeline is introduced. [ADR-0007](0007-explicit-untimed-attempt-creation.md)
supersedes this ADR's no-start rule only for explicit `--new-attempt`; never submitting
and the narrow answer-PATCH allowlist remain unchanged.

Verified answer types: multiple answer (select all); single choice (multiple choice
with one answer, encoded by Ultra as `multipleanswer` with `answerSelectionLimit: 1`);
and true/false (`eitherOr` / `true_false`). Essay, short answer, fill-in-the-blank,
matching, ordering, calculated, file response, and anything else are unsupported:
**answer in Blackboard**. Type support does not establish live acceptance of every clear case.
