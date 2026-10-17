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
**Description:** Smart authentication entry point. By default, executes zero-touch automated SSO login with real-time macOS SMS Duo 2FA passcode interception (<3ms).
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

### `bb check` / `bb session check`
**Description:** High-speed HTTP REST API probe (<120ms) verifying session token validity without launching a browser. Returns exit code 0 if authenticated, 1 if expired.

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
**Description:** Auto-discovers all enrolled courses from the Blackboard Ultra REST API, filters for the current active semester (e.g. Fall 2026), and updates `config.json`.
- `--term <TERM>`: Target a specific term (e.g. `--term FA2026`, `--term SP2026`, or `--term all`).

### `bb terms`
**Description:** Lists all lifetime enrolled terms and courses without modifying `config.json`.

### `bb courses`
**Description:** Prints the current active courses configured in `config.json`.
- `--json`: Emits configured courses as JSON array.

---

## 📚 Academic Scrapers

### `bb briefing`
**Description:** Master aggregation command. Concurrently queries global activity stream, calendar due dates, course announcements, and gradebooks across all courses in parallel (<6s).
- `--json`: Emits complete standardized v2 JSON schema to stdout.
- `--out <file>`: Exports JSON directly to specified filepath.
- `--telegram`: Dispatches formatted summary card to configured Telegram chat.
- `--md`, `--save`: Saves Markdown report to `output/briefing.md`.

### `bb due [WINDOW]`
**Description:** Cross-source deadline aggregator combining Blackboard's global calendar items and per-course gradebooks in <200ms. Deduplicates items and applies relative window filters.
- `WINDOW`: Positional filter, e.g. `7d` (default), `14d`, `30d`, `150d`, `overdue`, `all`.
  - Example: `bb due 14d`
  - Example: `bb due overdue`
- `--exclude-completed`: Excludes assignments already submitted or graded.
- `--json`: Emits structured deadline JSON.
- Legacy: `bb --due 7d`, `bb --upcoming 10` (maps to `bb due 10d`).

### `bb calendar [COURSE]`
**Description:** High-speed HTTP REST API scraper (<150ms) for global calendar events with automatic localized date formatting. Automatically falls back to Playwright browser if REST is blocked.
- `COURSE`: Positional course code or keyword (e.g. `bb calendar IS410`).
- `-c <COURSE>`: Named course flag (e.g. `-c IS410`).
- `--json`: Emits raw JSON event items.

### `bb announcements [COURSE]`
**Description:** Fast REST API announcements extractor (<120ms per course) with HTML-to-Markdown formatting and unread status.
- `COURSE`: Positional course code or keyword (e.g. `bb announcements MATH215`).
- `-c <COURSE>`: Named course flag.
- `--all`: Scrape all configured courses in parallel.
- `--json`: Emits structured JSON.

### `bb grades [COURSE]`
**Description:** Fast REST API gradebook extractor (<150ms per course) retrieving assessment titles, points possible, earned scores, due dates, and running grades.
- `COURSE`: Positional course code or keyword (e.g. `bb grades IS410`).
- `-c <COURSE>`: Named course flag.
- `--all`: Scrape all courses in parallel.
- `--json`: Emits structured JSON.

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
List gradable items for discovery. Use `--filter <text>` to narrow the list, then `bb assignment` to inspect one. Supports `-c`, `--all` and `--json`.

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

### `bb search <QUERY>`
**Description:** Omnisearch across all courses for files, assignments, and documents matching keyword query.
- `QUERY`: Positional search string (e.g. `bb search "Syllabus"`).
- `-c <COURSE>`: Restrict search to specific course.

### `bb download <ITEM_OR_QUERY>`
**Description:** Downloads specific Blackboard content item, attachment, or search query directly to `downloads/<CourseName>/`.
- `ITEM_OR_QUERY`: Positional target (e.g. `bb download "Worksheet_1.pdf"` or `bb download _8825690_1`).
- `-c <COURSE>`: Restrict target course.
- `--out-dir <dir>`: Custom destination directory (default: `./downloads`).

### `bb activity`
**Description:** Global activity stream feed showing recent updates, grades, and announcements.

### `bb profile`
**Description:** Retrieves student profile information (<150ms).

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
