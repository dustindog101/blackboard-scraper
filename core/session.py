import asyncio
import functools
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from getpass import getpass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout, Error as PlaywrightError
except ImportError:
    sync_playwright = None
    class PlaywrightTimeout(Exception):
        pass
    class PlaywrightError(Exception):
        pass

from core.sms_listener import check_sms_access, ensure_messages_sync, get_current_max_rowid, normalize_passcode, wait_for_duo_sms_passcode
from core.browser import BrowserProfileLockedError, launch_persistent_context
from core.login_flow import (
    AUTHENTICATED,
    LoginFlowError,
    cancelled,
    DUO_METHOD_LABELS,
    duo_methods,
    is_verified,
    wait_for_blackboard,
    wait_for_sso,
)

from core.passcodes import Source, TerminalSource, collect_passcode, file_source, immediate_source
from core.http_refresh import NEEDS_LOGIN, REFRESHED, forget_idp_storage, record_idp_storage, refresh_session_http
from core.session_state import (
    NETWORK_MESSAGE,
    UNKNOWN,
    VALID,
    LoginInProgress,
    login_lock,
    probe_session,
    record_user_agent,
)
from core.config import (
    BLACKBOARD_BASE,
    LOGIN_INDICATORS,
    SESSION_DIR,
    ensure_config_exists,
    has_auto_login_credentials,
    load_config,
    save_auto_login_credentials,
)

# ============================================================
# Browser helpers
# ============================================================


def _launch_context(pw, headless: bool = True, cdp_url: str = None):
    """Launch a persistent browser context with saved session, or connect via CDP."""
    if cdp_url:
        print(f"🔌 Connecting to existing browser at {cdp_url}...")
        browser = pw.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        return context, page

    try:
        context, _ = launch_persistent_context(pw, SESSION_DIR, headless=headless)
    except BrowserProfileLockedError as error:
        print(f"\n❌ {error}", file=sys.stderr)
        raise SystemExit(1) from None

    # Explicitly load session cookies
    cookie_file = Path(SESSION_DIR) / "cookies.json"
    if cookie_file.exists():
        try:
            cookies = json.loads(cookie_file.read_text())
            if cookies:
                context.add_cookies(cookies)
        except Exception:
            pass

    page = context.pages[0] if context.pages else context.new_page()
    # Speed up loads by blocking heavy media
    try:
        page.route("**/*.{png,jpg,jpeg,gif,avif,webp,mp4,webm,woff,woff2,ttf,svg,ico}", lambda route: route.abort())
    except Exception:
        pass
    return context, page


def _is_login_page(url: str) -> bool:
    """Check if URL indicates a login/SSO redirect or the guest login portal."""
    parsed = urlparse(url)
    url_lower = url.lower()
    if parsed.netloc == "blackboard.umbc.edu" and parsed.path in ("", "/"):
        return True
    return any(indicator in url_lower for indicator in LOGIN_INDICATORS) or "guest" in url_lower


AUTH_SELECTORS = [
    "[data-analytics-id='base.nav.navigation.logout']",
    "[data-analytics-id='base.nav.navigation.courses']",
    "base-side-menu",
    "bb-course-navigation",
    "a[href*='/ultra/institution-page']",
]

LOGIN_SELECTORS = [
    "a:has-text('Log into Blackboard via myUMBC')",
    "a:has-text('UMBC Login')",
    "a:has-text('Sign in')",
    "input[type='password']",
    "input[name='j_username']",
]

SSO_ERROR_SELECTORS = [
    ".alert.alert-danger",
    ".alert-danger",
    ".alert-error",
    "div.alert",
    "div[role='alert']",
    "#status",
    "#msg",
    ".form-element-error",
    ".errors",
    "p.error",
    "span.error",
    "div.error",
    ".error-message",
]

SSO_ERROR_PATTERNS = [
    (re.compile(r"incorrect|invalid\s+(?:user|username|password|credentials)", re.I), "INVALID_CREDENTIALS"),
    (re.compile(r"account\s+locked|too\s+many\s+(?:failed\s+)?attempts|disabled|suspended", re.I), "ACCOUNT_LOCKED"),
    (re.compile(r"password\s+(?:has\s+)?expired|reset\s+your\s+password", re.I), "PASSWORD_EXPIRED"),
    (re.compile(r"authentication\s+failed|login\s+failed|could\s+not\s+be\s+verified|cannot\s+be\s+determined", re.I), "AUTH_FAILED"),
]


def detect_sso_error(page) -> tuple[bool, str, str]:
    """
    Check if the current page displays an SSO authentication error banner or message.
    Returns: (is_error: bool, error_type: str, error_text: str)
    error_type is one of: 'INVALID_CREDENTIALS', 'ACCOUNT_LOCKED', 'PASSWORD_EXPIRED', 'AUTH_FAILED', or ''
    """
    try:
        # 1. Check specific error elements first
        for sel in SSO_ERROR_SELECTORS:
            try:
                loc = page.locator(sel)
                cnt = loc.count()
                if cnt > 0:
                    for i in range(min(cnt, 3)):
                        item = loc.nth(i)
                        if item.is_visible():
                            txt = item.inner_text(timeout=200).strip()
                            if txt:
                                for pattern, err_type in SSO_ERROR_PATTERNS:
                                    if pattern.search(txt):
                                        return True, err_type, txt
                                # Informational alerts are not authentication failures.
            except Exception:
                continue

        # 2. Check full body text if on a login/SSO URL
        url_lower = page.url.lower()
        if any(ind in url_lower for ind in LOGIN_INDICATORS):
            body_txt = page.locator("body").inner_text(timeout=200)
            for pattern, err_type in SSO_ERROR_PATTERNS:
                match = pattern.search(body_txt)
                if match:
                    lines = [ln.strip() for ln in body_txt.splitlines() if pattern.search(ln)]
                    extracted = lines[0] if lines else match.group(0)
                    return True, err_type, extracted
    except Exception:
        pass

    return False, "", ""



def _session_debug(debug: bool, msg: str):
    if debug:
        print(f"   [session-debug] {msg}")


def _matching_selectors(page, selectors) -> list[str]:
    matched = []
    try:
        for sel in selectors:
            if page.locator(sel).count() > 0:
                matched.append(sel)
    except Exception:
        return matched
    return matched


def _normalized_text(value: str | None) -> str:
    return " ".join((value or "").split())


PASSCODE_SELECTOR = "input[name='passcode'], input[id*='passcode'], input[autocomplete='one-time-code']"
TRUST_LABELS = ("Yes, this is my device", "Trust browser", "Remember this device")


