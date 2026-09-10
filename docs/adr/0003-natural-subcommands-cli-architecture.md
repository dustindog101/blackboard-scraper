# Natural Subcommands CLI Architecture

## Context
Blackboard Scraper previously modeled all CLI operations as flags on the root executable (e.g. `bb --login`, `bb --briefing`, `bb --due 7d`, `bb --grades`). This deviated from standard POSIX/GNU/Unix command ergonomics (exemplified by `git`, `gh`, `docker`, and `cargo`) where primary operations are subcommands, and flags (`--`) are reserved for options and modifiers. Additionally, authentication was fragmented into cryptic developer flags like `--auto-exp`, daemon management required remembering four distinct global flags (`--bot-status`, `--bot-stop`, etc.), and single-course queries required mandatory `-c` flags.

## Decision
We refactor the CLI parser into a hierarchical, subcommand-based architecture:
1. **First-class Subcommands**: Actions are verbs/nouns (`bb briefing`, `bb due`, `bb grades`, `bb outline`, `bb bot`, `bb session`).
2. **Smart Zero-Touch Login (`bb login`)**: Plain `bb login` defaults to automated headless SSO login with real-time macOS SMS 2FA interception (<3ms). We retire the cryptic `--auto-exp` flag. We provide `--manual` (or `manual`) for visible browser fallback, and accept both `bb login auto` and `bb login --auto` for user convenience.
3. **Positional Parameter Conveniences**: Common arguments like course codes (`bb outline IS410`), deadline windows (`bb due 14d`, defaulting to `7d` if omitted), search queries (`bb search "syllabus"`), and download targets (`bb download "file.pdf"`) can be passed directly as positional arguments.
4. **Hierarchical Resource Grouping**: Daemon controls are consolidated under `bb bot [start|stop|status|restart|run]`, and session operations under `bb session [check|stats|info]` (with `bb check` as a top-level shortcut).
5. **Transparent Backward Compatibility**: A pre-dispatch shim intercepts legacy root flags (e.g. `bb --due 7d`, `bb --auto-exp`), routes them to the new subcommands, and emits a friendly one-line migration tip without interrupting execution.

## Consequences
CLI ergonomics become intuitive, memorable, and aligned with modern command-line standards. Daily workflows require less typing and zero jargon. Automated scripts, cron jobs, menubar integrations, and agent workflows remain 100% backward compatible without breaking changes.
