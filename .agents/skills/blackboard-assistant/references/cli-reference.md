# Blackboard Scraper CLI Complete Reference

Commands can be invoked globally via `bb`, `blackboard`, `bbscraper`, or directly via `python3 main.py <command>`. Legacy `--flags` are fully backward-compatible.

### Authentication & Sessions
```bash
# Verify session token validity (<150ms HTTP probe)
bb check
bb session check

# View session creation timestamp and usage stats
bb session info

# View detailed session lifespan telemetry & rolling stats
bb session stats

# Fully automated login with real-time macOS SMS Duo 2FA extraction
bb login
bb login auto

# Force clean re-login with automated SMS 2FA extraction
bb login --force

# Manual browser login with visible window
bb login --manual

# Logout (clears cached cookies and session metadata)
bb logout
```

### Academic Scrapers
```bash
# Run daily briefing across all enrolled courses
bb briefing

# Check upcoming deadlines (default: 7 days)
bb due 7d
bb due 14d --json
bb due overdue

# Check latest grades across all courses
bb grades
bb grades IS410

# Check announcements across all courses
bb announcements
bb announcements ECON122

# Search course content / syllabus
bb search "Syllabus"
bb search "Midterm Exam" -c ECON122

# Inspect course outline (shallow summary by default with folder item counts)
bb outline IS410

# Selectively expand a specific folder by name or ID
bb outline IS410 -f "Homework"
bb outline IS410 -f _105740_1

# Inspect full recursive tree (all folders expanded)
bb outline IS410 --expand-all

# Limit tree depth
bb outline IS410 --depth 2

# Launch interactive terminal folder explorer
bb outline IS410 -i

# Download course file or document
bb download "Worksheet_1.pdf"
```

### Course Discovery & Term Isolation
```bash
# Auto-discover and save current active semester courses
bb discover

# List all lifetime enrolled academic terms and courses
bb terms

# Filter discovery to a specific term
bb discover --term FA2026

# List currently configured courses in config.json
bb courses
```

### Background Telegram Bot Daemon
```bash
# Start background Telegram bot daemon
bb bot start

# Stop running bot daemon
bb bot stop

# Restart bot daemon (broadcasts rich startup card)
bb bot restart

# Check daemon health, PID, and RSS memory
bb bot status

# Run bot directly in foreground (for debugging)
bb bot
```

