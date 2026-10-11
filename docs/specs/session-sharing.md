# Blackboard session sharing: minimal v1 plan

Status: implemented in an isolated feature worktree; see
`session-sharing-validation.md` for observed tests and remaining live Windows
acceptance. The primary checkout has not been updated.
Scope: move the user's existing Blackboard login between Mac and Windows.

## User workflow

Implemented commands:

```sh
# Mac, after the normal login when needed
bb session export ~/Downloads/bb-session.json
```

Copy that file to Windows using any existing file-transfer method:

```powershell
bb session import "$HOME\Downloads\bb-session.json"
bb check
bb due 7d
```

There is no encryption, passphrase, cloud service, account setup, or new
third-party dependency. The file contains login cookies, so keep it private.
Export copies the session; it does not log the Mac out.

## Keep the change small

Use the existing `.session/cookies.json` list format and `SESSION_DIR`.
Do not relocate session files, replace the authentication architecture, change
Smart Login, change OTP behavior, refactor scraper HTTP helpers, alter course
configuration, or introduce session renewal/synchronization.

Add export/import functions in `core/session_transfer.py` and extend the
existing `bb session` CLI. Keep `check`, `stats`, `info`, `telemetry`, and
legacy flag mappings compatible. Changes to browser helpers should be limited
to what import needs to synchronize the existing managed browser profile.

## File format

Plain UTF-8 JSON containing a cookie list, compatible with existing cookie
loading. Preserve cookie values and supported attributes without rewriting
domain, path, expiry, HttpOnly, Secure, or SameSite. Preserve session cookies
with expiry -1. Keep cookies applicable to `blackboard.umbc.edu`, using exact
host/parent-domain cookie matching rather than substring matching.

Do not copy the Chromium profile directory, credentials/config.json, logs,
telemetry, or downloaded school data. No format envelope, artificial import
deadline, metadata migration, or broad storage-state export is needed for v1.

Import can also accept the existing raw cookies.json list. Imported unrelated
cookie domains are excluded before any authenticated request or installation.
Cookie files are never printed to terminal output or included in Git commits.

## Export behavior

1. Read the existing saved cookies without launching a browser or logging in.
2. Check valid JSON/list/cookie structure and that Blackboard-applicable cookies
   exist. Preserve the current source session unchanged.
3. Write the selected list to the requested file using a temporary sibling and
   atomic replace. An existing destination requires `--overwrite`.
4. Report the path and cookie count only. Recommend `bb check` if the user wants
   to confirm the source login first.

Export is usable offline. It copies the saved state and does not promise that
Blackboard still accepts it; import performs that live check.

## Import behavior

1. Read the selected file, with a modest size limit, and validate cookie shape,
   fields, expiry semantics, and safe cookie strings before making requests.
   Reject malformed JSON and files with no usable Blackboard cookies.
2. Probe `/learn/api/public/v1/users/me` using the candidate cookies in memory.
   Do not overwrite the current cookie file just to test the candidate. Keep
   HTTPS verification and correctly scoped cookies; do not forward cookies to
   another origin. Show only the authenticated account, never raw responses.
3. If Blackboard rejects the session, tell the user to log in on Mac and export
   again. If the network or service is unavailable, say validation could not
   finish. Both cases leave the existing destination session unchanged. Never
   invoke auto-login or OTP as a side effect of import validation.
4. Synchronize the tool's existing managed browser cookies with the validated
   list so HTTP reads and Playwright fallback use the same session. Use the
   existing browser helpers, snapshot the previous browser cookies for rollback,
   and replace the cookie set rather than adding it to stale browser cookies.
   Verify the candidate account through the browser context as well. Do not
   inject into an external CDP/browser session. If the managed browser profile
   is busy, return a clear close-and-retry message rather than killing processes.
   A missing browser returns the existing browser-install guidance.
5. Atomically replace `.session/cookies.json` with the validated list. Import
   itself authorizes replacing the saved session; no extra account-confirmation
   wizard or `--replace` requirement. A same-file source/destination is supported
   because the input is fully read before writing.
6. Restore previous browser cookies if synchronization or file writing fails.
   Clean up staging files. Report success only after the installed cookie file
   can be read back and the managed browser context has the same account.

Imports should run with the tool's bot/menubar/browser activity stopped so a
separate login cannot race the transfer. Do not add a general cross-process
session-store framework in this version. During import, the managed Chromium
profile's existing exclusive-use behavior prevents another browser writer;
atomic file replacement prevents HTTP readers from seeing partial JSON.

Keep unrelated `.session` files and config unchanged. Import is not a new login:
existing login timestamps must not be rewritten as if Windows just completed
SSO. Broader telemetry fixes are outside v1.

A fresh Windows installation may also need its courses configured. Use the
existing `bb discover` after import when needed; do not silently copy Mac
config.json or make course discovery part of the import transaction.

## Evidence already gathered on October 10, 2026

A read-only probe loaded source cookies in memory and selected 11 cookies
applicable to Blackboard out of 36 saved cookies:

- `/users/me` returned HTTP 200 and the same account with Mac and Windows
  user-agent strings.
- A fresh, nonpersistent Chrome context seeded only with those cookies returned
  the same account and displayed authenticated Blackboard navigation markers.
- No original profile, localStorage, IndexedDB, Duo interaction, or source cookie
  file modification was required.

This supports cookie-only transfer. A Windows user-agent test on Mac is not an
actual Windows test; the finished CLI still needs real Windows acceptance.

## Focused regression tests

Use synthetic cookies and temporary session directories for automated tests:

- Export/import round trip preserves applicable cookies and attributes.
- Export does not alter source cookies, profile, metadata, or config.
- Malformed JSON, invalid cookie fields, expired/rejected sessions, and network
  failures leave the previous destination cookie file unchanged.
- Failure while writing leaves the old file intact and restores browser cookies.
- HTTP and browser fallback use the imported cookie set, including when the
  destination profile contains an older session.
- Profile-in-use errors, paths containing spaces/non-ASCII characters, and
  importing directly from a file named cookies.json work predictably.
- Existing session subcommands, login, logout, and legacy flags still work.
- Import/export output does not contain cookie values.

Run the focused suites on Mac and Windows, plus existing affected CLI/auth tests.
Extend CI with these tests rather than treating CLI help as transfer coverage.

Real acceptance for the completed feature:

1. Export on Mac and import on Windows without reentering OTP.
2. Run `bb check` and a normal read-only command such as `bb due 7d`.
3. Exercise the existing Playwright fallback and verify the same account.
4. Restart the Windows tool and repeat; verify Mac still works.
5. Try a bad/expired file with a working Windows session and confirm it survives.

These are completion gates, not results already observed. Avoid assessment
attempts/submissions while testing session transfer.

## Future work, separate from v1

Optional encrypted exports, direct device transfer, richer portable browser
state if Blackboard starts requiring it, shared session storage/locking,
renewal reminders, and automatic synchronization can be planned later.

Blackboard controls expiry and may reject a copied session on another device
or network. If that happens, report it clearly; do not promise indefinite
sharing or hide a Mac proxy behind the import command.
