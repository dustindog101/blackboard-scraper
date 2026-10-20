"""Session health, request identity and the cross-process login lock.

No Playwright here: everything runs without a browser.
"""

import contextlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from core.config import BLACKBOARD_BASE, SESSION_DIR

# probe_session() outcomes.
VALID = "valid"
EXPIRED = "expired"  # missing cookies, 401/403, or a redirect to login
UNKNOWN = "unknown"  # network error, timeout, 5xx: says nothing about the login

PROBE_PATH = "/learn/api/public/v1/users/me"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
NETWORK_MESSAGE = (
    "Could not reach Blackboard (network or service problem). Your saved login was not changed; try again shortly."
)


# ---------------------------------------------------------------- identity

def _metadata_file() -> Path:
    return Path(SESSION_DIR) / "session_metadata.json"


def user_agent() -> str:
    """The User-Agent of the browser that created the cookies, else a current default."""
    try:
        saved = json.loads(_metadata_file().read_text()).get("user_agent")
        if isinstance(saved, str) and saved.startswith("Mozilla/"):
            return saved
    except (OSError, ValueError, AttributeError):
        pass
    return DEFAULT_USER_AGENT


def record_user_agent(raw: str) -> None:
    """Remember the login browser's User-Agent (headless marker removed)."""
    if not isinstance(raw, str) or not raw.startswith("Mozilla/"):
        return
    agent = raw.replace("HeadlessChrome", "Chrome")
    try:
        path = _metadata_file()
        data = json.loads(path.read_text()) if path.exists() else {}
        data["user_agent"] = agent
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except (OSError, ValueError):
        pass


def _domain_matches(cookie_domain: str, host: str) -> bool:
    domain = cookie_domain.lower()
    if domain.startswith("."):
        domain = domain[1:]
        return host == domain or host.endswith("." + domain)
    return host == domain


def _path_matches(cookie_path: str, request_path: str) -> bool:
    cookie_path = cookie_path or "/"
    if request_path == cookie_path:
        return True
    prefix = cookie_path if cookie_path.endswith("/") else cookie_path + "/"
    return request_path.startswith(prefix)


def load_cookies() -> list[dict]:
    try:
        data = json.loads((Path(SESSION_DIR) / "cookies.json").read_text())
    except (OSError, ValueError):
        return []
    return [c for c in data if isinstance(c, dict) and c.get("name") is not None] if isinstance(data, list) else []


def cookie_header(url: str | None = None, cookies: list[dict] | None = None, now: float | None = None) -> str | None:
    """Cookie header for one URL: only cookies whose domain, path and expiry apply.

    Defaults to the Blackboard REST API, which is what the REST Fast-Path calls.
    Returns None when nothing applies (no session).
    """
    target = urlsplit(url or BLACKBOARD_BASE + PROBE_PATH)
    host, path = (target.hostname or "").lower(), target.path or "/"
    now = time.time() if now is None else now
    chosen: dict[str, tuple[int, str]] = {}
    for cookie in load_cookies() if cookies is None else cookies:
        expires = cookie.get("expires", -1)
        if isinstance(expires, (int, float)) and expires > 0 and expires <= now:
            continue
        if cookie.get("secure") and target.scheme != "https":
            continue
        if not _domain_matches(str(cookie.get("domain", "")), host):
            continue
        cookie_path = str(cookie.get("path") or "/")
        if not _path_matches(cookie_path, path):
            continue
        name = str(cookie["name"])
        if name not in chosen or len(cookie_path) >= chosen[name][0]:  # most specific path wins
            chosen[name] = (len(cookie_path), f"{name}={cookie.get('value', '')}")
    if not chosen:
        return None
    return "; ".join(item for _, item in chosen.values())


# ------------------------------------------------------------------- probe

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def probe_session(timeout: float = 3.5, *, cookies: list[dict] | None = None, url: str | None = None) -> tuple[str, dict | None]:
    """One REST call to /users/me with the saved cookies. Never logs in.

    Returns (VALID, user) | (EXPIRED, None) | (UNKNOWN, None).
    """
    target = url or BLACKBOARD_BASE + PROBE_PATH
    header = cookie_header(url=target, cookies=cookies)
    if not header:
        return EXPIRED, None
    request = urllib.request.Request(
        target,
        headers={"Cookie": header, "User-Agent": user_agent(), "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                return UNKNOWN, None
            try:
                data = json.loads(response.read(1_000_000).decode("utf-8"))
            except ValueError:
                return EXPIRED, None  # HTML login page served with 200
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (301, 302, 303, 307, 308, 401, 403):
            return EXPIRED, None
        return UNKNOWN, None
    except Exception:  # URLError, timeout, connection reset, TLS
        return UNKNOWN, None
    if isinstance(data, dict) and any(data.get(key) for key in ("id", "studentId", "userName")):
        return VALID, data
    return EXPIRED, None


# -------------------------------------------------------------- login lock

class LoginInProgress(Exception):
    """Another bb process holds the login lock."""


def _try_lock(handle) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def login_lock(wait_seconds: float = 20.0, announce=print):
    """Hold the login lock; at most one bb login (and Duo text) runs at a time.

    A second caller says "login already in progress", waits up to wait_seconds
    for the first to finish, then raises LoginInProgress. The OS drops the lock
    if the holder dies, so there are no stale lock files to clean up.
    """
    path = Path(SESSION_DIR) / "login.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115 - closed in finally
    try:
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + wait_seconds
        announced = False
        while not _try_lock(handle):
            if not announced:
                announce("Login already in progress in another bb process; waiting for it to finish...")
                announced = True
            if time.monotonic() >= deadline:
                raise LoginInProgress("Login already in progress in another bb process. Try again when it finishes.")
            time.sleep(0.25)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        with contextlib.suppress(OSError):
            handle.close()
