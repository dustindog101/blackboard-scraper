"""Browserless refresh through the IdP, with scripted responses (no network)."""

import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from core import http_refresh as hr

BB = {"name": "JSESSIONID", "value": "old", "domain": "blackboard.umbc.edu", "path": "/", "expires": -1}
IDP = {"name": "__Host-shib_idp_session", "value": "idp", "domain": "webauth.umbc.edu", "path": "/", "expires": -1,
       "secure": True, "httpOnly": True}
ME = '{"id":"_1_1","userName":"fixture"}'


def page(url, action, **fields):
    inputs = "".join(f'<input name="{k}" type="hidden" value="{v}"/>' for k, v in fields.items())
    return (200, {}, f'<form action="{action}" method="post">{inputs}</form>')


READ = page("idp", "/idp/x?execution=e1s1", **{
    "shib_idp_ls_exception.shib_idp_session_ss": "", "shib_idp_ls_success.shib_idp_session_ss": "false",
    "shib_idp_ls_value.shib_idp_session_ss": "", "shib_idp_ls_supported": "", "_eventId_proceed": ""})
WRITE = (200, {}, 'function f(){writeLocalStorage("shib_idp_session_ss", "NEWVALUE");}'
                  '<form action="/idp/x?execution=e1s2"><input name="shib_idp_ls_success.shib_idp_session_ss" value="false"/>'
                  '<input name="_eventId_proceed"/></form>')
RESPONSE = page("idp", "https://blackboard.umbc.edu/auth-saml/saml/SSO/alias/_4893_1", SAMLResponse="abc", RelayState="r")
ENTRY = page("bb", "https://webauth.umbc.edu/idp/profile/SAML2/POST/SSO", SAMLRequest="req")


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        for target in ("core.http_refresh.SESSION_DIR", "core.session_state.SESSION_DIR"):
            patcher = patch(target, self.dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.dir / "cookies.json").write_text(json.dumps([IDP, BB]))
        hr.save_idp_storage({"shib_idp_session_ss": "OLDVALUE"})
        self.sent = []

    def run_chain(self, script, verified=True, verification_status=None):
        script = list(script)

        def fake(opener, url, data=None, timeout=10):
            self.sent.append((url, data))
            if url.endswith("/users/me"):
                if verification_status is not None:
                    return verification_status, {}, "unavailable"
                return (200, {}, ME) if verified else (401, {}, "{}")
            item = script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        with patch.object(hr, "_request", side_effect=fake):
            return hr.refresh_session_http()


class TestRefresh(Base):
    FULL = [ENTRY, (302, {"Location": "/idp/x"}, ""), READ, WRITE, RESPONSE, (302, {"Location": "/ultra"}, ""), (200, {}, "<html>ultra</html>")]

    def test_full_chain_refreshes_and_replays_idp_storage(self):
        # the SAML entry page is served on blackboard.umbc.edu; the landing page too
        self.assertEqual(self.run_chain(self.FULL), hr.REFRESHED)
        read_post = next(d for u, d in self.sent if d and b"shib_idp_ls_value" in d)
        self.assertIn(b"OLDVALUE", read_post)
        self.assertIn(b"shib_idp_ls_supported=true", read_post)
        self.assertEqual(hr.load_idp_storage()["shib_idp_session_ss"], "NEWVALUE")
        self.assertTrue(any(c["name"] == "__Host-shib_idp_session" for c in json.loads((self.dir / "cookies.json").read_text())))
        self.assertEqual(json.loads((self.dir / "idp_storage.json").read_text()), {"shib_idp_session_ss": "NEWVALUE"})
        self.assertEqual({path.name for path in self.dir.iterdir()}, {"cookies.json", "idp_storage.json"})

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits do not describe Windows ACL permissions")
    def test_refreshed_storage_and_cookies_have_private_posix_permissions(self):
        self.assertEqual(self.run_chain(self.FULL), hr.REFRESHED)
        for name in ("cookies.json", "idp_storage.json"):
            with self.subTest(name=name):
                self.assertEqual((self.dir / name).stat().st_mode & 0o777, 0o600)

    def test_credential_form_needs_login_and_nothing_is_saved(self):
        login = page("idp", "/idp/x?execution=e1s2", j_username="", j_password="", _eventId_proceed="Log In")
        before = (self.dir / "cookies.json").read_text()
        self.assertEqual(self.run_chain([ENTRY, (302, {"Location": "/idp/x"}, ""), login]), hr.NEEDS_LOGIN)
        self.assertEqual((self.dir / "cookies.json").read_text(), before)
        self.assertEqual(hr.load_idp_storage()["shib_idp_session_ss"], "OLDVALUE")

    def test_never_follows_to_other_hosts_such_as_duo(self):
        self.assertEqual(self.run_chain([ENTRY, (302, {"Location": "https://api-1.duosecurity.com/frame"}, "")]), hr.NEEDS_LOGIN)
        self.assertFalse(any("duosecurity" in u for u, _ in self.sent))

    def test_network_failure_is_unknown(self):
        self.assertEqual(self.run_chain([urllib.error.URLError("down")]), hr.UNKNOWN)
        self.assertEqual(self.run_chain([(503, {}, "")]), hr.UNKNOWN)

    def test_unverified_result_is_not_saved(self):
        before = (self.dir / "cookies.json").read_text()
        self.assertEqual(self.run_chain(self.FULL, verified=False), hr.NEEDS_LOGIN)
        self.assertEqual((self.dir / "cookies.json").read_text(), before)

    def test_unknown_final_validation_preserves_both_caches(self):
        for status in (429, 503):
            with self.subTest(status=status):
                before = (self.dir / "cookies.json").read_bytes()
                storage = (self.dir / "idp_storage.json").read_bytes()
                self.assertEqual(self.run_chain(self.FULL, verification_status=status), hr.UNKNOWN)
                self.assertEqual((self.dir / "cookies.json").read_bytes(), before)
                self.assertEqual((self.dir / "idp_storage.json").read_bytes(), storage)

    def test_no_second_network_probe_after_verified_commit(self):
        with patch("core.session_state.probe_session", return_value=(hr.UNKNOWN, None)) as probe:
            self.assertEqual(self.run_chain(self.FULL), hr.REFRESHED)
        probe.assert_not_called()

    def test_without_saved_idp_storage_needs_login(self):
        hr.forget_idp_storage()
        self.assertEqual(self.run_chain([]), hr.NEEDS_LOGIN)


class TestWiring(unittest.TestCase):
    def test_unknown_refresh_never_starts_a_login(self):
        from core import session
        with patch.object(session, "refresh_session_http", return_value=hr.UNKNOWN), \
             patch.object(session, "check_session_state", return_value=("expired", None)), \
             patch.object(session, "ensure_config_exists", return_value=(False, {"auto_login": {"username": "u", "password": "p"}})), \
             patch.object(session, "sync_playwright") as playwright, patch.object(session, "login_lock"):
            self.assertFalse(session.login_auto.__wrapped__(headless=True))
        playwright.assert_not_called()

    def test_forced_login_skips_refresh(self):
        from core import session
        with patch.object(session, "refresh_session_http") as refresh, \
             patch.object(session, "ensure_config_exists", return_value=(False, {})), \
             patch.object(session.sys.stdin, "isatty", return_value=False):
            session.login_auto.__wrapped__(force=True)
        refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
