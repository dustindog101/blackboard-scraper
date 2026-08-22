---
name: blackboard-assistant
description: Comprehensive operational runbook and automation skill for UMBC Blackboard Ultra. Use whenever the user asks to scrape or check Blackboard, view grades, check upcoming due dates/calendar, fetch announcements, run daily briefings, search course outlines/syllabi, auto-discover active semester courses, manage the Telegram bot daemon (@blackboardscrapbot), monitor session health/lifespan telemetry, or perform automated SSO logins with macOS SMS 2FA extraction.
---

# 🎓 UMBC Blackboard Ultra Assistant & Scraper Skill

This skill teaches agents how to operate, debug, and query the **UMBC Blackboard Ultra Scraper & Academic Assistant** toolsuite.

---

## ⚡ Quick Reference: Most Common Commands

Commands can be run globally via **`bb`**, **`blackboard`**, or **`bbscraper`** from any terminal directory (or `./.venv/bin/python main.py <flags>` from the repo root).

| Goal | CLI Command | Output / Behavior |
| :--- | :--- | :--- |
| **Daily Academic Briefing** | `bb --briefing` | Parallel scrape across all enrolled courses (Deadlines + Grades + Announcements) |
| **Upcoming Deadlines** | `bb --due 7d` | Scrapes global calendar and course activity stream |
| **Latest Grades** | `bb --grades` | Fetches grades, letter grades, and instructor feedback |
| **Recent Announcements** | `bb --announcements` | Retrieves latest course-wide announcements |
| **Course Outline (Shallow)** | `bb --outline -c MATH215` | Default shallow view with folder item counts & IDs |
| **Selective Folder Expansion** | `bb --outline -c MATH215 -f "Homework"` | Selectively expands target folder by name or ID |
| **Full Outline Tree** | `bb --outline -c MATH215 --expand-all` | Full recursive tree with all subfolders expanded |
| **Interactive Folder Explorer** | `bb --outline -c MATH215 -i` | Interactive terminal menu to browse & expand folders |
| **Clean Outline JSON** | `bb --outline -c MATH215 --json` | Compact, streamlined JSON without bloated empty fields |
| **Download File / Note** | `bb --download "Worksheet_1.pdf"` | Auto-discovers course and downloads file directly to disk |
| **Download by Item ID** | `bb --download _8825690_1` | Downloads specific Blackboard item/notebook by exact ID |
| **Active Course Discovery** | `bb --discover` | Intelligently isolates current active term (Fall 2026) in <200ms |
| **Session Health Probe** | `bb --check-session` | Ultra-fast HTTP REST API probe (<150ms) |
| **Session Lifespan Stats** | `bb --session-stats` | Displays telemetry, rolling average lifespan, and auto-refresh timing |
| **Automated 2FA Login** | `bb --auto-exp` | Full SSO login with real-time macOS SMS Duo code interception |
| **Telegram Bot Status** | `bb --bot-status` | Inspects bot daemon PID, RSS memory, and session status |
| **Restart Bot Daemon** | `bb --bot-restart` | Gracefully reloads Telegram daemon |
| **Launch Menubar App** | `bb --menubar` | Starts native macOS status bar app (`🎓 BB 🟢`) |

---

## 🧭 Agent Decision Tree: Handling User Prompts

### 1. "What do I have due this week?" / "Check my deadlines"
1. Verify session: `bb --check-session`.
2. If expired: run `bb --auto-exp` to refresh session with zero typing.
3. Run: `bb --due 7d --json` (or `bb --briefing`).
4. Format output nicely with Course Name, Assignment Title, Due Date, and Points.

### 2. "Check my grades" / "Did any new grades post?"
1. Run `bb --grades`.
2. Report each course's current running average, recently graded items, and feedback.

