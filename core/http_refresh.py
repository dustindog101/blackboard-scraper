"""Browserless session refresh: renew Blackboard through the UMBC IdP with plain HTTPS.

When the Blackboard cookies are gone but the UMBC IdP session is still alive,
the SAML round trip needs no credentials, no Duo and no browser:

    Blackboard /auth-saml  ->  IdP (webauth.umbc.edu)  ->  SAMLResponse POST back

The IdP keeps its session in the browser's localStorage (`shib_idp_session_ss`)
as well as in cookies, so a browser login also saves that value
(`record_idp_storage`). Here it is replayed through the IdP's "client storage"
forms exactly as the page's JavaScript would, and the updated value the IdP
writes back is saved again.

Anything that needs a person (credential form, Duo, unknown host, unknown page)
stops with NEEDS_LOGIN so the caller falls back to the browser login.
"""

import http.cookiejar
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from core.config import BLACKBOARD_BASE, SESSION_DIR
from core.session_state import UNKNOWN, VALID, cookie_header, load_cookies, user_agent

REFRESHED = "refreshed"
NEEDS_LOGIN = "needs_login"
IDP_ORIGIN = "https://webauth.umbc.edu"
SAML_ENTRY = BLACKBOARD_BASE + "/auth-saml/saml/login?apId=_4893_1&redirectUrl=" + urllib.parse.quote(BLACKBOARD_BASE + "/ultra", safe="")
MAX_HOPS = 12
_ALLOWED_HOSTS = ("blackboard.umbc.edu", "webauth.umbc.edu")
_LS_KEYS = ("shib_idp_session_ss",)


