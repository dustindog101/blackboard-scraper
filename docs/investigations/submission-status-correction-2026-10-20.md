# Submission status correction — 2026-10-20

The owner reported that an assessment shown as submitted had only been started. The earlier
read-command acceptance verified execution and JSON parsing but did not independently verify
that submission claim. The defect was real; the earlier presentation overstated the evidence.

## Live reproduction and source

With `/learn/api/public/v1/users/me` matched to the configured account, fresh GETs returned:

- Aggregate public gradebook: `NeedsGrading`.
- The same student's internal column grade record: `NEEDS_GRADING`.
- The associated attempt list: `IN_PROGRESS`.

The old shared helper mapped the aggregate value directly to `needs_grading`, displayed it as
submitted, and put it in the completed set. It did not inspect attempt state. Another native
assessment had the same discrepancy. A separate pending-grade assignment instead had a
`NEEDS_GRADING` attempt and a submission receipt. This distinguishes a draft from an actual hand-in.

Read paths used, with identifiers omitted:

1. `/learn/api/public/v2/courses/{course}/gradebook/users/{user}`
2. `/learn/api/v1/courses/{course}/gradebook/columns/{column}/grades?userId={user}`
3. `/learn/api/v1/courses/{course}/gradebook/columns/{column}/grades/{grade}/attempts`

No answer/detail expansion, attempt creation, attempt update, submission or notification was
performed. The collaborative browser was logged out; this is live API verification, not an
independent visual LMS-page comparison. No fresh authentication was needed.

## Repair

- Verify native assessment/assignment attempt metadata, with user/course/column/grade checks.
  Aggregate `NeedsGrading`, `ATTEMPTED`, or the existence of a saved attempt is not hand-in proof.
- Keep active work `in_progress` and open in grades, due dates, briefing, and assignment lists.
  Failed verification is `unknown`; explicit auth failure remains an auth failure.
- Do not depend on attempt array order. Open attempts/retakes cannot be hidden by older final
  attempts or posted grades. Preserve posted points separately for grade totals/counts. An
  instructor-posted grade with no online attempt is not called missing: offline work can be graded.
- Discussion checks count only the student's published posts. Saved draft text/files are not
  rendered as a submission when the attempt remains in progress.
- Remove check marks/emoji from shared status wording and grades diagnostics. Human grade text,
  summary and Markdown show earned / possible points without percentages or letters. Stop
  reconstructing letter boundaries; JSON keeps the compatibility key but never invents a letter.
- An official percent-only total is not relabeled as points out of 100. Its point total is unavailable.
- Update the runbook, coding standards and historical acceptance notes accordingly.

## Verification

- A regression matching the live discrepancy failed against the previous code, then passed
  after the repair. It does not fetch answers or create an attempt.
- Full macOS suite: **487 passed, 1 skipped, 117 subtests passed in 34.48s**. The skip is the
  opt-in real-browser fixture. Ruff, `git diff --check`, and the skill validator passed.
- Independent Sol high review reproduced two additional multi-attempt edge cases. Both were
  repaired with regressions; re-review found no blocker in the examined paths.
- Live text and JSON reads passed for `grades ENGL100`, `grades IS410`, `grades --summary`,
  `due 7d`, `due overdue`, `briefing`, and a filtered ENGL100 assignment list. JSON parsed, the
  reported assessment was `in_progress`, and its deadline's `completed` was false. Both native
  in-progress assessments remained open; the actually handed-in assignment stayed awaiting grade.
- During command validation all Playwright imports, authentication recovery, and non-GET
  requests were blocked. Observed JSON process times were about **0.47–1.16 seconds**; these
  are observations, not latency guarantees.
- No new Windows execution is claimed for this repair. Earlier native Windows/auth acceptance
  remains evidence for its stated historical code commit only. Main and installed CLI remain
  unchanged pending review; this repair is confined to the local candidate.

No raw provider responses, account identifiers, credentials, cookies, codes, screenshots or
personal grade values are included in this note.
