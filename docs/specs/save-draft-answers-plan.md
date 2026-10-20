# Implementation plan: save answers without submitting

Status: proposed. Companion [specification](save-draft-answers.md) is normative.
This PR adds no CLI behavior and performs no live Blackboard mutation.

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
