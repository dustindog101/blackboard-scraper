"""Plain cookie-file transfers without changing normal login behavior."""

import http.cookiejar
import json
import math
import os
import re
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from core.config import BLACKBOARD_BASE, SESSION_DIR

MAX_FILE_BYTES = 1024 * 1024
COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class SessionTransferError(Exception):
    """A safe, actionable error that never includes cookie values."""


def _read_bytes(path: Path) -> bytes:
    try:
        with path.open("rb") as file:
            data = file.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise SessionTransferError("Cannot read cookie file. Check the path and file permissions.") from None
    if len(data) > MAX_FILE_BYTES:
        raise SessionTransferError("Cookie file is too large (maximum 1 MiB).")
    return data


def _cookies_from_bytes(data: bytes) -> list[dict]:
    try:
        cookies = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError):
        raise SessionTransferError("Cookie file must contain a JSON list of cookies.") from None
    if not isinstance(cookies, list) or not cookies or len(cookies) > 1000:
        raise SessionTransferError("Cookie file must contain a nonempty list of at most 1000 cookies.")
    host = urlparse(BLACKBOARD_BASE).hostname
    selected = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise SessionTransferError("Invalid cookie record.")
        name, value, domain, path = (cookie.get(key) for key in ("name", "value", "domain", "path"))
        if not all(isinstance(field, str) for field in (name, value, domain, path)):
            raise SessionTransferError("Each cookie needs string name, value, domain, and path fields.")
        if (not COOKIE_NAME.fullmatch(name) or not domain or not path.startswith("/")
                or any(ord(ch) < 32 or ord(ch) == 127 for field in (value, domain, path) for ch in field)
                or ";" in value):
            raise SessionTransferError("Invalid cookie name, value, domain, or path.")
        for flag in ("secure", "httpOnly"):
            if flag in cookie and not isinstance(cookie[flag], bool):
                raise SessionTransferError("Invalid cookie boolean attribute.")
        if "sameSite" in cookie and cookie["sameSite"] not in ("Strict", "Lax", "None"):
            raise SessionTransferError("Invalid cookie SameSite attribute.")
        expiry = cookie.get("expires", -1)
        if (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                or not -1 <= expiry <= 32503680000 or not math.isfinite(expiry)
                or (expiry < 0 and expiry != -1)):
            raise SessionTransferError("Invalid cookie expiration.")
        normalized = domain.lower()
        if normalized == host or (normalized.startswith(".") and
                                  (normalized[1:] == host or host.endswith(normalized))):
            selected.append(cookie)
    if not selected:
        raise SessionTransferError("No Blackboard cookies found in this file.")
    return selected


