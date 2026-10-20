# Smart Login: existing SSO bypassed the username form

## Scope and source identity

- Worktree: `fix/bb-login-redirect-reliability`, based on `1ae7bc3`.
- Installed `bb` targets `/Users/king/school/tools/blackboard-scraper`, whose
  uncommitted login changes were inspected, not imported wholesale or modified.
- Original failure: both plain login and automatic refresh timed out waiting for
  the username input before Duo. The original dirty implementation explicitly
  waits 10 seconds for that input after the portal click.
- A private, ignored copy of the installed browser profile reproduced the issue.
  No original profile, config, cache, launcher, or permission was changed.

## Before

The installed implementation was invoked through its existing `login_auto`
workflow on the copied profile, with Telegram disabled for this diagnostic.
Credentials and all output were kept in memory; only allowlisted diagnostics
were retained. No credential form appeared and no Duo SMS was requested.

```text
host-only navigation timeline (seconds from start):
1.04  blackboard.umbc.edu
1.36  webauth.umbc.edu
1.85  blackboard.umbc.edu
2.89  blackboard.umbc.edu
11.50 login_auto -> False
      filling UMBC username and password: Timeout 10000ms exceeded
```

A fresh isolated profile instead presented the normal visible `j_username` and
`j_password` form by 2.22 seconds. The copied installed profile used existing UMBC
SSO and returned to Blackboard with no credential form. The failing wait had
already started and never reconsidered the destination.

The clean committed baseline has different pre-login logic: it issues blind
Enter presses when no fields are visible. The new behavioral regression was run
before editing production code and failed on that baseline:

```text
python -m unittest discover -s tests -p test_login_redirects.py
Ran 1 test: FAILED
Expected keyboard.press not to have been called; Enter was called twice.
```

Thus the live 10-second failure belongs to the installed dirty implementation;
the clean baseline regression independently establishes unsafe assumptions when
SSO bypasses the form. They are not claimed as identical source versions.

## Change

`core/login_flow.py` observes portal, UMBC form, Duo, and Blackboard states under
one monotonic SSO deadline. Each poll rechecks the host and current form. It
fills only visible inputs on the trusted UMBC host, handles remembered usernames
and split username/password steps, and submits each step once. Replaced documents
and frames are re-observed. A direct SSO-to-Duo transition needs no username.

A Blackboard Ultra URL is a candidate state, not proof of authentication. The
browser's current cookies must receive HTTP 200 plus an identity from `/users/me`
without following redirects. Only then is the cookie cache replaced atomically.
Failed verification, navigation, cancellation, or a write error preserves the
previous cache. The same flow serves plain login, synchronous refresh, and
asynchronous refresh. Async callers signal cancellation to their login worker.

Errors use fixed categories and recovery guidance without page text, full URLs,
passcodes, or authentication screenshots. Authentication failure never selects a
different browser. The known profile-lock spellings now stop candidate selection
immediately; the rest of Issue #50's startup lifecycle work remains open.

Only small supporting safeguards were ported/adapted from the original dirty
changes: bounded error-text reads, ignoring informational alerts, known lock
recognition, atomic cache replacement, cancellation signaling, and CLI failure
status. SMS parsing logic, Telegram parser, browser-selector, and broad prior reliability
changes were not imported. Passcode output is redacted, and repeat/invalid codes
are rejected before submission. The SMS listener also omits code, sender, and message body from its output.
SMS tracking starts before navigation because
Duo can deliver on arrival.

The first worktree live attempt exposed a portal matcher mismatch: the actual
link ends in a period. The matcher and fixture now cover trailing punctuation.
An overlapping diagnostic correctly reported a locked worktree profile; only
the investigation's own diagnostic process and its children were stopped before
continuing. No user browser was closed and no lock file was deleted. Low disk
space interrupted creation of a duplicate diagnostic profile; only duplicate
files and browser caches created by this investigation were removed.

## After: live macOS acceptance

Commands below used this worktree's `main.py` with the original checkout's Python
environment (`/Users/king/school/tools/blackboard-scraper/.venv/bin/python`).
The worktree had a private config with Telegram disabled and its own copied
browser profile. Durations include process startup; these are single observations,
not performance distribution claims.

```text
python main.py check       -> EXPIRED
python main.py login       -> exit 0, verified existing SSO, Duo skipped, 2.99 s
python main.py check       -> ACTIVE via HTTP
python main.py due 7d      -> exit 0, 1.10 s

# Restore only the expired cookie cache in this isolated worktree.
# Retain the browser profile to exercise the real refresh path.
python main.py check       -> EXPIRED
python main.py due 7d      -> automatic login refresh, refreshed successfully,
                             exit 0, 3.74 s
python main.py check       -> ACTIVE via HTTP
python main.py due 7d --json -> exit 0, valid JSON, 5 items
```

No fresh credential or Duo SMS submission was needed. No coursework attempt was
started and no course content was changed. The original installed launcher still
uses the original dirty checkout; this PR does not deploy over that checkout.

## Regression and maintenance evidence

```text
python -m unittest discover -s tests -p 'test_login*py'
Ran 31 tests: OK
python -m unittest discover -s tests -p 'test_*.py'
Ran 226 tests: OK (macOS, Python 3.14)
ruff check .             -> All checks passed
python main.py --help    -> exit 0
git diff --check         -> clean
```

Browser fixtures intercept all requests and use synthetic credentials. Covered
cases include portal punctuation, redirect races and skipped forms, delayed
forms, hidden inputs, password-only and split forms, trusted iframe forms that
navigate the top page, authentication errors, informational alerts, unknown
hosts/layouts, unverified Ultra pages, closed pages, cancellation, and safe
timeouts. Boundary tests cover HTTP verification/disposal, preserved cache on
failure, no auth-triggered browser retry, known profile locks, rejected borrowed
browser automation, CLI status, and sync/async refresh callers.

Limits: no fresh real Duo login, native Windows run, Windows SMS/Telegram entry,
or unattended scheduled run was performed. The existing Duo interaction and SMS
parser still have separately investigated reliability work in the original dirty
checkout; these fixtures do not validate real Messages access. There is no new
permission requirement. Issue #50's remaining initialization rollback and async
driver ownership changes are not completed by this PR.

If UMBC changes its form or host, add a sanitized fixture, verify it fails, and
update the observer's state/selector contract. Unknown states stop with manual
recovery guidance rather than resubmitting credentials or changing browsers.
Rollback is a code revert; no data migration is required.