def _click_exact_text_in_any_frame(page, target_text, timeout: int = 10_000, stop_when=None) -> bool:
    """Search alternative labels under one deadline, stopping at the next state."""
    targets = (target_text,) if isinstance(target_text, str) else tuple(target_text)
    deadline = time.monotonic() + timeout / 1000.0
    while True:
        if stop_when is not None and stop_when():
            return False
        for frame in page.frames:
            for target in targets:
                try:
                    words = r"\s+".join(re.escape(word) for word in target.split())
                    role_name = re.compile(r"^\s*" + words + r"(?:$|[\s.,:;!?])", re.I)
                    text_name = re.compile(r"^\s*" + words + r"[\s.,:;!?)]*$", re.I)
                    locators = [
                        frame.get_by_role("button", name=role_name),
                        frame.get_by_role("link", name=role_name),
                        frame.get_by_text(text_name),
                    ]
                    for locator in locators:
                        for idx in range(locator.count()):
                            item = locator.nth(idx)
                            if item.is_visible():
                                remaining = max(1, int((deadline - time.monotonic()) * 1000))
                                item.click(timeout=min(1000, remaining), no_wait_after=True)
                                return True
                except PlaywrightError:
                    # A redirect can invalidate a frame while it is inspected.
                    continue
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(min(100, max(1, (deadline - time.monotonic()) * 1000)))


def _duo_passcode_input(page):
    for frame in page.frames:
        try:
            inputs = frame.locator(PASSCODE_SELECTOR)
            for idx in range(inputs.count()):
                item = inputs.nth(idx)
                if item.is_visible():
                    return item
        except PlaywrightError:
            continue
    return None


def _visible_duo_error(page) -> bool:
    for frame in page.frames:
        try:
            # Snapshot text in one DOM operation. A count/is_visible/inner_text
            # sequence can wait 30 seconds for an error removed by navigation.
            texts = frame.locator(
                ".error-message:visible, [role='alert']:visible, .error:visible"
            ).evaluate_all("elements => elements.map(element => element.innerText)")
            if any(re.search(r"incorrect|invalid|expired|denied|too many|try again", text, re.I) for text in texts):
                return True
        except PlaywrightError:
            continue
    return False


def _trust_prompt_visible(page) -> bool:
    for frame in page.frames:
        try:
            for label in TRUST_LABELS:
                if frame.get_by_text(label, exact=True).first.is_visible():
                    return True
        except PlaywrightError:
            continue
    return False


def _wait_for_passcode_outcome(page, timeout: int = 15_000, previous_error: bool = False, cancel_event=None) -> str:
    """An unchanged input is pending, not proof that Duo rejected the code."""
    deadline = time.monotonic() + timeout / 1000.0
    error_cleared = not previous_error
    while True:
        if cancelled(cancel_event):
            raise LoginFlowError("Login cancelled.")
        if _is_clearly_authenticated_url(page.url) or _trust_prompt_visible(page):
            return "accepted"
        if _visible_duo_error(page):
            if error_cleared:
                return "rejected"
        else:
            error_cleared = True
        if time.monotonic() >= deadline:
            return "timeout"
        page.wait_for_timeout(100)


def _read_windows_passcodes(result_queue, stop_event, console):
    pending = ""
    extended_key = False
    while not stop_event.wait(0.05):
        if not console.kbhit():
            continue
        char = console.getwch()
        if extended_key:
            extended_key = False
        elif char in ("\x00", "\xe0"):
            extended_key = True
        elif char in ("\r", "\n"):
            print()
            result_queue.put(("cli", pending))
            pending = ""
        elif char in ("\b", "\x7f"):
            if pending:
                pending = pending[:-1]
                print("\b \b", end="", flush=True)
        elif char in ("\x03", "\x1a"):
            stop_event.set()
        elif char.isascii() and char.isdigit():
            pending += char
            print("*", end="", flush=True)


def _read_terminal_passcodes(result_queue, stop_event, automatic_capture: bool = False):
    """One cancellable terminal reader per login, including across retries."""
    if not sys.stdin.isatty():
        return
    hint = " (or wait for SMS auto-capture)" if automatic_capture else ""
    print(f"  🔑 Enter your Duo passcode{hint}: ", end="", flush=True)
    if sys.platform == "win32":
        import msvcrt
        _read_windows_passcodes(result_queue, stop_event, msvcrt)
    else:
        import select
        while not stop_event.wait(0.1):
            try:
                readable, _, _ = select.select([sys.stdin], [], [], 0)
                if readable:
                    line = sys.stdin.readline()
                    if not line:
                        result_queue.put(("terminal-closed", None))
                        return
                    result_queue.put(("cli", line.strip()))
            except (OSError, ValueError):
                result_queue.put(("terminal-closed", None))
                return


def _sms_passcode_source(available, reason, start_rowid, sent_at, wait_seconds, excluded):
    """macOS Messages reader; only codes that arrive after the SMS was requested count."""
    if not available:
        return Source("sms", "macOS Messages", unavailable=reason or ("only available on macOS" if sys.platform != "darwin" else "unavailable"))

    def run(emit, stop):
        code = wait_for_duo_sms_passcode(
            start_rowid=start_rowid, after_unix_timestamp=sent_at, timeout_seconds=wait_seconds,
            excluded_codes=excluded, stop_event=stop, access_checked=True,
        )
        if code:
            emit(code, time.time())
            return None
        return f"no new SMS in {wait_seconds}s"
    return Source("sms", "macOS Messages", run)


def _telegram_passcode_source(notifier, attempt, max_attempts, hint, excluded=frozenset(), method="sms"):
    """Ask the owner on Telegram and accept the reply."""
    if notifier is None:
        return Source("telegram", "Telegram", unavailable="not configured")

    def run(emit, stop):
        def prompt():
            if not stop.is_set():
                instruction = f"Duo sent a text{hint}." if method == "sms" else "Open Duo Mobile for a current passcode."
                notifier.send_raw_message(
                    f"🔐 <b>UMBC Duo passcode needed (attempt {attempt}/{max_attempts})</b>\n"
                    f"{instruction} Reply with the 6–8 digit code."
                )
        # Establish the update offset before asking. A fast reply must not be
        # discarded as an old message while polling is starting.
        code = None if stop.is_set() else notifier.poll_for_passcode(
            timeout_sec=120, stop_event=stop, on_ready=prompt, excluded_codes=excluded,
        )
        if code and not stop.is_set():
            emit(code, time.time())
            return None
        return "no reply"
    return Source("telegram", "Telegram", run)


def _approve_duo_push(page, notifier, cancelled, timeout: float = 90.0) -> bool:
    """Send (if needed) a Duo Push and wait for the user to approve it on their phone."""
    # Duo usually sends the push by itself on arrival; only press an explicit send button.
    _click_exact_text_in_any_frame(page, ("Send Duo Push", "Send me a Push", "Duo Push"), timeout=1500,
                                  stop_when=lambda: _trust_prompt_visible(page) or "duosecurity.com" not in page.url.lower())
    print("   📲 Approve the Duo push on your phone now (waiting up to 90s)...", flush=True)
    if notifier:
        _notify_login(notifier, "🔐 <b>Approve the Duo push on your phone</b> to finish the Blackboard login.")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancelled():
            return False
        if _trust_prompt_visible(page) or "duosecurity.com" not in page.url.lower():
            print("   ✅ Duo push approved!")
            return True
        page.wait_for_timeout(500)
    print("   ❌ The Duo push was not approved in time. Run bb login again and approve it, or use bb login --manual.")
    return False


