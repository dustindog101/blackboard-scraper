# Blackboard Scraper v2 (High-Speed & Concurrent)

Automated, high-performance headless scraper and school assistant for **UMBC Blackboard Ultra**. Built on Playwright Async + Chromium with persistent SSO session caching, task-aware adaptive concurrency, deep outline & syllabus extraction, assignment drawer inspection, cross-course deadline aggregation, clean terminal UI by default, and standardized JSON export.

---

## ⚡ Key Highlights (v2 Upgrades)

- **🖥️ Beautiful CLI Output by Default (No Unwanted Files)**:
  - Every command formats directly to terminal stdout with trees (`├─`, `└─`, `📁`, `📜 [SYLLABUS]`, `📄`, `📝`) and aligned ASCII tables.
  - No Markdown files are written unless `--md` or `--save` is explicitly passed.
- **📦 Standardized v2 Composite JSON Schema**:
  - Full school intelligence document via `--json` or `--out <file.json>`. Includes course metadata, syllabi, recursive outlines, assignment rubrics/points, grades, and announcements.
- **⚡ Task-Aware Smart Adaptive Concurrency**:
  - Automatically selects optimal concurrency profile (Light: 6-8 workers, Medium: 4-5 workers, Heavy: 2-3 workers).
  - Dynamically scales up on fast responses and throttles down on slow networks or timeouts.
- **🛡️ Closed-Course Circuit Breakers**:
  - Instantly detects closed course modals in `<120ms` to avoid burning idle worker cycles.
- **📁 Deep Course Outline & Syllabus Grabber (`--outline`)**:
  - Multi-level accordion expansion for folders, learning modules, syllabi, documents, and downloadable attachments across Ultra and Classic layouts.
- **📝 Safe Assignment Drawer Inspector (`--assignments`)**:
  - Safely extracts prompts, rubric weights, submission status, allowed attempts, and starter files without triggering timed tests.
- **📅 Cross-Course Due Date Aggregator (`--due 7d / 14d / overdue`)**:
  - Combines deadlines across Global Calendar, Gradebooks, and Outlines.
- **🤖 Modular Telegram Alerts & Bot Control (Optional)**:
  - Admin-secured bot daemon (`telegram_bot.py` or `python3 main.py --bot`) with zero external pip dependencies.

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

### 3. Run Daily Briefing (Prints directly to CLI stdout)
```bash
python3 main.py --briefing
```

---

## CLI Output Modes

### 1. Terminal UI (Default)
```bash
# Formatted briefing digest
python3 main.py --briefing

# Upcoming deadlines ASCII table
python3 main.py --due 7d

# Hierarchical outline tree
python3 main.py --outline --all

# Deep assignment details & rubrics
python3 main.py --assignments --all
```

### 2. Standardized JSON Output
```bash
# Output JSON directly to CLI stdout
python3 main.py --briefing --json
python3 main.py --outline --all --json
python3 main.py --due 7d --json

# Export JSON directly to a file
python3 main.py --briefing --out my_briefing.json
python3 main.py --grades --all --out grades.json
python3 main.py --assignments --all --out assignments.json

# Minified compact JSON
python3 main.py --briefing --json --compact
```

### 3. Optional Markdown Saving
```bash
# Save markdown files into output/ directory
python3 main.py --briefing --md
python3 main.py --outline --all --md
```

---

## Item Filtering & Selection

- **Filter by Item Type**:
  ```bash
  python3 main.py --outline --all --type syllabus     # Grab syllabi across courses
  python3 main.py --outline --all --type assignment   # Grab assignments
  python3 main.py --outline --all --type document     # Grab lecture notes & docs
  python3 main.py --outline --all --type folder       # Grab folders
  ```
- **Filter by Keyword**:
  ```bash
  python3 main.py --outline --all --filter "Homework"
  python3 main.py --assignments --all --filter "Project"
  ```
- **Omnisearch Across Courses**:
  ```bash
  python3 main.py --find "Syllabus"
  ```

---

## 🤖 Modular Telegram Integration (Optional)

The Telegram integration is **100% optional** and requires **zero external pip dependencies** (built using standard library HTTP). It remains completely inactive unless enabled in `config.json` or `.env`.

### 1. Configuration (`config.json`)
```json
{
  "telegram": {
    "enabled": true,
    "bot_token": "YOUR_BOT_TOKEN_FROM_BOTFATHER",
    "admin_chat_id": 123456789
  }
}
```

### 2. Interactive Telegram Bot Daemon
```bash
python3 main.py --bot
# or
python3 telegram_bot.py
```

#### Supported Bot Commands:
- `/briefing` — Run full concurrent briefing
- `/due [days]` — Upcoming deadlines (e.g. `/due 7`)
- `/grades [course]` — View latest grades
- `/announcements [course]` — View recent course announcements
- `/courses` — List configured courses and IDs
- `/check` — Test Blackboard session health
- `/watch [mins]` — Periodic background monitoring for new grades/announcements
- `/help` — Command manual

---

## License

MIT