def _serialize(cookies: list[dict]) -> bytes:
    try:
        return (json.dumps(cookies, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise SessionTransferError("Cookie file contains unsupported JSON values.") from None


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace one file only after the complete new contents are flushed."""
    temporary = None
    try:
        if path.is_symlink():
            raise SessionTransferError("Refusing to replace a cookie-file symlink.")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".bb-cookies-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except OSError:
        raise SessionTransferError("Cannot write cookie file. Check permissions, disk space, and open files.") from None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _identity(user: dict) -> str:
    if isinstance(user, dict):
        for key in ("id", "userName", "studentId"):
            if isinstance(user.get(key), str) and user[key]:
                return user[key]
    raise SessionTransferError("Blackboard did not return an authenticated account.")


def _probe_cookies(cookies: list[dict]) -> dict:
    """Check candidate cookies without touching saved state, telemetry, or SSO."""
    jar = http.cookiejar.CookieJar()
    for cookie in cookies:
        expiry = cookie.get("expires", -1)
        domain = cookie["domain"].lower()
        dotted = domain.startswith(".")
        jar.set_cookie(http.cookiejar.Cookie(
            0, cookie["name"], cookie["value"], None, False, domain, dotted, dotted,
            cookie["path"], True, cookie.get("secure", False), int(expiry) if expiry >= 0 else None,
            expiry < 0, None, None, {}, False,
        ))
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NoRedirect())
    request = urllib.request.Request(
        f"{BLACKBOARD_BASE}/learn/api/public/v1/users/me",
        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise SessionTransferError("Blackboard could not validate the imported session.")
            body = response.read(MAX_FILE_BYTES + 1)
            if len(body) > MAX_FILE_BYTES:
                raise SessionTransferError("Blackboard returned an unexpected validation response.")
            user = json.loads(body)
            _identity(user)
            return user
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (301, 302, 303, 307, 308, 401):
            raise SessionTransferError("Session expired or rejected. Log in on your Mac and export again.") from None
        raise SessionTransferError("Blackboard refused validation or is unavailable. Your session was not replaced.") from None
    except (OSError, ValueError):
        raise SessionTransferError("Could not validate with Blackboard. Check the connection and retry.") from None


@contextmanager
def _managed_browser():
    from core.session import _launch_context, sync_playwright

    if sync_playwright is None:
        raise SessionTransferError("Browser unavailable. Install Playwright and run: playwright install chromium")
    try:
        with sync_playwright() as playwright:
            try:
                context, _ = _launch_context(playwright, headless=True)
            except (Exception, SystemExit):
                raise SessionTransferError(
                    "Cannot open the managed browser. Close its windows/background jobs and retry; "
                    "if no browser is installed, run: playwright install chromium"
                ) from None
            try:
                yield context
            finally:
                context.close()
    except SessionTransferError:
        raise
    except Exception:
        raise SessionTransferError("Managed browser failed during import. Close background jobs and retry.") from None


def _probe_browser(context) -> dict:
    try:
        response = context.request.get(
            f"{BLACKBOARD_BASE}/learn/api/public/v1/users/me", timeout=10000, max_redirects=0,
        )
        try:
            if response.status != 200:
                raise SessionTransferError("Browser rejected this session. Log in on Mac and export again.")
            user = response.json()
            _identity(user)
            return user
        finally:
            response.dispose()
    except SessionTransferError:
        raise
    except Exception:
        raise SessionTransferError("Could not validate imported cookies in the browser. Retry when connected.") from None


def export_session(filename: str, *, overwrite: bool = False) -> int:
    source = Path(SESSION_DIR) / "cookies.json"
    destination = Path(filename).expanduser().resolve()
    if destination == source.resolve():
        raise SessionTransferError("Export must use a different file from the saved session.")
    if destination.exists() and not overwrite:
        raise SessionTransferError("Export file already exists. Use --overwrite to replace it.")
    cookies = _cookies_from_bytes(_read_bytes(source))
    _atomic_write(destination, _serialize(cookies))
    return len(cookies)


def import_session(filename: str) -> tuple[int, dict]:
    cookies = _cookies_from_bytes(_read_bytes(Path(filename).expanduser()))
    data = _serialize(cookies)
    destination = Path(SESSION_DIR) / "cookies.json"
    previous = _read_bytes(destination) if destination.exists() else None
    user = _probe_cookies(cookies)
    written = False
    previous_browser = []
    try:
        with _managed_browser() as context:
            previous_browser = context.cookies()
            try:
                context.clear_cookies()
                context.add_cookies(cookies)
                if _identity(_probe_browser(context)) != _identity(user):
                    raise SessionTransferError("HTTP and browser account checks disagree. Session was not replaced.")
                # Keep any refreshed cookies set by the verification response.
                cookies = _cookies_from_bytes(_serialize(context.cookies()))
                data = _serialize(cookies)
                current = _read_bytes(destination) if destination.exists() else None
                if current != previous:
                    raise SessionTransferError("Saved session changed during import. Stop background jobs and retry.")
                _atomic_write(destination, data)
                written = True
                if _read_bytes(destination) != data:
                    raise SessionTransferError("Imported cookie file could not be verified.")
            except Exception as error:
                try:
                    context.clear_cookies()
                    context.add_cookies(previous_browser)
                    if written:
                        if previous is None:
                            destination.unlink(missing_ok=True)
                        else:
                            _atomic_write(destination, previous)
                        written = False
                except Exception:
                    raise SessionTransferError("Import failed and rollback could not finish. Close background jobs and retry.") from None
                if isinstance(error, SessionTransferError):
                    raise error from None
                raise SessionTransferError("Import failed; previous cookies preserved. Check the file, connection, and open jobs.") from None
    except Exception as error:
        # A close/flush failure can happen after the body committed the file.
        if written:
            try:
                if previous is None:
                    destination.unlink(missing_ok=True)
                else:
                    _atomic_write(destination, previous)
                with _managed_browser() as rollback_context:
                    rollback_context.clear_cookies()
                    rollback_context.add_cookies(previous_browser)
            except Exception:
                raise SessionTransferError(
                    "Import failed during browser shutdown; rollback could not finish. Close background jobs and retry."
                ) from None
        if isinstance(error, SessionTransferError):
            raise error from None
        raise SessionTransferError("Import failed; close background jobs and retry.") from None
    return len(cookies), user