def _wait_for_authenticated_session(context, page, timeout: int = 30_000) -> bool:
    """Verify cookies directly, without waiting for the Blackboard SPA/assets."""
    deadline = time.monotonic() + timeout / 1000.0
    host = urlparse(BLACKBOARD_BASE).hostname
    while time.monotonic() < deadline:
        if urlparse(page.url).hostname == host:
            try:
                response = context.request.get(
                    f"{BLACKBOARD_BASE}/learn/api/public/v1/users/me",
                    headers={"Accept": "application/json"},
                    timeout=max(1, min(2000, int((deadline - time.monotonic()) * 1000))),
                    max_redirects=0,
                )
                try:
                    if response.status == 200:
                        data = response.json()
                        if isinstance(data, dict) and any(data.get(key) for key in ("id", "studentId", "userName")):
                            return True
                finally:
                    response.dispose()
            except (PlaywrightError, ValueError):
                pass
            delay = 300
        else:
            delay = 100
        page.wait_for_timeout(min(delay, max(0, (deadline - time.monotonic()) * 1000)))
    return False


def _notify_login(notifier, text: str):
    """Optional Telegram status must not delay authentication or shutdown."""
    import threading
    def send():
        try:
            notifier.send_raw_message(text)
        except Exception:
            pass
    threading.Thread(target=send, daemon=True).start()


def _has_auth_markers(page) -> bool:
    """Detect common Blackboard Ultra elements that only appear after auth."""
    return len(_matching_selectors(page, AUTH_SELECTORS)) > 0


def _has_login_markers(page) -> bool:
    """Detect common login CTAs/fields on Blackboard's unauthenticated landing pages."""
    return len(_matching_selectors(page, LOGIN_SELECTORS)) > 0


def _is_clearly_authenticated_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc == "blackboard.umbc.edu" and "/ultra/" in parsed.path and not _is_login_page(url)


def _follow_new_loc_if_present(page, debug: bool = False) -> bool:
    """
    If Blackboard sends us to /?new_loc=..., follow it once.
    Returns True if navigation was attempted.
    """
    try:
        parsed = urlparse(page.url)
        if parsed.netloc != "blackboard.umbc.edu":
            return False

        new_loc = parse_qs(parsed.query).get("new_loc", [None])[0]
        if not new_loc:
            _session_debug(debug, "No new_loc redirect detected.")
            return False

        target = urljoin(BLACKBOARD_BASE, unquote(new_loc))
        _session_debug(debug, f"Detected new_loc redirect target: {target}")
        if target == page.url:
            _session_debug(debug, "new_loc target equals current URL; skipping follow.")
            return False

        page.goto(target, wait_until="domcontentloaded", timeout=15_000)
        page.wait_for_timeout(1500)
        _session_debug(debug, f"Followed new_loc. Final URL after follow: {page.url}")
        return True
    except Exception:
        _session_debug(debug, "Failed while following new_loc redirect.")
        return False


def _is_authenticated_session(page, debug: bool = False) -> bool:
    """
    Robust auth check that combines URL checks with DOM markers.
    Handles transient Blackboard redirects like /?new_loc=...
    """
    current = page.url
    _session_debug(debug, f"Initial URL after goto(/ultra/course): {current}")

    if _is_clearly_authenticated_url(current):
        _session_debug(debug, "Classified ACTIVE from authenticated /ultra URL.")
        return True

    auth_hits = _matching_selectors(page, AUTH_SELECTORS)
    if auth_hits:
        _session_debug(debug, f"Classified ACTIVE from auth markers: {auth_hits}")
        return True
    _session_debug(debug, "Auth markers: none")

    followed = _follow_new_loc_if_present(page, debug=debug)
    if not followed:
        _session_debug(debug, "No redirect follow performed.")

    if _is_clearly_authenticated_url(page.url):
        _session_debug(debug, "Classified ACTIVE after new_loc follow.")
        return True

    auth_hits = _matching_selectors(page, AUTH_SELECTORS)
    if auth_hits:
        _session_debug(debug, f"Classified ACTIVE from post-follow auth markers: {auth_hits}")
        return True

    login_hits = _matching_selectors(page, LOGIN_SELECTORS)
    if login_hits:
        _session_debug(debug, f"Classified EXPIRED from login markers: {login_hits}")
        return False
    _session_debug(debug, "Login markers: none")

    _session_debug(debug, "Ambiguous state. Waiting +4s and re-evaluating once.")
    page.wait_for_timeout(4000)
    _session_debug(debug, f"URL after stabilization wait: {page.url}")

    if _is_clearly_authenticated_url(page.url):
        _session_debug(debug, "Classified ACTIVE after stabilization wait.")
        return True

    auth_hits = _matching_selectors(page, AUTH_SELECTORS)
    if auth_hits:
        _session_debug(debug, f"Classified ACTIVE from auth markers after stabilization: {auth_hits}")
        return True

    login_hits = _matching_selectors(page, LOGIN_SELECTORS)
    if login_hits:
        _session_debug(debug, f"Classified EXPIRED from login markers after stabilization: {login_hits}")
        return False

    _session_debug(debug, "UNKNOWN_SESSION_STATE: no definitive auth or login markers found.")
    return False


def _session_cookie_summary(context) -> tuple[int, int]:
    bb_count = 0
    umbc_count = 0
    try:
        for c in context.cookies():
            domain = c.get("domain", "")
            if "blackboard.umbc.edu" in domain:
                bb_count += 1
            if "umbc.edu" in domain:
                umbc_count += 1
    except Exception:
        pass
    return bb_count, umbc_count


