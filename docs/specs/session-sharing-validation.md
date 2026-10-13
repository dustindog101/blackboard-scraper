# Session sharing validation

## Scope

Minimal plain JSON export/import, implemented on `feat/session-sharing` in an
isolated worktree. Existing login, OTP handling, session paths, scraper engines,
and course configuration are unchanged. The transfer itself adds no dependency.
Full Windows CI exposed an existing missing timezone database; `tzdata` is now
declared only for Windows in both installation dependency lists.

## Local verification on October 11, 2026

- 152 automated tests passed; one optional native macOS menubar UI test was
  skipped under the base dependency configuration used by CI (153 total).
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
- A real-browser integration test uses a local synthetic authentication service
  and exercises export/import, stale-cookie cleanup, synchronous/asynchronous
  browser restart, a Unicode file path, and rejected-import preservation.
  This test runs on every CI OS without real student cookies.

Live tests use only read-only account/deadline/browser navigation requests.
No assessment attempts or submissions are initiated. Exported live cookies and
configuration remain outside Git; no passwords or OTPs were copied for testing.
The primary checkout's session is only read to seed the isolated test session.

## Windows verification

The CI workflow runs the full base-dependency test suite on Mac, Linux, and
Windows with Python 3.10, 3.11, and 3.12.

All ten checks in CI run `38125143813` passed for feature commit `b02a991`:
lint and Mac/Linux/Windows tests on Python 3.10, 3.11, and 3.12, including the
real-browser integration test with synthetic authentication.

Real Windows SSH acceptance passed on October 12, 2026:

- Fetched feature commit `b02a991` into a separate Windows Git worktree. Created
  a working Python 3.12.10 environment with the declared dependencies; no
  changes were needed to the session-transfer implementation.
- Ran all 153 tests on the actual Windows machine: 152 passed, one optional
  Mac-only native UI test skipped.
- Exported the current Mac session with the implemented CLI and copied the
  plain JSON file over SSH. Windows import exited 0 with the verified account,
  without a campus password configured or OTP interaction.
- New CLI processes passed `bb check` repeatedly; `due 7d --json` returned
  valid JSON with exit status 0.
- Restarted both existing synchronous and asynchronous managed browsers.
  Each verified the same account and displayed authenticated Blackboard UI.
- Malformed and expired imports exited 1 and left the working destination
  cookie file byte-for-byte unchanged. The following session check stayed active.
- Windows export exited 0 and did not modify its source cookie file.
- The original Windows checkout's saved session stayed unchanged. No grades,
  cookie values, credentials, or user identifiers appear in the test report.

The optional bundled Chromium download timed out on this host. The tool's
existing system-browser fallback successfully ran all browser tests, so the
download was unnecessary. SSH over LAN was intermittent; completed checks are
reported from their final successful process results, not inferred from transport
timeouts. No live cookies were uploaded to CI or committed.

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
