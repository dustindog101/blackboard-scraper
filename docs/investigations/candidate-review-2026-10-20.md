# Blackboard candidate review — 2026-10-20

The review candidate combines `fix/cli-commands` (including `fix/status-model`),
`fix/browser-login`, and the coding-standards cleanup. Production-code acceptance
is against **bd49433**; subsequent parent changes are documentation only.
The installed checkout and `main` remain at **31d28c8** pending the owner's review.
No push, new PR, main merge, scheduled-job change or academic submission was made.

## Changes reviewed

- Real item scores, recognized official course totals or labeled graded-points
  calculations, shared completion statuses and local dates.
- Distinct assignment identities despite repeated titles; identity-safe calendar
  joins and Telegram urgent/overdue output without duplicate presentation.
- Explicit partial-data errors, clean JSON, privacy-safe default activity and
  discussions, lazy browser startup, concurrent REST requests and safe folder downloads.
- Shared installed-browser selection with compatible-default preference, profile
  lock handling and startup cleanup; HTTPS renewal before browser authentication.
- Fresh passcode sources, BOM-compatible code files, cancellation/retry handling,
  offered Duo methods, verification before cache persistence and meaningful exits.
- A shorter repo-owned skill aligned to the implementation. Installed/main skills
  are unchanged; see `skill-audit-2026-10-20.md` for the separate Claude workaround.

Independent Sol high reviewers reproduced grade, identity, partial-result and
login defects; corresponding regressions failed before their fixes. The final
bounded auth review found no remaining material blockers in the examined changes.
It did not claim fresh live acceptance or prove the cause of the historical timeout.

## Parent verification

- macOS Python 3.14.5, project environment: **467 passed, 1 skipped,
  112 subtests passed**, pytest in **32.79 s** at bd49433.
- Ruff, `git diff --check`, and the Blackboard skill validator passed.
- CLI import did not load Playwright. `--help`, `login --help` and `grades --help`
  completed in approximately **0.08 s** each in a warm observation.
- Read-only text/JSON acceptance with intended identity checked is recorded in
  `candidate-read-validation-2026-10-20.md`; the browser initializer was guarded
  to fail on the REST happy path. Search was about 2.5 s, above the approximate
  1.5 s target; other measured core reads were below that target.
- Earlier Windows and login evidence has exact tested refs and limits in
  `windows-continuation-2026-10-20.md` and
  `login-browser-continuation-2026-10-20.md`. Earlier refs are not final-code proof.

Final fresh Mac/Windows authentication and final Windows execution are recorded
separately by the acceptance continuation. Provider grades, external homework
credit, bot delivery and installed-main behavior are distinct from these code checks.
