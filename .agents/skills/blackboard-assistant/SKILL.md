---
name: blackboard-assistant
description: Read UMBC Blackboard through the bb CLI, inspect course work and grades, or recover its login. Use for Blackboard requests; use school ops skills only for an explicitly requested job or tracker update.
---

# Blackboard Assistant

Use this skill with the checkout that supplies the executable. Global `bb` can point to a different checkout: inspect its launcher before relying on worktree-only features. In a selected checkout, run `main.py <command>` with its project interpreter (`.venv/bin/python` when available).

Read requests stay read-only. Do not start attempts, submit, send notifications, update trackers, download files, or change course configuration unless the request calls for that action. Existing user authorization applies. Keep credentials, cookies, Duo codes and personal identifiers out of output and durable notes.

## Choose the read

| Request | Command |
| --- | --- |
| What needs doing this week? | `bb due 7d --exclude-completed` |
| Anything overdue? | `bb due overdue` |
| Daily overview | `bb briefing` |
| Grades | `bb grades` or `bb grades <COURSE>` |
| Recent instructor posts | `bb announcements --since 7d` |
| Verified unread posts | `bb announcements --unread` (requires browser verification) |
| Find a gradable item | `bb assignments <COURSE> --filter "<title terms>"` |
| Instructions / rubric | `bb assignment "<exact title>" -c <COURSE>`; add `--rubric` for visible rubric |
| Existing submission / answers | The same assignment command with `--full` |
| Course material | `bb outline <COURSE>`, `bb search "<terms>" -c <COURSE>` |
| Configured / enrolled courses | `bb courses` / `bb terms` |

Text output is suitable for direct answers. Use `--json` for parsing; stdout should contain one JSON document and diagnostics go to stderr. Dates use `due_at` in structured output; present local time with its zone.

Grades display points without inferred letters. Preserve the source label: a points calculation does not establish a weighted final grade, and an official percent-only total is not a point score. Status is shared across grades, due, briefing and assignments: `graded`, `needs_grading`, `submitted`, `not_submitted`, `in_progress`, `exempt`, `unknown`. A gradebook `NEEDS_GRADING` or existing attempt does not prove submission: verify native assignment/assessment attempt state; discussions require the student's own published posts. An active attempt is in progress, not submitted. Failed verification is unknown and must remain open. Unknown or inaccessible data is not proof of missing work or no grades.

Recent announcements are not necessarily unread. REST read state is unknown unless verified. Do not infer publisher completion, attendance or credit from Blackboard metadata; a provider-specific request needs that provider's authoritative state. Assignment inspection identifies external tools without launching them.

`bb discover` replaces the configured course list. Use it for requested discovery/term changes, not merely to list enrollments. Derive courses and terms live instead of using fixed semester lists or example IDs.

## Save chosen answers (explicit request only)

Use `bb answers "<test>" -c <COURSE>` to show an existing attempt. Preview with
repeatable `--set 1=A --set 2=A,B --set 3=False` (or `--export`, edit `changes[]`,
then `--file`). **Always preview chosen answers before saving.** Only the student's
explicit save request authorizes `--apply`. `N=clear` clears; True/False/T/F and
letters are case-insensitive. Letters follow bb's list, which may differ from
Blackboard's shuffled display. Never match by option text. Shorthand shares the
file pipeline and uses a fresh runtime baseline without file-age expiry.

Only an explicit request to start authorizes `--new-attempt`; show/preview/apply
alone never start. It starts an eligible untimed test then shows it, or combines
with `--set ... --apply` to start and save. Reuse an existing IN_PROGRESS attempt.
Unknown safety/count/deadline settings, restrictions, no attempts remaining or less
than 120 seconds until due refuse. Combined start/save requires readable prior
questions for preflight; otherwise start separately, then preview/save. The creation
request was observed live from the Ultra UI (bb's own start offline-tested; uses up one attempt). HTTP → installed browser → optional bundled
Playwright; fallback is allowed only before a start dispatch. Never retry a dispatched
start, including HTTP 401/404. GET must confirm exactly one new IN_PROGRESS attempt
and its ID. Time-limit/access-code prompts refuse. Never submit; the student reviews
and submits. Saved answers may be visible to course staff.

Verified answer types: multiple answer (select all); single choice (multiple choice
with one answer, encoded by Ultra as `multipleanswer` with `answerSelectionLimit: 1`);
and true/false (`eitherOr` / `true_false`). Essay, short answer, fill-in-the-blank,
matching, ordering, calculated, file response, and anything else are unsupported:
**answer in Blackboard**. Type support does not establish live acceptance of every clear case.

Baseline re-reads are not CAS; avoid concurrent browser/other-host editing. Each
PATCH reconciles before the next, then a final snapshot checks every requested
answer under an OS lock. Safety uses the same `_safe_due` check for start and save,
including the observed no-timeLimit/CONTINUAL Ultra shape; unknown enums refuse.
HTTP answer-save 401 may fall back before persistence; **attempt creation has no
post-dispatch fallback**. Exit 3 refuses before dispatch; exit 4 reports a failed or
unknown run after a start or save dispatch. Preserve `new_attempt` receipts and
inspect Blackboard before retrying. Ctrl-C stops new writes and reconciles.
See the CLI and API references for the maintained contract.

## Session and login

Run the requested read directly; it already probes the session. Use `bb check` when session health itself is the question. Verify `/users/me` identity when choosing between accounts; a successful request alone does not identify the intended account.

- Active: continue.
- Expired: plain `bb login` first reuses the current session or attempts HTTPS refresh before browser SSO.
- Unknown/network/server failure: report unavailable and retry reasonably; do not treat it as expiry or trigger forced login.

Use `bb login --force` only when the user requests a clean reauthentication or concrete recovery requires clearing the saved sign-in. Browser login uses the installed Chromium-family OS default when supported, then installed alternatives with an explanation; bundled Chromium is optional. Do not install a browser to answer a routine read.

Duo methods depend on what the page offers (SMS, push or passcode). Let the existing login process collect the response; do not restart it just to deliver a late code. For an agent-mediated file handoff, start `bb login --code-file <private temporary path>` once, wait for `WAITING_FOR_CODE`, then deliver only the user's fresh code to that path. The login discards a pre-existing file and deletes a consumed code. Keep the same process alive; a new input flag cannot be attached to a process already running. If the user completes push approval, continue that flow. Use `bb login --manual` when manual browser completion is needed.

## Details when needed

Read the checkout's [CLI reference](../../../docs/CLI_REFERENCE.md) for command flags and output shapes; use `bb <command> --help` to confirm installed support. For scraper changes, read [coding standards](../../../docs/agents/coding-standards.md) and the relevant ADR. Do not preload architecture and login internals for routine reads.
