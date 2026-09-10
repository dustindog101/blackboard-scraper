# Spec: Natural Unix-Style Subcommands CLI Architecture (v2)

**Issue:** [#18](https://github.com/dustindog101/blackboard-scraper/issues/18)
**Status:** In Progress (v2 — rebased on `origin/main` @ `4b0277f`, supersedes PR #19 which was based on stale `0695868` and silently dropped the quiz/assignment surface)
**Author:** AI Agent & Matt Pocock Methodologies
**Interactive RFC:** [https://www.madethis.website/s/26yfq2su/](https://www.madethis.website/s/26yfq2su/)

> Non-goal guardrail: this refactor changes **syntax only**. Every engine
> contract from ADR-0002 (HTTP REST fast-path + Playwright fallback) and
> ADR-0003 (gradable-item inspection, non-destructive info mode, attempt
> initiation guards) MUST survive. The v1 attempt lost `--assignment` and
> the assignments HTTP fast-path — this spec exists so that cannot recur.

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
- `bb grades [course]`: Gradebook inspection. Bare `bb grades` fans out to all courses.
  - Example: `bb grades`, `bb grades IS410`.
  - Flags: `-c <course>`, `--all`, `--json`, `--out <file>`, `--md`.
- `bb announcements [course]` (aliases: `announce`, `news`): Announcements reader. Bare `bb announcements` fans out to all courses.
  - Example: `bb announcements`, `bb announcements IS410`.
  - Flags: `-c <course>`, `--all`, `--json`, `--out <file>`, `--md`.
- `bb outline [course]`: Course outline tree and modules. Requires a course (fails fast before session probe).
  - Example: `bb outline IS410`, `bb outline IS410 -f "Homework"`.
  - Flags: `-f <folder>`, `--expand-all` / `--deep`, `--depth <N>`, `-i` / `--interactive`, `--type <type>`, `--filter <kw>`, `--json`, `--out <file>`, `--md`.
- `bb assignments [course]` (alias: `assign`): Deep assignment drawer and rubric inspector. **Must keep HTTP REST fast-path first** (`scrape_course_assignments_http`), Playwright browser fallback second.
  - Flags: `-c <course>`, `--all`, `--filter <kw>`, `--json`, `--out <file>`, `--md`.
- `bb assignment <target> [-c course]` (aliases: `quiz`, `asmt`, `assessment`): Single gradable-item inspector. **Must keep non-destructive info mode default + attempt guards** (`scrape_assessment_attempt_async`).
  - Example: `bb assignment "Homework 1" -c IS410`, `bb assignment _8954640_1 --json`.
  - Flags: `--start-attempt` / `--begin-attempt` (allow starting an attempt if none active), `--force-start` (confirm TIMED exams), `--json`, `--out <file>`, `--md`.
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
- `bb guide [topic]` (alias: `help`): Shows rich topic manual (`auth`, `courses`, `schema`, `telegram`, `concurrency`). With no topic, lists available topics (must NOT default to auth).

---

## 3. Backward Compatibility Contract

A lightweight pre-parsing interceptor converts legacy root flags to canonical subcommands before argument evaluation. **Complete inventory** (every historical root flag MUST map — adding a flag without a shim entry is a release-blocking bug):

| Legacy invocation | Canonical |
| :--- | :--- |
| `bb --briefing [opts]` | `bb briefing [opts]` |
| `bb --due [window] [opts]` (bare `--due` ➔ `7d`) | `bb due [window] [opts]` |
| `bb --upcoming N` | `bb due Nd` |
| `bb --auto-exp [--force]` | `bb login [--force]` |
| `bb --login [--auto\|--visible]` | `bb login [auto\|--manual]` |
| `bb --logout` | `bb logout` |
| `bb --check-session` | `bb session check` |
| `bb --session-stats` / `--session-telemetry` | `bb session stats` |
| `bb --session-info` | `bb session info` |
| `bb --grades [course opts]` | `bb grades [course opts]` |
| `bb --announcements [course opts]` | `bb announcements [course opts]` |
| `bb --outline [course opts]` | `bb outline [course opts]` |
| `bb --assignments [course opts]` | `bb assignments [course opts]` |
| `bb --assignment <T> [--start-attempt] [--force-start]` | `bb assignment <T> [--start-attempt] [--force-start]` |
| `bb --quiz/--asmt/--assessment <T> [...]` | `bb assignment <T> [...]` |
| `bb --begin-attempt` | `bb assignment ... --start-attempt` (normalized) |
| `bb --discussions [course opts]` | `bb discussions [course opts]` |
| `bb --calendar [course opts]` | `bb calendar [course opts]` |
| `bb --activity` | `bb activity` |
| `bb --profile` | `bb profile` |
| `bb --courses` / `--list-courses` | `bb courses` |
| `bb --discover [--term T]` | `bb discover [--term T]` |
| `bb --list-terms` | `bb terms` |
| `bb --find/--search <Q>` | `bb search <Q>` |
| `bb --grab/--download <I>` | `bb download <I>` |
| `bb --bot-start` / `--bot -d` / `--bot --daemon` | `bb bot start` |
| `bb --bot-stop` | `bb bot stop` |
| `bb --bot-restart` | `bb bot restart` |
| `bb --bot-status` | `bb bot status` |
| `bb --bot` (bare) | `bb bot run` |
| `bb --menubar` | `bb menubar` |
| `bb --guide <topic>` | `bb guide <topic>` |

Existing automation, cron tasks, and agents will continue executing without failure while receiving an optional one-line guidance notice on `stderr`.

### Deliberate semantic deltas (the ONLY allowed behavior changes)
1. Bare `bb grades` / `bb announcements` (no course) fan out to **all** courses instead of erroring. Rationale: matches how agents actually call them; cost is bounded (LIGHT profile, concurrent).
2. Legacy `bb --login` (prompt-mode auto) routes to smart SMS-intercept login. On non-macOS the same path falls back to terminal/Telegram 2FA entry, so no platform loses a login method.
3. `bb outline` / `bb assignments` / `bb discussions` with no matching course fail fast **before** the session probe (typos must not trigger logins).
4. `bb guide` with no topic lists topics instead of printing auth.

---

## 4. Edge Cases & Future-Proofing

1. **Single legacy flag per invocation**: the interceptor handles one legacy root flag; remaining tokens pass through positionally (e.g. `bb --due 7d --exclude-completed --json` ➔ `bb due 7d --exclude-completed --json`). Multi-command invocations (`bb --briefing --grades`) were never valid and remain invalid.
2. **First-token fast path**: if `argv[0]` is a known subcommand/alias, argv passes through untouched — later tokens that look like legacy flags (e.g. `--all`, `--json`) are real subcommand options, not legacy. The known-set SHOULD be extended with every new subcommand (correctness does not depend on it — unknown first tokens fall through to the flag scan and survive unchanged when no legacy flag matches — but keep it in sync anyway).
3. **`-f` / `-v` are per-subcommand**: `-f` = `--force` under `login`, `--folder` under `outline`; `-v`/`--visible` under `login` implies manual fallback. Document, don't "fix".
4. **Missing positionals**: `bb search` / `bb download` without the required positional fail via argparse (`error: the following arguments are required`); `bb assignment` without target fails with a guiding stderr message. `bb due` with no window defaults to `7d`.
5. **Attempt safety is inviolable**: `bb assignment` MUST NOT start attempts or timers unless `--start-attempt` is passed, and MUST NOT start timed exams without `--force-start`. Any refactor touching this path requires re-verification against ADR-0003.
6. **Engine preservation checklist** (run on every CLI refactor): assignments HTTP fast-path hit returns data in <2s; `bb assignment "Homework 1" -c IS410` returns `[REST Fast-Path]`; `ruff check main.py` clean (the v1 `Tuple` import bug broke Python 3.10–3.13); full unit suite green.
7. **Adding a future command**: add (a) subparser, (b) dispatcher branch, (c) interceptor set entry + legacy mapping if replacing flags, (d) README + CLI_REFERENCE + skill docs, (e) spec row above, (f) shim + parse tests. Missing (c) or (f) blocks the release.
