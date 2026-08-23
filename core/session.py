import asyncio
import json
import re
import sys
import time
from getpass import getpass
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout, Error as PlaywrightError

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
        # Default context is available via browser.contexts[0]
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        return context, page
    else:
        # Launch persistent context
        # Check if another process is using it by looking for the SingletonLock
        lock_file = Path(SESSION_DIR) / "SingletonLock"
        if lock_file.exists():
            # If the user kills the script ungracefully, the lock might be leftover.
            # Playwright usually cleans it up, but just in case we let Playwright try and fail with a clear message.
            pass

        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=SESSION_DIR,
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 800}
            )

            # Explicitly load session cookies to prevent them dropping between runs
            import json
            cookie_file = Path(SESSION_DIR) / "cookies.json"
            if cookie_file.exists():
                try:
                    cookies = json.loads(cookie_file.read_text())
                    if cookies:
                        context.add_cookies(cookies)
                except Exception:
                    pass

            # persistent context starts with 1 page
            page = context.pages[0]
            # Speed up loads by blocking heavy media
            page.route("**/*.{png,jpg,jpeg,gif,avif,webp,mp4,webm,woff,woff2,ttf,svg,ico}", lambda route: route.abort())
            return context, page
        except PlaywrightError as e:
            if "Target directory" in str(e) and "is in use" in str(e):
                print("\n❌ [ERROR] The Playwright Browser Session is locked.")
                print("   This means another scraper process or visible Chromium instance is currently using it.")
                print(f"   Please close any running Chrome windows (or kill runaway python scripts) tied to {SESSION_DIR.name}/ and try again.")
                raise SystemExit(1)
            else:
                raise


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
                            txt = item.inner_text().strip()
                            if txt:
                                for pattern, err_type in SSO_ERROR_PATTERNS:
                                    if pattern.search(txt):
                                        return True, err_type, txt
                                return True, "AUTH_FAILED", txt
            except Exception:
                continue

        # 2. Check full body text if on a login/SSO URL
        url_lower = page.url.lower()
        if any(ind in url_lower for ind in LOGIN_INDICATORS):
            body_txt = page.locator("body").inner_text()
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


def _click_exact_text_in_any_frame(page, target_text: str, timeout: int = 10_000) -> bool:
    """
    Click the first visible element whose rendered text exactly matches target_text.

    Duo sometimes renders these actions as buttons, links, or role=button nodes
    inside an iframe. Matching by exact visible text keeps us from selecting the
    wrong passcode method.
    """
    target_text = _normalized_text(target_text)
    target_lower = target_text.lower().rstrip(".,:;!?)")
    selectors = ["button", "a", "[role='button']", "input[type='submit']"]

    deadline = time.time() + (timeout / 1000.0)
    while time.time() <= deadline:
        for frame in page.frames:
            try:
                text_loc = frame.get_by_text(target_text, exact=False)
                for idx in range(text_loc.count()):
                    item = text_loc.nth(idx)
                    if item.is_visible():
                        item.click(timeout=timeout)
                        return True
            except Exception:
                pass

            try:
                role_loc = frame.get_by_role("button", name=re.compile(re.escape(target_text), re.I))
                for idx in range(role_loc.count()):
                    item = role_loc.nth(idx)
                    if item.is_visible():
                        item.click(timeout=timeout)
                        return True
            except Exception:
                pass

            for selector in selectors:
                try:
                    locator = frame.locator(selector)
                    count = locator.count()
                except Exception:
                    continue

                for idx in range(count):
                    item = locator.nth(idx)
                    try:
                        if not item.is_visible():
                            continue
                        candidate = _normalized_text(item.inner_text())
                        if not candidate:
                            candidate = _normalized_text(item.get_attribute("aria-label"))
                        if not candidate and selector.startswith("input"):
                            candidate = _normalized_text(item.get_attribute("value"))

                        candidate_lower = candidate.lower().rstrip(".,:;!?)")
                        if candidate_lower == target_lower or target_lower in candidate_lower:
                            item.click(timeout=timeout)
                            return True
                    except Exception:
                        continue

        page.wait_for_timeout(250)

    return False


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

    # Clear session metadata
    meta_file = SESSION_DIR / "session_metadata.json"
    if meta_file.exists():
        meta_file.unlink()
        print("   ✅ Cleared session metadata")

    # Clear browser profile cookies too
    try:
        with sync_playwright() as p:
            # Launch with existing profile but clear cookies
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=SESSION_DIR,
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 800}
            )
            # Get all cookies and clear them
            cookies = ctx.cookies()
            if cookies:
                ctx.clear_cookies()
                print(f"   ✅ Cleared {len(cookies)} browser cookies from profile")
            ctx.close()
    except Exception as e:
        print(f"   ⚠️ Could not clear browser cookies: {e}")

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