### 3. "Log me in" / "Refresh my Blackboard session"
1. Run `bb --auto-exp --force`.
2. The engine will:
   - Load student credentials from `config.json` (`BH69617`).
   - Fill username and password on UMBC SSO portal.
   - Select Duo "Text message passcode".
   - Intercept the incoming Duo text code directly from macOS Messages (`~/Library/Messages/chat.db`) via monotonic ROWID delta tracking in <3ms.
   - Submit the passcode and save fresh session cookies.

### 4. "Search for [Topic] in my classes" (e.g. "Find the syllabus for database")
1. Run `bb --search "Syllabus"`.
2. Returns matching document links, descriptions, and parent folder paths.

### 5. "Download [File / Note / Worksheet]"
1. Run `bb --download "<file_name_or_id>"` (e.g. `bb --download "Math215_Worksheet_1.pdf"` or `bb --download "Chapter01.ipynb"`).
2. The downloader will automatically locate the correct course and save the file into `downloads/<CourseName>/`.

### 6. "Which courses am I enrolled in?"
1. Run `bb --courses`.
2. If courses appear outdated or user changed semesters, run `bb --discover` to auto-detect the current semester.

---

## 🏗️ Architecture & Core Components

```text
tools/blackboard-scraper/
├── main.py                     # Primary CLI router & async entrypoint
├── menubar.py / ui/menubar.py  # Native macOS Menubar app (rumps)
├── config.json                 # Enrolled active courses & auto_login credentials
├── .session/                   # Cookies, session metadata, bot.log & PID locks
├── core/
│   ├── api.py                  # High-speed REST API client (<150ms HTTP probes)
│   ├── session.py              # Playwright SSO login & session lifecycle
│   ├── session_tracker.py      # Session lifespan telemetry & rolling analytics
│   ├── sms_listener.py         # macOS Messages SQLite 2FA passcode extractor
│   ├── course_discovery.py     # Academic term intelligence & course filtering
│   ├── async_engine.py         # Playwright async pool & concurrency manager
│   ├── route_optimizer.py      # Resource blocker (saves 70% bandwidth & RAM)
│   └── diff_tracker.py         # Content hashing for instant change detection
├── scrapers/
│   ├── briefing.py             # Global aggregator (due dates + grades + news)
│   ├── calendar.py             # Calendar due date scraper
│   ├── grades.py               # Ultra gradebook scraper
│   ├── announcements.py        # Course announcements extractor
│   ├── outline.py              # Nested course tree explorer
│   └── search.py               # Parallel course keyword search
└── telegram/
    ├── bot.py                  # Long-polling Telegram bot daemon (@blackboardscrapbot)
    ├── notifier.py             # HTML message formatter & Telegram dispatcher
    └── keyboard.py             # Inline interactive keyboards & menus
```

---

## ⚠️ Critical Gotchas & Rules for Agents

1. **Async Event Loops & Playwright**:
   - `main_async` in `main.py` runs inside `asyncio.run()`.
   - Never call synchronous Playwright (`sync_playwright()`) directly inside `main_async`. Always wrap synchronous functions in `await asyncio.to_thread(func, ...)`.
2. **Current Enrolled Courses**:
   - Current semester is **Fall 2026** (Student: `Amanuel • BH69617`).
   - Active courses: `IS 410`, `ECON 122`, `ENGL 100`, `MATH 215`, `AGNG 100`.
   - Merged parent course IDs (e.g. `_105737_1` for IS 410) contain the actual content outline; child sections are auto-deduplicated.
3. **Session Verification**:
   - Do NOT launch a full browser to check session status. Use `quick_check_session_http()` or `python3 main.py --check-session`, which verifies user authentication via REST API in `<120ms`.
4. **macOS 2FA SMS Interception**:
   - macOS Messages SQLite DB is located at `~/Library/Messages/chat.db`.
   - Always track incoming SMS using `ROWID > start_rowid` to avoid carrier timestamp drift.

---

## 📚 Detailed Reference Manuals
- [CLI Reference Guide](./references/cli-reference.md)
- [API & REST Endpoints](./references/api-endpoints.md)
- [Duo SMS 2FA Troubleshooting](./references/duo-sms-troubleshooting.md)
