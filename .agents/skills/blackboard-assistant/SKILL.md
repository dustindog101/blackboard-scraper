---
name: blackboard-assistant
description: Comprehensive operational runbook and automation skill for UMBC Blackboard Ultra. Use whenever the user asks to scrape or check Blackboard, view grades, check upcoming due dates/calendar, fetch announcements, run daily briefings, search course outlines/syllabi, auto-discover active semester courses, manage the Telegram bot daemon (@blackboardscrapbot), monitor session health/lifespan telemetry, or perform automated SSO logins with macOS SMS 2FA extraction.
---

# 🎓 UMBC Blackboard Ultra Assistant & Scraper Skill

This skill teaches agents how to operate, debug, and query the **UMBC Blackboard Ultra Scraper & Academic Assistant** toolsuite.

---

## ⚡ Quick Reference: Most Common Commands

Always run commands with the local virtual environment: `./.venv/bin/python main.py <flags>` from `/Users/king/Desktop/school files/tools/blackboard-scraper`.

| Goal | CLI Command | Output / Behavior |
| :--- | :--- | :--- |
| **Daily Academic Briefing** | `python3 main.py --briefing` | Parallel scrape across all enrolled courses (Deadlines + Grades + Announcements) |
| **Upcoming Deadlines** | `python3 main.py --due 7d` | Scrapes global calendar and course activity stream |
| **Latest Grades** | `python3 main.py --grades` | Fetches grades, letter grades, and instructor feedback |
| **Recent Announcements** | `python3 main.py --announcements` | Retrieves latest course-wide announcements |
| **Keyword Search** | `python3 main.py --search "Syllabus"` | Deep content search across course outline trees |
| **Course Outline Tree** | `python3 main.py --outline -c IS410` | Dumps complete outline folders, items, and files |
| **Active Course Discovery** | `python3 main.py --discover` | Intelligently isolates current active term (Fall 2026) in <200ms |
| **Session Health Probe** | `python3 main.py --check-session` | Ultra-fast HTTP REST API probe (<150ms) |
| **Session Lifespan Stats** | `python3 main.py --session-stats` | Displays telemetry, rolling average lifespan, and auto-refresh timing |
| **Automated 2FA Login** | `python3 main.py --auto-exp` | Full SSO login with real-time macOS SMS Duo code interception |
| **Telegram Bot Status** | `python3 main.py --bot-status` | Inspects bot daemon PID, RSS memory, and session status |
| **Restart Bot Daemon** | `python3 main.py --bot-restart` | Gracefully reloads Telegram daemon |
| **Launch Menubar App** | `python3 menubar.py` | Starts native macOS status bar app (`🎓 BB 🟢`) |

---

## 🧭 Agent Decision Tree: Handling User Prompts

### 1. "What do I have due this week?" / "Check my deadlines"
1. Verify session: `python3 main.py --check-session`.
2. If expired: run `python3 main.py --auto-exp` to refresh session with zero typing.
3. Run: `python3 main.py --due 7d --json` (or `main.py --briefing`).
4. Format output nicely with Course Name, Assignment Title, Due Date, and Points.

### 2. "Check my grades" / "Did any new grades post?"
1. Run `python3 main.py --grades`.
2. Report each course's current running average, recently graded items, and feedback.

### 3. "Log me in" / "Refresh my Blackboard session"
1. Run `python3 main.py --auto-exp --force`.
2. The engine will:
   - Load student credentials from `config.json` (`BH69617`).
   - Fill username and password on UMBC SSO portal.
   - Select Duo "Text message passcode".
   - Intercept the incoming Duo text code directly from macOS Messages (`~/Library/Messages/chat.db`) via monotonic ROWID delta tracking in <3ms.
   - Submit the passcode and save fresh session cookies.

### 4. "Search for [Topic] in my classes" (e.g. "Find the syllabus for database")
1. Run `python3 main.py --search "Syllabus"`.
2. Returns matching document links, descriptions, and parent folder paths.

### 5. "Which courses am I enrolled in?"
1. Run `python3 main.py --courses`.
2. If courses appear outdated or user changed semesters, run `python3 main.py --discover` to auto-detect the current semester.

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
