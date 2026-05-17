import json
import time
import subprocess
import zipfile
import threading
import queue
import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout, Error as PlaywrightError

from core.config import BLACKBOARD_BASE, LOGIN_INDICATORS, SESSION_DIR, load_config

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
                print(f"\n❌ [ERROR] The Playwright Browser Session is locked.")
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


def check_session(quiet: bool = False, debug: bool = False, headless: bool = True) -> bool:
    """
    Test if the saved session is still valid.
    Launches headless browser, navigates to Blackboard homepage,
    checks if we land on Ultra (valid) or get redirected to SSO (expired).
    """
    try:
        with sync_playwright() as p:
            try:
                context, page = _launch_context(p, headless=headless)
            except SystemExit:
                # Profile in use
                if not quiet:
                    print("⚠️  Cannot check session: Profile is currently in use by another process.")
                return False

            if not quiet:
                print("⏳ Checking session validity...")
                if debug:
                    mode = "headless" if headless else "visible"
                    print(f"   [session-debug] Browser mode: {mode}")

            try:
                page.goto(f"{BLACKBOARD_BASE}/ultra/course", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(3000)
            except Exception as e:
                if not quiet: print(f"   ⚠️ Network error: {e}")
                context.close()
                return False

            valid = _is_authenticated_session(page, debug=debug)
            if debug and not quiet:
                bb_count, umbc_count = _session_cookie_summary(context)
                print(f"   [session-debug] Cookie summary: blackboard.umbc.edu={bb_count}, *.umbc.edu={umbc_count}")

            if valid:
                track_session_usage("usage")
                if not quiet:
                    meta_file = SESSION_DIR / "session_metadata.json"
                    data = {}
                    if meta_file.exists():
                        data = json.loads(meta_file.read_text())
                    login_time = data.get("login_time_human", "Unknown")
                    print("✅ Session is ACTIVE.")
                    print(f"   Current URL: {page.url}")
                    print(f"   Logged in:   {login_time}")
            else:
                if not quiet:
                    print("❌ Session EXPIRED or missing.")
                    print(f"   Redirected to: {page.url}")
                    print("   Run `python3 main.py --login` to re-authenticate.")

            context.close()
            return valid
    except Exception as e:
        if not quiet: print(f"   ⚠️ Error checking session: {e}")
        return False


def login(force: bool = False, username: str = None, password: str = None, cdp_url: str = None):
    """
    Handle the SSO login flow.
    If already logged in, does nothing unless force=True.
    """
    if cdp_url:
        print("🔌 Ignoring --login since you are connected to an existing CDP browser.")
        print("   Please login directly in your attached browser window.")
        return

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


def listen_for_duo_code(code_queue: queue.Queue, timeout: int = 300):
    """
    Background thread to listen for a Duo SMS using `imsg chats` and `imsg history`.
    Pushes the first 7-digit passcode found into the `code_queue` and terminates.
    Since Duo dynamically changes shortcodes, we poll the top 3 most recent SMS chats.
    """
    start_time = time.time()
    deadline = start_time + timeout
    
    # We will only look at messages created AFTER we started listening
    # to avoid pulling an old code. We use a timezone-aware ISO string parser
    
    seen_ids = set()
    
    while time.time() < deadline:
        try:
            # 1. Get recent SMS chats (limit 10)
            chats_proc = subprocess.run(["imsg", "chats", "--limit", "10", "--json"], capture_output=True, text=True)
            if chats_proc.returncode != 0:
                time.sleep(2)
                continue
                
            active_chat_ids = []
            for line in chats_proc.stdout.strip().split('\n'):
                if not line: continue
                try:
                    chat = json.loads(line)
                    # Don't strictly check service type, just ensure it has an identifier
                    if "identifier" in chat:
                        active_chat_ids.append(chat["id"])
                except json.JSONDecodeError:
                    continue
                    
            # 2. Check the message history for each of those chats
            for chat_id in active_chat_ids:
                hist_proc = subprocess.run(["imsg", "history", "--chat-id", str(chat_id), "--limit", "5", "--json"], capture_output=True, text=True)
                for line in hist_proc.stdout.strip().split('\n'):
                    if not line: continue
                    try:
                        msg = json.loads(line)
                        msg_id = msg.get("id")
                        if not msg_id or msg_id in seen_ids:
                            continue
                        
                        seen_ids.add(msg_id)
                        text = msg.get("text", "")
                        
                        # Only accept messages created recently
                        if "passcode" in text.lower():
                            match = re.search(r'\b(\d{4,9})\b', text)
                            if match:
                                msg_time_str = msg.get("created_at", "")
                                # Basic fast check: if it was sent after our start timestamp, it's ours
                                # We can't guarantee server time alignment, but we assume it's created ~now
                                # Let's parse it precisely
                                try:
                                    # Strip trailing Z and fractional seconds for simple parsing
                                    # Example: 2026-02-28T00:55:53.414Z -> 2026-02-28T00:55:53
                                    clean_time = msg_time_str.split('.')[0].replace('Z', '')
                                    dt = datetime.fromisoformat(clean_time).replace(tzinfo=timezone.utc)
                                    msg_ts = dt.timestamp()
                                    
                                    # ONLY accept messages received AFTER we launched the script (with 15 sec buffer for clock skew)
                                    if msg_ts >= start_time - 15:
                                        code_queue.put(match.group(1))
                                        return
                                    else:
                                        print(f"   [Debug] Ignored old passcode {match.group(1)} from {msg_time_str}")
                                except Exception as e:
                                    print(f"   ⚠️ Could not parse time: {e}")
                    except Exception:
                        continue
                        
        except Exception as e:
            pass
            
        time.sleep(2)
        
    code_queue.put(Exception("Timeout listening for SMS."))


def login_auto(username: str = None, password: str = None, duo_sender: str = None, headless: bool = False, cdp_url: str = None):
    """
    Experimental: Fully automated login via SSO + Duo SMS.
    Uses `imsg watch` to intercept 2FA codes.
    """
    print("\n⚠️  [EXPERIMENTAL] --loginauto is an experimental feature.")
    print("   Automated credential handling carries risk. Passwords in config.json are plaintext.")
    print("   UMBC's SSO or Duo configuration may change at any time, breaking this feature without notice.")
    print("   If login fails, run: python3 main.py --login\n")
    
    if cdp_url:
        print("🔌 Ignoring --loginauto since you are connected to an existing CDP browser.")
        return

    # Check dependencies
    try:
        subprocess.run(["imsg", "--version"], capture_output=True, check=True)
    except FileNotFoundError:
        print("❌ `imsg` CLI tool is NOT installed. Run: brew install nicholasgasior/brew/imsg")
        return

    config = load_config().get("auto_login", {})
    usr = username or config.get("username")
    pwd = password or config.get("password")

    if not usr or not pwd:
        print("❌ Username and Password are required. Either pass them via CLI or set them in config.json['auto_login']")
        return

    print("🚀 Starting Automated SSO Login...")
    with sync_playwright() as p:
        try:
            context, page = _launch_context(p, headless=headless)
        except SystemExit:
            return

        try:
            page.goto(f"{BLACKBOARD_BASE}/ultra/course")
            
            # --- 1. UMBC LOGIN ---
            print("   ↳ Navigating UMBC Login...")
            
            # Click the initial portal button if present
            try:
                portal_btn = page.locator("a:has-text('Log into Blackboard via myUMBC'), a:has-text('UMBC Login')").nth(0)
                if portal_btn.is_visible(timeout=5000):
                    portal_btn.click()
            except PlaywrightTimeout:
                pass
            
            # They might ask for username first, or username+password depending on entry point
            try:
                # Wait for username field
                usr_field = page.locator("input[name='j_username'], input[type='email'], input#username").nth(0)
                usr_field.wait_for(state="visible", timeout=10000)
                usr_field.fill(usr)
                
                page.wait_for_timeout(1000)
                pwd_field = page.locator("input[name='j_password'], input[type='password'], input#password").nth(0)
                
                if pwd_field.is_visible(timeout=2000):
                    pwd_field.fill(pwd)
                    page.keyboard.press("Enter")
                else:
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(2000)
                    if pwd_field.is_visible(timeout=5000):
                        pwd_field.fill(pwd)
                        page.keyboard.press("Enter")
            except PlaywrightTimeout:
                print("   ⚠️  Did not find standard login fields. We might already be logged in or at Duo.")
            
            # --- 2. REACHING DUO ---
            try:
                # We might already be logged in (e.g., active session bypassed SSO)
                if "blackboard.umbc.edu/ultra" in page.url:
                    print("   ✅ Already authenticated (active session detected). Skipping Duo.")
                else:
                    page.wait_for_url("**/duosecurity.com/**", timeout=15000)
            except PlaywrightTimeout:
                pass
            
            # If we didn't end up on Duo and are already at Blackboard, skip the rest
            if "blackboard.umbc.edu/ultra" in page.url:
                if _is_authenticated_session(page):
                    track_session_usage("login_auto")
                    print("✨ Session saved successfully.")
                context.close()
                return

            print("   ↳ Reaching Duo 2FA...")
            page.wait_for_timeout(3000) # Give Duo time to settle

            # --- 3. START LISTENER ---
            print(f"   📡 Starting global SMS listener for Duo passcode (up to 5m)...")
            code_queue = queue.Queue()
            listener = threading.Thread(target=listen_for_duo_code, args=(code_queue, 300))
            listener.daemon = True
            listener.start()

            # --- 4. TRIGGER SMS ---
            print("   ↳ Requesting SMS Passcode...")
            
            # 4A. Click "Other options"
            try:
                # wait_for_function does not enforce strict UI visibility, just DOM presence
                page.wait_for_function('() => !!document.querySelector("a.button--link[href*=\'/all_methods\']")')
                page.evaluate("""() => {
                    const btn = document.querySelector("a.button--link[href*='/all_methods']");
                    if (btn) btn.click();
                }""")
                page.wait_for_timeout(1500)
            except Exception as e:
                pass # Often Duo defaults to showing all methods depending on caching
            
            # 4B. Click "Text message passcode" / SMS
            try:
                page.wait_for_function("""() => {
                    return !!Array.from(document.querySelectorAll('div.method-label')).find(el => el.textContent.includes('Text message passcode') || el.textContent.includes('Send me a passcode'));
                }""")
                page.evaluate("""() => {
                    const el = Array.from(document.querySelectorAll('div.method-label')).find(el => 
                        el.textContent.includes('Text message passcode') || el.textContent.includes('Send me a passcode')
                    );
                    if (el && el.closest('a.auth-method')) {
                        el.closest('a.auth-method').click();
                    } else if (el) {
                        el.click();
                    } else {
                        throw new Error("Could not find Text message passcode label");
                    }
                }""")
            except Exception as e:
                print(f"   ⚠️ Click for 'Text message passcode' failed: {e}")

            # Look for the input to ensure we're at passcode stage
            passcode_input = page.locator("#passcode-input, input[id*='passcode']").first
            
            try:
                passcode_input.wait_for(state="visible", timeout=10000)
            except PlaywrightTimeout:
                print("   ❌ Timeout waiting for the passcode input field to appear. Duo might be stuck or changed.")
                page.screenshot(path="duo_timeout_state.png")
                listener.join(1)
                context.close()
                return

            # --- 5. WAIT FOR CODE ---
            print("   ⏳ Waiting for 6-digit code via iMessage (up to 5m)...")
            try:
                code_result = code_queue.get(timeout=300)
                if isinstance(code_result, Exception):
                    raise code_result
                if not code_result:
                    raise Exception("Listener returned empty code.")
                    
                code = code_result
                print(f"   💬 Received Code: {code}")
            except queue.Empty:
                print("   ❌ Timeout: Did not receive an SMS code within 5 minutes.")
                listener.join(1)
                context.close()
                return
            
            # --- 6. ENTER CODE ---
            print("   ↳ Submitting Code...")
            passcode_input.fill(code)
            
            verify_btn = page.locator(".verify-button, button[type='submit']:has-text('Verify')").first
            if verify_btn.is_visible():
                verify_btn.click()
            else:
                page.keyboard.press("Enter")

            # --- 7. WAIT FOR SUCCESS & TRUST BROWSER ---
            print("   ⏳ Checking for 'Trust browser' option and waiting for Dashboard...")
            
            # Attempt to click "Yes, this is my device" if it appears before Dashboard loads
            try:
                page.wait_for_timeout(3000)
                clicked_trust = page.evaluate("""() => {
                    const btn = document.querySelector('#trust-browser-button');
                    if (btn) {
                        btn.click();
                        return true;
                    }
                    return false;
                }""")
                if clicked_trust:
                    print("   ↳ Clicked 'Yes, this is my device' via JS evaluate.")
            except Exception as e:
                print(f"   ⚠️ Trust device click check failed: {e}")
                
            try:
                page.wait_for_url("**/ultra/**", timeout=30_000)
                print("   ✅ Successfully authenticated and reached Blackboard!")
            except PlaywrightTimeout:
                print(f"   ❌ Timeout waiting for Blackboard Dashboard to load. Current URL: {page.url}")
                page.screenshot(path="timeout_state.png")
                print("   📸 Saved screenshot of current state to: timeout_state.png")
                context.close()
                return
            
            page.wait_for_timeout(5000) # give Bb time to fully cookie up
            
            # Save session
            cookies = context.cookies()
            (Path(SESSION_DIR) / "cookies.json").write_text(json.dumps(cookies))
            
            track_session_usage("login")
            print("✨ Auto-Login Successful!")
                
        except Exception as e:
            print(f"❌ Auto-Login encountered an unhandled error: {e}")
            
        finally:
            context.close()


def _require_session(cdp_url: str = None) -> bool:
    """Check session before scraping. Returns True if valid."""
    if cdp_url: return True
    if not check_session(quiet=True, headless=True):
        print("❌ Session invalid or expired. Run: python3 main.py --login")
        if not cdp_url:
            print("   Tip: run with --visible to let the browser handle login.")
        return False
    return True
