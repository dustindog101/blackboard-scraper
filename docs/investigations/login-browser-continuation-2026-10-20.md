# Login/browser continuation: 2026-10-20

## Scope

Started from a clean T3 worktree and fast-forwarded to `fix/browser-login` at
`fe3a01b`. Changes are limited to login/session source, passcode-related Telegram
polling, offline tests, and this note. No original checkout or preexisting
worktree was edited. No push, PR, main merge, bot daemon, academic write, or
assessment attempt was performed.

Protected configuration was read without printing it. Live browser work used
this worktree's newly created application profile. Only protected auth artifacts
were copied from the existing application session; the original profile was not
reset or deleted. Diagnostic output excluded credentials, cookie values,
passcodes, identity fields, page bodies, and SAML query strings.

## Reproduced defects and fixes

- Unknown HTTP state was reduced to a Boolean in automatic login. Both session
  guards also fell through after an unknown HTTPS refresh. They now stop without
  browser/Duo. Both guards accept optional `checked_state`, skipping a duplicate
  initial probe; the async login cancellation seam remains intact.
- Refresh verification treated API 429/5xx as needing login, and a second probe
  after committing the cache could turn a successful refresh into login fallback.
  Verification is tri-state, precedes persistence, and is the final network check.
  Cookie merging now honors server deletions instead of resurrecting old entries.
- A cancelled passcode race still started sources, and cooperative readers could
  overlap the next attempt. Cancelled races start none; cancellation is signalled
  and cooperative source cleanup is joined under a shared 0.5-second bound.
- Telegram discarded an immediate reply because its initial update offset was
  fetched after prompting. It also returned a code after cancellation during an
  HTTP request. Offset initialization now precedes the prompt, cancelled polls
  publish nothing, and rejected replies are excluded while polling continues.
- A visible `Duo Push` menu option was never selected. The fixture now succeeds.
  When SMS is absent, explicit code/file input selects an available mobile-code
  option before push. Mobile-code prompts no longer claim that a text was sent.
- PowerShell UTF-8 BOM files were consumed and rejected. The reader accepts
  `utf-8-sig`; invalid, stale, and previously rejected BOM input still fails.
- Manual login replaced the cache on an unverified Ultra URL. It now uses HTTP
  verification and atomic persistence, with context cleanup on failure. The TUI
  manual fallback also avoids reacquiring the lock already held by auto-login.
- Session checks now say identity was verified, without printing identifiers or
  claiming a fixed latency.

The regressions above were run against the baseline behavior before their fixes;
Telegram and push checks used baseline functions loaded in memory, leaving the
worktree intact. Browser fixtures also cover mid-flow BOM code-file delivery,
code-file cancellation, mobile passcode selection, and Telegram when Messages
is unavailable. Test defaults isolate application storage and disable live
Messages/IdP access.

## Post-Duo timeout investigation

Two fresh Mac SMS requests were made, one per run, with automatic resends blocked
by the diagnostic harness. No further sends were made.

1. Baseline `fe3a01b`, fresh application browser profile: explicit SMS request at
   6.02 s; Duo acceptance at 15.57 s; trust click at 15.60 s; login success at
   19.21 s; HTTP valid at 19.27 s. Login lock released.
2. Continuation before the snapshot-probe fix: request at 5.49 s; acceptance at
   10.66 s; trust click at 10.67 s; Blackboard navigation at 12.82 s. Post-Duo
   verification timed out and login returned false at 70.99 s. Previous saved
   cookies still verified at 71.06 s. Login lock released.

Thus forced localStorage cleanup did not prevent the timeout. It is not an
established explanation.

The proposed API-path shadow-cookie mechanism **was reproduced locally** using a
real browser and local HTTP server. Before the simulated SAML response, an API
401 sets a path-specific unauthenticated session cookie through
`BrowserContext.request`, which shares the browser jar. A later valid root
cookie is shadowed, and the original waiter times out after reaching the final
page. The corrected verifier sends a filtered cookie snapshot through the
existing REST probe, accepts no response cookies, and passes that fixture. It
never deletes a browser cookie.

Live evidence does **not** establish that this caused the real timeout. An
inspection after browser restart found no usable session cookie in the failed
profile; isolated HTTP comparisons returned 401 both with all remaining cookies
and with the API-path shadow excluded. The preserved working cache returned 200
with and without that shadow. Browser restart loses session-scoped state, and an
earlier read-only diagnostic had itself used the old shared request interface;
these are limits on reconstructing the failed in-flight jar.

## Verification and remaining limits

- Baseline full offline suite: 354 passed, 1 skipped, 97 subtests.
- Final full offline suite: 377 passed, 1 skipped, 110 subtests in 31.99 s
  (macOS, Python 3.14.5, installed project environment).
- Ruff and whitespace checks: passed.
- Final-source HTTPS refresh using a separate protected copy: refreshed in
  0.92 s; persisted HTTP identity matched a configured login alias.
- Final-source snapshot verifier using a temporary browser with the preserved
  valid cache: verified in 0.07 s; browser cookies unchanged; intended identity
  matched via the protected config alias, without printing it.
- Live `main.py check`: active, redacted output; 0.05 s process duration.

The two-send allowance was exhausted before the snapshot-probe change. There is
no fresh live Duo acceptance for that final change. The real timeout's cause
remains unproved despite the demonstrated local mechanism. Telegram, push,
mobile-only passcodes, manual login, and code-file behavior were validated
synthetically, not through additional live sends. Windows execution belongs to
the parent session's Windows agent; this note makes no final Windows acceptance
claim. In-flight Telegram polling requests retain their existing network timeouts;
a cancelled poll cannot emit a code or start another poll. Prompt sends and
optional status notifications retain the notifier's existing HTTP retries and
daemon-thread behavior; Python cannot interrupt an already running HTTP call.
