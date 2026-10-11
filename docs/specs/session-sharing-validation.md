# Session sharing validation

## Scope

Minimal plain JSON export/import, implemented on `feat/session-sharing` in an
isolated worktree. Existing login, OTP handling, session paths, scraper engines,
and course configuration are unchanged. No new dependency is required.

## Local verification on October 11, 2026

- 150 automated tests passed; one optional native macOS menubar UI test was
  skipped under the base dependency configuration used by CI (151 total).
- All Ruff checks passed and `git diff --check` was clean.
- The installed optional menubar UI test aborts the process when run in this
  terminal environment. It is outside the transfer change and is not claimed
  as a passing native GUI check.
- Actual `session export` and `session import` CLI commands passed against
  Blackboard in an isolated session directory, without credentials or OTP.
- After import, a new CLI process reported active authentication and `due 7d
  --json` returned valid JSON.
- After the import browser closed, the existing asynchronous browser manager
  reopened the saved profile, verified the authenticated account over HTTP,
  and displayed authenticated Blackboard UI navigation.
- Actual malformed and expired-file imports exited with status 1. The working
  destination cookie file stayed byte-for-byte unchanged in both cases.
- Unit tests additionally cover stale browser cookie replacement, cookie refresh,
  identity mismatch, failed writes, browser shutdown failure, rollback, file
  paths with spaces/non-ASCII characters, and existing CLI/legacy actions.

Live tests use only read-only account/deadline/browser navigation requests.
No assessment attempts or submissions are initiated. Exported live cookies and
configuration remain outside Git; no passwords or OTPs were copied for testing.
The primary checkout's session is only read to seed the isolated test session.

## Windows verification

The CI workflow runs the full base-dependency test suite on Mac, Linux, and
Windows with Python 3.10, 3.11, and 3.12.

Real Windows SSH acceptance is pending: the configured LAN route is unavailable,
and the Windows Tailscale peer reports offline. The Mac client was reconnected
with its existing settings to try the authorized SSH connection. No Windows
session was replaced, and no live cookies have been uploaded to CI.

Before claiming complete real-device acceptance, run the finished export/import
commands on the user's reachable Windows host in an isolated checkout, verify
HTTP and browser fallback after restart, and confirm rejected imports preserve
the destination session. Windows CI does not replace this live-device check.

## Usage

```sh
# Mac
bb session export ~/Downloads/bb-session.json
```

```powershell
# Windows, after copying the file and stopping managed background jobs
bb session import "$HOME\Downloads\bb-session.json"
bb check
bb due 7d
```

Keep the plain cookie file private. Import replaces saved cookies after
validation; export does not log out the source. Blackboard still controls
session expiration. `--overwrite` applies only to replacing an export file.
