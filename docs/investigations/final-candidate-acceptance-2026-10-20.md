# Final candidate acceptance — 2026-10-20

Tested production-code commit: **`bd494336fef2d7b00deb4be7e765efd2bb1d1815`**.
Result: native Windows offline/browser checks, one fresh Mac SSO, one fresh Windows SSO,
and Windows read-command smoke passed. These are application acceptance observations,
not coursework completion or independent visual comparison against the LMS gradebook.

## Isolation and execution

- Mac evidence branch: `test/final-candidate-acceptance-2026-10-20`, own T3 worktree
  `t3-7cc551c5`; first update was `git merge --ff-only review/blackboard-candidate-2026-10-20`.
  Read AGENTS, coding standards, domain context, REST/attempt-safety ADRs, Blackboard skill,
  and the four preceding investigation notes dated 2026-10-20.
- Windows: existing owned `C:\Users\jason\bb-test-codex-20261020\repo`, native Python
  **3.13.15**, existing editable `.venv`; `win-lan` transport. Fetched a tracked-history-only
  bundle, then created **`test/final-bd49433-2026-10-20` directly at the candidate**.
  No reset or attempt to merge the divergent previous Windows test branch.
- Bundle SHA-256 matched both hosts:
  `579f49684179f72e694d1d693d90dfe006a363d79c516f0a1451c28b5eaacdcc`.
  Remote `git bundle verify` and exact HEAD verification passed.
- Mac config was copied from the protected installed checkout. The earlier application
  `.session` profile was copied into a real worktree-local directory, excluding locks;
  the known-valid candidate cookie cache was copied in and separately backed up privately.
  Original files were never symlinked, cleared, or used as the writable profile.
- Windows retained its isolated config/auth copies and privately backed them up before
  fresh SSO. Both isolated configs disabled Telegram; the auth harness also removed
  Telegram credentials from its process environment and disabled notification overrides.
- Harnesses stayed outside tracked source. No source fixes, browser downloads, academic
  writes, new attempts, notifications, bot/menubar runs, push, PR, or main update.

## Native Windows offline and browser acceptance

Reused the outside-repo `run_validation.py` invocation documented in
`windows-continuation-2026-10-20.md`, with UTF-8 mode and `PLAYWRIGHT_BROWSERS_PATH`
pointing to the absent owned `empty-playwright-cache` directory. One final full run:

- `python -X utf8 -m pytest tests -q -rs --durations=10`: **464 passed, 4 skipped,
  110 subtests passed in 54.19s**; child exit **0**, process elapsed **55.17s**.
- Skips: opt-in browser-smoke fixture; POSIX permissions; POSIX terminal polling;
  macOS/rumps menubar. Portable persistence, BOM code-file and browser fixtures ran.
- `python -X utf8 -m ruff check .`: **passed**, child exit **0**, **0.20s**.
  The wrapper does not aggregate child failures; both child exits were checked explicitly.
- Installed **Microsoft Edge 155.0.4283.45** passed sync headless, sync visible, and
  async headless launch, `about:blank` navigation, and close. **3.73s** total;
  temporary profiles removed. Exactly one stderr explanation across all three:
  `Using Microsoft Edge: your default browser (Firefox) can't run the Blackboard profile.`
- Downloaded-browser cache remained absent. Final remote tracked status was clean.

The parent separately reported Mac full-suite results at the same code commit:
**467 passed, 1 skipped, 112 subtests in 32.79s**, lint and skill validator clean.
That suite was not duplicated by this acceptance lane; it is parent-provided evidence.

## Live authentication: distinct evidence

All fresh auth was sequential and owned by this lane. The harness called the candidate's
`login_auto(headless=True, auto_exp=True, force=True, code_file=...)`, with a 112-second
cancellation timer and one-SMS/no-resend guards. No retries or extra sends occurred.
Raw login output stayed in memory; only aggregate events were emitted. `/users/me`
was checked after persistence against configured username aliases (`userName`, `studentId`,
or email), without emitting identifiers. Both fresh flows released their login locks.

- **Preserved cache, Mac:** initial copied cache had matching intended identity. Ordinary
  non-forced login returned true in **0.086s**, without changing that cache; subsequent
  identity probe took **0.058s**. This proves reuse, not fresh SSO or refresh.
