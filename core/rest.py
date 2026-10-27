"""Minimal authenticated REST client for the cached Blackboard session (ADR-0002).

Errors are distinct (coding standards section 1): 401 is ``auth`` (session expired), 403 is
``forbidden`` (not visible to students), 404 is ``not_found``, and a network error or 5xx is
``unknown``. Nothing here ever logs in or opens a browser.
"""
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, List, Optional

from core.config import BLACKBOARD_BASE, SESSION_DIR
from core.session_state import cookie_header as filtered_cookie_header, user_agent


class DocumentRedirects(urllib.request.HTTPRedirectHandler):
    """Redirect policy for document downloads: https only, to the provider's own hosts, and the session
    Cookie is dropped on any host but Blackboard's. A refused redirect raises HTTPError 403."""

    def __init__(self, blackboard):
        self.blackboard = blackboard

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlsplit(newurl).hostname or ""
        bb_host = urllib.parse.urlsplit(BLACKBOARD_BASE).hostname
        allowed = (host == bb_host or re.fullmatch(r"alt-[a-z0-9]+\.blackboard\.com", host) or host.endswith(".content.blackboardcdn.com")) if self.blackboard else (
            host == "docs.google.com" or host.endswith(".googleusercontent.com")
        )
        if not allowed or urllib.parse.urlsplit(newurl).scheme != "https":
            raise urllib.error.HTTPError(req.full_url, 403, "Document redirect outside its provider", headers, fp)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and host != bb_host:
            redirected.remove_header("Cookie")
        return redirected


class RestError(Exception):
    """A failed REST call. ``kind`` is one of auth, forbidden, not_found, unknown."""

    def __init__(self, kind: str, status: Optional[int] = None, detail: str = ""):
        self.kind = kind
        self.status = status
        super().__init__(detail or (f"{kind} ({status})" if status else kind))


def _cookies() -> list[dict]:
    cookie_file = SESSION_DIR / "cookies.json"
    try:
        data = json.loads(cookie_file.read_text())
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def cookie_header() -> Optional[str]:
    """Cookie header for Blackboard requests, or None when there is no saved session."""
    return filtered_cookie_header(cookies=_cookies())


def xsrf_token() -> Optional[str]:
    """XSRF token Blackboard requires on POSTs (XSRF-TOKEN or BbRouter cookie of the umbc.edu host)."""
    header = cookie_header() or ""
    cookies = [c for c in _cookies() if f"{c.get('name')}={c.get('value')}" in header.split("; ")]
    for c in cookies:
        if c.get("name") == "XSRF-TOKEN" and c.get("value"):
            return c["value"]
    for c in cookies:
        if c.get("name") == "BbRouter":
            match = re.search(r"xsrf:([0-9a-fA-F-]+)", c.get("value", ""))
            if match:
                return match.group(1)
    return None


# This server stalls every burst by ~2 s when more than ~6 requests are in flight (measured), so all
# REST calls through this client share one cap.
MAX_CONNECTIONS = 6
_SLOTS = threading.BoundedSemaphore(MAX_CONNECTIONS)


def _open(req: urllib.request.Request, timeout: float) -> bytes:
    """Single network seam (tests replace this)."""
    with _SLOTS, urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _request(path: str, *, body: Optional[dict], timeout: float) -> Any:
    cookies = cookie_header()
    if not cookies:
        raise RestError("auth", 401, "no saved session")
    url = path if path.startswith("http") else f"{BLACKBOARD_BASE}{path}"
    headers = {"Cookie": cookies, "User-Agent": user_agent(), "Accept": "application/json"}
    data = None
    if body is not None:
        token = xsrf_token()
        if not token:
            raise RestError("unknown", None, "no XSRF token in saved session")
        data = json.dumps(body).encode("utf-8")
        headers.update({
            "Content-Type": "application/json;charset=UTF-8",
            "x-blackboard-xsrf": token,
            "Origin": BLACKBOARD_BASE,
            "Referer": f"{BLACKBOARD_BASE}/ultra/stream",
        })
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        return json.loads(_open(req, timeout).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        kind = {401: "auth", 403: "forbidden", 404: "not_found"}.get(exc.code, "unknown")
        raise RestError(kind, exc.code, f"HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RestError("unknown", None, str(exc)) from None


def get_json(path: str, timeout: float = 8.0) -> Any:
    """GET a Blackboard REST path and return the decoded JSON. Raises RestError."""
    return _request(path, body=None, timeout=timeout)


def post_json(path: str, body: dict, timeout: float = 10.0) -> Any:
    """POST a JSON body (used only for read-only 'retrieve' endpoints). Raises RestError."""
    return _request(path, body=body, timeout=timeout)


def get_pages(path: str, max_pages: int = 10, timeout: float = 8.0) -> List[dict]:
    """All ``results`` across ``paging.nextPage`` links. Raises RestError if the first page fails."""
    results: List[dict] = []
    next_path: Optional[str] = path
    for _ in range(max_pages):
        if not next_path:
            break
        data = get_json(next_path, timeout)
        results.extend(data.get("results", []))
        next_path = (data.get("paging") or {}).get("nextPage")
    return results
