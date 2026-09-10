# Spec: Natural Unix-Style Subcommands CLI Architecture

**Issue:** [#18](https://github.com/dustindog101/blackboard-scraper/issues/18)  
**Status:** In Progress  
**Author:** AI Agent & Matt Pocock Methodologies  
**Interactive RFC:** [https://www.madethis.website/s/26yfq2su/](https://www.madethis.website/s/26yfq2su/)

---

## 1. Problem Statement

Blackboard Scraper previously modeled all commands as boolean flags on the root `bb` executable:
```bash
bb --login
bb --auto-exp
bb --briefing
bb --due 7d
bb --grades -c IS410
bb --outline -c IS410
bb --bot-start
bb --check-session
```

This caused friction:
1. **Unnatural double-dash prefixes** for primary operations.
2. **Confusing developer jargon**: `--auto-exp` was intimidating to students who didn't know what "exp" meant.
3. **Flat root flag pollution**: background daemons and session probes cluttered the global argument space.
4. **Mandatory `-c` flags** for single courses even when unambiguous.
5. **Flag-passed queries**: `bb --search "query"` instead of `bb search query`.

---

## 2. Specification & Architecture

### 2.1 Core Grammar
```text
bb [GLOBAL_FLAGS] <SUBCOMMAND> [SUB_ACTION] [POSITIONAL_ARGS] [OPTIONS]
```

### 2.2 Canonical Subcommand Structure

#### Authentication & Session Management
- `bb login`: Performs smart zero-touch automated SSO login with real-time macOS SMS 2FA extraction in <3ms.
  - `--manual`, `--visible`, `-v`: Opens visible browser window.
  - `--force`, `-f`: Force re-authentication even if active session exists.
  - `--passcode <code>`: Supply 6-digit Duo passcode directly.
  - Accepts positional aliases `bb login auto` and `bb login manual`.
- `bb logout`: Clears cached cookies and session tokens.
- `bb session check`: Fast HTTP REST session token probe (<120ms).
  - Shortcut alias: `bb check`.
- `bb session stats`: Displays rolling session lifespan telemetry and auto-refresh timing.
- `bb session info`: Displays session creation timestamp and last-used timestamp.

#### Academic Scrapers
- `bb briefing` (alias: `bb brief`): Concurrent daily overview across all enrolled courses (<6s).
  - Flags: `--json`, `--out <file>`, `--telegram`, `--md` / `--save`.
- `bb due [window]` (defaults to `7d`): Cross-source deadline aggregator.
  - Example: `bb due`, `bb due 14d`, `bb due overdue`, `bb due all`.
  - Flags: `--exclude-completed`, `--json`, `--out <file>`, `--md`.
- `bb grades [course]`: Gradebook inspection.
  - Example: `bb grades`, `bb grades IS410`.
  - Flags: `-c <course>`, `--all`, `--json`, `--out <file>`, `--md`.
- `bb announcements [course]` (aliases: `announce`, `news`): Announcements reader.
  - Example: `bb announcements`, `bb announcements IS410`.
  - Flags: `-c <course>`, `--all`, `--json`, `--out <file>`, `--md`.
- `bb outline [course]`: Course outline tree and modules.
  - Example: `bb outline IS410`, `bb outline IS410 -f "Homework"`.
  - Flags: `-f <folder>`, `--expand-all` / `--deep`, `--depth <N>`, `-i` / `--interactive`, `--type <type>`, `--filter <kw>`, `--json`, `--out <file>`, `--md`.
- `bb assignments [course]` (alias: `assign`): Deep assignment drawer and rubric inspector.
  - Flags: `-c <course>`, `--all`, `--filter <kw>`, `--json`, `--out <file>`, `--md`.
- `bb calendar [course]` (alias: `cal`): HTTP REST calendar items.
  - Flags: `-c <course>`, `--json`, `--out <file>`, `--md`.
- `bb activity`: Homepage global activity stream reader.
- `bb profile` (alias: `whoami`): Authenticated student profile information.
- `bb discussions [course]` (alias: `discuss`): Course discussion boards.

#### Content & Downloader
- `bb search <query>` (alias: `find`): Omnisearch across courses for documents, files, and assignments.
- `bb download <item_id_or_name>` (aliases: `grab`, `get`): Direct download to `downloads/<CourseName>/`.
  - Flags: `--out-dir <dir>` (default: `./downloads`).

#### Course Discovery & Academic Terms
- `bb courses`: Lists currently configured active courses.
- `bb courses discover` (alias: `bb discover`): Discovers and saves current semester courses.
  - Flags: `--term <term>` (e.g. `FA2026`, `current`, `all`).
- `bb courses terms` (alias: `bb terms`): Lists all lifetime enrolled terms.

#### Telegram Bot Daemon
- `bb bot run` (or `bb bot`): Runs bot in foreground.
- `bb bot start`: Starts bot detached in background.
- `bb bot stop`: Stops background bot daemon.
- `bb bot restart`: Gracefully restarts bot daemon.
- `bb bot status`: Inspects bot daemon PID, RSS memory, and session status.

#### System & Documentation
- `bb menubar` (alias: `app`): Starts native macOS status bar menu app.
- `bb guide <topic>` (alias: `help`): Shows rich topic manual (`auth`, `courses`, `schema`, `telegram`, `concurrency`).

---

## 3. Backward Compatibility Contract

A lightweight pre-parsing interceptor converts legacy root flags to canonical subcommands before argument evaluation:
- `bb --briefing` ➔ `bb briefing`
- `bb --due 7d` ➔ `bb due 7d`
- `bb --auto-exp` ➔ `bb login`
- `bb --check-session` ➔ `bb session check`
- `bb --bot-status` ➔ `bb bot status`

Existing automation, cron tasks, and agents will continue executing without failure while receiving an optional one-line guidance notice on `stderr`.