- **Fresh post-fix Mac SSO:** one SMS requested at **5.671s**, Duo accepted at **19.311s**.
  Verification/persistence had completed by **21.675s**, when context close began on
  `blackboard.umbc.edu/ultra`. Login returned success at **46.964s**; persisted identity
  matched at **47.033s** (probe **0.069s**). Observed identity responses were HTTP **200**.
  Context shutdown added approximately **25.29s**: full login time is about **47s**, not
  22s. This is a performance limitation, with no speculative cause or source fix assigned.
- **Fresh Windows SSO:** one SMS requested at **9.566s**, `WAITING_FOR_CODE` at **9.702s**.
  Mac Messages ROWID/time was snapshotted before the remote process started; the listener
  accepted a new matching message only. Its code was bridged privately by SSH subprocess
  stdin into the already-running login's code file, never interpolated into argv/logs.
  Duo accepted at **13.812s**; verification/persistence completed by context close at
  **20.289s** on `blackboard.umbc.edu/ultra/`. Login success **20.672s**, persisted matching
  identity **20.860s** (probe **0.188s**). Observed probe responses included early **401**
  and later **200**; final authoritative state was valid. Remote process exit **0**.
- **HTTPS-only refresh, Mac:** after fresh login, a separate temporary auth copy called
  `refresh_session_http(timeout=8)` directly, without browser or another SSO/Duo send.
  It returned `refreshed` in **0.810s**; saved cookies changed and their subsequent identity
  probe was valid and matched. The temporary copy was removed. This is refresh acceptance,
  separate from preserved-cache reuse and fresh SSO; it does not prove a live verification
  response rotated cookies. That edge is covered by the candidate's offline regression.

Both consumed code files were absent afterward. A fresh successful post-fix run does not
establish the cause of the earlier historical timeout. Telegram, push, mobile-only input,
manual-login and actual interactive Windows console input were not newly live-tested.

## Windows REST read-command smoke

Immediately after fresh Windows login, `/users/me` again matched the configured alias.
The outside-repo runner executed the candidate through its existing project interpreter,
blocking every `playwright` import in each child before importing the CLI. **28 invocations
passed**, all exit **0**, nonempty output, no traceback, no Playwright import. All **13 JSON
variants parsed as exactly one JSON document**. No raw responses or personal grade values
were saved. Grades/summary and briefing exposed both official and calculated labels.

Process times below include child startup/import; paired values are text / JSON seconds:

- `--help`: **0.311**; `grades --summary`: **1.109**.
- `grades`: **1.119 / 0.914**; `due 7d`: **0.924 / 1.116**.
- `briefing`: **1.006 / 1.523**; `assignments IS410`: **1.096 / 1.025**.
- `announcements`: **0.973 / 0.598**; `activity`: **0.526 / 0.476**.
- `calendar`: **0.602 / 0.586**; `terms`: **0.563 / 0.627**.
- `discussions ENGL100`: **0.702 / 0.706**; `outline IS410`: **1.211 / 1.081**.
- `search Syllabus`: **2.717 / 2.605**; `courses`: **0.303 / 0.296**.
- `profile`: **0.550 / 0.534**.

Text discussions emitted the topics-only guidance; search emitted progress on stderr.
JSON stdout still parsed cleanly. Help slightly exceeded the 0.3s target, briefing JSON
slightly exceeded about 1.5s, and search exceeded it materially. Single observations are
not latency guarantees. Local configured `courses` output is not enrollment verification.
No independent LMS UI/arithmetic cross-check or publisher-credit validation is claimed.

## Preservation and delivery

Before/after SHA-256 comparison found **zero changed/missing files among 12,343 Mac
original files** (protected config, installed auth/profile, and the copied-from prior
application profile). On Windows, **all three original config/cookies/metadata hashes
matched**, including the protected synced checkout. Hash values and source file inventory
remain private; tracked evidence contains aggregate counts only. Isolated test auth changed
as intended and prior cache copies remain private for recovery. No secret values, account
identifiers, page HTML, or screenshots were emitted or added to this evidence commit.

Delivery is one local evidence-only commit on the Mac test branch. No production-source
failure was observed in this lane; performance and coverage limitations are retained above.
