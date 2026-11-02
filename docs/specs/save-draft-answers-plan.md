# Implementation plan: save answers without submitting

Status: original implementation plan retained below as history. The branch now
implements save, shorthand and guarded explicit retakes; see the current
[specification](save-draft-answers.md), [protocol](save-draft-answers-protocol.md),
ADR-0006 and ADR-0007. Original CAS/browser/no-start gates are superseded only as
those decisions specify. This extension ran no live start, apply or submit.

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
Creation route and body were observed live from the Ultra UI (2026-10-20, 201 Created);
`bb`'s own start has run offline only. See the [protocol](save-draft-answers-protocol.md).
Combined start/save preflights against readable prior questions before spending an
attempt; without them start separately, then preview/save. New question changes may
prevent saving after creation (exit 4, created ID retained). File/attempt pinning cannot
be combined with new-attempt. No new schedules or unattended starts are introduced.

Verified answer types: multiple answer (select all); single choice (multiple choice
with one answer, encoded by Ultra as `multipleanswer` with `answerSelectionLimit: 1`);
and true/false (`eitherOr` / `true_false`). Essay, short answer, fill-in-the-blank,
matching, ordering, calculated, file response, and anything else are unsupported:
**answer in Blackboard**. Type support does not establish live acceptance of every clear case.

## 1. Review and prove the protocol

- Review scope, schema, safety boundaries and failure semantics before coding.
- Read `AGENTS.md`, `CONTEXT.md`, coding standards, ADR-0002/0003/0005; work in an
  isolated checkout from current main. Keep the installed CLI and school workspace unchanged.
- Find an explicitly authorized disposable untimed practice target. Observe normal answer
  saving without starting a new attempt or submitting. Collect only synthetic/redacted fixtures.
- Establish exact endpoint/method/body, stable IDs, all three answer encodings, clear,
  revisions/conditional writes, server permission/deadline enforcement and independent GET.
- Deliver a short protocol note marking observed vs inferred behavior. If concurrent-update
  or atomic editability guarantees cannot be established, stop apply implementation; document
  the blocker. A local baseline comparison is insufficient proof of race protection.

Exit gate: reviewer can assess a proven save-only protocol. No guessed production writes.

## 2. Read model, schema and preview

- Add a structured answer projection separate from the existing text-oriented inspector.
  Retain raw provider identity/encoding internally; expose only the validated public schema.
- Proposed modules: `core/assessment_answers.py` for snapshot/change validation and pure
  comparisons; `scrapers/answers.py` for fresh provider reads and protocol adaptation.
  Keep general REST/session/target resolution helpers shared, not copied.
- Implement complete paginated identity/attempt/question reads and a canonical definition
  fingerprint; do not change existing `bb assignment --full` fields incompatibly.
- Add `bb answers` export/preview parser and dispatcher before the generic `target` assignment
  branch in `main.py` (that branch currently matches any nonempty target). Prevent dispatch
  fallthrough. Preserve help startup budget and lazy heavy imports.
- Publish JSON Schema, synthetic template, help and human diff rendering. Export changes=[];
  support explicit clear, sparse edits, strict file parsing and validation.
- Add offline parser/validation/preview tests first. Assert zero remote mutations for every
  read mode, malformed file and unsupported case, including legacy argument interception.

Exit gate: useful, side-effect-free export and preview; valid/invalid fixtures behave as spec.

## 3. Guarded save transport and coordinator

- Proposed `core/answer_save_transport.py`: narrow route/method/body allowlist using shared
  origin-scoped auth/CSRF handling. Do not broaden `core/rest.py::post_json` into an arbitrary
  user-controlled writer or reuse whole-attempt serialization.
- Add per-attempt local locking and bounded crash recovery; document cross-host/browser limits.
- Implement preflight of the entire edit set, fresh baseline checks, conditional writes,
  cutoffs and idempotent no-op detection. Never accept --force-start or start a timer.
- Isolate request states: not_dispatched, dispatched_unknown, acknowledged, readback_verified.
  On ambiguous failures, reconcile GET-only and stop. No write retry or automatic rollback.
- Verify exact persisted answers and final attempt state; report partial/conflict/unknown
  honestly with per-question outcomes. Handle process cancellation and output failures.
- Tests exercise real request construction at the transport seam with synthetic provider
  responses, including concurrent edits and partial saves. Assert no start/submit/completion
  route/field is reachable, even on failure and redirect paths.

Exit gate: full edge-case matrix passes offline; unsupported provider capabilities remain blocked.

## 4. Documentation and authorized acceptance

- Update README, `docs/CLI_REFERENCE.md`, `.agents/skills/blackboard-assistant/SKILL.md`
  and its CLI/API references, and `CONTEXT.md` only where new concepts require it.
- Add an ADR for explicit draft mutation, preserving inspection defaults. State that remote
  save authorization is required, course staff may see saved answers, and this feature never
  submits. School workflow adoption/schedules are a separate decision.
- Run focused offline tests, then `python -m pytest tests -q`, `ruff check .` and
  `git diff --check`. Check text/JSON/help contracts and supported Mac/Windows behavior.
  Report preexisting failures separately; do not edit unrelated code to make a green report.
- After explicit practice-target authorization, demonstrate one supported save per question
  type plus clear, inspect fresh GET and browser reload, unchanged attempt count/state and
  untouched answers. Save sanitized evidence; do not infer a grade or submission receipt.
- If live acceptance is unavailable, label implementation unverified and do not enable a
  production apply path that assumes an unproven protocol. Preview can be independently useful.
- Commit only task-owned files, push the implementation branch, create/link its PR, and
  hand back exact checks and unresolved blockers. Do not merge or replace the installed CLI
  merely because a documentation or implementation PR exists.

## Handoff to the next agent

Review this documentation PR first; Manny will arrange the reviewer and implementer.
Do not start implementation or delegation from this planning task. Use the spec's matrix
as acceptance criteria and retain this plan's discovery gate. Never use the student's
actual due-today assessment to experiment with answer-save payloads.
