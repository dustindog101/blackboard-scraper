# Natural Subcommands CLI Architecture

> Supersedes the flag-based CLI while preserving every engine contract from
> ADR-0002 (HTTP REST fast-path + Playwright fallback) and ADR-0003
> (gradable-item inspection with attempt safety). This refactor changes
> **syntax only** — no scraper engine, attempt guard, or downloader behavior
> is removed or weakened.

## Context
Blackboard Scraper previously modeled all CLI operations as flags on the root executable (e.g. `bb --login`, `bb --briefing`, `bb --due 7d`, `bb --grades`). This deviated from standard POSIX/GNU/Unix command ergonomics (exemplified by `git`, `gh`, `docker`, and `cargo`) where primary operations are subcommands, and flags (`--`) are reserved for options and modifiers. Additionally, authentication was fragmented into cryptic developer flags like `--auto-exp`, daemon management required remembering four distinct global flags (`--bot-status`, `--bot-stop`, etc.), and single-course queries required mandatory `-c` flags.

## Decision
We refactor the CLI parser into a hierarchical, subcommand-based architecture:
1. **First-class Subcommands**: Actions are verbs/nouns (`bb briefing`, `bb due`, `bb grades`, `bb outline`, `bb bot`, `bb session`).
2. **Smart Zero-Touch Login (`bb login`)**: Plain `bb login` defaults to automated headless SSO login with real-time macOS SMS 2FA interception (<3ms). We retire the cryptic `--auto-exp` flag. We provide `--manual` (or `manual`) for visible browser fallback, and accept both `bb login auto` and `bb login --auto` for user convenience. Note: legacy `bb --login` (prompt-mode auto) now also routes to the SMS-intercept path; on non-macOS `login_auto(auto_exp=True)` falls back to terminal/Telegram 2FA entry, so behavior is preserved cross-platform.
3. **Positional Parameter Conveniences**: Common arguments like course codes (`bb outline IS410`), deadline windows (`bb due 14d`, defaulting to `7d` if omitted), search queries (`bb search "syllabus"`), download targets (`bb download "file.pdf"`), and single-assessment targets (`bb assignment "Homework 1" -c IS410`) can be passed directly as positional arguments.
4. **Hierarchical Resource Grouping**: Daemon controls are consolidated under `bb bot [start|stop|status|restart|run]`, session operations under `bb session [check|stats|info]` (with `bb check` as a top-level shortcut), course operations under `bb courses [list|discover|terms]` (with `bb discover` / `bb terms` shortcuts), and **assessment inspection under `bb assignment <target>`** (aliases `quiz`, `asmt`, `assessment`) — preserving the full ADR-0003 surface including `--start-attempt` / `--force-start` attempt guards.
5. **Transparent Backward Compatibility**: A pre-dispatch Legacy Flag Interceptor intercepts legacy root flags (e.g. `bb --due 7d`, `bb --auto-exp`, `bb --assignment _123_1 --start-attempt`), routes them to the new subcommands, and emits a friendly one-line migration tip without interrupting execution. The complete legacy inventory (including `--assignment/--quiz/--asmt/--assessment`, `--start-attempt/--begin-attempt`, `--force-start`, `--upcoming`, `--find`, `--grab`, `--bot-*`, `--session-*`, `--discover/--list-terms/--courses`, `--guide`) is covered and tested.
6. **Deliberate semantic deltas** (documented, not accidental): bare `bb grades` / `bb announcements` with no course now fan out to all courses instead of erroring; `bb outline` / `bb assignments` / `bb discussions` still fail fast when no course matches (now before the session probe, so typos don't trigger logins); `bb guide` with no topic lists topics instead of defaulting to auth.

## Consequences
CLI ergonomics become intuitive, memorable, and aligned with modern command-line standards. Daily workflows require less typing and zero jargon. Automated scripts, cron jobs, menubar integrations, and agent workflows keep working via the Legacy Flag Interceptor; the few deliberate semantic deltas above are the only behavior changes, and each is covered by tests. Future commands follow the checklist: parser subcommand + dispatcher branch + interceptor set entry + docs + tests (see spec).
