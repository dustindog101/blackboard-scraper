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
- [🖥️ Beautiful CLI Output by Default](#️-beautiful-cli-output-by-default)
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

1. **HTTP REST Fast-Path (<150ms)**: Read-only scrapers (`--due`, `--calendar`, `--announcements`, `--grades`, `--outline`, `--search`, `--profile`) query Blackboard's public REST APIs directly using session cookies, returning sub-second responses with zero browser overhead.
2. **Graceful Playwright Fallback**: If an API endpoint is restricted or blocked, scrapers notify the user and seamlessly delegate to Playwright browser DOM extraction.
3. **Clean CLI Output by Default**: All commands format structured, beautiful output directly to terminal `stdout`. No unwanted Markdown or temporary files are written to disk unless explicitly requested (`--md` or `--out <file>`).
4. **Task-Aware Smart Concurrency**: The worker pool automatically adjusts concurrency ceilings based on operation complexity—shallow scraping runs fast at 6–8 parallel workers, while deep accordion drawers run safely at 2–3 workers.
5. **Closed-Course Circuit Breakers**: Closed or unavailable courses are detected in `< 120ms` via HTTP 403 or DOM error modals, immediately releasing worker threads.
6. **Zero-Pip Telegram Bot**: Full Telegram alerting and interactive bot control built using Python standard library HTTP (`urllib`), keeping the codebase lean and decoupled.

---

## 🚀 Quick Start (Lightweight CLI-First)

The base scraper is 100% lightweight and cross-platform (Windows, macOS, Linux). It requires only `playwright` and `beautifulsoup4`.

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
playwright install chromium
./install-cli.sh   # Registers global 'bb', 'blackboard', and 'bbscraper' commands
```

#### On Windows (PowerShell):
```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
.\install-cli.ps1   # Registers global 'bb', 'blackboard', and 'bbscraper' commands
```

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

---

## 🖥️ Beautiful CLI Output by Default

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

### Deadline Table View (`bb due 7d`):
```text
📅 Upcoming Deadlines & Due Dates (7D)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Course                    | Assignment                          | Due Date             | Status
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
Runs global activity, calendar, course announcements, and grades concurrently:
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

### 3. Deep Assignment & Rubric Inspector (`bb assignments`)
Safely inspects assessment slideover drawers without triggering timed tests:
```bash
bb assignments --all
bb assignments IS410 --json
```

### 4. Cross-Course Deadline Aggregator (`bb due`)
Aggregates deadlines across global calendar, course gradebooks, and outlines:
```bash
bb due 7d                            # Deadlines in next 7 days
bb due 14d                           # Deadlines in next 14 days
bb due overdue                       # Overdue items
bb due 7d --exclude-completed        # Exclude graded/submitted
```

### 5. Course Announcements & Grades
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
```

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
| | `bb login --manual` | `--login` | Open browser window for SSO / Duo manual push |
| | `bb login auto` | `--login --auto` | Automated SSO + terminal Duo passcode prompt |
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
| | `bb assignments [COURSE]` | `--assignments -c <C>` | Assignment details, rubrics, points, starter files |
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
| **Help & Guides** | `bb guide <TOPIC>` | `--guide <TOPIC>` | Show detailed topic manuals (`auth`, `courses`, `schema`, etc.) |

---

## 📜 License

MIT License. Designed for UMBC students and educational research.
