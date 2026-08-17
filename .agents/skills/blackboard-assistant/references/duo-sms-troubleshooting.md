# Duo 2FA SMS Passcode Extraction & Troubleshooting

### Mechanism Overview
1. UMBC Duo login initiates an SMS text message dispatch to student's phone (`ending in 2146`).
2. macOS Messages automatically syncs SMS/iMessage texts to local database `~/Library/Messages/chat.db`.
3. `core/sms_listener.py` captures the highest existing `ROWID` before triggering the button, then polls SQLite every 0.5s for rows where `ROWID > start_rowid` and `is_from_me = 0`.
4. Extracted codes are matched against ranked regex patterns (`UMBC SMS passcode.*: (\d{7})` or standard 6-digit codes).

### Troubleshooting Steps

1. **Permission / Full Disk Access**:
   - If `chat.db` raises `PermissionDenied` or `Operation not permitted`:
     - Ensure Terminal / Python has Full Disk Access in **macOS System Settings → Privacy & Security → Full Disk Access**.
     - `core/sms_listener.py` automatically attempts fallback via `/opt/homebrew/bin/imsg`.

2. **Duo Prompt Timeout**:
   - Default timeout is **90 seconds**.
   - If cellular SMS is delayed, the dual-channel queue accepts manual entry via terminal prompt or Telegram reply (`@blackboardscrapbot`).

3. **Wrong Course Detection**:
   - If past courses are detected, run `python3 main.py --discover` to refresh term isolation for **Fall 2026**.
