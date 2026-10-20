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

Grades use the course's visible official total where identified, otherwise a clearly labeled calculation from included graded points. Preserve that source label: a points calculation does not establish a weighted final grade. Status is shared across grades, due, briefing and assignments: `graded`, `needs_grading`, `submitted`, `not_submitted`, `exempt`, `unknown`. Unknown or inaccessible data is not proof of missing work or no grades. A discussion's raw `NEEDS_GRADING` is insufficient without the shared model's own-post check.

Recent announcements are not necessarily unread. REST read state is unknown unless verified. Do not infer publisher completion, attendance or credit from Blackboard metadata; a provider-specific request needs that provider's authoritative state. Assignment inspection identifies external tools without launching them.

`bb discover` replaces the configured course list. Use it for requested discovery/term changes, not merely to list enrollments. Derive courses and terms live instead of using fixed semester lists or example IDs.

## Session and login

Run the requested read directly; it already probes the session. Use `bb check` when session health itself is the question. Verify `/users/me` identity when choosing between accounts; a successful request alone does not identify the intended account.

- Active: continue.
- Expired: plain `bb login` first reuses the current session or attempts HTTPS refresh before browser SSO.
- Unknown/network/server failure: report unavailable and retry reasonably; do not treat it as expiry or trigger forced login.

Use `bb login --force` only when the user requests a clean reauthentication or concrete recovery requires clearing the saved sign-in. Browser login uses the installed Chromium-family OS default when supported, then installed alternatives with an explanation; bundled Chromium is optional. Do not install a browser to answer a routine read.

Duo methods depend on what the page offers (SMS, push or passcode). Let the existing login process collect the response; do not restart it just to deliver a late code. For an agent-mediated file handoff, start `bb login --code-file <private temporary path>` once, wait for `WAITING_FOR_CODE`, then deliver only the user's fresh code to that path. The login discards a pre-existing file and deletes a consumed code. Keep the same process alive; a new input flag cannot be attached to a process already running. If the user completes push approval, continue that flow. Use `bb login --manual` when manual browser completion is needed.

## Details when needed

Read the checkout's [CLI reference](../../../docs/CLI_REFERENCE.md) for command flags and output shapes; use `bb <command> --help` to confirm installed support. For scraper changes, read [coding standards](../../../docs/agents/coding-standards.md) and the relevant ADR. Do not preload architecture and login internals for routine reads.
