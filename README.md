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
- [⚡ Smart Adaptive Concurrency Engine](#-smart-adaptive-concurrency-engine)
- [🤖 Modular Telegram Bot & Alerts (Optional)](#-modular-telegram-bot--alerts-optional)
- [📋 Complete CLI Flag Reference](#-complete-cli-flag-reference)

---

## ⚡ Core Architectural Principles

1. **Clean CLI Output by Default**: All commands format structured, beautiful output directly to terminal `stdout`. No unwanted Markdown or temporary files are written to disk unless explicitly requested (`--md` or `--out <file>`).
2. **Task-Aware Smart Concurrency**: The worker pool automatically adjusts concurrency ceilings based on operation complexity—shallow scraping runs fast at 6–8 parallel workers, while deep accordion drawers run safely at 2–3 workers.
3. **Closed-Course Circuit Breakers**: Closed or unavailable courses are detected in `< 120ms` via `#notification-modal-api-error`, immediately releasing worker threads.
4. **Context-Level Route Optimization**: Automatically aborts images, media, webfonts, and third-party trackers to minimize memory and accelerate DOM parsing.
5. **Zero-Pip Telegram Bot**: Full Telegram alerting and interactive bot control built using Python standard library HTTP (`urllib`), keeping the codebase lean and decoupled.

---

## 🚀 Quick Start

### 1. Installation

```bash
cd "tools/blackboard-scraper"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Authenticate (One-time)

```bash
# Automated SSO + Duo SMS passcode
python3 main.py --login --auto

# Or open visible browser window
python3 main.py --login
```

### 3. Run Commands (100% Headless)

```bash
# Get your daily school briefing
python3 main.py --briefing

# Check upcoming deadlines for the next 7 days
python3 main.py --due 7d

# View course outline and syllabi
python3 main.py --outline --all
```

---

## 🔐 Authentication & Headless Sessions

### How It Works:
- **Session Persistence**: Session cookies and local storage are saved in `.session/cookies.json` and `.session/`.
- **Long-Lived Tokens**: Sessions remain valid for **weeks to months**.
- **100% Fully Headless**: All ongoing scrapers, cron jobs, background watchers, and Telegram bot interactions run headlessly with zero browser popups or prompts.

```bash
# Check if current session is active
python3 main.py --check-session

# View session creation and last-used timestamps
python3 main.py --session-info

# Clear session cookies to logout
python3 main.py --logout
```

---

## 🖥️ Beautiful CLI Output by Default

By default, **no Markdown files are saved to disk**. Commands print directly to terminal `stdout`:

### Outline Tree View (`python3 main.py --outline -c IS410`):
```text
📚 Course Outline: IS 410 Introduction to Database Design (_105737_1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📜 [SYLLABUS] Course Syllabus & Policies [syllabus]
   └ 🔗 Download PDF: https://blackboard.umbc.edu/.../Syllabus.pdf
📁 Week 1: Relational Algebra & SQL [folder]
   ├─ 📄 Lecture 1 Slides [document]
   │  └ 🔗 Slides.pdf: https://blackboard.umbc.edu/.../slides.pdf
   └─ 📝 Homework 1: ER Diagrams [assignment] — (Due: Sep 15, 2026)
```

### Deadline Table View (`python3 main.py --due 7d`):
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

Target specific courses using codes, keywords, IDs, or comma-separated lists:

```bash
# 1. Target by Course Code
python3 main.py --outline -c IS410
python3 main.py --assignments -c ENGL100
python3 main.py --grades -c "ECON 122"

# 2. Target Multiple Courses (Comma-separated)
python3 main.py --outline -c IS410,ENGL100,MATH215

# 3. Target by Fuzzy Title Keyword
python3 main.py --outline -c Database
python3 main.py --grades -c Accounting

# 4. Target All Courses
python3 main.py --outline --all
python3 main.py --assignments --all
```

---

## 📚 Scraper Feature Reference

### 1. Composite Daily Briefing (`--briefing`)
Runs global activity, calendar, course announcements, and grades concurrently:
```bash
python3 main.py --briefing                          # Terminal UI
python3 main.py --briefing --json                   # JSON to stdout
python3 main.py --briefing --out briefing.json      # JSON to file
python3 main.py --briefing --telegram               # Push to Telegram
```

### 2. Course Outline & Selective Folder Explorer (`--outline`)
Traverses course outlines with smart shallow summary views, folder item counting, selective folder expansion, and interactive browsing:
```bash
python3 main.py --outline -c IS410                  # Shallow summary with folder item counts (Default)
python3 main.py --outline -c IS410 -f "Homework"     # Selectively expand specific folder by name
python3 main.py --outline -c IS410 -f _105740_1      # Selectively expand specific folder by ID
python3 main.py --outline -c IS410 --expand-all     # Deep recursive tree (all folders expanded)
python3 main.py --outline -c IS410 --depth 2        # Limit expansion to 2 depth levels
python3 main.py --outline -c IS410 -i               # Interactive terminal folder explorer menu
python3 main.py --outline --all                     # All courses
python3 main.py --outline --all --type syllabus     # Syllabi only
python3 main.py --outline --all --type assignment   # Assignments only
python3 main.py --outline --all --type document     # Lecture docs only
python3 main.py --outline --all --filter "Homework" # Search keyword
```

### 3. Deep Assignment & Rubric Inspector (`--assignments`)
Safely inspects assessment slideover drawers without triggering timed tests:
```bash
python3 main.py --assignments --all
python3 main.py --assignments -c IS410 --json
```

### 4. Cross-Course Deadline Aggregator (`--due`)
Aggregates deadlines across global calendar, course gradebooks, and outlines:
```bash
python3 main.py --due 7d                            # Deadlines in next 7 days
python3 main.py --due 14d                           # Deadlines in next 14 days
python3 main.py --due overdue                       # Overdue items
python3 main.py --due 7d --exclude-completed        # Exclude graded/submitted
```

### 5. Course Announcements & Grades
```bash
python3 main.py --announcements --all
python3 main.py --grades -c IS410
python3 main.py --grades --all --json
```

### 6. Omnisearch Across Courses (`--find`)
Search across all course titles, modules, and assignment descriptions:
```bash
python3 main.py --find "Project"
python3 main.py --find "Syllabus"
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

### 2. Start Bot Daemon
```bash
python3 main.py --bot
# or
python3 telegram_bot.py
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

## 📋 Complete CLI Flag Reference

| Category | Flag | Description |
| :--- | :--- | :--- |
| **Help & Guides** | `--guide {auth,courses,schema,telegram,concurrency}` | Show detailed topic manuals |
| **Authentication** | `--login` | Login via UMBC SSO (skips if active) |
| | `--login --auto` | Fully automated SSO + Duo text passcode login |
| | `--duo-passcode <code>` | Supply 6-digit Duo SMS code directly |
| | `--check-session` | Validate session cookies headlessly |
| | `--session-info` | Display session timestamps |
| | `--logout` | Clear session cookies |
| **Scrapers** | `--briefing` | High-speed concurrent school briefing |
| | `--due [WINDOW]` | Upcoming deadlines (`7d`, `14d`, `overdue`) |
| | `--outline` | Full course outline tree, syllabi, and files |
| | `--assignments` | Assignment details, rubrics, points, starter files |
| | `--grades` | Gradebook items and scores |
| | `--announcements` | Course announcements |
| | `--activity` | Homepage activity stream |
| | `--calendar` | Global calendar items |
| | `--find <query>` | Omnisearch across all courses |
| | `--profile` | Student profile information |
| **Selection & Filter**| `--course, -c <ID/Code>` | Target course(s) (e.g. `IS410` or `IS410,ENGL100`) |
| | `--all` | Target all configured courses |
| | `--folder, -f <query>` | Expand specific folder/module by name or ID |
| | `--expand-all, --deep` | Recursively expand all folders (full tree view) |
| | `--depth <N>` | Limit display expansion to `<N>` depth levels |
| | `--interactive, -i` | Interactive terminal folder browser menu |
| | `--type <type>` | Filter outline by type (`syllabus`, `document`, `assignment`, `folder`) |
| | `--filter <text>` | Filter items by keyword substring |
| **Output Formats** | `--json` | Output structured JSON to CLI stdout |
| | `--out <file>` | Export structured JSON directly to file |
| | `--md`, `--save` | Save Markdown reports to `output/` directory |
| | `--compact` | Minified JSON output |
| **Performance** | `--concurrency <N>` | Override dynamic worker pool size |
| | `--visible, -v` | Launch visible browser window for debugging |
| **Telegram** | `--telegram` | Send briefing/alerts to configured chat |
| | `--bot` | Launch interactive Telegram bot daemon |

---

## 📜 License

MIT License. Designed for UMBC students and educational research.
