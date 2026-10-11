"""Transfer failure paths use synthetic cookies and isolated local files."""

import asyncio
import copy
import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import main
from core import session_transfer as transfer


def cookie(value="synthetic-import-secret", **attributes):
    return dict(name="session", value=value, domain="blackboard.umbc.edu", path="/",
                expires=-1, secure=True, httpOnly=True, sameSite="Lax", **attributes)


class FakeContext:
    def __init__(self, cookies):
        self.saved = copy.deepcopy(cookies)
        self.add_error = False

    def cookies(self):
        return copy.deepcopy(self.saved)

    def clear_cookies(self):
        self.saved = []

    def add_cookies(self, cookies):
        if self.add_error and cookies and cookies[0]["value"] == "synthetic-import-secret":
            raise ValueError("synthetic-import-secret")
        self.saved.extend(copy.deepcopy(cookies))


class TestTransfer(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bb transfer ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = self.root / "session"
        self.session.mkdir()
        self.saved = self.session / "cookies.json"
        self.original = [cookie("synthetic-existing-secret")]
        self.old_bytes = json.dumps(self.original).encode()
        self.saved.write_bytes(self.old_bytes)
        self.input = self.root / "moving session ü.json"
        self.input.write_text(json.dumps([cookie()]), encoding="utf-8")
        self.context = FakeContext(self.original)
        self.user = {"id": "synthetic-user", "userName": "synthetic-account"}
        self.addCleanup(patch.stopall)
        patch.object(transfer, "SESSION_DIR", self.session).start()
        self.http = patch.object(transfer, "_probe_cookies", return_value=self.user).start()
        self.browser = patch.object(transfer, "_probe_browser", return_value=self.user).start()

        @contextmanager
        def managed():
            yield self.context

        self.managed = patch.object(transfer, "_managed_browser", side_effect=managed).start()

    def assert_preserved(self):
        self.assertEqual(self.saved.read_bytes(), self.old_bytes)
        self.assertEqual(self.context.cookies(), self.original)
        self.assertEqual(list(self.session.glob(".bb-cookies-*")), [])

    def test_export_offline_preserves_source_and_attributes(self):
        destination = self.root / "export session.json"
        self.assertEqual(transfer.export_session(str(destination)), 1)
        self.assertEqual(json.loads(destination.read_text()), self.original)
        self.assert_preserved()
        self.http.assert_not_called()
        self.managed.assert_not_called()

    def test_export_existing_file_requires_overwrite(self):
        with self.assertRaisesRegex(transfer.SessionTransferError, "--overwrite"):
            transfer.export_session(str(self.input))
        transfer.export_session(str(self.input), overwrite=True)
        self.assertEqual(json.loads(self.input.read_text()), self.original)

    def test_export_cannot_modify_source(self):
        with self.assertRaises(transfer.SessionTransferError):
            transfer.export_session(str(self.saved), overwrite=True)
        self.assert_preserved()

    def test_export_missing_source(self):
        self.saved.unlink()
        with self.assertRaisesRegex(transfer.SessionTransferError, "Cannot read"):
            transfer.export_session(str(self.root / "new.json"))

    def test_import_roundtrip_replaces_stale_browser_cookies(self):
        self.context.saved.append(dict(cookie("stale"), name="stale_cookie"))
        metadata = self.session / "session_metadata.json"
        metadata.write_text('{"login_time":"original"}')
        count, user = transfer.import_session(str(self.input))
        self.assertEqual(count, 1)
        self.assertEqual(user, self.user)
        self.assertEqual(json.loads(self.saved.read_text()), [cookie()])
        self.assertEqual(self.context.cookies(), [cookie()])
        self.assertEqual(metadata.read_text(), '{"login_time":"original"}')

    def test_first_import_without_saved_cookies(self):
        self.saved.unlink()
        transfer.import_session(str(self.input))
        self.assertEqual(json.loads(self.saved.read_text()), [cookie()])

    def test_import_same_file_reads_before_replacing(self):
        transfer.import_session(str(self.saved))
        self.assertEqual(json.loads(self.saved.read_text()), self.original)

    def test_unicode_bom_file_supported(self):
        self.input.write_text(json.dumps([cookie()]), encoding="utf-8-sig")
        transfer.import_session(str(self.input))

    def test_invalid_json_and_shape_fail_without_mutation(self):
        for contents in ("not JSON", "{}", "[]", "[1]", '[{"name":"x"}]'):
            with self.subTest(contents=contents):
                self.input.write_text(contents)
                with self.assertRaises(transfer.SessionTransferError):
                    transfer.import_session(str(self.input))
                self.assert_preserved()
        self.http.assert_not_called()
        self.managed.assert_not_called()

    def test_invalid_cookie_fields_do_not_leak_or_mutate(self):
        for changes in ({"value": "secret\r\nHeader: x"}, {"name": "bad;name"},
                        {"expires": float("nan")}, {"expires": float("inf")},
                        {"expires": 10 ** 300}, {"expires": -(10 ** 300)},
                        {"expires": True}, {"expires": -2},
                        {"secure": "true"}, {"sameSite": "bad"}, {"path": "relative"}):
            self.input.write_text(json.dumps([dict(cookie(), **changes)]))
            with self.subTest(changes=list(changes)):
                with self.assertRaises(transfer.SessionTransferError) as caught:
                    transfer.import_session(str(self.input))
                self.assertNotIn("synthetic-import-secret", str(caught.exception))
                self.assert_preserved()

    def test_oversized_file_rejected_before_probe(self):
        self.input.write_bytes(b" " * (transfer.MAX_FILE_BYTES + 1))
        with self.assertRaisesRegex(transfer.SessionTransferError, "too large"):
            transfer.import_session(str(self.input))
        self.http.assert_not_called()
        self.assert_preserved()

    def test_domain_matching_keeps_only_applicable_cookies(self):
        candidates = [cookie(), dict(cookie(), domain=".umbc.edu"),
                      dict(cookie(), domain=".blackboard.umbc.edu"),
                      dict(cookie(), domain="webauth.umbc.edu"),
                      dict(cookie(), domain="umbc.edu"),
                      dict(cookie(), domain="blackboard.umbc.edu.evil.example")]
        self.input.write_text(json.dumps(candidates))
        transfer.import_session(str(self.input))
        self.assertEqual(json.loads(self.saved.read_text()), candidates[:3])

    def test_no_blackboard_cookies(self):
        self.input.write_text(json.dumps([dict(cookie(), domain="example.com")]))
        with self.assertRaisesRegex(transfer.SessionTransferError, "No Blackboard"):
            transfer.import_session(str(self.input))
        self.assert_preserved()

    def test_rejected_or_unreachable_http_preserves_destination(self):
        for message in ("Session expired", "Could not validate connection"):
            self.http.side_effect = transfer.SessionTransferError(message)
            with self.assertRaisesRegex(transfer.SessionTransferError, message):
                transfer.import_session(str(self.input))
            self.assert_preserved()
        self.managed.assert_not_called()

    def test_busy_profile_preserves_file(self):
        self.managed.side_effect = transfer.SessionTransferError("Close browser")
        with self.assertRaisesRegex(transfer.SessionTransferError, "Close browser"):
            transfer.import_session(str(self.input))
        self.assert_preserved()

    def test_browser_auth_failure_rolls_back(self):
        self.browser.side_effect = transfer.SessionTransferError("Browser rejected session")
        with self.assertRaisesRegex(transfer.SessionTransferError, "Browser rejected"):
            transfer.import_session(str(self.input))
        self.assert_preserved()

    def test_different_browser_identity_rolls_back(self):
        self.browser.return_value = {"id": "other-user"}
        with self.assertRaisesRegex(transfer.SessionTransferError, "disagree"):
            transfer.import_session(str(self.input))
        self.assert_preserved()

    def test_response_cookie_refresh_is_persisted(self):
        def refresh(context):
            context.saved[0]["value"] = "synthetic-refreshed-secret"
            return self.user

        self.browser.side_effect = refresh
        transfer.import_session(str(self.input))
        self.assertEqual(json.loads(self.saved.read_text()), self.context.cookies())
        self.assertEqual(self.context.cookies()[0]["value"], "synthetic-refreshed-secret")

    def test_browser_cookie_install_failure_is_sanitized(self):
        self.context.add_error = True
        with self.assertRaises(transfer.SessionTransferError) as caught:
            transfer.import_session(str(self.input))
        self.assertNotIn("synthetic-import-secret", str(caught.exception))
        self.assert_preserved()

    def test_failed_file_replace_restores_browser_and_cleans_temp(self):
        with patch.object(transfer.os, "replace", side_effect=PermissionError("secret")):
            with self.assertRaises(transfer.SessionTransferError):
                transfer.import_session(str(self.input))
        self.assert_preserved()

    def test_readback_failure_restores_old_file(self):
        real_read = transfer._read_bytes
        reads = 0

        def read(path):
            nonlocal reads
            if path == self.saved:
                reads += 1
                if reads == 3:
                    raise transfer.SessionTransferError("Cannot verify")
            return real_read(path)

        with patch.object(transfer, "_read_bytes", side_effect=read):
            with self.assertRaises(transfer.SessionTransferError):
                transfer.import_session(str(self.input))
        self.assert_preserved()

    def test_browser_shutdown_failure_restores_previous_session(self):
        opens = 0

        @contextmanager
        def managed():
            nonlocal opens
            opens += 1
            yield self.context
            if opens == 1:
                raise transfer.SessionTransferError("Could not close browser")

        self.managed.side_effect = managed
        with self.assertRaisesRegex(transfer.SessionTransferError, "close browser"):
            transfer.import_session(str(self.input))
        self.assertEqual(opens, 2)
        self.assert_preserved()

    def test_concurrent_file_change_is_not_overwritten(self):
        newer = b'[{"newer":"login"}]'

        def browser_probe(_):
            self.saved.write_bytes(newer)
            return self.user

        self.browser.side_effect = browser_probe
        with self.assertRaisesRegex(transfer.SessionTransferError, "changed during import"):
            transfer.import_session(str(self.input))
        self.assertEqual(self.saved.read_bytes(), newer)
        self.assertEqual(self.context.cookies(), self.original)

    def test_cli_dispatch_does_not_require_login_or_print_cookies(self):
        stdout = io.StringIO()
        with patch.object(main, "_require_session_async", side_effect=AssertionError("must not login")), redirect_stdout(stdout):
            asyncio.run(main.main_async(main._parse_args(["session", "import", str(self.input)])))
        self.assertIn("Verified account: synthetic-account", stdout.getvalue())
        self.assertNotIn("synthetic-import-secret", stdout.getvalue())

    def test_cli_failure_has_nonzero_exit_without_traceback(self):
        self.http.side_effect = transfer.SessionTransferError("Session expired")
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            asyncio.run(main.main_async(main._parse_args(["session", "import", str(self.input)])))
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("Session expired", stderr.getvalue())
        self.assertNotIn("synthetic-import-secret", stderr.getvalue())


class TestProbe(unittest.TestCase):
    def response(self, body, status=200):
        response = Mock(status=status)
        response.read.return_value = body
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        return opener

    def test_http_authenticated_json(self):
        opener = self.response(b'{"id":"user"}')
        with patch.object(transfer.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(transfer._probe_cookies([cookie()]), {"id": "user"})

    def test_html_invalid_json_missing_identity_and_large_response(self):
        for body in (b'<html>login</html>', b'{}', b'[]', b'x' * (transfer.MAX_FILE_BYTES + 1)):
            opener = self.response(body)
            with patch.object(transfer.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(transfer.SessionTransferError):
                    transfer._probe_cookies([cookie()])

    def test_http_status_and_network_failures_are_actionable(self):
        for status in (302, 401, 403, 429, 500):
            opener = Mock()
            opener.open.side_effect = urllib.error.HTTPError("https://blackboard.umbc.edu", status, "secret", {}, None)
            with patch.object(transfer.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(transfer.SessionTransferError) as caught:
                    transfer._probe_cookies([cookie()])
                self.assertNotIn("secret", str(caught.exception))
        opener.open.side_effect = urllib.error.URLError("secret")
        with patch.object(transfer.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(transfer.SessionTransferError, "connection"):
                transfer._probe_cookies([cookie()])

    def test_cookiejar_respects_path_domain_and_expiry(self):
        cookies = [cookie(), dict(cookie(), name="expired", expires=1),
                   dict(cookie(), name="wrongpath", path="/unrelated"),
                   dict(cookie(), name="parent", domain=".umbc.edu")]
        jars = []

        def build(*handlers):
            jars.append(handlers[0].cookiejar)
            return self.response(b'{"id":"user"}')

        with patch.object(transfer.urllib.request, "build_opener", side_effect=build):
            transfer._probe_cookies(cookies)
        request = transfer.urllib.request.Request(transfer.BLACKBOARD_BASE + "/learn/api/public/v1/users/me")
        jars[0].add_cookie_header(request)
        header = request.get_header("Cookie")
        self.assertIn("session=", header)
        self.assertIn("parent=", header)
        self.assertNotIn("expired=", header)
        self.assertNotIn("wrongpath=", header)
        offsite = transfer.urllib.request.Request("https://example.com/")
        jars[0].add_cookie_header(offsite)
        self.assertIsNone(offsite.get_header("Cookie"))
        self.assertIsNone(transfer._NoRedirect().redirect_request(request, None, 302, "", {}, "https://example.com/"))

    def test_browser_identity_request_disposes_response(self):
        context = Mock()
        response = context.request.get.return_value
        response.status = 200
        response.json.return_value = {"id": "user"}
        self.assertEqual(transfer._probe_browser(context), {"id": "user"})
        response.dispose.assert_called_once()
        self.assertEqual(context.request.get.call_args.kwargs["max_redirects"], 0)


class TestTransferCLI(unittest.TestCase):
    def test_new_and_existing_actions(self):
        for action in ("import", "export"):
            args = main._parse_args(["session", action, "path with spaces.json"])
            self.assertEqual(args.action, action)
            self.assertEqual(args.file, "path with spaces.json")
        self.assertTrue(main._parse_args(["session", "export", "f.json", "--overwrite"]).overwrite)
        for action in ("check", "stats", "info", "telemetry"):
            self.assertEqual(main._parse_args(["session", action]).action, action)
        self.assertEqual(main._parse_args(["session"]).action, "check")

    def test_invalid_combinations_fail_before_network(self):
        for argv in (["session", "import"], ["session", "export"],
                     ["session", "check", "extra.json"], ["session", "stats", "--overwrite"],
                     ["session", "import", "f.json", "--overwrite"],
                     ["session", "import", "f.json", "--cdp", "http://localhost:9222"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main._parse_args(argv)
            self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
