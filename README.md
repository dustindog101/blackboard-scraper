# 🎓 Blackboard Ultra Scraper v2

[![Architecture](https://img.shields.io/badge/Architecture-Async%20Playwright%20%2B%20Python%203.10%2B-blue.svg)](#architecture)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Zero-Dependencies Telegram](https://img.shields.io/badge/Telegram-Built--in%20Standard%20Lib-orange.svg)](#-modular-telegram-bot--alerts-optional)

An automated, high-performance headless scraper and academic intelligence engine for **UMBC Blackboard Ultra**. Built with **Playwright Async**, persistent SSO session caching, task-aware dynamic concurrency, multi-level accordion outline traversal, safe assignment rubric inspection, cross-course deadline aggregation, and standardized v2 JSON schemas.

---

## 📑 Table of Contents

- [⚡ Core Architectural Principles](#-core-architectural-principles)
- [🚀 Quick Start](#-quick-start)
- [🔐 Authentication & Headless Sessions](#-authentication--headless-sessions)
- [CLI Output by Default](#cli-output-by-default)
- [📦 Standardized v2 JSON Schemas](#-standardized-v2-json-schemas)
- [🔀 Smart Course Selection Syntax](#-smart-course-selection-syntax)
- [📚 Scraper Feature Reference](#-scraper-feature-reference)
  - [1. Composite Daily Briefing](#1-composite-daily-briefing---briefing)
  - [2. Course Outline & Syllabus Extractor](#2-course-outline--syllabus-extractor---outline)
  - [3. Deep Assignment & Rubric Inspector](#3-deep-assignment--rubric-inspector---assignments)
  - [4. Cross-Course Deadline Aggregator](#4-cross-course-deadline-aggregator---due)
  - [5. Course Announcements & Grades](#5-course-announcements--grades)
  - [6. Omnisearch Across Courses](#6-omnisearch-across-courses---find)
  - [7. Direct Course File Downloader](#7-direct-course-file-downloader---download)
  - [8. Course Discovery & Active Term Isolation](#8-course-discovery--active-term-isolation---discover)
- [⚡ Smart Adaptive Concurrency Engine](#-smart-adaptive-concurrency-engine)
- [🤖 Modular Telegram Bot & Alerts (Optional)](#-modular-telegram-bot--alerts-optional)
- [🖥️ Optional macOS Menubar App](#️-optional-macos-menubar-app---menubar)
- [📋 Complete CLI Flag Reference](#-complete-cli-flag-reference)

---

## ⚡ Core Architectural Principles

1. **HTTP REST Fast-Path**: Read-only scrapers query Blackboard's REST APIs using saved session cookies. A valid-session read starts no browser; timing depends on the number of requests and courses.
2. **Graceful Playwright Fallback**: If an API endpoint is restricted or blocked, scrapers notify the user and seamlessly delegate to Playwright browser DOM extraction.
3. **Clean CLI Output by Default**: All commands format structured, beautiful output directly to terminal `stdout`. No unwanted Markdown or temporary files are written to disk unless explicitly requested (`--md` or `--out <file>`).
4. **Task-Aware Smart Concurrency**: The worker pool automatically adjusts concurrency ceilings based on operation complexity—shallow scraping runs fast at 6–8 parallel workers, while deep accordion drawers run safely at 2–3 workers.
5. **Closed-Course Circuit Breakers**: Closed or unavailable courses are detected in `< 120ms` via HTTP 403 or DOM error modals, immediately releasing worker threads.
6. **Zero-Pip Telegram Bot**: Full Telegram alerting and interactive bot control built using Python standard library HTTP (`urllib`), keeping the codebase lean and decoupled.

---

## 🚀 Quick Start (Lightweight CLI-First)

The base scraper runs on Windows, macOS, and Linux. Install the Python dependencies below; an additional browser download is optional.

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/dustindog101/blackboard-scraper.git
cd blackboard-scraper
```

#### On macOS / Linux:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./install-cli.sh   # Registers global 'bb', 'blackboard', and 'bbscraper' commands
```

#### On Windows (PowerShell):
```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
.\install-cli.ps1   # Registers global 'bb', 'blackboard', and 'bbscraper' commands
```

The scraper prefers your installed default browser when it is Chromium-compatible.
Otherwise it tries Edge on Windows or Chrome on macOS/Linux, then other compatible
installations (including Brave), and explains the fallback.
It launches a separate Blackboard profile in `.session/`; your everyday browser
profile is not used. Login and scraping never download browser binaries.

If no compatible browser is installed, or browser policies prevent automation,
install one shared Chromium binary for both headless and visible login:

```bash
python -m playwright install chromium --no-shell
```

`--no-shell` skips the separate headless shell. Playwright itself is still required
for SSO/Duo automation. Safari and stock Firefox cannot be reused by Playwright.

Optional overrides apply to login, session import, and browser scraping:

```bash
# macOS / Linux: pin an installed browser, or bundled Chromium
BLACKBOARD_BROWSER=chrome bb login --manual
BLACKBOARD_BROWSER=chromium bb login
# A nonstandard Chromium executable (absolute path)
BLACKBOARD_BROWSER_PATH="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" bb login
```

```powershell
# Windows PowerShell
$env:BLACKBOARD_BROWSER = "msedge"
bb login --manual
# Restore automatic selection
Remove-Item Env:BLACKBOARD_BROWSER
# A portable/custom install
$env:BLACKBOARD_BROWSER_PATH = "C:\Browsers\chrome.exe"
```

Supported `BLACKBOARD_BROWSER` values: `auto` (default), `chrome`, `msedge`,
`brave`, `chromium`. `BLACKBOARD_BROWSER_PATH` takes precedence. Explicit choices
fail with an actionable error instead of silently switching browsers. Existing
Playwright installations with a headless shell remain supported as a last fallback.

---

### 2. Authenticate (One-Time)

```bash
# Option A [Recommended on macOS]: Smart automated SSO + real-time macOS SMS Duo 2FA interception
bb login

# Option B: Manual browser login to solve SSO & Duo manually / with Touch ID
bb login --manual

# Option C: Automated SSO + terminal Duo SMS passcode entry
bb login auto
```

Smart Login also handles an existing UMBC SSO session that skips the credential
form or Duo. It saves cookies only after Blackboard's HTTP identity check
succeeds. Failed login exits with status 1; changed or missing SSO
forms produce a bounded error with `bb login --manual` recovery guidance.

---

### 3. Run Commands (100% Headless)

```bash
# Auto-discover your active semester courses
bb discover

# Get your daily school briefing
bb briefing

# Check upcoming deadlines for the next 7 days
bb due 7d

# View course outline and syllabi (positional course argument)
bb outline IS410
```

---

## 🔐 Authentication & Headless Sessions

### How It Works:
- **Session Persistence**: Session cookies and local storage are saved in `.session/cookies.json` and `.session/`.
- **Long-Lived Tokens**: Sessions remain valid for **weeks to months**.
- **100% Fully Headless**: All ongoing scrapers, cron jobs, background watchers, and Telegram bot interactions run headlessly with zero browser popups or prompts.

```bash
# Check if current session is active (<120ms REST probe)
bb check

# View session creation, last-used, and telemetry lifespan stats
bb session stats

# Clear session cookies to logout
bb logout
```

### Move a session from Mac to Windows

After logging in normally on the Mac, export its saved Blackboard cookies:

```bash
bb session export ~/Downloads/bb-session.json
```

Copy that plain JSON file to Windows, stop the scraper's background jobs/browser
windows, then import it:

```powershell
bb session import "$HOME\Downloads\bb-session.json"
bb check
bb due 7d
```

Import verifies the session over HTTP and in the managed browser before installing
it. Rejected files or failed writes preserve the previous cookies. Export works
offline and leaves the Mac session intact; it does not extend Blackboard's session
lifetime. Keep the exported cookie file private. No password, OTP, or encryption
setup is required. Use `--overwrite` to replace an existing export file. On a new
Windows setup, run `bb discover` if your courses are not configured.

---

## CLI Output by Default

By default, **no Markdown files are saved to disk**. Commands print directly to terminal `stdout`:

### Outline Shallow View (`bb outline IS410`):
```text
📚 Course Outline: IS 410 Introduction to Database Design (_105737_1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
├── 📜 Course Syllabus & Policies [syllabus] [ID: _105738_1]
├── 📁 Homework & Assignments [folder] [ID: _105740_1] (12 items: 8 assignments, 4 files)
├── 📁 Lecture Slides & Notes [folder] [ID: _105741_1] (24 items: 24 files)
└── 📁 Exam Review Materials [folder] [ID: _105742_1] (15 items: 10 files, 5 tests)

💡 Tip: Use '-f <name|ID>' to expand a folder, or '--expand-all' / '--deep' for full outline tree.
```

### Selective Folder View (`bb outline IS410 -f "Homework"`):
```text
📚 Course Outline: IS 410 Introduction to Database Design (_105737_1) ➔ 📁 Homework & Assignments
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
├── 📝 Homework 1: ER Diagrams [assignment] [ID: _105745_1] (Due: Sep 15, 2026)
├── 📝 Homework 2: SQL DDL [assignment] [ID: _105746_1] (Due: Sep 22, 2026)
└── 📎 Database Schema Template.sql [file] [ID: _105747_1]
```

### Deadline View (`bb due 7d`):
```text
Due in the next 7 days · 3 items

Today, Tue Oct 20
  1:00 PM EDT  ENGL 100  J#5: Library Research Module  10 pts  in progress

Sun Oct 25
  11:59 PM EDT  AGNG 100  Module 8 Assignment  100 pts  not submitted
```

--------------------------+-------------------------------------+----------------------+-----------
IS 410 Database Design    | Homework 1: ER Diagrams             | Sep 15, 2026 11:59PM | Upcoming
MATH 215 Finite Math      | Problem Set 1                       | Sep 16, 2026 11:59PM | Upcoming
```

---

## 📦 Standardized v2 JSON Schemas

When you need machine-readable structured data for LLM agents, dashboards, or external APIs:

- **`--json`**: Prints structured JSON directly to CLI stdout.
- **`--out <file>`**: Exports structured JSON directly to `<file>`.
- **`--compact`**: Emits minified JSON.

### Composite Schema (`python3 main.py --briefing --json`):
```json
{
  "version": "2.0",
  "source": "blackboard-scraper",
  "generated_at": 1786938000,
  "generated_at_human": "2026-08-16T23:40:00Z",
  "summary": {
    "total_courses": 5,
    "upcoming_deadlines_count": 2,
    "total_announcements_count": 8,
    "unread_announcements_count": 1
  },
  "user": {
    "username": "BH69617",
    "name": "Amanuel Hailie"
  },
  "courses": [
    {
      "course_id": "_105737_1",
      "course_name": "IS 410 Introduction to Database Design",
      "syllabus": {
        "title": "IS 410 Syllabus",
        "attachments": [
          { "filename": "Syllabus.pdf", "url": "https://blackboard.umbc.edu/..." }
        ]
      },
      "outline": [
        {
          "content_id": "node_1",
          "title": "Week 1: Relational Data Models",
          "content_type": "folder",
          "depth": 0,
          "links": [{ "text": "Slides.pdf", "url": "https://..." }]
        }
      ],
      "assignments": [
        {
          "title": "Project Milestone 1",
          "due_date": "2026-09-15 23:59",
          "points_possible": 100.0,
          "submission_status": "Unattempted",
          "is_timed_test": false,
          "instructions": "Design the ER diagram...",
          "rubric": [{ "criterion": "ER Diagram Completeness", "points": 50 }],
          "attachments": [{ "filename": "Spec.pdf", "url": "https://..." }]
        }
      ],
      "grades": [
        { "name": "Quiz 1", "grade": "95 / 100", "dueDate": "2026-09-10" }
      ],
      "announcements": [
        { "title": "Welcome", "unread": true, "meta": "Aug 15", "body": "Welcome everyone!" }
      ]
    }
  ],
  "global": {
    "activity_stream": [],
    "calendar_due_dates": []
  }
}
```

---

## 🔀 Smart Course Selection Syntax

Target specific courses using positional codes, keywords, IDs, or comma-separated lists:

```bash
# 1. Target by Course Code (Positional or -c)
bb outline IS410
bb assignments ENGL100
bb grades "ECON 122"

# 2. Target Multiple Courses (Comma-separated via -c)
bb outline -c IS410,ENGL100,MATH215

# 3. Target by Fuzzy Title Keyword
bb outline -c Database
bb grades -c Accounting

# 4. Target All Courses
bb outline --all
bb assignments --all
```

---

## 📚 Scraper Feature Reference

### 1. Composite Daily Briefing (`bb briefing`)
Fetches calendar, course announcements, and grades concurrently, then groups overdue work, upcoming deadlines and items needing attention:
```bash
bb briefing                          # Terminal UI
bb briefing --json                   # JSON to stdout
bb briefing --out briefing.json      # JSON to file
bb briefing --telegram               # Push to Telegram
```

### 2. Course Outline & Selective Folder Explorer (`bb outline`)
Traverses course outlines with smart shallow summary views, folder item counting, selective folder expansion, and interactive browsing:
```bash
bb outline IS410                     # Shallow summary with folder item counts (Default)
bb outline IS410 -f "Homework"       # Selectively expand specific folder by name
bb outline IS410 -f _105740_1        # Selectively expand specific folder by ID
bb outline IS410 --expand-all        # Deep recursive tree (all folders expanded)
bb outline IS410 --depth 2           # Limit expansion to 2 depth levels
bb outline IS410 -i                  # Interactive terminal folder explorer menu
bb outline --all                     # All courses
bb outline --all --type syllabus     # Syllabi only
bb outline --all --type assignment   # Assignments only
bb outline --all --type document     # Lecture docs only
bb outline --all --filter "Homework" # Search keyword
```

### Save answers without submitting (`bb answers`)

`bb answers "Practice Quiz" -c DEMO100` reads an existing in-progress Ultra test.
Use `--set 1=A --set 2=A,B --set 3=False` to preview; add `--apply` to save and verify.
`--set N=clear` clears; True/False/T/F and letters are case-insensitive. Letters follow
bb's list, which may differ from Blackboard's shuffled display; no option-text matching.
Or export with `--export answers.json`, edit `changes[]`, preview with `--file answers.json`,
then add `--apply`. Both inputs share the same save pipeline; runtime `--set` baselines
have no file expiry. Unchanged questions collapse; options show letters and radio/checkbox markers.

`--new-attempt` explicitly starts an eligible untimed test, then shows its answers;
with `--set ... --apply` it can start and save. It uses an existing IN_PROGRESS attempt
when present. Unknown safety/count/deadline data, restrictions, exhausted attempts and
less than 120 seconds until due refuse. HTTP → installed browser → optional bundled
Playwright; a dispatched start is never retried, and GET must confirm exactly one new
IN_PROGRESS attempt. The creation request was observed live from the Ultra UI; `bb`'s own start is offline-tested. Combined start/save
requires a readable prior question list for input preflight; otherwise start separately.
It never submits. Saved answers may be visible to course staff.

Verified answer types: multiple answer (select all); single choice (multiple choice
with one answer, encoded by Ultra as `multipleanswer` with `answerSelectionLimit: 1`);
and true/false (`eitherOr` / `true_false`). Essay, short answer, fill-in-the-blank,
matching, ordering, calculated, file response, and anything else are unsupported:
**answer in Blackboard**. Type support does not establish live acceptance of every clear case.

Fresh baseline checks are weaker than CAS; avoid concurrent browser/other-host edits.
Inspect Blackboard after any partial/unknown result. See the [CLI reference](docs/CLI_REFERENCE.md#bb-answers-target--c-course),
[protocol evidence](docs/specs/save-draft-answers-protocol.md), and
[retake decision](docs/adr/0007-explicit-untimed-attempt-creation.md).

### 3. Assignment Discovery (`bb assignments`)
List gradable items and their status, then inspect one by title or ID:
```bash
bb assignments AGNG
bb assignments ECON --filter "M3"
```

### 3b. Assignment Inspector (`bb assignment`)
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

```bash
bb assignment "Module 7 Assignment" -c AGNG              # Instructions and rubric availability
bb assignment "Module 7 Assignment" -c AGNG --instructions
bb assignment "Module 7 Assignment" -c AGNG --rubric      # Rubric only
bb assignment "Module 6 Assignment" -c AGNG --full        # Existing questions and answers
bb assignment "M3 Discussion" -c ECON --full              # Prompt and your own posts
bb assignment "Homework 1" -c IS410 --full                # Instruction document and submission files
bb assignment "Worksheet 5" -c MATH                       # Identifies Gradescope; no provider launch
bb assignment "Module 7 Assignment" -c AGNG --rubric --json
```
Title matching is case-insensitive. Exact titles win; a unique partial title auto-resolves. Ambiguous matches return candidates rather than silently choosing an item. Legacy flags (`bb --assignment`, `--quiz`, `--asmt`, `--assessment`) accept the same view flags.

### 4. Cross-Course Deadline Aggregator (`bb due`)
Aggregates deadlines across global calendar, course gradebooks, and outlines:
```bash
bb due 7d                            # Deadlines in next 7 days
bb due 14d                           # Deadlines in next 14 days
bb due overdue                       # Overdue items
bb due 7d --exclude-completed        # Exclude graded/submitted
```

### 5. Course Announcements & Grades
Grade text and saved Markdown show earned / possible points, an `official` or
`calculated` source label, and how many items are graded. They do not display
percentages, inferred letters, check marks, or emoji. When Blackboard supplies
only a percent-scaled overall grade, the point total is labeled unavailable.
JSON retains its `percent` and `letter` keys for compatibility; inferred
letters are null.

Submission wording follows the student's verified attempt or post. An
`IN_PROGRESS` attempt displays `in progress` and remains open
even when the public gradebook says Needs Grading.

```text
IS 410  Intro To Database Design
  18 / 20 pts  (official: Overall Grade) · 1 of 2 graded
  no due date  Quiz              18 / 20 pts  graded
  no due date  Library Research  10 pts possible  in progress
```

```bash
bb announcements                     # Announcements across all courses
bb announcements ECON122             # Announcements for single course
bb grades                            # Grades across all courses
bb grades IS410                      # Grades for single course
bb grades --json                     # Structured JSON output
```

### 6. Omnisearch Across Courses (`bb search`)
Search across all course titles, modules, and assignment descriptions:
```bash
bb search "Project"
bb search "Syllabus"
```

### 7. Direct Course File Downloader (`bb download`)
Automatically locates course and downloads attachments, PDFs, or Jupyter notebooks:
```bash
bb download "Worksheet_1.pdf"
bb download _8825690_1               # Download by exact Blackboard item ID
bb download "HW3.docx" -c IS410      # A file linked inside an assignment's description
bb download 23310389                 # By the pid in a /bbcswebdav/ URL (also _23310389_1)
bb download "Homework 3" -c IS410    # An assignment/test/document: downloads the files embedded in it
```
A download that returns a login page reports `session expired, run bb login` instead of saving it.

### 8. Course Discovery & Active Term Isolation (`bb discover`)
Intelligently queries Blackboard REST API and auto-populates `config.json` with active courses:
```bash
bb discover                          # Auto-detect current active term
bb discover --term FA2026            # Filter to specific semester
bb terms                             # List all lifetime enrolled terms & courses
bb courses                           # View configured courses
```

---

## ⚡ Smart Adaptive Concurrency Engine

The concurrency engine (`SmartWorkerPool` in `core/async_engine.py`) auto-tunes worker threads dynamically:

| Profile | Concurrency | Operations | Behavior |
| :--- | :---: | :--- | :--- |
| **`LIGHT`** | **6 – 8** | `--announcements`, `--grades`, `--calendar` | Maximum parallel throughput for shallow DOMs |
| **`MEDIUM`** | **4 – 5** | `--briefing`, `--due`, `--activity` | Balanced throughput for composite data streams |
| **`HEAVY`** | **2 – 3** | `--outline`, `--assignments` | Controlled tabs for deep treeview expansion & drawer animation safety |

- **Latency Auto-Scaling**: Scales up automatically when requests complete in `< 1.0s`.
- **Timeout Throttling**: Decrements concurrency automatically on slow networks or timeouts.
- **Circuit Breakers**: Skips closed courses in `< 120ms` to avoid burning idle cycles.

---

## 🤖 Modular Telegram Bot & Alerts (Optional)

The Telegram integration requires **zero external pip packages** and is completely dormant unless enabled.

### 1. Configuration (`config.json`)
```json
{
  "telegram": {
    "enabled": true,
    "bot_token": "YOUR_BOT_TOKEN_FROM_BOTFATHER",
    "admin_chat_id": 123456789
  }
}
```

### 2. Manage Bot Daemon
```bash
bb bot start          # Start Telegram bot daemon in background
bb bot status         # Check daemon status, PID, and RSS memory
bb bot restart        # Restart daemon and broadcast rich card
bb bot stop           # Stop running daemon
bb bot                # Run bot directly in foreground
```

### 3. Interactive Telegram Commands:
- `/briefing` — Trigger full concurrent school briefing
- `/due [days]` — View upcoming deadlines (e.g. `/due 7`)
- `/grades [course]` — Check latest grades
- `/announcements [course]` — Check unread announcements
- `/courses` — List enrolled courses
- `/check` — Check Blackboard session health
- `/watch [mins]` — Start periodic monitoring loop for new grades & announcements
- `/help` — Command guide

---

## 🖥️ Optional macOS Menubar App (`bb menubar`)

For macOS users who want background status monitoring and click-to-scrape controls in their macOS menu bar (`🎓 BB 🟢`):

```bash
# Install optional macOS menubar extra
pip install -e .[menubar]
# or
pip install rumps

# Launch Menubar app
bb menubar
```

---

## 📋 Complete CLI Command Reference

All commands support natural subcommands. Legacy `--flags` (e.g. `bb --briefing`, `bb --auto-exp`) remain 100% backward compatible.

| Category | Canonical Command | Legacy Flag | Description |
| :--- | :--- | :--- | :--- |
| **Authentication** | `bb login` | `--auto-exp` | Smart automated SSO + macOS SMS 2FA interception |
| | `bb login` | `--login` | Now routes to smart auto login (was prompt-mode auto) |
| | `bb login --manual` | `--login --visible` | Open browser window for SSO / Duo manual push |
| | `bb login auto` | `--login --auto` | Automated SSO (now SMS-intercept path; terminal fallback on non-macOS) |
| | `bb check` | `--check-session` | Validate session cookies via fast HTTP probe (<120ms) |
| | `bb session info` | `--session-info` | Display session timestamps |
| | `bb session stats` | `--session-stats` | Deep session telemetry & lifespan analytics |
| | `bb logout` | `--logout` | Clear cached session cookies |
| **Course Discovery** | `bb discover` | `--discover` | Auto-discover active semester courses & save to `config.json` |
| | `bb discover --term <T>` | `--term <TERM>` | Filter discovery by academic term (`FA2026`, `all`) |
| | `bb terms` | `--list-terms` | List all lifetime enrolled terms & courses |
| | `bb courses` | `--courses` | List configured courses and IDs |
| **Scrapers** | `bb briefing` | `--briefing` | High-speed concurrent school briefing |
| | `bb due [WINDOW]` | `--due [WINDOW]` | Upcoming deadlines (`7d`, `14d`, `overdue`) |
| | `bb outline [COURSE]` | `--outline -c <C>` | Full course outline tree, syllabi, and files |
| | `bb assignments [COURSE]` | `--assignments -c <C>` | Assignment discovery and status; use `bb assignment` for detail |
| | `bb assignment <TARGET>` | `--assignment <T>` | Single-item inspector with existing answers and the current user's discussion submissions (aliases: `quiz`, `asmt`, `assessment`) |
| | `bb assignment <T> --start-attempt` | `--assignment <T> --start-attempt` | Allow starting an attempt (`--begin-attempt` also accepted) |
| | `bb assignment <T> --start-attempt --force-start` | `--force-start` | Confirm starting a timed exam |
| | `bb grades [COURSE]` | `--grades -c <C>` | Gradebook items and scores |
| | `bb announcements [COURSE]` | `--announcements` | Course announcements |
| | `bb activity` | `--activity` | Homepage activity stream |
| | `bb calendar [COURSE]` | `--calendar` | Global calendar items |
| | `bb search <QUERY>` | `--search <query>` | Omnisearch across all courses |
| | `bb download <ITEM>` | `--download <item>` | Direct file/attachment downloader |
| | `bb profile` | `--profile` | Student profile information |
| **Course Selection** | `<COURSE>` / `-c <CODE>` | `-c <ID/Code>` | Target course(s) (e.g. `IS410` or `-c IS410,ENGL100`) |
| | `--all` | `--all` | Target all configured courses |
| **Outline Controls** | `-f, --folder <query>` | `--folder, -f` | Expand specific folder/module by name or ID |
| | `--expand-all, --deep` | `--expand-all` | Recursively expand all folders (full tree view) |
| | `--depth <N>` | `--depth <N>` | Limit display expansion to `<N>` depth levels |
| | `-i, --interactive` | `-i` | Interactive terminal folder browser menu |
| | `--type <type>` | `--type <type>` | Filter outline by type (`syllabus`, `document`, `assignment`, `folder`) |
| | `--filter <text>` | `--filter <text>` | Filter items by keyword substring |
| **Output Formats** | `--json` | `--json` | Output structured JSON to CLI stdout |
| | `--out <file>` | `--out <file>` | Export structured JSON directly to file |
| | `--md`, `--save` | `--md`, `--save` | Save Markdown reports to `output/` directory |
| | `--compact` | `--compact` | Minified JSON output |
| **Performance** | `--concurrency <N>` | `--concurrency` | Override dynamic worker pool size |
| | `-v, --visible` | `-v, --visible` | Launch visible browser window for debugging |
| **Daemon & Menubar** | `bb bot start` | `--bot-start` | Launch Telegram bot daemon in background |
| | `bb bot status` | `--bot-status` | Inspect running bot daemon PID and memory |
| | `bb bot restart` | `--bot-restart` | Gracefully restart bot daemon |
| | `bb bot stop` | `--bot-stop` | Stop background bot daemon |
| | `bb bot` | `--bot` | Launch interactive Telegram bot in foreground |
| | `bb menubar` | `--menubar` | Launch optional native macOS Menubar app |
| **Help & Guides** | `bb help [CMD]` | — | Show help (all commands, or one command: `bb help outline`) |
| | `bb <CMD> --help` | — | Per-command options, usage, and examples |
| | `bb guide <TOPIC>` | `--guide <TOPIC>` | Show detailed topic manuals (`auth`, `courses`, `schema`, etc.) |
| | `bb --version` | — | Print CLI version |

---

## 📜 License

MIT License. Designed for UMBC students and educational research.