class _Form(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = attrs.get("action") or ""
        elif tag == "input" and attrs.get("name") and self.action is not None:
            self.fields[attrs["name"]] = attrs.get("value") or ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _storage_file() -> Path:
    return Path(SESSION_DIR) / "idp_storage.json"


def load_idp_storage() -> dict[str, str]:
    try:
        data = json.loads(_storage_file().read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in _LS_KEYS and isinstance(v, str)} if isinstance(data, dict) else {}


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_idp_storage(values: dict[str, str]) -> None:
    kept = {k: v for k, v in values.items() if k in _LS_KEYS and v}
    if kept:
        _write_private(_storage_file(), json.dumps(kept))


def record_idp_storage(context) -> bool:
    """After a browser login: keep the IdP's localStorage session for later HTTP refreshes."""
    try:
        for origin in context.storage_state().get("origins", []):
            if origin.get("origin") == IDP_ORIGIN:
                save_idp_storage({i["name"]: i["value"] for i in origin.get("localStorage", [])})
                return True
    except Exception:
        pass
    return False


def forget_idp_storage() -> None:
    try:
        _storage_file().unlink()
    except OSError:
        pass


def _jar_from(cookies: list[dict]) -> http.cookiejar.CookieJar:
    jar = http.cookiejar.CookieJar()
    for c in cookies:
        domain = str(c.get("domain", ""))
        dotted = domain.startswith(".")
        expires = c.get("expires", -1)
        jar.set_cookie(http.cookiejar.Cookie(
            0, c["name"], c.get("value", ""), None, False, domain, dotted, dotted, c.get("path") or "/", True,
            bool(c.get("secure")), int(expires) if isinstance(expires, (int, float)) and expires > 0 else None,
            not (isinstance(expires, (int, float)) and expires > 0), None, None, {}, False,
        ))
    return jar


def _merge_cookies(old: list[dict], jar: http.cookiejar.CookieJar) -> list[dict]:
    """Old cookies, with everything the refresh set or renewed replacing its match."""
    merged = {(c["name"], c.get("domain"), c.get("path")): c for c in old}
    for c in jar:
        key = (c.name, c.domain, c.path)
        previous = merged.get(key, {})
        merged[key] = {
            "name": c.name, "value": c.value or "", "domain": c.domain, "path": c.path,
            "expires": c.expires if c.expires else -1,
            "httpOnly": previous.get("httpOnly", c.has_nonstandard_attr("HttpOnly")),
            "secure": bool(c.secure), "sameSite": previous.get("sameSite", "Lax"),
        }
    return list(merged.values())


def _request(opener, url, data=None, timeout=10):
    request = urllib.request.Request(url, data=data, headers={"User-Agent": user_agent(), "Accept": "text/html,application/json"})
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.headers, response.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            return error.code, error.headers, error.read(2_000_000).decode("utf-8", "replace")
        finally:
            error.close()


def _host_ok(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and parts.hostname in _ALLOWED_HOSTS


def _next_step(url: str, body: str, storage: dict[str, str], written: dict[str, str]):
    """(post_url, fields) for the auto-submit/continue form on this page, else None."""
    form = _Form()
    form.feed(body)
    fields = form.fields
    if form.action is None or "j_username" in fields or "j_password" in fields:
        return None  # credential form or no form: a person is needed
    for key, value in re.findall(r'writeLocalStorage\("([^"]+)",\s*"([^"]*)"\)', body):
        written[key] = value.encode().decode("unicode_escape") if "\\" in value else value
    if any(name.startswith("shib_idp_ls_value.") for name in fields):  # "read" page
        for name in list(fields):
            if name.startswith("shib_idp_ls_value."):
                key = name.split(".", 1)[1]
                fields[name] = storage.get(key, "")
                fields["shib_idp_ls_success." + key] = "true" if key in storage else "false"
        fields["shib_idp_ls_supported"] = "true"
    elif not ({"SAMLRequest", "SAMLResponse", "_eventId_proceed"} & set(fields)):
        return None
    return urllib.parse.urljoin(url, form.action), fields


def refresh_session_http(timeout: float = 10.0) -> str:
    """Try to renew the Blackboard session without a browser.

    Returns REFRESHED (cookies saved, session verified), NEEDS_LOGIN (the IdP
    wants a person) or UNKNOWN (network trouble: change nothing).
    """
    from core.session_state import probe_session
    cookies = load_cookies()
    storage = load_idp_storage()
    if not cookies or not storage:
        return NEEDS_LOGIN
    jar = _jar_from(cookies)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NoRedirect())
    written: dict[str, str] = {}
    url, data = SAML_ENTRY, None
    started = time.monotonic()
    try:
        for _ in range(MAX_HOPS):
            if time.monotonic() - started > timeout * 3 or not _host_ok(url):
                return NEEDS_LOGIN
            status, headers, body = _request(opener, url, data, timeout)
            if status in (301, 302, 303, 307, 308) and headers.get("Location"):
                url, data = urllib.parse.urljoin(url, headers["Location"]), None
                continue
            if status >= 500:
                return UNKNOWN
            if status != 200:
                return NEEDS_LOGIN
            step = _next_step(url, body, storage, written)
            if step is None:
                if urllib.parse.urlsplit(url).hostname == "blackboard.umbc.edu":
                    break  # landed on Blackboard
                return NEEDS_LOGIN
            url, fields = step
            data = urllib.parse.urlencode(fields).encode()
        else:
            return NEEDS_LOGIN
    except (urllib.error.URLError, TimeoutError, OSError):
        return UNKNOWN

    # Verify with the renewed cookies before replacing anything on disk.
    merged = _merge_cookies(cookies, jar)
    if cookie_header(cookies=merged) is None:
        return NEEDS_LOGIN
    try:
        verified = _verify(opener)
    except (urllib.error.URLError, TimeoutError, OSError):
        return UNKNOWN
    if not verified:
        return NEEDS_LOGIN
    _write_private(Path(SESSION_DIR) / "cookies.json", json.dumps(merged))
    if written:
        save_idp_storage({**storage, **written})
    return REFRESHED if probe_session()[0] == VALID else NEEDS_LOGIN


def _verify(opener) -> bool:
    status, _, body = _request(opener, BLACKBOARD_BASE + "/learn/api/public/v1/users/me", timeout=8)
    if status != 200:
        return False
    try:
        data = json.loads(body)
    except ValueError:
        return False
    return isinstance(data, dict) and any(data.get(key) for key in ("id", "studentId", "userName"))
