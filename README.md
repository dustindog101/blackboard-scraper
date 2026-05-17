# Blackboard Scraper

Automated, headless scraper for **UMBC Blackboard Ultra**. Uses Playwright (Chromium) to maintain a persistent session via Google Workspace SSO. Scrapes announcements, grades, calendar due dates, discussions, activity streams, and user profiles — outputting JSON and/or Markdown.

Built for integration with the [Daily Helper Hub](https://github.com/dustindog101/daily-helper-hub) but works standalone.

---

## Features

- **Persistent SSO session** — login once via UMBC WebAuth + Duo 2FA; reuse headlessly for weeks.
- **Experimental auto-login** — fully automated SSO + Duo SMS code interception via `imsg` (macOS).
- **All core scrapers:**
  - Announcements (per-course or all)
  - Grades (per-course or all)
  - Calendar due dates (infinite-scroll support)
  - Discussion boards (posts, participants, titles-only)
  - Activity stream (global feed)
  - User profile
- **Composite briefing** — runs all core scrapers in sequence with a single command.
- **JSON export** — structured envelope format, integrable with external tools/agents.
- **Markdown output** — human-readable per-course and per-scraper files.
- **Headless by default**; use `--visible` to debug.

---

## Prerequisites

- Python 3.10+
- macOS (for auto-login via `imsg`; manual login works on any OS)

---

## Setup

```bash
cd tools/blackboard-scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

---

## Quick Start

### Daily Briefing (recommended for AI agents)

```bash
python3 main.py --briefing
```

Default output: `output/exports/blackboard_export.json` (JSON).
Add `--md` to also write Markdown files.

### First-Time Login

```bash
python3 main.py --login
```

A browser window opens. Complete UMBC WebAuth + Duo 2FA. Once the Blackboard dashboard loads, the session is saved to `.session/` for future headless use.

### Check Session

```bash
python3 main.py --check-session
python3 main.py --session-info
```

---

## Session Management

### Manual Login

```bash
python3 main.py --login              # skip if session is valid
python3 main.py --login --force      # force re-login
python3 main.py --login --username u@umbc.edu --password p   # auto-fill credentials
```

### Automated Login (Experimental)

Requires the [`imsg` CLI](https://github.com/nicholasgasior/imsg) (macOS only) with Full Disk Access granted to your terminal for SMS interception.

```bash
python3 main.py --login --auto
python3 main.py --login --auto --username u@umbc.edu --password p
```

Credentials can also be stored in `config.json`:

```json
{
  "courses": { ... },
  "auto_login": {
    "username": "u@umbc.edu",
    "password": "your-password"
  }
}
```

### CDP (Chrome DevTools Protocol)

Connect to an already-running browser instead of launching a new one:

```bash
python3 main.py --briefing --cdp http://localhost:9222
```

---

## Course Discovery

```bash
python3 main.py --discover-courses   # scrape active courses → config.json
python3 main.py --courses            # list configured courses
```

---

## Scraper Commands

### Profile

```bash
python3 main.py --profile            # print to terminal
python3 main.py --profile --md       # print + save to output/profile.md
```

### Announcements

```bash
python3 main.py --announcements -c _100001_1           # single course
python3 main.py --announcements --all                  # all courses
python3 main.py --announcements --all --md             # save markdown
```

### Grades

```bash
python3 main.py --grades -c _100001_1
python3 main.py --grades --all
python3 main.py --grades --all --out grades.json
```

### Discussions

```bash
python3 main.py --discussions -c _100001_1
python3 main.py --discussions -c _100001_1 --titles-only
python3 main.py --discussions -c _100001_1 --max-posts 0
python3 main.py --discussions --all --posts-only
```

### Calendar (Due Dates)

```bash
python3 main.py --calendar
python3 main.py --calendar --out calendar.json
```

### Activity Stream

```bash
python3 main.py --activity
python3 main.py --activity --raw               # raw scraper output
```

---

## Output

### JSON Export Format

The default briefing export is a JSON envelope:

```json
{
  "source": "blackboard-scraper",
  "generated_at": 1712345678,
  "items": [
    {
      "kind": "announcement",
      "course_id": "_100001_1",
      "course_name": "CIS 101 Introduction to Computing",
      "title": "Exam 2 Review Session",
      "notes": "We will hold a review session...",
      "due_at": 1712345678,
      "source_ref": "announcement:_100001_1:Exam 2 Review Session",
      "group_name": "School",
      "priority": 3,
      "is_starred": true,
      "metadata": {
        "posted": "Apr 10, 2026 2:30 PM",
        "unread": "true"
      }
    }
  ]
}
```

Item kinds: `announcement`, `grade`, `calendar_due`, `discussion`, `activity`.

### Output Directory Structure

```
output/
├── exports/
│   └── blackboard_export.json      # machine-readable (--briefing default)
├── briefing.md                     # comprehensive markdown digest
├── activity/
│   └── stream.md
├── calendar/
│   └── due_dates.md
├── announcements/
│   ├── _100001_1.md
│   └── ...
└── grades/
    ├── _100001_1.md
    └── ...
```

### Output Flags

| Flag | Description |
|------|-------------|
| `--out FILE` | Write JSON to FILE instead of stdout |
| `--md` | Also save markdown files to `output/` |
| `--raw` | Output raw scraper dicts (pre-transform) |
| `--compact` | Minified JSON |
| `--source NAME` | Value for JSON `source` field (default: `blackboard-scraper`) |
| `--group NAME` | Group label for exported items (default: `School`) |

---

## Project Structure

```
blackboard-scraper/
├── main.py                  # CLI entry point & orchestrator
├── config.json              # course ID → name mappings
├── requirements.txt         # playwright
├── .gitignore               # ignores .session/
├── core/
│   ├── config.py            # paths, course loader
│   ├── session.py           # SSO login, auto-login, session persistence
│   ├── export_json.py       # JSON envelope builder
│   └── output.py            # output directory helpers
├── scrapers/
│   ├── base.py              # shared navigation / parsing utilities
│   ├── activity.py          # global activity stream
│   ├── announcements.py     # course announcements
│   ├── briefing.py          # composite briefing orchestrator
│   ├── calendar.py          # due dates (infinite scroll)
│   ├── discussions.py       # discussion boards
│   ├── grades.py            # gradebooks
│   └── profile.py           # user profile
├── docs/
│   └── CLI_REFERENCE.md     # full CLI reference
└── output/                  # generated output (gitignored)
```

---

## Integration: Daily Helper Hub

This scraper is designed to feed into the [Daily Helper Hub](https://github.com/dustindog101/daily-helper-hub). The hub's Blackboard connector (`daily-helper-hub/connectors/blackboard/`) reads the markdown output and POSTs it to the hub's ingest API.

```bash
# In the daily-helper-hub directory:
python3 connectors/blackboard/push_from_markdown.py
```

---

## Debugging

Run any command with `--visible` to show the browser window:

```bash
python3 main.py --calendar --visible
```

This is useful when Blackboard's UI changes or Duo prompts unexpectedly appear.

### Session Issues

```bash
python3 main.py --check-session --visible   # run check with visible browser
python3 main.py --check-session --debug     # verbose session diagnostics
```

---

## Full CLI Reference

See [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md) for a complete breakdown of every flag and argument.

---

## License

MIT
