# Explicit untimed attempt creation for bb answers

## Context

The student requested an explicit retake flag and HTTP → installed browser → optional
bundled Playwright transport order. Existing inspection and `bb assignment --start-attempt`
must keep their current behavior. A new attempt may consume an allowed attempt even
without submission, so creation must be explicit and never replayed after dispatch.

## Decision

Add `bb answers TARGET --new-attempt`, separate from the answer PATCH adapter.
It can show the newly created attempt or save `--set` changes with `--apply`.
Use apply's `_safe_due` check and conservative 120-second due window. Require a
complete current-user inventory and known positive attempts remaining. Ultra's
observed `deploymentSettings.attemptCount` maps to attempts allowed; accept an explicit
`attemptsAllowed` spelling too, refusing contradictions and unlimited/unknown sentinels.
If one IN_PROGRESS attempt exists, report and use it. Multiple active attempts refuse.
Serialize CLI creation with an assessment-scoped OS lock; recheck settings, identity,
count, deadline and cancellation immediately before dispatch, including after browser startup.
This local lock cannot serialize another host or a student's browser.

The creation allowlist is exactly POST to
`/learn/api/v1/courses/{courseId}/gradebook/columns/{columnId}/attempts` with the fixed
native-test IN_PROGRESS body observed live from the Ultra UI (201 Created) and documented in the protocol. HTTP is tried first. Missing session/XSRF
before dispatch allows the browser; all post-dispatch failures, including 401/404,
reconcile by GET only. Browser launch uses `core.browser` (installed candidates first,
optional bundled Playwright last). On the test's overview page click only the `Start attempt N` link;
never confirm a time-limit warning or access-code prompt. Intercept browser requests
and block every non-read request except one exact allowlisted start POST. Never submit.

After any dispatch, GET must establish exactly one new IN_PROGRESS attempt, owned by
the same user on the same course, with all prior IDs still present. Report its ID.
Bound reconciliation to three inventory reads with 1+2 seconds backoff; never replay
the start. Missing/contradictory results exit 4 with an unknown receipt. A lost HTTP
acknowledgment may be resolved by this independent readback, without another write.
Any subsequent answer-save failure also exits 4 and retains the creation receipt.

`--set` remains a translation to file mode's ID-based changes list. It uses the runtime
baseline without file-age expiry and shares validation, fingerprint, lock and readback
logic. Letters follow bb's list, which may differ from Blackboard's shuffled display;
no option-text matching is permitted. Preflight the whole shorthand against readable
prior questions before spending a retake; if unavailable, require a separate start,
then preview/save. Translate again against the newly read attempt; changed questions
may prevent saving after creation. `--file` and `--attempt` pin existing attempts and
cannot combine with new-attempt. `--export` destinations validate before creation.

Verified answer types: multiple answer (select all); single choice (multiple choice
with one answer, encoded as `multipleanswer` with `answerSelectionLimit: 1`); and
true/false (`eitherOr` / `true_false`). Essay, short answer, fill-in-the-blank, matching,
ordering, calculated, file response, and anything else are unsupported: **answer in
Blackboard**. This scope applies to both file and shorthand inputs.

## Consequences and evidence

This supersedes ADR-0006's no-start rule only for explicit `--new-attempt`. Answer
PATCH routes remain unable to create/submit; legacy assignment behavior is unchanged.
The route, body and `Start attempt N` link were observed live in a student-authorized retake;
`bb`'s own HTTP and browser start have run offline only. No submission was ever made.
Offline synthetic tests cover grammar, output, shared saves, eligibility, fallback,
route/body rejection, ambiguity, lost acknowledgments, cancellation and receipts.

The weaker baseline concurrency accepted in ADR-0006 remains. Another browser/host
can race a start; exactly-one-new GET verification detects some conflicts but cannot
prevent consuming a duplicate attempt. Never claim server-enforced idempotency or CAS.