def quick_check_session_http(timeout: float = 3.5) -> tuple[bool, dict | None]:
    """
    Super lightweight, instantaneous HTTP probe for valid Blackboard session.
    Takes ~120ms with 0 browser tabs, 0 Chromium memory overhead, and 100% accuracy.
    Queries /learn/api/public/v1/users/me using cached cookies.
    Returns: (is_valid: bool, user_info: Optional[dict])
    """
    cookie_file = Path(SESSION_DIR) / "cookies.json"
    if not cookie_file.exists():
        return False, None

    try:
        import urllib.request
        import urllib.error

        cookies_list = json.loads(cookie_file.read_text())
        cookie_header = "; ".join([
            f"{c['name']}={c['value']}"
            for c in cookies_list
            if "blackboard.umbc.edu" in c.get("domain", "") or "umbc.edu" in c.get("domain", "")
        ])
        if not cookie_header:
            return False, None

        headers = {
            "Cookie": cookie_header,
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }

        url = f"{BLACKBOARD_BASE}/learn/api/public/v1/users/me"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if "id" in data or "studentId" in data or "userName" in data:
                    track_session_usage("usage")
                    return True, data
    except urllib.error.HTTPError:
        return False, None
    except Exception:
        pass

    return False, None


async def check_session_async(quiet: bool = False, debug: bool = False, headless: bool = True, fast_only: bool = False) -> bool:
    """
    High-speed session tester.
    Uses ultra-lightweight sub-150ms HTTP API probe first, with fallback to headless browser.
    """
    # 1. Ultra-fast HTTP API probe (<150ms, 0 browser overhead)
    http_valid, user_data = quick_check_session_http()
    if http_valid and user_data:
        if not quiet:
            meta_file = SESSION_DIR / "session_metadata.json"
            login_time = "Unknown"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text())
                    login_time = meta.get("login_time_human", "Unknown")
                except Exception:
                    pass
            uid = user_data.get("studentId") or user_data.get("userName") or user_data.get("id")
            print("✅ Session is ACTIVE (Verified via HTTP API in <150ms)")
            print(f"   Student ID / User: {uid}")
            print(f"   Session Logged In: {login_time}")
        return True

    if fast_only:
        if not quiet:
            print("❌ Session EXPIRED or missing cookies.")
            print("   Run `python3 main.py --login` to authenticate.")
        return False

    # 2. Browser-level fallback verification (if HTTP probe inconclusive)
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
                    print("   Run `python3 main.py --login` to re-authenticate.")

            return valid
    except Exception as e:
        if not quiet:
            print(f"   ⚠️ Error checking session: {e}")
        return False


def check_session(quiet: bool = False, debug: bool = False, headless: bool = True) -> bool:
    """Synchronous session tester using instant HTTP probe."""
    http_valid, user_data = quick_check_session_http()
    if http_valid:
        if not quiet:
            uid = user_data.get("studentId") or user_data.get("userName") or "Active"
            print(f"✅ Session is ACTIVE (Student: {uid})")
        return True

    # Fallback to async loop runner
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return asyncio.run_coroutine_threadsafe(
                check_session_async(quiet=quiet, debug=debug, headless=headless),
                loop
            ).result()
    except Exception:
        pass
    return asyncio.run(check_session_async(quiet=quiet, debug=debug, headless=headless))





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


