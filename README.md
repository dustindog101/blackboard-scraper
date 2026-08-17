# Blackboard Scraper v2 (High-Speed & Concurrent)

Automated, high-performance headless scraper and school assistant for **UMBC Blackboard Ultra**. Built on Playwright Async + Chromium with persistent SSO session caching, concurrent multi-course worker pools, deep assignment extraction, due date aggregation, and optional modular Telegram alerts & bot control.

---

## ⚡ Key Highlights (v2 Upgrades)

- **11.2× Speedup via Async Concurrency**: Scrapes all course announcements and gradebooks concurrently in parallel browser tabs bounded by an async semaphore pool (`--concurrency 4`).
- **Context-Wide Route Aborting**: Strips heavy images, video, webfonts, and 3rd-party telemetry to save ~74% bandwidth and cut page load times.
- **Adaptive DOM State Synchronization**: Zero fixed sleep delays (`wait_for_timeout`); uses event-driven selector races and mutation watchers.
- **📁 Course Outline & Treeview Traversal (`--outline`)**: Recursively expands modules, folders, documents, syllabi, and links.
- **📝 Deep Assignment Inspector (`--assignments`)**: Safely opens assignment drawers to extract prompts, points possible, rubric weights, submission status, and downloadable starter files (with timed test protection).
- **📅 Cross-Course Due Date Aggregator (`--due 7d / 14d / overdue`)**: Merges deadlines across Global Calendar, Gradebooks, and Outlines.
- **🔍 Omnisearch & Grabber (`--find`, `--grab`)**: Instant keyword search across all courses and direct document extraction.
- **🤖 Modular Telegram Notifications & Interactive Bot**: Admin-secured bot daemon (`telegram_bot.py` or `python3 main.py --bot`) supporting `/briefing`, `/due`, `/grades`, `/announcements`, `/courses`, `/check`, and `/watch` with zero required external pip dependencies.

---

## Setup & Requirements

```bash
cd "tools/blackboard-scraper"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

---

## Quick Start

### 1. Authenticate Once
```bash
python3 main.py --login
```
*(Complete UMBC SSO and Duo 2FA in the visible browser window. Session cookies are cached in `.session/` for headless reuse).*

### 2. Check Session
```bash
python3 main.py --check-session
```

### 3. Run Concurrent Daily Briefing
```bash
python3 main.py --briefing --concurrency 4
```

---

## CLI Reference & Capabilities

### Core Scrapers

| Command | Description |
| :--- | :--- |
| `python3 main.py --briefing` | Concurrent daily briefing across all courses (JSON + Markdown) |
| `python3 main.py --due 7d` | Cross-course aggregated due dates for next 7 days (`14d`, `30d`, `overdue`) |
| `python3 main.py --outline --all` | Scrape complete course outlines and module trees |
| `python3 main.py --assignments --all` | Deep scrape assignments, rubrics, and instructions |
| `python3 main.py --find "Project 1"` | Omnisearch across all enrolled courses |
| `python3 main.py --grab "<item_id>"` | Download or extract details of a specific item |
| `python3 main.py --grades --all` | Scrape gradebook across all courses |
| `python3 main.py --announcements --all` | Scrape announcements across all courses |
| `python3 main.py --calendar` | Scrape global calendar |
| `python3 main.py --activity` | Scrape homepage activity stream |

### Performance & Modifiers

- `--concurrency N`: Max concurrent browser tabs (default: `4`).
- `--exclude-completed`: With `--due`: hide already submitted/graded items.
- `--visible`: Launch with visible Chromium window for debugging.
- `--cdp <URL>`: Connect to existing browser instance via Chrome DevTools Protocol.
- `--out <FILE>`: Save structured JSON envelope to custom path.
- `--raw`: Output raw Python dict structures instead of envelope.

---

## 🤖 Modular Telegram Integration (Optional)

The Telegram integration is **100% optional** and requires **zero external pip dependencies** (built using standard library HTTP). It remains completely inactive unless enabled in `config.json` or `.env`.

### 1. Configuration
In `config.json`:
```json
{
  "telegram": {
    "enabled": true,
    "bot_token": "YOUR_BOT_TOKEN_FROM_BOTFATHER",
    "admin_chat_id": 123456789,
    "notifications": {
      "daily_briefing": { "enabled": true },
      "grade_updates": { "enabled": true },
      "urgent_due_alerts": { "enabled": true }
    }
  }
}
```
*Or set via environment variables:*
- `TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."`
- `TELEGRAM_ADMIN_CHAT_ID="123456789"`
- `TELEGRAM_NOTIFY_ENABLED="true"`

### 2. Push Notifications
Send daily briefing and new grade alerts directly to Telegram:
```bash
python3 main.py --briefing --telegram
```

### 3. Interactive Telegram Bot Daemon
Run the self-contained bot daemon:
```bash
python3 main.py --bot
# or
python3 telegram_bot.py
```

#### Supported Bot Commands:
- `/briefing` — Trigger on-demand concurrent briefing and send formatted digest
- `/due [days]` — View upcoming deadlines (e.g. `/due 7`)
- `/grades [course]` — View latest grades
- `/announcements [course]` — View recent course announcements
- `/courses` — List configured courses and IDs
- `/check` — Test Blackboard session health
- `/watch [interval_mins]` — Toggle automatic background monitoring for grade/announcement changes
- `/help` — Display command manual

---

## Output Architecture

```
output/
├── briefing.md                  # Comprehensive daily briefing
├── calendar/
│   └── due_dates.md             # Aggregated deadlines
├── outlines/
│   ├── _100001_1.md             # Course outline & module hierarchy
│   └── ...
├── assignments/
│   ├── _100001_1.md             # Assignment prompts, rubrics, points
│   └── ...
├── announcements/               # Course announcements
├── grades/                      # Graded item tables
└── activity/                    # Activity feed
```

---

## License

MIT