def _navigate(page, url: str, wait_render: int = 6000) -> bool:
    """
    Navigate to a URL and wait for SPA render.
    Returns True if we landed on the expected page, False if redirected to login.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Wait a bit for Blackboard's internal Angular/React routing
        # and checking if it kicks us to SSO
        page.wait_for_timeout(wait_render)

        if not _is_authenticated_session(page):
            return False

        # In specific instances where Bb opens a drawer or pane, the URL might not change but we're still logged in.
        return True
    except PlaywrightTimeout:
        print(f"   ⚠️  Network timeout navigating to {url}")
        return False
    except Exception as e:
        print(f"   ⚠️  Error navigating to {url}: {e}")
        return False


def _navigate_and_check(p, url: str, headless: bool = True, cdp_url: str = None):
    """Helper to launch, check session, navigate, and return (context, page)"""
    context, page = _launch_context(p, headless=headless, cdp_url=cdp_url)

    if not _navigate(page, url):
        print("\n❌ Session invalid or expired. Run: python3 main.py --login")
        context.close()
        raise PlaywrightError("Session invalid")

    track_session_usage("usage")
    return context, page


# ============================================================
# Session management
# ============================================================

def logout(keep_config_creds: bool = True):
    """
    Clear the session to force re-login.
    Keeps config credentials intact for auto-fill on next login.

    Args:
        keep_config_creds: If True, keeps config.json auto_login credentials.
                         If False, also clears config.json auto_login section.
    """
    print("🚪 Logging out of Blackboard session...")

    # Clear cookies (this invalidates the session)
    cookies_file = Path(SESSION_DIR) / "cookies.json"
    if cookies_file.exists():
        cookies_file.unlink()
        print("   ✅ Cleared saved cookies")
    forget_idp_storage()

    # Clear session metadata
    meta_file = SESSION_DIR / "session_metadata.json"
    if meta_file.exists():
        meta_file.unlink()
        print("   ✅ Cleared session metadata")

    # Clear browser profile cookies too
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx, _ = _launch_context(p, headless=True)
            cookies = ctx.cookies()
            if cookies:
                ctx.clear_cookies()
                print(f"   ✅ Cleared {len(cookies)} browser cookies from profile")
            ctx.close()
    except Exception:
        # If browser cannot be launched, saved cookies are already cleared above
        pass

    # Optionally clear config credentials
    if not keep_config_creds:
        from core.config import load_config, CONFIG_FILE
        config = load_config()
        if "auto_login" in config:
            del config["auto_login"]
            CONFIG_FILE.write_text(json.dumps(config, indent=2))
            print("   ✅ Cleared config.json credentials")

    print("   Logout complete. Run --login to re-authenticate.")


def track_session_usage(event_type: str = "usage"):
    """
    Update session metadata to track when it was created and last used.
    """
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    meta_file = SESSION_DIR / "session_metadata.json"

    data = {}
    if meta_file.exists():
        try:
            data = json.loads(meta_file.read_text())
        except Exception:
            pass

    now = datetime.now().isoformat()
    now_human = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if event_type == "login":
        data["login_time"] = now
        data["login_time_human"] = now_human

    data["last_used_time"] = now
    data["last_used_time_human"] = now_human

    meta_file.write_text(json.dumps(data, indent=2))


def _http_refresh() -> bool | None:
    """Renew the Blackboard session over HTTPS through the still-valid IdP session.

    True = refreshed (done), False = a person is needed (use the browser login),
    None = network trouble (change nothing, do not start a login).
    """
    print("🔄 Session expired; trying a browserless refresh (no browser, no Duo)...")
    started = time.monotonic()
    result = refresh_session_http()
    if result == REFRESHED:
        track_session_usage("usage")
        print(f"✅ Session refreshed over HTTPS in {time.monotonic() - started:.1f}s.")
        return True
    if result == NEEDS_LOGIN:
        print("   ↳ The UMBC sign-in itself has expired; a full login is needed.")
        return False
    print(f"⚠️ {NETWORK_MESSAGE}", file=sys.stderr)
    return None


def check_session_state(timeout: float = 3.5) -> tuple[str, dict | None]:
    """One HTTP probe: (VALID | EXPIRED | UNKNOWN, user). Never starts a login."""
    state, user = probe_session(timeout=timeout)
    if state == VALID:
        track_session_usage("usage")
    return state, user


def quick_check_session_http(timeout: float = 3.5) -> tuple[bool, dict | None]:
    """Boolean view of check_session_state: UNKNOWN (network trouble) counts as not valid."""
    state, user = check_session_state(timeout)
    return state == VALID, user


async def check_session_async(quiet: bool = False, debug: bool = False, headless: bool = True, fast_only: bool = True) -> bool:
    """
    High-speed session tester.
    Uses ultra-lightweight sub-150ms HTTP API probe without spawning a browser.
    If fast_only is False, falls back to headless browser verification if HTTP probe fails.
    """
    # 1. Ultra-fast HTTP API probe (<150ms, 0 browser overhead)
    state, user_data = check_session_state()
    if state == VALID:
        if not quiet:
            meta_file = SESSION_DIR / "session_metadata.json"
            login_time = "Unknown"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text())
                    login_time = meta.get("login_time_human", "Unknown")
                except Exception:
                    pass
            print("✅ Session is ACTIVE (identity verified via HTTP API)")
            print(f"   Session Logged In: {login_time}")
        return True

    if state == UNKNOWN:
        if not quiet:
            print(f"⚠️ {NETWORK_MESSAGE}")
        return False

    if fast_only:
        if not quiet:
            print("❌ Session EXPIRED or missing cookies.")
            print("   Run `bb login` to authenticate.")
        return False

    # 2. Browser-level fallback verification (if explicitly requested via fast_only=False)
    try:
        from core.async_engine import AsyncSessionManager, EngineConfig, AdaptiveDOM
        config = EngineConfig(headless=headless)
        session_manager = AsyncSessionManager(config)
        await session_manager.initialize()

        async with session_manager.acquire_page() as page:
            if not quiet:
                print("⏳ Running browser verification fallback...")

            try:
                await page.goto(f"{BLACKBOARD_BASE}/ultra/course", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1.5)
            except Exception as e:
                if not quiet:
                    print(f"   ⚠️ Network error: {e}")
                return False

            current = page.url
            valid = _is_clearly_authenticated_url(current)
            if not valid:
                matched, _ = await AdaptiveDOM.wait_for_any_selector(
                    page,
                    AUTH_SELECTORS + LOGIN_SELECTORS,
                    timeout=4000,
                )
                if matched and matched in AUTH_SELECTORS:
                    valid = True

            if valid:
                track_session_usage("usage")
                if not quiet:
                    print("✅ Session is ACTIVE.")
            else:
                if not quiet:
                    print("❌ Session EXPIRED or missing.")
                    print("   Run `bb login` to re-authenticate.")

            return valid
    except Exception as e:
        if not quiet:
            print(f"   ⚠️ Error checking session: {e}")
        return False


def check_session(quiet: bool = False, debug: bool = False, headless: bool = True, fast_only: bool = True) -> bool:
    """Synchronous session tester using instant HTTP probe."""
    state, user_data = check_session_state()
    if state == VALID:
        if not quiet:
            print("✅ Session is ACTIVE (identity verified via HTTP API)")
        return True

    if state == UNKNOWN:
        if not quiet:
            print(f"⚠️ {NETWORK_MESSAGE}")
        return False

    if fast_only:
        if not quiet:
            print("❌ Session EXPIRED or missing cookies.")
            print("   Run `bb login` to authenticate.")
        return False

    # Fallback to async loop runner if fast_only=False
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return asyncio.run_coroutine_threadsafe(
                check_session_async(quiet=quiet, debug=debug, headless=headless, fast_only=False),
                loop
            ).result()
    except Exception:
        pass
    return asyncio.run(check_session_async(quiet=quiet, debug=debug, headless=headless, fast_only=False))





def _exclusive_login(busy_result):
    """Only one bb process logs in at a time; the others wait briefly, then give up cleanly."""
    def decorate(function):
        @functools.wraps(function)
        def run(*args, **kwargs):
            try:
                with login_lock():
                    return function(*args, **kwargs)
            except LoginInProgress as busy:
                print(f"❌ {busy}")
                return busy_result
        return run
    return decorate


def prompt_credentials_tui(default_auto_exp: bool = True) -> tuple[Optional[str], Optional[str], bool, str]:
    """
    Interactive terminal setup wizard for UMBC credentials and auto-login mode.
    Returns (username, password, save_to_config, chosen_mode).
    chosen_mode is one of "auto_exp", "auto", or "manual".
    Returns (None, None, False, "") if user cancels or inputs are invalid.
    """
    print("\n" + "═" * 66)
    print(" 🎓 UMBC Blackboard Login & Setup Wizard")
    print("═" * 66)
    print(" No active session or saved credentials detected.")
    print(" Let's configure your UMBC credentials to proceed with auto-login.\n")

    try:
        raw_usr = input(" 📧 UMBC Email / Campus ID (e.g. ab12345 or user@umbc.edu): ").strip()
        if not raw_usr:
            print("❌ Username cannot be empty.")
            return None, None, False, ""

        raw_pwd = getpass(" 🔑 UMBC Password: ").strip()
        if not raw_pwd:
            print("❌ Password cannot be empty.")
            return None, None, False, ""

        save_choice = input("\n 💾 Save credentials to config.json for future automated logins? [Y/n]: ").strip().lower()
        save_to_config = save_choice not in ("n", "no")

        is_mac = sys.platform == "darwin"
        print("\n ⚡ Select Login Mode:")
        if is_mac:
            print("    [1] Auto-Exp (Automated SSO + macOS SMS 2FA Interception) [Recommended]")
            print("    [2] Auto SSO (Automated SSO + Terminal/Telegram Passcode Entry)")
            print("    [3] Visible Browser (Manual Login Window)")
            default_choice = "1" if default_auto_exp else "2"
        else:
            print("    [1] Auto SSO (Automated SSO + Terminal/Telegram Passcode Entry) [Recommended]")
            print("    [2] Visible Browser (Manual Login Window)")
            default_choice = "1"

        mode_choice = input(f" 👉 Select mode (default: {default_choice}): ").strip() or default_choice
        print("═" * 66 + "\n")

        if is_mac:
            if mode_choice == "1":
                chosen_mode = "auto_exp"
            elif mode_choice == "2":
                chosen_mode = "auto"
            elif mode_choice == "3":
                chosen_mode = "manual"
            else:
                chosen_mode = "auto_exp" if default_auto_exp else "auto"
        else:
            if mode_choice == "1":
                chosen_mode = "auto"
            elif mode_choice == "2":
                chosen_mode = "manual"
            else:
                chosen_mode = "auto"

        return raw_usr, raw_pwd, save_to_config, chosen_mode

    except (KeyboardInterrupt, EOFError):
        print("\n\n❌ Login setup cancelled by user.")
        return None, None, False, ""


@_exclusive_login(False)
def login(force: bool = False, username: str = None, password: str = None, cdp_url: str = None) -> bool:
    """
    Handle the SSO login flow.
    If already logged in, does nothing unless force=True.

    Credentials are loaded in this priority order:
    1. CLI arguments (--username, --password)
    2. config.json auto_login section
    """
    if cdp_url:
        print("🔌 Ignoring --login since you are connected to an existing CDP browser.")
        print("   Please login directly in your attached browser window.")
        return False

    # Ensure config exists
    ensure_config_exists(notify=True)

    # Auto-load credentials from config if not provided via CLI
    if not username or not password:
        config = load_config()
        auto_login_config = config.get("auto_login", {})
        if not username:
            username = auto_login_config.get("username")
            if username:
                print("   📋 Using username from config")
        if not password:
            password = auto_login_config.get("password")
            if password:
                print("   📋 Using password from config")

    if not force:
        print("⏳ Checking existing session...")
        if check_session(quiet=True):
            print("✅ You are already logged in! (Use --force to login anyway)")
            return True

    print("\n🚀 Opening browser for SSO Login...")
    print("   Please complete the UMBC login and Duo 2FA in the window that appears.")
    print("   The browser will close automatically once the Blackboard dashboard loads.")

    with sync_playwright() as p:
        try:
            # We NEED a visible context for manual 2FA
            context, page = _launch_context(p, headless=False)
        except SystemExit:
            return False

        try:
            page.goto(f"{BLACKBOARD_BASE}/ultra/course", wait_until="domcontentloaded", timeout=30_000)
            print("⏳ Waiting for you to complete UMBC login and Duo 2FA...")
            wait_for_blackboard(context, page, BLACKBOARD_BASE, timeout=300_000)
            return _persist_login(context, None, "✨ Session saved successfully.")
        except LoginFlowError as error:
            print(f"❌ {error}")
            return False
        except PlaywrightError:
            print("❌ Login browser failed. Saved cache preserved; run bb login --manual again.")
            return False
        finally:
            context.close()


@_exclusive_login(False)
def login_auto(username: str = None, password: str = None, headless: bool = False, cdp_url: str = None, auto_exp: bool = False, force: bool = False, passcode: str = None, cancel_event=None, code_file: str = None, code_timeout: float = 300.0) -> bool:
    """
    Automated login via SSO + Duo text passcode.
    When code_file is set, SSO starts once and the Duo code is read from that file
    (polled every 0.5s, deleted after reading) so an outside agent can supply it.
    When auto_exp=True, automatically listens for incoming macOS SMS/iMessage 2FA passcodes.
    When force=True, clears cookies and forces a full re-authentication.
    When passcode is provided, submits the supplied 6–8 digit code directly on the first attempt.
    Returns True if login succeeds, False otherwise.
    """
    import threading

    login_stop_event = cancel_event if cancel_event is not None else threading.Event()

    def _cancelled() -> bool:
        if not login_stop_event.is_set():
            return False
        print("   ❌ Login cancelled.")
        return True

    if _cancelled():
        return False

    if cdp_url:
        print("🔌 Ignoring 'bb login auto' since you are connected to an existing CDP browser.")
        return False

    # Ensure config exists. If missing, blank config is created with notification.
    created_blank, cfg = ensure_config_exists(notify=True)

    usr = username or cfg.get("auto_login", {}).get("username")
    pwd = password or cfg.get("auto_login", {}).get("password")

    is_mac = sys.platform == "darwin"
    if code_file:
        passcode = None
        try:
            # Drop any stale code so it can never be submitted for this login.
            Path(code_file).unlink(missing_ok=True)
        except OSError as e:
            print(f"❌ Cannot use code file: {e}", flush=True)
            return False
    if passcode is not None:
        passcode = normalize_passcode(passcode)
        if passcode is None:
            print("❌ Passcodes must contain 6–8 digits.")
            return False

    # If credentials are not present, check if we can prompt via TUI or fail gracefully
    if not usr or not pwd:
        if sys.stdin.isatty():
            if _cancelled():
                return False
            usr, pwd, save_creds, chosen_mode = prompt_credentials_tui(default_auto_exp=auto_exp or True)
            if _cancelled():
                return False
            if not usr or not pwd:
                return False
            if save_creds:
                save_auto_login_credentials(usr, pwd)
            if chosen_mode == "manual":
                # This entrypoint already owns the login lock.
                login.__wrapped__(force=force, username=usr, password=pwd, cdp_url=cdp_url)
                valid, _ = quick_check_session_http()
                return valid
            if chosen_mode == "auto_exp":
                auto_exp = True
            elif chosen_mode == "auto":
                auto_exp = False
        else:
            print("❌ No login detected and no credentials found in config.json.")
            if not created_blank:
                print("   A blank config has been created or verified at: config.json")
            print("   Please populate config.json['auto_login'] or run: bb login")
            return False

    if not force:
        state, _ = check_session_state()
        if _cancelled():
            return False
        if state == UNKNOWN:
            print(f"⚠️ {NETWORK_MESSAGE}", file=sys.stderr)
            return False
        if state == VALID:
            print("✅ You are already logged in! (Use 'bb login --force' to re-authenticate)")
            return True
        refreshed = _http_refresh()
        if refreshed is not False:
            return bool(refreshed)

    if auto_exp:
        if is_mac:
            print("\n⚡ [EXPERIMENTAL] Automated SSO Login with Real-Time macOS SMS 2FA Extraction")
        else:
            print("\n⚡ Automated SSO Login with Terminal & Telegram 2FA Entry (Windows/Linux)")
    else:
        print("\n⚠️  [EXPERIMENTAL] 'bb login auto' is an experimental feature.")
        print("   UMBC's SSO or Duo configuration may change at any time, breaking this feature without notice.")
        print("   If login fails, run: bb login --manual\n")

    print("   📋 Credentials loaded.")
    print("🚀 Starting Automated SSO Login...")
    login_stage = "initializing"

    tg_notifier = None
    try:
        from telegram.notifier import TelegramNotifier
        notifier = TelegramNotifier()
        if notifier.enabled:
            tg_notifier = notifier
    except Exception:
        pass

    sms_available = False
    sms_reason = "only available on macOS"
    if is_mac:
        sms_available, sms_reason = check_sms_access()
        if not sms_available:
            print(f"   ⚠️ SMS auto-capture unavailable: {sms_reason}")
        elif not ensure_messages_sync():
            print("   ⚠️ Messages could not be started. Open Messages to enable SMS syncing, or enter the code manually.")
    if _cancelled():
        return False

    with sync_playwright() as p:
        try:
            login_stage = "launching browser context"
            context, page = _launch_context(p, headless=headless)
        except SystemExit:
            return False
        except Exception as e:
            print(f"❌ Could not launch login browser ({type(e).__name__}). Check browser installation and profile availability.")
            return False

        try:
            login_stage = "following UMBC SSO redirects"
            if _cancelled():
                return False
            if force:
                context.clear_cookies()
                forget_idp_storage()
                try:  # the IdP keeps its session in localStorage too; a stale copy confuses a fresh sign-in
                    page.goto("https://webauth.umbc.edu/idp/status", wait_until="domcontentloaded", timeout=10_000)
                    page.evaluate("localStorage.clear()")
                except Exception:
                    pass
                print("   🧹 Forced clean re-authentication: session cookies cleared.")
            # Snapshot before navigation: Duo can send a text on arrival.
            start_rowid = get_current_max_rowid() if sms_available else 0
            trigger_time = time.time()
            page.goto(f"{BLACKBOARD_BASE}/ultra/course", wait_until="domcontentloaded", timeout=30_000)
            state = wait_for_sso(
                context, page, usr, pwd, BLACKBOARD_BASE, detect_sso_error,
                cancel_event=login_stop_event,
            )
            if state == AUTHENTICATED:
                return _persist_login(context, login_stop_event, "✨ Auto-Login Successful! (Verified UMBC SSO / trusted Duo device; no passcode needed.)")

            # --- 4. REACHED DUO PROMPT ---
            print("   ↳ Duo is asking for verification...")

            # --- 5. CLICK "OTHER OPTIONS" ---
            login_stage = "clicking Duo Other options"
            if "duosecurity.com" in page.url.lower():
                print("   ↳ Clicking 'Other options'...")
                try:
                    if _click_exact_text_in_any_frame(
                        page, "Other options", timeout=6000,
                        stop_when=lambda: (
                            _duo_passcode_input(page) is not None
                            or _is_clearly_authenticated_url(page.url)
                            or page.get_by_text("Text message passcode", exact=False).first.is_visible()
                        ),
                    ):
                        print("   ↳ Clicked 'Other options'.")
                    else:
                        print("   ↳ Duo may already display available methods.")
                except Exception:
                    print("   ⚠️ Could not click Other options. Use bb login --manual if Duo changed.")
                finally:
                    page.wait_for_timeout(1000)

            if _returned_to_blackboard(context, page):
                return _persist_login(context, login_stop_event, "✨ Auto-Login Successful! (Duo passed this device.)")

            # --- 5b. CHOOSE THE DUO METHOD THIS ACCOUNT ACTUALLY OFFERS ---
            login_stage = "choosing a Duo method"
            methods = duo_methods(page)
            print(f"   ↳ Duo offers: {', '.join(methods) if methods else 'nothing bb can use'}.")
            if not methods or methods == ["call"]:
                print("   ❌ Duo shows no text, push or passcode method bb can use. "
                      "Add one in your Duo settings or run bb login --manual.")
                return False
            method = "passcode" if (passcode or code_file) and "sms" not in methods and "passcode" in methods else methods[0]
            if method == "push":
                if not _approve_duo_push(page, tg_notifier, _cancelled):
                    return False
            elif method == "passcode":
                sms_available = False  # nothing is texted; Duo Mobile shows the code
                print("   ↳ Text message passcode is not available; using the Duo Mobile passcode.")
            passcode_accepted = method == "push"

            if method != "push":
                # --- 6. SELECT "TEXT MESSAGE PASSCODE" SPECIFICALLY ---
                login_stage = "selecting Duo passcode method"
                if "duosecurity.com" in page.url.lower():
                    print(f"   ↳ Selecting {'Text message passcode' if method == 'sms' else 'Duo Mobile passcode'}...")
                    try:
                        if _duo_passcode_input(page) is None:
                            start_rowid = get_current_max_rowid() if sms_available else 0
                            trigger_time = time.time()
                            if not _click_exact_text_in_any_frame(page, DUO_METHOD_LABELS[method], timeout=6000):
                                if _returned_to_blackboard(context, page):
                                    return _persist_login(context, login_stop_event, "✨ Auto-Login Successful! (Duo passed this device.)")
                                raise PlaywrightTimeout("Text message passcode control not found")
                            print("   ↳ Passcode is being sent..." if method == "sms" else "   ↳ Enter the current passcode from Duo Mobile.")
                        else:
                            print("   ↳ Passcode input already active.")
                    except PlaywrightTimeout:
                        if _returned_to_blackboard(context, page):
                            return _persist_login(context, login_stop_event, "✨ Auto-Login Successful! (Duo passed this device.)")
                        duo_body = page.locator("body").inner_text()
                        if "limit" in duo_body.lower() or "attempts" in duo_body.lower() or "denied" in duo_body.lower():
                            print("   ❌ Duo denied the request or reported a limit. Use bb login --manual.")
                        else:
                            print("   ❌ Could not find 'Text message passcode' button.")

                        return False
                    except Exception:
                        if _returned_to_blackboard(context, page):
                            return _persist_login(context, login_stop_event, "✨ Auto-Login Successful! (Duo passed this device.)")
                        print("   ❌ Could not select Text message passcode. Use bb login --manual.")

                        return False

                if _cancelled():
                    return False

                # --- 7. WAIT FOR PASSCODE INPUT (including embedded Duo frames) ---
                login_stage = "waiting for Duo passcode input"
                input_deadline = time.monotonic() + 12.0
                passcode_input = _duo_passcode_input(page)
                while passcode_input is None and time.monotonic() < input_deadline:
                    if _cancelled():
                        return False
                    if _returned_to_blackboard(context, page):
                        return _persist_login(context, login_stop_event, "✨ Auto-Login Successful! (Duo passed this device.)")
                    page.wait_for_timeout(100)
                    passcode_input = _duo_passcode_input(page)
                if _cancelled():
                    return False
                if passcode_input is None:
                    print("   ❌ Timeout waiting for passcode input. Use 'bb login --manual'.")
                    return False

                sent_to_hint = ""
                try:
                    body_text = page.locator("body").inner_text()
                    match = re.search(r"ending in (\d{4})", body_text)
                    if match:
                        sent_to_hint = f" (sent to number ending in {match.group(1)})"
                except Exception:
                    pass

                # --- 8 & 9. PASSCODE ENTRY & RETRY LOOP ---
                login_stage = "submitting Duo passcode"
                if _cancelled():
                    return False
                max_attempts = 3
                tried_codes = set()
                sms_recovery_used = False
                sms_wait_seconds = 25
                terminal = TerminalSource(_read_terminal_passcodes, login_stop_event, sms_available) if sys.stdin.isatty() else None
                passcode_accepted = False

                for attempt in range(1, max_attempts + 1):
                    if _cancelled():
                        return False
                    if passcode and attempt == 1:
                        sources = [immediate_source(passcode)]
                        window = 5.0
                    else:
                        sources = [_sms_passcode_source(sms_available, sms_reason, start_rowid, trigger_time, sms_wait_seconds, frozenset(tried_codes)),
                                   _telegram_passcode_source(tg_notifier, attempt, max_attempts, sent_to_hint, frozenset(tried_codes), method),
                                   terminal.source() if terminal else Source("cli", "terminal", unavailable="not an interactive terminal")]
                        if code_file:
                            sources.append(file_source(code_file))
                            print("WAITING_FOR_CODE", flush=True)
                        window = max(120.0, float(code_timeout)) if code_file else 120.0
                        if attempt > 1:
                            print("   ⚠️ Enter a different passcode, or request a new one in Duo. The rejected code will be ignored.")
                        print(f"   📲 Waiting for your Duo passcode{sent_to_hint} (attempt {attempt}/{max_attempts}).")
                    race = collect_passcode(sources, tried=tried_codes, sent_at=trigger_time, timeout=window, cancel=login_stop_event)
                    if _cancelled():
                        return False
                    code = race.code
                    if code is None:
                        if sms_available and not sms_recovery_used and attempt < max_attempts and not code_file:
                            sms_recovery_used = True
                            sms_wait_seconds = 65
                            resend_rowid = get_current_max_rowid()
                            resend_time = time.time()
                            if _click_exact_text_in_any_frame(
                                page, ("Send a new passcode", "Send new passcode", "Resend passcode"), timeout=2000,
                            ):
                                start_rowid, trigger_time = resend_rowid, resend_time
                                print("   ↳ SMS delivery is delayed; requested one replacement passcode.")
                            else:
                                print("   ↳ Duo is not ready to resend; continuing to wait for the original SMS.")
                            continue
                        print(f"   ❌ No passcode received. Tried -> {race.summary()}.")
                        print("   Use 'bb login --manual' to continue in a browser.")
                        return False
                    print(f"   ↳ Passcode received via {race.source}; submitting.", flush=True)
                    tried_codes.add(code)
                    previous_error = _visible_duo_error(page)
                    if _cancelled():
                        return False
                    passcode_input.fill(code)
                    if _cancelled():
                        return False
                    # Submit once. Searching after Enter could click Verify twice or
                    # waste a timeout after Duo has already redirected.
                    if not _click_exact_text_in_any_frame(page, "Verify", timeout=1000):
                        passcode_input.press("Enter", timeout=2000, no_wait_after=True)
                    outcome = _wait_for_passcode_outcome(page, previous_error=previous_error, cancel_event=login_stop_event)
                    if _cancelled():
                        return False
                    if outcome == "accepted":
                        print("   ✅ Duo passcode accepted!")
                        passcode_accepted = True
                        break
                    if outcome == "timeout":
                        print("   ❌ Duo did not confirm the result. Use 'bb login --manual'; the code will not be resubmitted.")
                        return False
                    print("   ⚠️ Duo rejected that passcode (wrong or expired).")
                    if attempt == max_attempts:
                        print("   ❌ Maximum passcode attempts reached.")
                        return False
                    print("   ↳ Waiting for another code; the rejected one will be ignored.")

                if not passcode_accepted:
                    return False

            # --- 10. TRUST BROWSER (if prompted) ---
            if _cancelled():
                return False
            login_stage = "handling Duo trust browser prompt"
            print("   ⏳ Checking for 'Trust browser' prompt...")
            try:
                if _click_exact_text_in_any_frame(
                    page, TRUST_LABELS, timeout=5000,
                    stop_when=lambda: _is_clearly_authenticated_url(page.url),
                ):
                    print("   ↳ Clicked 'Yes, this is my device'.")
            except Exception:
                pass

            # --- 11. WAIT FOR A VERIFIED BLACKBOARD SESSION ---
            login_stage = "waiting for Blackboard dashboard"
            print("   ⏳ Waiting for Blackboard to load...")
            wait_for_blackboard(context, page, BLACKBOARD_BASE, cancel_event=login_stop_event)
            if not _persist_login(context, login_stop_event, "✨ Auto-Login Successful!"):
                return False
            if tg_notifier:
                try:
                    _notify_login(tg_notifier, "✅ <b>Blackboard Auto-Login Successful!</b>\nYour session is cached and ready.")
                except Exception:
                    pass
            return True

        except LoginFlowError as e:
            # LoginFlowError text is fixed guidance (no page text, URLs or codes).
            print(f"❌ {e}")
            if tg_notifier and "cancelled" not in str(e).lower():
                try:
                    tg_notifier.send_raw_message(f"❌ <b>Blackboard Auto-Login Failed</b>: {e}")
                except Exception:
                    pass
            return False
        except Exception as e:
            print(f"❌ Auto-Login failed during {login_stage} ({type(e).__name__}). Check your connection or run bb login --manual.")
            return False

        finally:
            login_stop_event.set()
            for tab in list(context.pages):
                try:
                    tab.close(run_before_unload=False)
                except PlaywrightError:
                    pass
            context.close()



def _returned_to_blackboard(context, page) -> bool:
    """True when Duo has already handed the browser back with a live session."""
    location = urlparse(page.url)
    if location.hostname != urlparse(BLACKBOARD_BASE).hostname or not location.path.startswith("/ultra"):
        return False
    return is_verified(context, BLACKBOARD_BASE)


def _persist_login(context, cancel_event, success_message: str) -> bool:
    """Verify the browser session over HTTP, then replace the cookie cache.

    Any failure (cancel, verification, write) keeps the previous cache.
    """
    if cancelled(cancel_event):
        return False
    if not is_verified(context, BLACKBOARD_BASE):
        print("   ❌ Blackboard session could not be verified; saved cache preserved.")
        return False
    if cancelled(cancel_event) or not _save_session(context):
        return False
    try:
        record_user_agent(context.pages[0].evaluate("navigator.userAgent"))
    except Exception:
        pass
    record_idp_storage(context)
    track_session_usage("login")
    print(success_message)
    return True


def _save_session(context) -> bool:
    """Atomically persist verified browser cookies for later runs."""
    temp_path = None
    try:
        cookies = context.cookies()
        target = Path(SESSION_DIR) / "cookies.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent,
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(cookies, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        return True
    except Exception as e:
        print(f"   ⚠️  Could not save session cookies: {e}")
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


async def _login_auto_async(**kwargs):
    """Signal the owned login worker when its async caller is cancelled."""
    import threading
    cancel_event = threading.Event()
    try:
        return await asyncio.to_thread(login_auto, cancel_event=cancel_event, **kwargs)
    except asyncio.CancelledError:
        cancel_event.set()
        raise


async def _require_session_async(cdp_url: str = None, checked_state: str | None = None) -> bool:
    """Fast check for session before async scraping with auto-recovery and TUI setup."""
    if cdp_url:
        return True

    # One sub-150ms HTTP probe. Only a confirmed expiry may start a login (which texts a Duo code).
    state = checked_state if checked_state is not None else check_session_state()[0]
    if state == VALID:
        return True
    if state == UNKNOWN:
        print(f"⚠️ {NETWORK_MESSAGE}", file=sys.stderr)
        return False

    # Expired: the HTTPS refresh needs no browser and no Duo text, so it goes first.
    refreshed = await asyncio.to_thread(_http_refresh)
    if refreshed is not False:
        return bool(refreshed)

    # Session missing or expired -> check config & credentials
    print("\n⚠️ No active Blackboard session detected.")
    created_blank, cfg = ensure_config_exists(notify=True)

    if has_auto_login_credentials(cfg):
        print("🔄 Valid credentials found in config.json. Attempting automated login refresh (auto-exp)...")
        await _login_auto_async(
            username=None,
            password=None,
            headless=True,
            cdp_url=None,
            auto_exp=True,
            force=False,
        )
        valid, _ = quick_check_session_http()
        if valid:
            print("✅ Session successfully refreshed. Proceeding...")
            return True

    # 4. No credentials -> interactive TUI setup wizard if terminal is available
    if sys.stdin.isatty():
        usr, pwd, save_creds, chosen_mode = prompt_credentials_tui(default_auto_exp=True)
        if usr and pwd:
            if save_creds:
                save_auto_login_credentials(usr, pwd)
            if chosen_mode == "manual":
                await asyncio.to_thread(login, False, usr, pwd, None)
            else:
                await _login_auto_async(
                    username=usr,
                    password=pwd,
                    headless=True,
                    cdp_url=None,
                    auto_exp=(chosen_mode == "auto_exp"),
                    force=False,
                )
            valid, _ = quick_check_session_http()
            if valid:
                print("✅ Session successfully established. Proceeding...")
                return True

    print("❌ Session invalid or expired. Run: python3 main.py --auto-exp or python3 main.py --login")
    return False


def _require_session(cdp_url: str = None, checked_state: str | None = None) -> bool:
    """Check session before scraping with auto-recovery and TUI setup."""
    if cdp_url:
        return True

    state = checked_state if checked_state is not None else check_session_state()[0]
    if state == VALID:
        return True
    if state == UNKNOWN:
        print(f"⚠️ {NETWORK_MESSAGE}", file=sys.stderr)
        return False

    refreshed = _http_refresh()
    if refreshed is not False:
        return bool(refreshed)

    print("\n⚠️ No active Blackboard session detected.")
    created_blank, cfg = ensure_config_exists(notify=True)

    if has_auto_login_credentials(cfg):
        print("🔄 Valid credentials found in config.json. Attempting automated login refresh (auto-exp)...")
        login_auto(
            username=None,
            password=None,
            headless=True,
            cdp_url=None,
            auto_exp=True,
            force=False,
        )
        valid, _ = quick_check_session_http()
        if valid:
            print("✅ Session successfully refreshed. Proceeding...")
            return True

    if sys.stdin.isatty():
        usr, pwd, save_creds, chosen_mode = prompt_credentials_tui(default_auto_exp=True)
        if usr and pwd:
            if save_creds:
                save_auto_login_credentials(usr, pwd)
            if chosen_mode == "manual":
                login(False, usr, pwd, None)
            else:
                login_auto(
                    username=usr,
                    password=pwd,
                    headless=True,
                    cdp_url=None,
                    auto_exp=(chosen_mode == "auto_exp"),
                    force=False,
                )
            valid, _ = quick_check_session_http()
            if valid:
                print("✅ Session successfully established. Proceeding...")
                return True

    print("❌ Session invalid or expired. Run: python3 main.py --auto-exp or python3 main.py --login")
    return False
