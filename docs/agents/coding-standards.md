# Coding Standards

Rules every change to `bb` must follow. Reviewers (human or `/code-review`) check against this file.
Architecture decisions live in `docs/adr/`; vocabulary in `CONTEXT.md`. This file is how we write code.

## 1. Fetching data

- **REST Fast-Path first** (ADR-0002). Use Blackboard's REST endpoints with the cached session cookies.
  Open a browser only when REST is forbidden or missing for that data, and say so on stderr.
- **Never start a browser "just in case".** Commands whose data is all REST-reachable must not create an
  `AsyncSessionManager` or call `_launch_context` on the happy path. Create it lazily inside the fallback.
- **One request per course, not per item**, when an endpoint can return the whole set
  (e.g. `gradebook/users/{userId}` instead of `columns/{id}/grades` per column).
- **Courses run concurrently** (`asyncio.gather` / thread pool). Never loop over courses serially.
- **Errors are distinct.** 401 means "session expired, run `bb login`"; 403 means "not visible to students";
  404 means "not found". Never turn an auth failure into an empty result.
- A network error or 5xx is "unknown", not "expired". Never start an automatic login (which texts a Duo code)
  because of a network blip.

## 2. Browsers

- Use the **installed browser**, not a Playwright download: Chrome on macOS, Edge first on Windows, then
  whatever Chromium-family browser exists (Chrome, Brave, Edge). Bundled Chromium is an optional last resort,
  never required. The download costs too much disk.
- **One shared launcher** for sync and async paths. No module copies its own candidate loop.
- A profile-lock error (`ProcessSingleton`, "in use") stops immediately with "another `bb` or browser is using
  the profile". Don't retry it with the next browser; only a missing or broken binary falls through.
- Failed startups release what they created (driver, context).

## 3. Output

Output is the product. Every command must be useful at a glance and easy to parse.

- **Text output** is for a student reading a terminal:
  - grouped by course with a short course label (`IS 410`, not `IS410_1234_FA2026: ...`);
  - sorted (deadlines by due time, announcements newest first, grades by due date);
  - aligned columns, with a one-line summary per group (for example `94.3% A (official)`);
  - times in **local time with the zone shown** (`Tue Oct 21 11:59 PM EDT`), never raw UTC;
  - says what is missing or estimated (`calculated`, `3 not yet graded`, `partial: 1 course failed`);
  - no tracebacks, no debug lines, no `--` placeholders that mean "we didn't fetch it".
- **`--json`**: stdout is exactly one JSON document. Progress, warnings and fallback notices go to **stderr**.
  Dates are ISO-8601 with offset (`due_at`). Field names are stable and documented in `docs/CLI_REFERENCE.md`.
- `--md`, where supported, mirrors the text content.
- **Status comes from one model.** `grades`, `due`, `briefing`, `assignments`, Telegram and the menubar use the same
  Gradable Item status helper (graded / needs_grading / submitted / not_submitted / exempt / unknown).
  When we don't know, say `unknown`. Never default to a made-up status like `Unopened`.

## 4. Speed

- `bb --help` under 0.3 s. Heavy imports (`playwright`, `pypdf`) are lazy and live inside the function that needs them.
- A REST-only command across all current courses should finish in about 1.5 s on a warm session.
- No fixed `sleep`/`wait_for_timeout` when a condition can be awaited instead.

## 5. Safety (ADR-0003)

- Read-only by default. Never start an attempt, submit, post or change anything without the explicit flags.
- Never print or write cookies, tokens, passwords, Duo codes or student IDs.

## 6. Keep it small

- No new dependency without a clear need. No dead code, debug files, screenshots or scratch scripts in commits
  (put one-off live probes in `tools/dev/`).
- Reuse the shared helpers (REST GET, cookie header, course list, date formatting) instead of writing another.
- Prefer deleting code to adding flags.

## 7. Done means

- Offline tests for the changed behavior pass: `python -m pytest tests -q` (or `python -m unittest discover -s tests`).
  Tests mock HTTP and never need a live session.
- `ruff check .` passes.
- One **live read-only run** of each changed command (text and `--json`) has been checked by eye, with timing.
- `docs/CLI_REFERENCE.md` and `README.md` match the new behavior.
- The PR body lists what changed, how it was verified (with timings), and anything left undone.
