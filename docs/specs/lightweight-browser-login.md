# Lightweight browser login draft

When an authenticated session exists, the REST Fast-Path already avoids a browser.
SSO/Duo login still needs browser automation, but it does not need a second browser
installation on machines with Chrome, Edge, or Brave. The old selector already
attempted installed browsers; the setup instructions still required downloading
Chromium, and Windows tried Chrome before Edge.

## Approaches considered

| Approach | Storage benefit | Windows / macOS behavior | Decision |
| --- | --- | --- | --- |
| Reuse an installed Chromium browser | No additional browser binary | Edge first on Windows, Chrome first on macOS; Brave and per-user installations supported | Default |
| Install full Chromium with `--no-shell` | Avoids a second headless-shell binary | Same binary handles headless automation and visible manual Duo login | Optional fallback |
| Install only the headless shell | Smaller dedicated browser | Cannot serve visible manual login / interactive Duo flows | Not the default |
| Use Safari or stock Firefox | Would avoid a download | Playwright requires its patched Firefox/WebKit builds, so it cannot automate these installed browsers | Unsupported |
| Browser-free HTTP SSO | Could remove the browser dependency entirely | Would need a separately maintained implementation of the live UMBC and Duo login flow; existing code offers no stable browser-free authentication API | Future research |
| Native webviews / Selenium | Could reuse OS components or an installed browser | Adds separate Windows/macOS integrations or driver dependencies and would replace the existing login automation | Defer |
| Attach to a running browser via CDP | Avoids starting another browser | Already available with `--cdp`; needs a deliberately debug-enabled browser and an appropriate profile | Advanced existing option |

The draft keeps Playwright's Python/driver package. Reusing an installed executable
reduces disk use and setup downloads; it does not promise lower browser RAM use.
No browser is downloaded automatically during login. Browser downloads, when
requested by the user, are cached by Playwright and are not repeated every login.
Existing caches are not deleted by this change because other projects may use them.

## Draft behavior

- One selector serves synchronous login/session import and asynchronous scraping.
- Windows: Edge, Chrome, Brave, system Chromium/Chrome Beta, cached Playwright Chromium.
- macOS/Linux: Chrome, Edge, Brave, system Chromium/Chrome Beta, cached Playwright Chromium.
- Chrome/Edge use Playwright's native channels first. Standard machine/per-user
  locations and PATH cover additional installed executables.
- The profile stays in `.session/`, separate from the user's everyday browser profile.
- Full Playwright Chromium (`channel="chromium"`) runs both headed and headless,
  allowing `python -m playwright install chromium --no-shell`. Legacy default
  Chromium/headless-shell installs remain the final fallback.
- `BLACKBOARD_BROWSER=auto|chrome|msedge|brave|chromium` optionally pins a browser.
  `BLACKBOARD_BROWSER_PATH` pins an absolute executable path and takes precedence.
  Explicit choices do not silently fall through to a different browser.
- Launch failures explain how to retry with an installed browser or an optional
  smaller Chromium download. Browser selection messages use stderr/logging.
- Playwright >=1.49 is required for Chromium's new headless channel.

## Validation

Local macOS: Ruff passed; 206 tests passed (one opt-in smoke test skipped in the
normal suite). A separate real Chrome fixture passed with an empty
`PLAYWRIGHT_BROWSERS_PATH`: form entry, redirect, cookie capture, async restart,
and visible restart, all in temporary profiles. No school credentials or live
UMBC/Duo requests were used.

CI includes Chrome on macOS and Edge on Windows with an empty Playwright cache and
no browser installation step. Each runs automatic selection and a pinned channel,
including visible login. Linux verifies the minimal full-Chromium fallback with
`--no-shell`. The existing Python 3.10/3.11/3.12 matrix on all three OSes also uses
`--no-shell` and explicitly exercises bundled Chromium session transfer.

CI results are pending. These fixtures verify browser launch, DOM automation,
and cookie persistence; they do not prove a fresh live UMBC/Duo authentication.

## Sources

- [Playwright browser channels, smaller installs, and unsupported stock Safari/Firefox](https://playwright.dev/python/docs/browsers)
- [Playwright 1.49 Chromium headless channel](https://playwright.dev/python/docs/release-notes#version-149)
