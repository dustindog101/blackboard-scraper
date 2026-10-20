# AGENTS.md

## Agent skills

### Issue tracker

GitHub issues via `gh` CLI (`dustindog101/blackboard-scraper`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default canonical triage roles (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context (`CONTEXT.md` and `docs/adr/` at repo root). See `docs/agents/domain.md`.

### Coding standards

Read `docs/agents/coding-standards.md` before changing code. It covers REST-first fetching, the shared
installed-browser launcher, output rules (text, `--json`, timezones), speed budgets and what "done" means.
