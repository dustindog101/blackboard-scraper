# Blackboard Scraper CLI Complete Reference

### Authentication & Sessions
```bash
# Verify session token validity (<150ms HTTP probe)
python3 main.py --check-session

# View session creation timestamp and usage stats
python3 main.py --session-info

# View detailed session lifespan telemetry & rolling stats
python3 main.py --session-stats

# Fully automated login with real-time macOS SMS Duo 2FA extraction
python3 main.py --auto-exp

# Force clean re-login with automated SMS 2FA extraction
python3 main.py --auto-exp --force

# Manual browser login with visible window
python3 main.py --login --visible

# Logout (clears cached cookies and session metadata)
python3 main.py --logout
```

### Academic Scrapers
```bash
# Run daily briefing across all enrolled courses
python3 main.py --briefing

# Check upcoming deadlines (default: 7 days)
python3 main.py --due 7d
python3 main.py --due 14d --json

# Check latest grades across all courses
python3 main.py --grades
python3 main.py --grades -c IS410

# Check announcements across all courses
python3 main.py --announcements

# Search course content / syllabus
python3 main.py --search "Syllabus"
python3 main.py --search "Midterm Exam" -c ECON122

# Inspect course outline (shallow summary by default with folder item counts)
python3 main.py --outline -c IS410

# Selectively expand a specific folder by name or ID
python3 main.py --outline -c IS410 -f "Homework"
python3 main.py --outline -c IS410 -f _105740_1

# Inspect full recursive tree (all folders expanded)
python3 main.py --outline -c IS410 --expand-all

# Limit tree depth
python3 main.py --outline -c IS410 --depth 2

# Launch interactive terminal folder explorer
python3 main.py --outline -c IS410 -i
```

### Course Discovery & Term Isolation
```bash
# Auto-discover and save current active semester courses
python3 main.py --discover

# List all lifetime enrolled academic terms and courses
python3 main.py --list-terms

# Filter discovery to a specific term
python3 main.py --discover --term FA2026

# List currently configured courses in config.json
python3 main.py --courses
```

### Background Telegram Bot Daemon
```bash
# Start background Telegram bot daemon
python3 main.py --bot-start

# Stop running bot daemon
python3 main.py --bot-stop

# Restart bot daemon (broadcasts rich startup card)
python3 main.py --bot-restart

# Check daemon health, PID, and RSS memory
python3 main.py --bot-status

# Run bot directly in foreground (for debugging)
python3 main.py --bot
```