def login(force: bool = False, username: str = None, password: str = None, cdp_url: str = None):
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
        return

    # Ensure config exists
    ensure_config_exists(notify=True)

    # Auto-load credentials from config if not provided via CLI
    if not username or not password:
        config = load_config()
        auto_login_config = config.get("auto_login", {})
        if not username:
            username = auto_login_config.get("username")
            if username:
                print(f"   📋 Using username from config: {username}")
        if not password:
            password = auto_login_config.get("password")
            if password:
                print("   📋 Using password from config")

    if not force:
        print("⏳ Checking existing session...")
        if check_session(quiet=True):
            print("✅ You are already logged in! (Use --force to login anyway)")
            return

    print("\n🚀 Opening browser for SSO Login...")
    print("   Please complete the UMBC login and Duo 2FA in the window that appears.")
    print("   The browser will close automatically once the Blackboard dashboard loads.")

    with sync_playwright() as p:
        try:
            # We NEED a visible context for manual 2FA
            context, page = _launch_context(p, headless=False)
        except SystemExit:
            return

        page.goto(f"{BLACKBOARD_BASE}/ultra/course")

        # Basic auto-fill if credentials provided via CLI (useful for headless CI, though Duo blocks full automation)
        if username and password:
            print("   Attempting auto-fill of credentials...")
            page.wait_for_timeout(3000)
            if "google" in page.url or "sso" in page.url:
                try:
                    # Very brittle, Google changes this constantly. Best effort.
                    if page.locator("input[type='email']").is_visible(timeout=2000):
                        page.fill("input[type='email']", username)
                        page.keyboard.press("Enter")

                    page.wait_for_timeout(2000)
                    if page.locator("input[type='password']").is_visible(timeout=3000):
                        page.fill("input[type='password']", password)
                        page.keyboard.press("Enter")
                except Exception as e:
                    print(f"   ⚠️ Auto-fill failed: {e}")

        # Wait for the user to reach the actual Blackboard Ultra dashboard
        print("⏳ Waiting for you to complete Duo 2FA...")
        try:
            # The exact successful URL we wait for
            page.wait_for_url("**/ultra/**", timeout=300_000) # Give them 5 mins to authenticate
            print("✅ Reached Blackboard dashboard!")

            try:
                # Wait a few extra seconds to ensure cookies are fully set
                page.wait_for_timeout(3000)
                # Explicitly dump cookies (Chrome drops "Session" scoped cookies on exit otherwise)
                import json
                cookies = context.cookies()
                (Path(SESSION_DIR) / "cookies.json").write_text(json.dumps(cookies))
            except Exception:
                print("✅ Session saved (window closed early).")

        except PlaywrightTimeout:
            print("❌ Login timed out. Did not reach the dashboard within 5 minutes.")
            context.close()
            return

        # Verify it's actually logged in
        if _is_authenticated_session(page):
            track_session_usage("login")
            print("✨ Session saved successfully.")
        else:
            print("⚠️ URL looks correct, but it might still be a login page. Please verify by running --check-session.")

        context.close()


