# Blackboard Scraper — CLI Reference

This document provides a detailed breakdown of all available commands in the `main.py` script.

## Core Commands

### `--briefing`
**Usage:** `python3 main.py --briefing`
**Description:** The master command intended for Autonomous Agents. It sequentially runs the activity, calendar, announcements, and grades scrapers across all registered courses.
**Default Output:** `output/exports/blackboard_export.json`
**Markdown Option:** add `--output-format markdown` to keep the previous markdown behavior (`output/briefing.md` and per-scraper markdown files).

---

## Authentication & Sessions

### `--login`
**Usage:** `python3 main.py --login`
**Modifiers:**
- `--force`: Forces a new login session even if the current one is still valid.
**Description:** Opens a visible Chrome browser to allow the user to log into the UMBC Google Workspace via SSO and Duo 2FA. Once the Blackboard Dashboard is reached, the cookies are saved to `.session/` inside the project root for persistent headless use.

### `--auto` [EXPERIMENTAL]
**Usage:** `python3 main.py --login --auto`
**Modifiers:**
- `--username`, `-u <email>`: Google account email (or read from config.json; prompts if omitted)
- `--password`, `-p <pass>`: Google account password (or read from config.json; prompts if omitted)
- `--headless`: Run login attempt headlessly (risky, harder to debug if UI changes)
**Description:** Completely automates SSO login using Playwright, selects Duo text passcode, and prompts you to type the passcode into the CLI.
**⚠️  Warning:** This involves automated credential handling. Passwords in config.json are plaintext. UMBC's SSO or Duo configuration may change at any time.

### `--check-session`
**Usage:** `python3 main.py --check-session`
**Description:** Performs a silent, headless check to verify if the saved `.session/` cookies are still valid and have not been expired by the UMBC identity provider.

### `--session-info`
**Usage:** `python3 main.py --session-info`
**Description:** Reads the internal `session_metadata.json` telemetry to display when the session was originally created and when it was last utilized.

---

## Configuration

### `--discover-courses`
**Usage:** `python3 main.py --discover-courses`
**Description:** Headlessly navigates to the `/ultra/course` tab on Blackboard, scrapes all currently active courses (Current Term), formats them, and saves the mapping to `config.json`.

### `--list-courses`
**Usage:** `python3 main.py --list-courses`
**Description:** Prints the current contents of `config.json` to the terminal so you can easily verify what course strings (e.g. `_100001_1`) map to which Human-Readable Course Names.

---

## Targeted Scrapers

### `--profile`
**Usage:** `python3 main.py --profile [-w]`
**Modifiers:**
- `-w` or `--write`: Also saves the output to a markdown file instead of just printing it to the terminal.
**Description:** Scrapes the user's Blackboard profile page, extracting name, email, student username, pronouns, and privacy settings.
**Output:** Terminal (and `output/profile.md` if `-w` used).

### `--activity`
**Usage:** `python3 main.py --activity`
**Description:** Scrapes the main Activity Stream (`/ultra/stream`), gathering recent important notifications, assignment updates, and global alerts.
**Output:** `output/activity/stream.md`

### `--calendar`
**Usage:** `python3 main.py --calendar`
**Description:** Bypasses Angular lazy-loading using a custom infinite-scroll handler on the `/ultra/calendar` page to extract all upcoming "Due Dates" across all courses for the entire semester.
**Output:** `output/calendar/due_dates.md`

### `--announcements`
**Usage:** `python3 main.py --announcements -c <COURSE_ID>`
**Modifiers:**
- `--all`: Scrape announcements for all courses in `config.json`.
**Description:** Iterates through course announcement pages, parsing out titles, authors, posting dates, full HTML-converted-Markdown body content, and unread statuses.
**Output:** `output/announcements/<COURSE_ID>.md`

### `--grades`
**Usage:** `python3 main.py --grades -c <COURSE_ID>`
**Modifiers:**
- `--all`: Scrape grades for all courses in `config.json`.
**Description:** Iterates through course gradebooks, scraping MUI data tables to extract Assignment Title, Due Date, Status (Graded/Submitted/Upcoming), and the actual Score.
**Output:** `output/grades/<COURSE_ID>.md`

### `--discussions`
**Usage:** `python3 main.py --discussions -c <COURSE_ID>`
**Modifiers:**
- `--all`: Scrape discussions for all courses in `config.json`.
- `--max-post-clicks <N>`: Limit number of times "Load more" is clicked (0 for instant).
- `--max-participant-clicks <N>`: Limit participant expansion clicks.
- `--posts-only`: Skip scraping the participant side-panel.
- `--participants-only`: Skip scraping the post thread.
- `--titles-only`: Instantly pull thread titles without navigating inside threads.
**Description:** Iterates through course discussion threads, cleanly extracting Authors, Dates, and full Post Content, separate from participant data.
**Output:** `output/discussions/<COURSE_ID>.md` (or `_titles.md` if using `--titles-only`)

---

## System / Debug Flags

### `--visible`
**Usage:** Append `--visible` to any command. Example: `python3 main.py --briefing --visible`
**Description:** Disables headless mode. This forces the Playwright chromium instance to render on-screen so the user can watch the automation unfold. Crucial for debugging when Blackboard changes their UI or if Duo 2FA is randomly triggered.

---

## Output Controls

### `--output-format`
**Usage:** `--output-format json|markdown|both`
**Default:** `json`
**Description:** Controls whether the scraper writes machine-readable JSON, markdown files, or both.

### `--json-output`
**Usage:** `--json-output <path>`
**Description:** Custom path for JSON export output. Defaults to `output/exports/blackboard_export.json`.

### `--json-source`
**Usage:** `--json-source <label>`
**Description:** Sets the top-level `source` field in exported JSON.

### `--json-compact`
**Usage:** `--json-compact`
**Description:** Emits compact JSON instead of pretty-printed JSON.

### `--group-name`
**Usage:** `--group-name <name>`
**Description:** Default group label attached to exported task-like records (default: `School`).

## Output Examples

```bash
# JSON only (default)
python3 main.py --announcements --all

# Keep previous markdown-only behavior
python3 main.py --announcements --all --output-format markdown

# Emit both markdown and JSON
python3 main.py --briefing --output-format both
```
