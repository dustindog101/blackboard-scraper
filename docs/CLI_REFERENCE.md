# Blackboard Scraper v2 — Complete CLI Reference

This document provides a comprehensive breakdown of all commands, options, and workflows supported by the Blackboard Scraper CLI.

Commands can be invoked globally via `bb`, `blackboard`, `bbscraper`, or directly via `python3 main.py <flags>`.

---

## 📑 Command Categories

- [🔐 Authentication & Sessions](#-authentication--sessions)
- [🧭 Course Discovery & Term Management](#-course-discovery--term-management)
- [📚 Academic Scrapers](#-academic-scrapers)
- [🔀 Filtering & Course Selection](#-filtering--course-selection)
- [📦 Output Formats & File Export](#-output-formats--file-export)
- [⚡ Concurrency & Browser Controls](#-concurrency--browser-controls)
- [🤖 Telegram Bot Daemon](#-telegram-bot-daemon)
- [🖥️ Native macOS Menubar App](#️-native-macos-menubar-app)
- [📖 Built-in Help Guides](#-built-in-help-guides)

---

## 🔐 Authentication & Sessions

### `bb --login`
**Description:** Interactive login via UMBC SSO. Opens a browser if needed to solve Duo 2FA manually. Once logged in, session cookies are saved to `.session/cookies.json` for months of headless scraping.
- `--force`: Force a re-login even if the active session is valid.
- `--visible`, `-v`: Display visible browser window.

### `bb --auto-exp` / `bb --login-auto-exp`
**Description:** [Recommended on macOS] Fully automated headless SSO login with real-time SMS Duo 2FA interception from macOS Messages (`chat.db`) in <3ms.
- `--force`: Force clean re-login.

### `bb --login --auto`
**Description:** Automated login where Playwright enters credentials and requests Duo SMS passcode, accepting passcode via CLI prompt or Telegram reply.
- `--username`, `-u <id>`: UMBC username / email (reads from `config.json` if omitted).
- `--password`, `-p <pass>`: UMBC password.
- `--duo-passcode <code>`: Supply 6-digit passcode directly via CLI.

### `bb --check-session`
**Description:** High-speed HTTP REST API probe (<120ms) verifying session token validity without launching a browser.

### `bb --session-info`
**Description:** Displays session creation timestamp and last-used timestamp.

### `bb --session-stats` / `bb --session-telemetry`
**Description:** Displays deep session telemetry, total lifespan analytics, rolling averages, and auto-refresh timing.

### `bb --logout`
**Description:** Clears cached cookies and session files.

---

## 🧭 Course Discovery & Term Management

### `bb --discover` / `bb --discover-courses`
**Description:** Auto-discovers all enrolled courses from the Blackboard Ultra REST API, filters for the current active semester (e.g. Fall 2026), and updates `config.json`.
- `--term <TERM>`: Target a specific term (e.g. `--term FA2026`, `--term SP2026`, or `--term all`).

### `bb --list-terms`
**Description:** Lists all lifetime enrolled terms and courses without modifying `config.json`.

### `bb --courses` / `bb --list-courses`
**Description:** Prints the current active courses configured in `config.json`.
- `--json`: Emits configured courses as JSON array.

---

## 📚 Academic Scrapers

### `bb --briefing`
**Description:** Master aggregation command. Concurrently queries global activity stream, calendar due dates, course announcements, and gradebooks across all courses in parallel (<6s).
- `--json`: Emits complete standardized v2 JSON schema.
- `--out <file>`: Exports JSON to specified filepath.
- `--telegram`: Dispatches formatted summary card to Telegram.
- `--md`, `--save`: Saves Markdown report to `output/briefing.md`.

### `bb --due [WINDOW]` / `bb --upcoming <DAYS>`
**Description:** Cross-source deadline aggregator combining Blackboard's global calendar items and per-course gradebooks in <200ms. Deduplicates items and applies relative window filters.
- `WINDOW`: `7d` (default), `14d`, `30d`, `150d`, `overdue`, `all`.
- `--upcoming <N>`: Alias for `--due <N>d`.
- `--exclude-completed`: Excludes assignments already submitted or graded.
- `--json`: Emits structured deadline JSON.

### `bb --calendar`
**Description:** High-speed HTTP REST API scraper (<150ms) for global calendar events with automatic localized date formatting. Automatically falls back to Playwright browser if REST is blocked.
- `-c <COURSE>`: Filter calendar by course.
- `--json`: Raw JSON event items.

### `bb --announcements`
**Description:** Fast REST API announcements extractor (<120ms per course) with HTML-to-Markdown formatting and unread status.
- `-c <COURSE>`: Target specific course.
- `--all`: Scrape all configured courses in parallel.
- `--json`: Emits structured JSON.

### `bb --grades`
**Description:** Fast REST API gradebook extractor (<150ms per course) retrieving assessment titles, points possible, earned scores, due dates, and running grades.
- `-c <COURSE>`: Target specific course.
- `--all`: Scrape all courses in parallel.
- `--json`: Emits structured JSON.

### `bb --outline`
**Description:** Traverses course outline hierarchy, learning modules, syllabi, documents, and attachments.
- `-c <COURSE>`: Target course.
- `--folder`, `-f <query>`: Selectively expand and display specific folder/module by name or ID.
- `--expand-all`, `--deep`: Recursively expand all folders into a complete tree view.
- `--depth <N>`: Limit tree expansion to `<N>` depth levels.
- `--interactive`, `-i`: Interactive terminal menu to browse folders on demand.
- `--type <type>`: Filter items (`syllabus`, `assignment`, `document`, `folder`, `link`, `file`).
- `--filter <text>`: Keyword search filter.
- `--json`: Emits clean, streamlined JSON outline.

### `bb --assignments`
**Description:** Deep assignment and rubric inspector. Safely inspects assessment drawers, point breakdowns, and instructions without starting timed tests.
- `-c <COURSE>`: Target course.
- `--all`: Scrape all courses.
- `--json`: Structured JSON.

### `bb --find <QUERY>` / `bb --search <QUERY>`
**Description:** Omnisearch across all courses for files, assignments, and documents matching keyword query.

### `bb --download <ITEM_ID_OR_NAME>` / `bb --grab <ITEM_ID_OR_NAME>`
**Description:** Downloads specific Blackboard content item or attachment directly to `downloads/<CourseName>/`.
- `--out-dir <dir>`: Custom destination directory (default: `./downloads`).

### `bb --profile`
**Description:** Retrieves student profile information (<150ms).

### `bb --discussions`
**Description:** Scrapes course discussion threads and author posts.

---

## 🔀 Filtering & Course Selection

| Syntax | Example | Description |
| :--- | :--- | :--- |
| **By Code** | `-c IS410` | Matches course code `IS 410` |
| **By Multiple Codes** | `-c IS410,ENGL100,MATH215` | Targets multiple specific courses |
| **By Keyword** | `-c Database` | Matches any enrolled course containing "Database" |
| **By Exact ID** | `-c _105737_1` | Targets exact Blackboard internal ID |
| **All Courses** | `--all` | Targets all configured courses in parallel |

---

## 📦 Output Formats & File Export

- `--json`: Emits standardized JSON to terminal stdout (zero disk writes).
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

- `bb --bot`: Runs interactive Telegram bot in foreground.
- `bb --bot -d`: Starts bot daemon detached in background.
- `bb --bot-status`: Checks bot daemon PID, RSS memory, and session status.
- `bb --bot-restart`: Restarts background bot daemon.
- `bb --bot-stop`: Gracefully stops background bot daemon.

---

## 🖥️ Native macOS Menubar App

- `bb --menubar`: Launches native macOS status bar menu app (`🎓 BB 🟢`).

---

## 📖 Built-in Help Guides

- `bb --guide auth`: Authentication & headless execution guide.
- `bb --guide courses`: Course selection & syntax guide.
- `bb --guide schema`: Standardized v2 JSON schemas.
- `bb --guide telegram`: Telegram bot & alert setup.
- `bb --guide concurrency`: Concurrency engine & worker pool tuning.