def login_auto(username: str = None, password: str = None, headless: bool = False, cdp_url: str = None, auto_exp: bool = False, force: bool = False) -> bool:
    """
    Automated login via SSO + Duo text passcode.
    When auto_exp=True, automatically listens for incoming macOS SMS/iMessage 2FA passcodes.
    When force=True, clears cookies and forces a full re-authentication.
    Returns True if login succeeds, False otherwise.
    """
    if cdp_url:
        print("🔌 Ignoring --login --auto since you are connected to an existing CDP browser.")
        return False

    # Ensure config exists. If missing, blank config is created with notification.
    created_blank, cfg = ensure_config_exists(notify=True)

    usr = username or cfg.get("auto_login", {}).get("username")
    pwd = password or cfg.get("auto_login", {}).get("password")

    is_mac = sys.platform == "darwin"

    # If credentials are not present, check if we can prompt via TUI or fail gracefully
    if not usr or not pwd:
        if sys.stdin.isatty():
            usr, pwd, save_creds, chosen_mode = prompt_credentials_tui(default_auto_exp=auto_exp or True)
            if not usr or not pwd:
                return False
            if save_creds:
                save_auto_login_credentials(usr, pwd)
            if chosen_mode == "manual":
                login(force=force, username=usr, password=pwd, cdp_url=cdp_url)
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
            print("   Please populate config.json['auto_login'] or run: bb --auto-exp")
            return False

    if auto_exp:
        if is_mac:
            print("\n⚡ [EXPERIMENTAL] Automated SSO Login with Real-Time macOS SMS 2FA Extraction")
        else:
            print("\n⚡ Automated SSO Login with Terminal & Telegram 2FA Entry (Windows/Linux)")
    else:
        print("\n⚠️  [EXPERIMENTAL] --login --auto is an experimental feature.")
        print("   UMBC's SSO or Duo configuration may change at any time, breaking this feature without notice.")
        print("   If login fails, run: python3 main.py --login\n")

    print(f"   📋 Credentials loaded (username: {usr})")
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

    with sync_playwright() as p:
        try:
            login_stage = "launching browser context"
            context, page = _launch_context(p, headless=headless)
            if force:
                context.clear_cookies()
                print("   🧹 Forced clean re-authentication: session cookies cleared.")
        except SystemExit:
            return False

        try:
            # Main credential submission loop with interactive retry on invalid credentials
            while True:
                login_stage = "opening Blackboard course landing page"
                page.goto(f"{BLACKBOARD_BASE}/ultra/course")
                page.wait_for_timeout(2500)

                # --- 1. CLICK PORTAL BUTTON IF PRESENT ---
                login_stage = "clicking UMBC portal button"
                print("   ↳ Looking for UMBC login portal button...")
                try:
                    if _click_exact_text_in_any_frame(page, "Log into Blackboard via myUMBC") or _click_exact_text_in_any_frame(page, "UMBC Login") or _click_exact_text_in_any_frame(page, "Sign in"):
                        print("   ↳ Clicking portal login button...")
                        page.wait_for_timeout(2000)
                except PlaywrightTimeout:
                    pass

                # --- 2. FILL USERNAME AND PASSWORD ---
                login_stage = "filling UMBC username and password"
                print(f"   ↳ Filling credentials for '{usr}'...")
                try:
                    usr_field = page.get_by_label(re.compile(r"Email Address / Username / Campus ID", re.I))
                    if usr_field.count() == 0:
                        usr_field = page.locator("input[type='text'], input[type='email'], input[name='j_username']").first
                    if usr_field.is_visible(timeout=6000):
                        usr_field.fill(usr)
                        page.wait_for_timeout(400)

                    pwd_field = page.get_by_label(re.compile(r"Password", re.I))
                    if pwd_field.count() == 0:
                        pwd_field = page.locator("input[type='password'], input[name='j_password']").first

                    if pwd_field.is_visible(timeout=2000):
                        pwd_field.fill(pwd)
                        try:
                            page.get_by_role("button", name=re.compile(r"^Log In$", re.I)).click(timeout=3000)
                        except Exception:
                            page.keyboard.press("Enter")
                    else:
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(1500)
                        try:
                            pwd_field.wait_for(state="visible", timeout=6000)
                            pwd_field.fill(pwd)
                            page.keyboard.press("Enter")
                        except PlaywrightTimeout:
                            pass
                except Exception as e:
                    print(f"   ⚠️ Exception while filling login fields: {e}")

                # --- 3. FAST TRANSITION MONITOR (<10s): Duo, Blackboard Direct, or SSO Auth Error ---
                login_stage = "waiting for SSO authentication response"
                duo_reached = False
                bb_reached = False
                sso_error_detected = False
                sso_err_type = ""
                sso_err_msg = ""

                for _ in range(20):  # 20 * 500ms = 10s max wait
                    page.wait_for_timeout(500)
                    cur_url = page.url.lower()

                    if "duosecurity.com" in cur_url:
                        duo_reached = True
                        break

                    if "blackboard.umbc.edu/ultra" in cur_url and _is_authenticated_session(page):
                        bb_reached = True
                        break

                    has_err, err_type, err_text = detect_sso_error(page)
                    if has_err:
                        sso_error_detected = True
                        sso_err_type = err_type
                        sso_err_msg = err_text
                        break

                if bb_reached:
                    print("   ✅ Already authenticated (active session detected). Skipping Duo.")
                    _save_session(context)
                    track_session_usage("login")
                    print("✨ Auto-Login Successful!")
                    context.close()
                    return True

                if duo_reached:
                    break

                if sso_error_detected:
                    print("\n" + "═" * 66)
                    print(f" ❌ [AUTHENTICATION FAILED] {sso_err_msg}")
                    if sso_err_type == "INVALID_CREDENTIALS":
                        print("   ↳ The UMBC username or password provided in config.json is incorrect.")
                    elif sso_err_type == "ACCOUNT_LOCKED":
                        print("   ↳ ⚠️ Your UMBC account appears to be locked due to too many failed attempts.")
                        print("   ↳ Please wait a few minutes or visit https://my.umbc.edu / contact DoIT.")
                    elif sso_err_type == "PASSWORD_EXPIRED":
                        print("   ↳ ⚠️ Your UMBC password has expired. Please reset it at https://my.umbc.edu.")
                    print("═" * 66 + "\n")

                    if tg_notifier:
                        try:
                            tg_notifier.send_raw_message(f"❌ <b>UMBC Login Failed</b>: {sso_err_msg}\nCheck credentials in <code>config.json</code>.")
                        except Exception:
                            pass

                    # Interactive recovery if in a terminal
                    if sys.stdin.isatty() and sso_err_type in ("INVALID_CREDENTIALS", "AUTH_FAILED"):
                        try:
                            retry_prompt = input(" 🔑 Would you like to enter your correct UMBC credentials now? [Y/n]: ").strip().lower()
                        except (KeyboardInterrupt, EOFError):
                            retry_prompt = "n"

                        if retry_prompt not in ("n", "no"):
                            new_usr, new_pwd, save_to_config, _ = prompt_credentials_tui(default_auto_exp=auto_exp)
                            if new_usr and new_pwd:
                                usr = new_usr
                                pwd = new_pwd
                                if save_to_config:
                                    save_auto_login_credentials(usr, pwd)
                                continue

                    page.screenshot(path="auth_error.png")
                    print("   📸 Screenshot saved to: auth_error.png")
                    context.close()
                    return False

                # Fallback check if on Duo
                if "duosecurity.com" in page.url:
                    break
                else:
                    print(f"   ⚠️ Did not reach Duo or detect definitive SSO error. Current URL: {page.url}")
                    try:
                        page.wait_for_url("**/duosecurity.com/**", timeout=4000)
                        break
                    except PlaywrightTimeout:
                        page.screenshot(path="duo_timeout_state.png")
                        print("   📸 Screenshot saved to: duo_timeout_state.png")
                        context.close()
                        return False

            # --- 4. REACHED DUO ---
            print("   ↳ Reached Duo 2FA page.")
            page.wait_for_timeout(2000)

            # --- 5. CLICK "OTHER OPTIONS" ---
            login_stage = "clicking Duo Other options"
            print("   ↳ Clicking 'Other options'...")
            try:
                if _click_exact_text_in_any_frame(page, "Other options", timeout=8000):
                    page.wait_for_timeout(1500)
                    print("   ↳ Clicked 'Other options'.")
                else:
                    print("   ↳ Duo may already display available methods.")
            except Exception as e:
                print(f"   ⚠️ Notice on 'Other options': {e}")
            finally:
                page.wait_for_timeout(1500)

            # --- 6. SELECT "TEXT MESSAGE PASSCODE" SPECIFICALLY ---
            login_stage = "selecting Duo text message passcode"
            print("   ↳ Selecting 'Text message passcode'...")
            from core.sms_listener import get_current_max_rowid
            start_rowid = get_current_max_rowid() if is_mac else 0
            try:
                passcode_input_check = page.locator("input[name='passcode'], input[id='passcode'], input[type='text'][autocomplete], input[id*='passcode']").first
                if not passcode_input_check.is_visible(timeout=1000):
                    if not _click_exact_text_in_any_frame(page, "Text message passcode", timeout=8000):
                        raise PlaywrightTimeout("Text message passcode control not found")
                    page.wait_for_timeout(2000)
                    print("   ↳ Clicked 'Text message passcode'. Passcode is being sent...")
                else:
                    print("   ↳ Passcode input already active.")
            except PlaywrightTimeout:
                duo_body = page.locator("body").inner_text()
                if "limit" in duo_body.lower() or "attempts" in duo_body.lower() or "denied" in duo_body.lower():
                    print(f"   ❌ Duo 2FA Notice: {duo_body[:140].strip()}")
                else:
                    print("   ❌ Could not find 'Text message passcode' button.")
                page.screenshot(path="duo_timeout_state.png")
                print("   📸 Screenshot saved to: duo_timeout_state.png")
                context.close()
                return False
            except Exception as e:
                print(f"   ❌ Could not select 'Text message passcode': {e}")
                page.screenshot(path="duo_timeout_state.png")
                print("   📸 Screenshot saved to: duo_timeout_state.png")
                context.close()
                return False

            # --- 7. WAIT FOR PASSCODE INPUT ---
            login_stage = "waiting for Duo passcode input"
            passcode_input = page.locator(
                "input[name='passcode'], input[id='passcode'], input[type='text'][autocomplete], input[id*='passcode']"
            ).first
            try:
                passcode_input.wait_for(state="visible", timeout=12000)
            except PlaywrightTimeout:
                print("   ❌ Timeout waiting for passcode input. Duo layout may have changed.")
                page.screenshot(path="duo_timeout_state.png")
                print("   📸 Screenshot saved to: duo_timeout_state.png")
                context.close()
                return False

            sent_to_hint = ""
            try:
                body_text = page.locator("body").inner_text()
                match = re.search(r"ending in (\d{4})", body_text)
                if match:
                    sent_to_hint = f" (sent to number ending in {match.group(1)})"
            except Exception:
                pass

            # --- 8 & 9. DUAL-CHANNEL PASSCODE ENTRY & RETRY LOOP ---
            login_stage = "submitting Duo passcode"
            import queue
            import threading

            max_attempts = 3
            passcode_accepted = False

            for attempt in range(1, max_attempts + 1):
                result_queue = queue.Queue()
                stop_event = threading.Event()
                trigger_time = time.time()

                # A. Real-time macOS SMS/iMessage Listener (macOS only)
                if is_mac:
                    def _sms_worker():
                        try:
                            from core.sms_listener import wait_for_duo_sms_passcode
                            c = wait_for_duo_sms_passcode(start_rowid=start_rowid, after_unix_timestamp=trigger_time, timeout_seconds=90)
                            if c and not stop_event.is_set():
                                result_queue.put(("sms", c))
                        except Exception:
                            pass
                    threading.Thread(target=_sms_worker, daemon=True).start()

                # B. Telegram prompt
                if tg_notifier:
                    header = "🔐 <b>UMBC Duo 2FA Passcode Required</b>" if attempt == 1 else f"❌ <b>Incorrect Duo Passcode (Attempt {attempt}/{max_attempts})</b>"
                    try:
                        tg_notifier.send_raw_message(
                            f"{header}\n\n"
                            f"Duo sent a 6-digit text passcode to your phone{sent_to_hint}.\n\n"
                            f"Reply with the 6-digit code or enter it in your terminal."
                        )
                        def _tg_worker():
                            c = tg_notifier.poll_for_passcode(timeout_sec=120, stop_event=stop_event)
                            if c and not stop_event.is_set():
                                result_queue.put(("telegram", c))
                        threading.Thread(target=_tg_worker, daemon=True).start()
                    except Exception:
                        pass

                print("\n" + "=" * 60)
                if attempt > 1:
                    print("  ❌ Incorrect passcode. Please check your phone for the latest code.")
                print(f"  📲 Duo text passcode sent to your phone{sent_to_hint} (Attempt {attempt}/{max_attempts}).")
                if is_mac:
                    print("  ⚡ Listening for incoming macOS SMS passcode in real-time...")
                else:
                    print("  💡 Enter the 6-digit passcode below, or reply via Telegram.")
                if tg_notifier:
                    print("  💡 You can also reply directly in Telegram or type it below.")
                print("=" * 60)

                # C. CLI terminal fallback
                def _cli_worker():
                    try:
                        prompt_label = "  🔑 Enter your Duo passcode (or wait for SMS auto-capture): " if is_mac else "  🔑 Enter your Duo passcode: "
                        cli_input = input(prompt_label).strip()
                        if cli_input and not stop_event.is_set():
                            result_queue.put(("cli", cli_input))
                    except (EOFError, KeyboardInterrupt):
                        pass

                threading.Thread(target=_cli_worker, daemon=True).start()

                code = None
                try:
                    src, code = result_queue.get(timeout=120)
                    stop_event.set()
                    if src == "sms":
                        print(f"\n   ⚡ \033[32mAuto-captured Duo SMS Passcode from macOS Messages: {code}\033[0m")
                        if tg_notifier:
                            tg_notifier.send_raw_message(f"⚡ <i>Auto-detected SMS passcode <code>{code}</code> from macOS Messages. Submitting...</i>")
                    elif src == "telegram":
                        print(f"\n   📲 Received Duo passcode from Telegram: {code}")
                except queue.Empty:
                    stop_event.set()
                    print("\n   ❌ Timeout waiting for passcode entry.")
                    context.close()
                    return False

                if not code:
                    print("   ❌ No passcode entered. Aborting.")
                    context.close()
                    return False

                # 2. Fill & Submit code
                print("   ↳ Submitting passcode...")
                passcode_input.fill(code)
                page.wait_for_timeout(400)

                try:
                    passcode_input.press("Enter")
                except Exception:
                    pass

                try:
                    _click_exact_text_in_any_frame(page, "Verify", timeout=3000)
                except Exception:
                    pass

                page.wait_for_timeout(2000)

                # 3. Check if passcode was rejected
                error_detected = False
                try:
                    err = page.locator("div.error-message, [role='alert'], div:has-text('Incorrect passcode'), div:has-text('Invalid passcode'), div:has-text('incorrect')").first
                    if err.is_visible(timeout=1000):
                        error_detected = True
                except Exception:
                    pass

                if not error_detected and ("duosecurity.com" not in page.url or not passcode_input.is_visible(timeout=1000)):
                    print("   ✅ Duo passcode accepted!")
                    passcode_accepted = True
                    break
                else:
                    print("   ⚠️  Duo rejected the passcode.")
                    if attempt == max_attempts:
                        print("   ❌ Maximum attempts reached. Aborting.")
                        if tg_notifier:
                            tg_notifier.send_raw_message("❌ <b>Login Failed</b>: Maximum Duo passcode attempts exceeded.")
                        context.close()
                        return False

            if not passcode_accepted:
                context.close()
                return False

            # --- 10. TRUST BROWSER (if prompted) ---
            login_stage = "handling Duo trust browser prompt"
            print("   ⏳ Checking for 'Trust browser' prompt...")
            try:
                if (
                    _click_exact_text_in_any_frame(page, "Yes, this is my device", timeout=5000)
                    or _click_exact_text_in_any_frame(page, "Trust browser", timeout=2000)
                    or _click_exact_text_in_any_frame(page, "Remember this device", timeout=2000)
                ):
                    print("   ↳ Clicked 'Yes, this is my device'.")
                    _save_session(context)
            except Exception:
                pass

            # --- 11. WAIT FOR BLACKBOARD DASHBOARD ---
            login_stage = "waiting for Blackboard dashboard"
            print("   ⏳ Waiting for Blackboard to load...")
            try:
                page.wait_for_url("**/ultra/**", timeout=60_000)
                print("   ✅ Successfully authenticated and reached Blackboard!")
            except PlaywrightTimeout:
                print(f"   ❌ Timeout waiting for Blackboard Dashboard. Current URL: {page.url}")
                page.screenshot(path="timeout_state.png")
                print("   📸 Screenshot saved to: timeout_state.png")
                context.close()
                return False

            page.wait_for_timeout(3000)  # let Blackboard fully cookie up
            _save_session(context)
            track_session_usage("login")
            if tg_notifier:
                try:
                    tg_notifier.send_raw_message("✅ <b>Blackboard Auto-Login Successful!</b>\nYour session is cached and ready.")
                except Exception:
                    pass
            print("✨ Auto-Login Successful!")
            return True

        except Exception as e:
            print(f"❌ Auto-Login encountered an unhandled error during '{login_stage}': {e}")
            return False

        finally:
            context.close()


