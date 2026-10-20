# Blackboard Scraper v2 — Complete CLI Reference

This document provides a comprehensive breakdown of all commands, options, and workflows supported by the Blackboard Scraper CLI.

Commands can be invoked globally via `bb`, `blackboard`, `bbscraper`, or directly via `python3 main.py <command>`.

All modern subcommands support natural syntax (e.g. `bb briefing`, `bb due 7d`, `bb grades IS410`). Legacy flags (`bb --briefing`, `bb --auto-exp`, `bb --due 7d`) are 100% backward compatible and transparently routed via the built-in legacy flag interceptor.

---

## 📑 Command Categories

- [🆘 Getting Help (`bb help`, `bb --help`)](#-getting-help)
- [🔐 Authentication & Sessions (`bb login`, `bb logout`, `bb session`)](#-authentication--sessions)
- [🧭 Course Discovery & Term Management (`bb discover`, `bb courses`, `bb terms`)](#-course-discovery--term-management)
- [📚 Academic Scrapers (`bb briefing`, `bb due`, `bb grades`, etc.)](#-academic-scrapers)
- [🔀 Course Selection Syntax](#-course-selection-syntax)
- [📦 Output Formats & File Export](#-output-formats--file-export)
- [⚡ Concurrency & Browser Controls](#-concurrency--browser-controls)
- [🤖 Telegram Bot Daemon (`bb bot`)](#-telegram-bot-daemon)
- [🖥️ Native macOS Menubar App (`bb menubar`)](#️-native-macos-menubar-app)
- [📖 Built-in Help Guides (`bb guide`)](#-built-in-help-guides)
- [🔄 Legacy Flags Backward Compatibility](#-legacy-flags-backward-compatibility)

---

## 🆘 Getting Help

Anything is learnable in 1–2 commands. Start at the top, then drill into one command:

```bash
bb --help              # All commands + 5 common examples
bb help                # Same concise overview
bb help <COMMAND>      # Help for one command (aliases work: bb help brief)
bb <COMMAND> --help    # Same thing (e.g. bb outline --help)
bb <COMMAND> help      # Same thing (e.g. bb outline help)
bb guide <TOPIC>       # Topic manuals: auth, courses, schema, telegram, concurrency
bb --version           # Print CLI version (also -V)
```

Every `bb <COMMAND> --help` page ends with copy-pasteable `Examples:`. Unknown commands fail fast with a suggestion (`bb outlin` → `Did you mean 'bb outline'?`).

---

## 🔐 Authentication & Sessions

### `bb login`
**Description:** Checks the saved session, tries HTTPS renewal when possible, then uses automated SSO and the verification methods offered by Duo. macOS Messages can supply a fresh SMS code when available.
- Mode syntax: accepts both flags and positionals:
  - `bb login` (Smart automated default on macOS)
  - `bb login auto` or `bb login --auto`
  - `bb login manual` or `bb login --manual` (Opens visible browser window for Duo push or Touch ID)
- Options:
  - `--force`: Force a re-login even if the active session is valid.
  - `--visible`, `-v`: Display visible browser window.
  - `--username`, `-u <id>`: UMBC username / email (reads from `config.json` if omitted).
  - `--password`, `-p <pass>`: UMBC password.
  - `--duo-passcode <code>`: Supply a 6–8 digit passcode directly via CLI.

An existing UMBC SSO session can skip the username/password form and Duo.
Automatic login observes those transitions under a 30-second SSO deadline,
after initial navigation (also limited to 30 seconds). It submits each credential
step once and saves cookies only after an HTTP identity check succeeds. Unknown
pages, authentication errors, and missing forms fail with recovery guidance;
`bb login --manual` remains the visible-browser fallback. Failed automatic login
returns status 1. Automatic refresh uses the same login flow and rechecks the
saved session before continuing the read command.

Authentication diagnostics omit page text, full URLs, credential values, and
passcodes. Automatic login does not save screenshots of authentication pages.

`--code-file <path>` is a live input channel: start login with an empty or absent file,
wait for `WAITING_FOR_CODE`, then provide the newly received code in that file while
login is running. It is consumed after reading; do not place old codes there before
starting. SMS, configured Telegram, terminal and file input can race; the first valid
code wins. Available Duo methods are detected; accounts without SMS can use a push
or passcode. `--manual` remains available when automation cannot use the offered method.

A confirmed expired session first tries an HTTPS-only UMBC session refresh. A network
failure is reported separately and must not trigger a new Duo login. The installed
Chromium-family default browser is preferred; Firefox/Safari defaults fall back to an
installed compatible browser with a short explanation. No browser download is required.

### `bb check` / `bb session check`
**Description:** HTTP identity probe without a browser. Returns exit code 0 if authenticated and 1 if expired or unavailable; output distinguishes expiry from network/server uncertainty.

### `bb session info`
**Description:** Displays session creation timestamp and last-used timestamp.

### `bb session stats` / `bb session telemetry`
**Description:** Displays deep session telemetry, total lifespan analytics, rolling averages, and auto-refresh timing.

### `bb session export <FILE>` / `bb session import <FILE>`
**Description:** Move saved Blackboard cookies between computers as plain UTF-8 JSON.

```bash
bb session export bb-session.json
bb session export bb-session.json --overwrite
bb session import bb-session.json
```

Export works offline and does not alter the source session. `--overwrite` permits
replacing an existing export file. Import tests the candidate account over HTTP
and in the tool's managed browser before atomically replacing saved cookies; it
never launches a login/OTP flow. Validation or write failures preserve prior
cookies. Stop background jobs and close managed browser windows before importing.
An external `--cdp` browser is not supported for transfers. Missing/malformed
files and unsuccessful imports exit with status 1; invalid CLI usage exits with
status 2. Keep the file private because it contains login cookies. If a copied
session expires, log in on Mac and export again. Course configuration is separate;
run `bb discover` on a fresh installation when needed.

### `bb logout`
**Description:** Clears cached cookies, session metadata, and saved authentication tokens.

---

## 🧭 Course Discovery & Term Management

### `bb discover`
**Description:** Finds the courses of the active term (the term whose date range contains today) and **replaces the course list in `config.json`**; it says so on stderr. Use `bb terms` to look without saving. If the course API is unavailable it falls back to the browser.
- `--json`: Emits `{term, saved, courses: [{id, name}]}`.
- `--term <TERM>`: Target a specific term (e.g. `--term FA2026`, `--term SP2026`, or `--term all`).

### `bb terms`
**Description:** Lists every enrolled term and its courses (active term first). Read-only: never touches `config.json`.
- `--json` / `--out FILE`: a JSON array of `{term, active, courses: [{id, name}]}`.

### `bb courses`
**Description:** Prints the current active courses configured in `config.json`.
- `--json`: Emits `{courses: [{course_id, course_name}]}`.

---

## 📚 Academic Scrapers

### `bb briefing`
**Description:** REST-only morning summary (about 1 s, no browser on the happy path): overdue work, work due today/tomorrow, later this week, a grade line per course (official or calculated), and announcements from the last 7 days. Built from the same Gradable Item status model as `grades`, `due` and `assignments`.
- `--json`: Emits the v2 composite document. Each course has `overall` (same shape as `grades --json`) and `grades` (items with `due_at`, `status`, `score`, `possible`, `percent`); `global.overdue` and `global.urgent` hold the deadline rows. Human-readable grade lines show earned / possible points and source labels.
- `--out <file>`: Exports JSON directly to specified filepath.
- `--telegram`: Dispatches formatted summary card to configured Telegram chat.
- `--md`, `--save`: Saves Markdown report to `output/briefing.md`.
- An expired session prints `Session expired, run `bb login`.` and exits 1.

### `bb due [WINDOW]`
**Description:** Deadlines from each course's gradebook (the status model) reconciled with Blackboard's calendar. Distinct gradebook items retain their column/content identities; calendar joins prefer source IDs and use titles only when unambiguous within a course. Deadlines are grouped by day, in local time with the zone (`Tue Oct 21 11:59 PM EDT`).
- `WINDOW`: `7d` (default), `14d`, `30d`, any `Nd`, `overdue`, `all`.
  - `overdue`: dated, past and not completed. An assessment with a verified `IN_PROGRESS` attempt is still open, even if the public gradebook says Needs Grading.
  - `all`: everything, plus `No due date` and `Already done` groups. Undated items appear only here.
- `--exclude-completed`: Hides completed items.
- `--json`: List of `{item_id, title, course_id, course, due_at (ISO-8601 UTC), status, completed, score, possible, percent, category, source}`.
- Legacy: `bb --due 7d`, `bb --upcoming 10` (maps to `bb due 10d`).

### `bb calendar [COURSE]`
**Description:** REST-first global calendar events with localized dates. Automatically falls back to Playwright browser if REST is blocked.
- `COURSE`: Positional course code or keyword (e.g. `bb calendar IS410`).
- `-c <COURSE>`: Named course flag (e.g. `-c IS410`).
- `--json`: Emits raw JSON event items.

### `bb announcements [COURSE]`
**Description:** Announcements for all current courses (or one), **newest first**, grouped by course with local time, age and a clipped body preview. REST only (about 0.4 s for all courses); posts from before the course's term start are marked `(older term)`.
- `COURSE` / `-c <COURSE>`: limit to one course. Omitted means all current courses.
- `--since 14d|2w|2026-09-01`, `--limit N` (per course): applied after sorting.
- `--unread`: only posts verified unread. The REST API has no read state, so this opens the browser (read-only) and joins by title; unknown state is excluded, with a warning on stderr.
- `--json`: `[{course_id, course_name, announcements: [{title, created, modified, body, unread, older_term}], error?}]`. `unread` is `true`, `false` or `null` (unknown); it is `null` unless `--unread` verified it.

### `bb grades [COURSE]`
**Description:** Per course: the overall grade, then every Gradable Item sorted by due date with earned / possible points, status and due time (local, with zone). Human-readable text and Markdown omit percentages and letters. Courses run in parallel with two bulk gradebook reads per course; targeted read-only attempt metadata checks verify native assessment states, including active retakes behind posted grades.
- Overall is labeled `official` when the course has a visible calculated total (`Overall Grade`, `Total Points Earned`), otherwise `calculated` from graded, non-exempt, included item points. An official percentage without actual point totals is labeled `point total unavailable`; it is never presented as earned points out of 100. `N of M graded` counts items that count toward the total.
- Statuses: `graded`, `needs_grading`, `submitted`, `in_progress`, `not_submitted`, `exempt`, `unknown`. `in_progress` displays as `in progress`. Submission requires the student's completed attempt or post; public Needs Grading alone does not prove submission.
- A posted score remains a real grade even if an active retake is open; it still contributes to graded-point calculations. A posted grade with no online attempt is not called missing, because work can be graded outside an online attempt. Failed attempt verification stays unknown and open.
- `COURSE`: Positional course code or keyword (e.g. `bb grades IS410`).
- `-c <COURSE>`: Named course flag.
- `--all`: All courses.
- `--json`: `[{course_id, course_name, overall:{source,label,score,possible,percent,letter,graded,total}, items:[{name,category,due_at,status,score,possible,percent,feedback}], excluded:[{name,reason}]}]`. The `percent` and `letter` keys remain for compatibility; inferred letter grades are null.
- An expired session prints `Session expired, run `bb login`.` and exits 1.

### `bb outline [COURSE]`
**Description:** Traverses course outline hierarchy, learning modules, syllabi, documents, and attachments.
- `COURSE`: Positional course code (e.g. `bb outline IS410`).
- `-c <COURSE>`: Named course flag.
- `--folder`, `-f <query>`: Selectively expand and display specific folder/module by name or ID.
- `--expand-all`, `--deep`: Recursively expand all folders into a complete tree view.
- `--depth <N>`: Limit tree expansion to `<N>` depth levels.
- `--interactive`, `-i`: Interactive terminal menu to browse folders on demand.
- `--type <type>`: Filter items (`syllabus`, `assignment`, `document`, `folder`, `link`, `file`).
- `--filter <text>`: Keyword search filter.
- `--json`: Emits clean, streamlined JSON outline.

### `bb assignments [COURSE]`
List gradable items for discovery. With no course it lists all current courses (fetched concurrently); text output is one table per course sorted by due time in local time with zone, and JSON adds `due_at` (ISO-8601). Use `--filter <text>` to narrow the list, then `bb assignment` to inspect one. Supports `-c`, `--all` and `--json`.

### `bb assignment <TARGET> [-c COURSE]`
Aliases: `quiz`, `asmt`, `assessment`. `TARGET` accepts a title, content ID or Blackboard URL. Exact matches win; ambiguous title matches return candidates.

The default shows instructions, brief assignment details, and a one-line rubric/work availability hint. It does not fetch previous question/answer payloads or discussion posts. Human-readable output is the default; JSON and Markdown honor the same selected view.

| Mode | Output |
| --- | --- |
| Default | Instructions, due date/points/status, relevant test settings, and availability hints |
| `--instructions` | Instructions plus a rubric availability hint; no previous work or grade summary |
| `--rubric` | Only the rubric visible before starting; no instructions or previous work |
| `--full` | Instructions, visible rubric, accessible existing questions/answers or your discussion posts, and submission file/receipt details |

The three flags are mutually exclusive. Existing scripts reading `questions` or `submissions` should add `--full`. Internal Python inspector functions retain full-detail defaults for compatibility.

Native rubric previews require `displayBefore: true`; hidden rubric definitions are omitted even with `--full`. A failed availability check is reported as unknown. Separately linked rubric documents follow their own access rules. Explicit prompt/rubric links are discovered only in an assignment-matching folder, not by scanning unrelated readings. Supported Google Docs, Blackboard PDFs and Word instruction documents can be read directly; other links remain links. Default instruction text is limited to 12,000 characters with an explicit truncation notice; focused/full views allow up to 60,000 per document. Files larger than 8 MiB are left as links. The default reads at most two instruction documents and explicitly lists any additional documents; `--instructions`, `--rubric`, and `--full` read all identified documents within the text/size limits. Missing rubric definitions are reported explicitly. No external learning tool is launched, and submission files are listed rather than downloaded automatically.

`--full` never starts an attempt. `--start-attempt` still uses the existing guards and implies full output; it cannot be combined with `--instructions` or `--rubric`. Timed assessments still require `--force-start`.

- `-c <COURSE>` scopes discovery; omitted searches configured courses.
- `--json`, `--raw`, `--out`, and `--md` all respect the selected view.
- Native submission assignments, tests, discussions, external-tool links and grade-only entries are identified separately. External provider visibility/submission state is not inferred from missing Blackboard attempt data.
- `--start-attempt` (`--begin-attempt`) permits starting a new attempt; `--force-start` additionally confirms a timed assessment.

### `bb answers <TARGET> [-c COURSE]`
Show, export, preview or explicitly save chosen answers in an existing `IN_PROGRESS`
native Ultra test attempt. Explicit `--new-attempt` can start an eligible untimed attempt.
Never submits. Saving or starting is a remote
mutation; agents need the student's explicit save request and must always preview first.
Saved answers may be visible to course staff.

```bash
bb answers "Practice Quiz" -c DEMO100                         # show saved answers
bb answers "Practice Quiz" -c DEMO100 --export answers.json   # export; no remote changes
# Edit only changes[] in answers.json, then preview:
bb answers "Practice Quiz" -c DEMO100 --file answers.json
bb answers "Practice Quiz" -c DEMO100 --file answers.json --apply
bb answers "Practice Quiz" -c DEMO100 --set 1=A --set 2=A,B --set 3=False  # preview
bb answers "Practice Quiz" -c DEMO100 --set 1=A --apply                  # save
bb answers "Practice Quiz" -c DEMO100 --new-attempt                     # start then show
bb answers "Practice Quiz" -c DEMO100 --new-attempt --set 1=A --apply    # retake and save
```

- `TARGET`: test title/substring, content ID or gradebook column ID; ambiguity refuses.
  Use `-c COURSE` to scope discovery. There is no bulk apply.
- `--attempt ID`: select among multiple eligible active attempts. Export/file mode pins
  the exact attempt; the command never silently substitutes another one.
- `--export FILE`: write a fresh template with `changes: []`; expires after 15 minutes.
- `--file FILE`: validate and preview the before/after changes without saving.
  Mutually exclusive with `--export` and `--set`; `--apply` requires `--file` or `--set`.
- `--set N=A,B`: repeatable shorthand using the answerable question number shown by bb
  (presentation blocks do not count). Letters are case-insensitive and follow bb's list,
  which may differ from Blackboard's shuffled display. True/false accepts True/False/T/F
  as well as letters. `N=clear` clears. Never matches by option text. Duplicate numbers,
  unknown letters, unsupported/absent numbers and excessive selections exit 2 before writes.
  It translates to the file mode's internal changes list and shares validation, fingerprints,
  baseline comparisons, locking, per-question readback and receipts. Its baseline is read at
  runtime; no 15-minute file expiry applies. Without `--apply`, it previews with zero writes
  unless the separate explicit `--new-attempt` flag is also present.
- `--new-attempt`: run the same `_safe_due` check as apply; require a complete known
  attempt inventory and positive attempts remaining (`attemptsAllowed` minus existing count;
  Ultra's observed `attemptCount` is the equivalent provider field). Unknown, contradictory,
  unlimited sentinel or exhausted counts refuse. The 120-second due window also applies.
  If one IN_PROGRESS attempt exists, report and use it without starting another; multiple
  active attempts refuse. A local assessment lock serializes CLI starts on this machine.
  HTTP first, then the installed browser, then optional bundled Playwright via `core.browser`.
  Only missing session/XSRF **before dispatch** permits browser fallback. After any start
  dispatch (including HTTP 401/404), never retry or switch transports: reconcile by GET only.
  Browser mode clicks exactly `Start new attempt` / `Start attempt`, refuses timer/access-code
  prompts and blocks every non-read request except the single allowlisted creation POST.
  GET must confirm exactly one new IN_PROGRESS attempt and its ID. Ambiguity exits 4.
  Both HTTP and browser creation are **UNVERIFIED live**; see [protocol](specs/save-draft-answers-protocol.md).
  With `--set`, all edits are preflighted against a readable prior attempt before creation,
  then translated again against the new runtime question list. If no prior question list
  is available, start separately, then preview/save. A changed new question definition may
  prevent saving after creation; receipt/exit 4 preserves the created attempt ID.
  Cannot combine with `--file` (pinned to an existing attempt) or `--attempt`.
- `--apply`: save requested changes sequentially and independently read them back.
  No mandatory stdin prompt; no automatic submission or rollback.
- `--json`: one versioned JSON document on stdout; progress/warnings go to stderr.
  `--compact` minifies JSON. Results include `outcome` and `submitted: false`, meaning
  this command sent no submission action, not a guarantee against external submission.
- `--out FILE`: also write the JSON result (an export result is a receipt, not its template).
  Keep input/template and result paths distinct. Output files use atomic replacement
  and owner-only permissions where supported; symlink destinations are refused.
- `--overwrite`: permit replacing an existing `--export` or `--out` file. It does not
  override conflicts, expiry, eligibility or deadline checks.

The file schema is version 1: `target` binds the site/course/assessment/attempt and
local account; `snapshot` holds timestamps, a definition fingerprint and baseline
questions; `unsupported` lists unsupported blocks; `changes[]` requests sparse edits.
Synthetic shape below: use a fresh export's actual IDs, binding, hash and timestamps;
do not copy the placeholder metadata into a live file.

```json
{
  "schema_version": 1,
  "target": {
    "origin": "https://blackboard.example.edu",
    "course_id": "_100_1",
    "assessment_id": "_200_1",
    "attempt_id": "_300_1",
    "account_binding": "example-only-local-digest"
  },
  "snapshot": {
    "observed_at": "2026-10-20T10:00:00-04:00",
    "expires_at": "2026-10-20T10:15:00-04:00",
    "definition_sha256": "example-only-definition-hash",
    "questions": [
      {
        "number": 1, "question_attempt_id": "_400_1", "question_id": "_500_1",
        "type": "single_choice", "prompt": "Choose a resource.",
        "points": 1, "max_selections": 1,
        "choices": [
          {"id": "new_00000000-0000-0000-0000-000000000001", "text": "Books"},
          {"id": "new_00000000-0000-0000-0000-000000000002", "text": "Articles"}
        ],
        "selected_choice_ids": []
      },
      {
        "number": 2, "question_attempt_id": "_401_1", "question_id": "_501_1",
        "type": "true_false", "prompt": "A catalog lists resources.",
        "points": 1, "max_selections": 1,
        "choices": [{"id": "true", "text": "True"}, {"id": "false", "text": "False"}],
        "selected_choice_ids": ["true"]
      }
    ]
  },
  "unsupported": [],
  "changes": [
    {
      "question_attempt_id": "_400_1",
      "operation": "set",
      "selected_choice_ids": ["new_00000000-0000-0000-0000-000000000002"]
    },
    {"question_attempt_id": "_401_1", "operation": "clear", "selected_choice_ids": []}
  ]
}
```

Edit only `changes[]`. `set` replaces the whole selected set for one question; use
stable choice IDs, never letters or display positions. Single-choice/true-false needs
one ID; multiple-answer accepts unique IDs up to `max_selections`. True/false IDs are
`"true"` and `"false"`. `clear` takes no selections (omit the field or use `[]`);
`set` with an empty list is invalid. Omitted questions stay unchanged; already desired
answers are no-ops. Duplicate/unknown fields, keys, choices or changes are rejected;
files are limited to 512 KiB. Nested snapshot question IDs and selection lists must
have the documented types; invalid UTF-8 is an input error. Re-export after expiry or a definition/baseline conflict.

Verified answer types: multiple answer (select all); single choice (multiple choice
with one answer, encoded by Ultra as `multipleanswer` with `answerSelectionLimit: 1`);
and true/false (`eitherOr` / `true_false`). Essay, short answer, fill-in-the-blank,
matching, ordering, calculated, file response, and anything else are unsupported:
**answer in Blackboard**. Type support does not establish live acceptance of every clear case.

Timed/proctored, password/secure-browser/IP-restricted,
forced-completion and no-backtracking tests are refused. All six recognized safety
booleans must be present and boolean. A missing `timeLimit` accepts an explicit false
`isTimeLimitEnabled`, `isTimerEnabled` or `isTimed`, with no contradictory enabled or
unknown timer flag. The observed untimed Ultra shape (no `timeLimit`, `timerCompletion: CONTINUAL`)
is also accepted by the shared safety check; other timer enums are unknown.
`gradingColumn.dueDate: null` means no deadline; a missing/invalid due date is unknown
and refuses. Settings and the 120-second deadline window are checked again immediately
before each actual dispatch, including after browser startup.

HTTP is first. Only missing session/XSRF or HTTP 401 permits the same allowlisted
PATCH through browser `fetch()`: installed browser first, optional bundled Playwright
last under `core.browser` launch order. HTTP 401 explicitly means Blackboard rejected
the request before persistence, even though the HTTP request was dispatched. Browser
fallback is allowed only before HTTP has persisted any answer. HTTP 403, 429, 5xx,
timeout, redirect and malformed responses never fall back. Other dispatched failures
stop the batch and reconcile by GET without replaying writes.
No automatic login/Duo occurs during this command.

**Concurrency limitation:** no CAS/conditional write was observed. Export-baseline
checks, an immediate pre-PATCH definition/choice-order re-read and a held OS attempt lock cannot prevent a
browser/other-host edit racing between GET and PATCH. The student authorized this
deviation; avoid concurrent editing. See [ADR-0006](adr/0006-explicit-answer-save-without-submit.md)
and the [protocol note](specs/save-draft-answers-protocol.md). After partial/unknown
results, inspect the attempt in Blackboard before retrying; earlier answers may persist.
The command never submits, but Blackboard or an instructor can change the attempt state.
With `--new-attempt`, JSON includes `new_attempt` (`requested`, `outcome`: `existing` /
`created_verified` / `unknown`, `dispatched`, `transport`, `attempt_id`, `submitted: false`).
Any later failure after creation exits 4 and retains this receipt. Exit 3 means no
creation or save dispatch. Exit 4 covers an unsuccessful run after either kind of dispatch.

| Exit | Meaning |
| --- | --- |
| `0` | Successful show/export/preview, confirmed start, fully verified save, or no changes needed |
| `2` | Invalid CLI/local input, including refused overwrite/symlink destinations |
| `3` | Refusal/conflict/unknown state before any start or PATCH dispatch |
| `4` | A start or PATCH was dispatched and the run ended partial, unknown or refused; inspect receipts |

Exit `4` is not proof of persistence. `new_attempt.dispatched` records start dispatch;
`dispatched` records whether an actual PATCH was
sent (including a rejected HTTP 401). Each PATCH must pass bounded GET reconciliation
before the next is sent: at most three reads with 1+2 seconds backoff and 0.6-second
per-read timeouts (about five seconds total). Unknown persistence stops immediately.
A separate final snapshot checks **every** requested answer, including unchanged and
previously verified values, question presence, definition and `IN_PROGRESS` status.
The OS lock stays held through that final read; abandoned lock files do not block
future processes. Ctrl-C/task cancellation stops further dispatches and lets readback
emit one receipt for already-dispatched writes.

`--out` is checked before any Blackboard write (existence, overwrite, symlink and
writable parent). Local JSON output uses a unique exclusive temporary file in the same
directory, mode 0600, then atomic replacement; symlink targets are refused. If writing
a receipt later fails, stdout still contains the real result and the save's exit code
is preserved, with the file error on stderr. `--json` input, parser and course-resolution
errors also produce one versioned JSON document (exit 2).

For final status `IN_PROGRESS`, the existing “NOT SUBMITTED” footer remains. Otherwise
the footer says: “This command sent no submit action. Blackboard now reports <status>.”
Unknown final status is displayed as `UNKNOWN`.

### `bb search <QUERY>`
**Description:** Omnisearch across all courses for files, assignments, and documents matching keyword query. Text output is grouped by course.
- `QUERY`: Positional search string (e.g. `bb search "Syllabus"`).
- `-c <COURSE>`: Restrict search to specific course.

### `bb download <ITEM_OR_QUERY>`
**Description:** Downloads specific Blackboard content item, attachment, or search query directly to `downloads/<CourseName>/`.
- `ITEM_OR_QUERY`: Positional target (e.g. `bb download "Worksheet_1.pdf"` or `bb download _8825690_1`).
- `-c <COURSE>`: Restrict target course.
- `--out-dir <dir>`: Custom destination directory (default: `./downloads`). It is only created when a file is written.
- A name that matches a **folder** is not downloaded: the command lists the files inside (and exits 0). A name that matches several items lists them and exits 1. Items that are not files (assignments, links) say so.

### `bb activity`
**Description:** Recent activity (grades, due dates, announcements, new content, discussion posts) grouped by course, newest first. Reads the Ultra stream REST endpoint (about 0.3 s); the browser is only a fallback when REST is unavailable. Repeated entries are merged and a discussion thread's responses collapse into one row.
- `--limit N`: newest N items (default 40, `0` for everything).
- `--json`: items with `category`, `course`, `title`, `at` and `due_at` (ISO-8601 with offset), `grade`, `message`.

### `bb profile`
**Description:** Shows your name, role and pronouns (<250ms). Student ID, username and email are deliberately not printed.

---

## 🔀 Course Selection Syntax

Single course targets can be passed directly as positional arguments or with `-c`:

| Syntax | Example | Description |
| :--- | :--- | :--- |
| **Positional Argument** | `bb outline IS410` | Matches course code `IS 410` |
| **By Code Flag** | `bb outline -c IS410` | Explicit `-c` flag |
| **By Multiple Codes** | `bb grades -c IS410,MATH215` | Targets multiple specific courses |
| **By Keyword** | `bb outline -c Database` | Matches enrolled course containing "Database" |
| **By Exact ID** | `bb outline -c _105737_1` | Targets exact Blackboard internal ID |
| **All Courses** | `bb grades --all` | Targets all configured courses in parallel |

---

## 📦 Output Formats & File Export

- `--json`: Emits standardized JSON to terminal stdout (zero disk writes). **stdout is exactly one JSON document; progress, warnings and fallback notices go to stderr.** Failures print `{"error": kind, "message": ...}` and exit non-zero.
- `--out <file>`: Exports JSON directly to specified file.
- `--compact`: Emits minified JSON.
- `--md`, `--save`: Saves Markdown report into `output/` directory.

---

## ⚡ Concurrency & Browser Controls

- `--concurrency <N>`: Override adaptive dynamic worker pool size (Default: 6–8 for light queries, 2–3 for deep outlines).
- `--visible`, `-v`: Run Playwright with visible browser window for debugging.
- `--cdp <URL>`: Connect to an existing browser via Chrome DevTools Protocol.

---

## 🤖 Telegram Bot Daemon

Subcommands under `bb bot`:

```bash
bb bot            # Run interactive Telegram bot in foreground (for debugging)
bb bot start      # Start bot daemon detached in background
bb bot stop       # Gracefully stop background bot daemon
bb bot restart    # Restart background bot daemon (broadcasts startup card)
bb bot status     # Check bot daemon PID, RSS memory, and session status
```

---

## 🖥️ Native macOS Menubar App

```bash
bb menubar        # Launches native macOS status bar menu app (🎓 BB 🟢)
```

---

## 📖 Built-in Help Guides

For command help use `bb help <COMMAND>` (see [🆘 Getting Help](#-getting-help)). Topic manuals live under `bb guide`:

```bash
bb guide               # List available guide topics
bb guide auth         # Authentication & headless execution guide
bb guide courses      # Course selection & syntax guide
bb guide schema       # Standardized v2 JSON schemas
bb guide telegram     # Telegram bot & alert setup
bb guide concurrency  # Concurrency engine & worker pool tuning
```

---

## 🔄 Legacy Flags Backward Compatibility

All prior `--flag` invocations remain 100% operational:

| Legacy Flag | Modern Subcommand | Behavior |
| :--- | :--- | :--- |
| `bb --login` | `bb login manual` / `bb login --manual` | Browser SSO login |
| `bb --auto-exp` | `bb login` / `bb login auto` | Smart automated SMS 2FA login |
| `bb --login --auto` | `bb login auto` | Automated login |
| `bb --check-session` | `bb check` or `bb session check` | Session status verification |
| `bb --session-stats` | `bb session stats` | Lifespan telemetry |
| `bb --session-info` | `bb session info` | Timestamp metadata |
| `bb --logout` | `bb logout` | Clear cookies & tokens |
| `bb --briefing` | `bb briefing` | Master aggregated briefing |
| `bb --due 7d` | `bb due 7d` | Deadline aggregator |
| `bb --grades` | `bb grades` | Gradebook extraction |
| `bb --announcements` | `bb announcements` | Course announcements |
| `bb --outline -c <C>` | `bb outline <C>` | Course hierarchy |
| `bb --search <Q>` | `bb search <Q>` | Cross-course search |
| `bb --download <I>` | `bb download <I>` | File download |
| `bb --discover` | `bb discover` | Term course discovery |
| `bb --list-terms` | `bb terms` | List academic terms |
| `bb --courses` | `bb courses` | List configured courses |
| `bb --bot-start` | `bb bot start` | Start daemon |
| `bb --bot-stop` | `bb bot stop` | Stop daemon |
| `bb --bot-restart` | `bb bot restart` | Restart daemon |
| `bb --bot-status` | `bb bot status` | Inspect daemon |

### Grade and deadline reliability

An official overall grade must be externally designated as the course total or use a
recognized overall-total name; a category subtotal such as "Quiz average" is not a
course grade. Human-readable grades show actual earned / possible points only.
Percent-scaled totals without real point totals are labeled as unavailable rather than
shown as points out of 100. When no official total is available, the labeled calculated
fallback uses included, non-exempt graded points and does not assume category weights.

Submission status is verified against the student's attempt or post when available.
A public Needs Grading state can coexist with an `IN_PROGRESS` attempt; that work is
reported as `in progress` and remains open in deadline views.

If student-grade records are unreadable, visible items retain `status: "unknown"` and
`grades --json` includes `records_error`. Text and briefing label this partial data as
"grades unavailable" rather than "no grade yet". Assignment-list errors expose an
`error` per course; 401 stops with login guidance, and 403 reports student visibility
without opening a browser.

Deadlines retain distinct column/content identities even when titles repeat. Calendar
rows join by source identity first; title-only joins require a unique match within the
same course. Telegram briefings include overdue work and avoid repeating urgent work
in the upcoming list.
