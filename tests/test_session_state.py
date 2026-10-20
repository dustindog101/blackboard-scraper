"""Probe tri-state, no auto-login on unknown, cookie header filtering, login lock."""

import asyncio
import contextlib
import io
import json
import multiprocessing
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import session, session_state
from core.session_state import EXPIRED, UNKNOWN, VALID


class SessionDir(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        patcher = patch.object(session_state, "SESSION_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_cookies(self, cookies):
        (self.dir / "cookies.json").write_text(json.dumps(cookies))


BB = {"name": "s", "value": "1", "domain": "blackboard.umbc.edu", "path": "/", "expires": -1}


class FakeResponse:
    def __init__(self, status=200, body=b'{"id":"_1_1"}'):
        self.status, self._body = status, body

    def read(self, _=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "x", {}, io.BytesIO())


class TestProbe(SessionDir):
    def probe(self, outcome):
        self.write_cookies([BB])
        opener = MagicMock()
        opener.open.side_effect = [outcome] if isinstance(outcome, BaseException) else None
        if not isinstance(outcome, BaseException):
            opener.open.return_value = outcome
        with patch("core.session_state.urllib.request.build_opener", return_value=opener):
            return session_state.probe_session()

    def test_valid(self):
        self.assertEqual(self.probe(FakeResponse())[0], VALID)

    def test_expired_for_auth_rejection_and_login_redirects(self):
        for code in (401, 403, 302):
            with self.subTest(code):
                self.assertEqual(self.probe(http_error(code)), (EXPIRED, None))

    def test_expired_for_html_200_and_no_cookies(self):
        self.assertEqual(self.probe(FakeResponse(body=b"<html>login</html>"))[0], EXPIRED)
        (self.dir / "cookies.json").unlink()
        self.assertEqual(session_state.probe_session()[0], EXPIRED)

    def test_unknown_for_network_timeout_and_server_errors(self):
        for outcome in (urllib.error.URLError("no network"), TimeoutError(), OSError("reset"), http_error(500), http_error(503), http_error(429)):
            with self.subTest(repr(outcome)):
                self.assertEqual(self.probe(outcome), (UNKNOWN, None))


class TestNoLoginOnUnknown(unittest.TestCase):
    def run_require(self, asynchronous):
        with patch.object(session, "check_session_state", return_value=(UNKNOWN, None)), \
             patch.object(session, "login_auto") as login, patch.object(session, "login") as manual, \
             patch.object(session, "ensure_config_exists") as config, \
             contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            result = asyncio.run(session._require_session_async()) if asynchronous else session._require_session()
        login.assert_not_called()
        manual.assert_not_called()
        config.assert_not_called()
        self.assertFalse(result)
        self.assertIn("network", err.getvalue().lower())

    def test_sync(self):
        self.run_require(False)

    def test_async(self):
        self.run_require(True)

    def test_require_session_probes_once(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous), patch.object(session, "check_session_state", return_value=(VALID, {})) as probe:
                result = asyncio.run(session._require_session_async()) if asynchronous else session._require_session()
                self.assertTrue(result)
                probe.assert_called_once()

    def test_expired_still_starts_login(self):
        with patch.object(session, "check_session_state", return_value=(EXPIRED, None)), \
             patch.object(session, "quick_check_session_http", return_value=(True, {})), \
             patch.object(session, "ensure_config_exists", return_value=(False, {})), \
             patch.object(session, "has_auto_login_credentials", return_value=True), \
             patch.object(session, "login_auto", return_value=True) as login, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(session._require_session())
        login.assert_called_once()

    def test_check_reports_network_not_expired(self):
        with patch.object(session, "check_session_state", return_value=(UNKNOWN, None)), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(session.check_session())
        self.assertIn("network", out.getvalue().lower())
        self.assertNotIn("EXPIRED", out.getvalue())


class TestCookieHeader(unittest.TestCase):
    def header(self, cookies, url="https://blackboard.umbc.edu/learn/api/public/v1/users/me", now=1000):
        return session_state.cookie_header(url, cookies, now=now)

    def test_domain_filter(self):
        cookies = [BB, dict(BB, name="shared", domain=".umbc.edu"), dict(BB, name="other", domain="evilumbc.edu"),
                   dict(BB, name="google", domain=".google.com"), dict(BB, name="sub", domain="my.umbc.edu")]
        self.assertEqual(self.header(cookies), "s=1; shared=1")

    def test_path_filter(self):
        cookies = [BB, dict(BB, name="api", path="/learn"), dict(BB, name="ultra", path="/ultra"), dict(BB, name="near", path="/lear")]
        self.assertEqual(self.header(cookies), "s=1; api=1")

    def test_expiry_filter(self):
        cookies = [BB, dict(BB, name="old", expires=500), dict(BB, name="new", expires=5000)]
        self.assertEqual(self.header(cookies), "s=1; new=1")

    def test_most_specific_path_wins_and_none_when_empty(self):
        cookies = [dict(BB, value="root"), dict(BB, value="deep", path="/learn")]
        self.assertEqual(self.header(cookies), "s=deep")
        self.assertIsNone(self.header([dict(BB, domain="x.com")]))

    def test_user_agent_is_the_login_browser_with_headless_marker_removed(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(session_state, "SESSION_DIR", Path(temp)):
            self.assertEqual(session_state.user_agent(), session_state.DEFAULT_USER_AGENT)
            session_state.record_user_agent("Mozilla/5.0 HeadlessChrome/155.0.0.0 Safari/537.36")
            self.assertEqual(session_state.user_agent(), "Mozilla/5.0 Chrome/155.0.0.0 Safari/537.36")


def _hold_lock(directory, ready, release):
    with patch.object(session_state, "SESSION_DIR", Path(directory)), session_state.login_lock():
        ready.set()
        release.wait(10)


class TestLoginLock(SessionDir):
    def test_second_process_sees_login_in_progress(self):
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        holder = context.Process(target=_hold_lock, args=(str(self.dir), ready, release))
        holder.start()
        try:
            self.assertTrue(ready.wait(15))
            messages = []
            started = time.monotonic()
            with self.assertRaises(session_state.LoginInProgress):
                with session_state.login_lock(wait_seconds=0.5, announce=messages.append):
                    self.fail("lock should be held")
            self.assertLess(time.monotonic() - started, 3)
            self.assertIn("already in progress", messages[0])
        finally:
            release.set()
            holder.join(10)
        with session_state.login_lock(wait_seconds=0):  # free once the holder exits
            pass

    def test_waits_then_acquires_when_holder_finishes(self):
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        holder = context.Process(target=_hold_lock, args=(str(self.dir), ready, release))
        holder.start()
        try:
            self.assertTrue(ready.wait(15))
            threading_release = __import__("threading").Timer(0.5, release.set)
            threading_release.start()
            with session_state.login_lock(wait_seconds=10, announce=lambda _: None):
                pass
        finally:
            release.set()
            holder.join(10)

    def test_login_wrappers_exit_cleanly_when_busy(self):
        with patch.object(session, "login_lock", side_effect=session_state.LoginInProgress("Login already in progress")), \
             patch.object(session, "ensure_config_exists") as config, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(session.login_auto())
            self.assertIsNone(session.login())
        config.assert_not_called()
        self.assertIn("already in progress", out.getvalue())


class TestDuoMethods(unittest.TestCase):
    def test_labels_cover_sms_push_passcode_call(self):
        from core.login_flow import DUO_METHOD_LABELS
        self.assertEqual(set(DUO_METHOD_LABELS), {"sms", "push", "passcode", "call"})

    def test_methods_detected_in_real_page_preferred_first(self):
        from core.login_flow import duo_methods
        pages = {
            "sms+push": ("<button>Duo Push</button><a href='#'>Text message passcode</a><button>Duo Mobile passcode</button>",
                         ["sms", "push", "passcode"]),
            "push only": ("<h1>Check for a Duo Push</h1><a href='#'>Other options</a>", ["push"]),
            "passcode only": ("<input name='passcode'>", ["passcode"]),
            "call only": ("<button>Phone call</button>", ["call"]),
            "nothing": ("<p>Hello</p>", []),
        }
        from core.browser import launch_persistent_context
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as profile, sync_playwright() as pw:
            try:
                context, _ = launch_persistent_context(pw, profile, headless=True)
            except Exception:
                self.skipTest("no browser available")
            try:
                page = context.new_page()
                for name, (html, expected) in pages.items():
                    with self.subTest(name):
                        page.route("https://id.duosecurity.com/**", (lambda body: lambda route, _request: route.fulfill(
                            status=200, content_type="text/html", body=body))(html))
                        page.goto("https://id.duosecurity.com/x")
                        self.assertEqual(duo_methods(page), expected)
                        page.unroute("https://id.duosecurity.com/**")
            finally:
                context.close()


if __name__ == "__main__":
    unittest.main()