def _save_session(context) -> None:
    """Persist browser cookies to disk so the session survives between runs."""
    try:
        cookies = context.cookies()
        (Path(SESSION_DIR) / "cookies.json").write_text(json.dumps(cookies))
    except Exception as e:
        print(f"   ⚠️  Could not save session cookies: {e}")


async def _require_session_async(cdp_url: str = None) -> bool:
    """Fast check for session before async scraping with auto-recovery and TUI setup."""
    if cdp_url:
        return True

    # 1. Fast sub-150ms HTTP probe
    valid, user_data = quick_check_session_http()
    if valid:
        return True

    # 2. Browser fallback check
    valid = await check_session_async(quiet=True, headless=True)
    if valid:
        return True

    # 3. Session missing or expired -> Check config & credentials
    print("\n⚠️ No active Blackboard session detected.")
    created_blank, cfg = ensure_config_exists(notify=True)

    if has_auto_login_credentials(cfg):
        print("🔄 Valid credentials found in config.json. Attempting automated login refresh (auto-exp)...")
        await asyncio.to_thread(
            login_auto,
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
                await asyncio.to_thread(
                    login_auto,
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


def _require_session(cdp_url: str = None) -> bool:
    """Check session before scraping with auto-recovery and TUI setup."""
    if cdp_url:
        return True

    valid, _ = quick_check_session_http()
    if valid:
        return True

    valid = check_session(quiet=True, headless=True)
    if valid:
        return True

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
