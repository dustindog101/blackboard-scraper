"""Regression coverage for SMS selection, retries, and login readiness."""

import contextlib
import io
import json
import os
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from login_fakes import sso_and_verification
from core import browser as browser_selection
from core import session
from core import sms_listener as sms
from telegram.notifier import TelegramNotifier


class TestSMSSelection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bb messages ")
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        db = home / "Library" / "Messages" / "chat.db"
        db.parent.mkdir(parents=True)
        self.conn = sqlite3.connect(db)
        self.addCleanup(self.conn.close)
        self.conn.executescript(
            "CREATE TABLE handle (id TEXT);"
            "CREATE TABLE message (text TEXT, attributedBody BLOB, date INTEGER, handle_id INTEGER, is_from_me INTEGER);"
        )
        self.now = time.time()
        self.addCleanup(patch.stopall)
        patch.object(sms.sys, "platform", "darwin").start()
        patch.object(sms.Path, "home", return_value=home).start()
        self.insert("baseline", rowid=10)

    def insert(self, text, rowid=11, age=0, outgoing=False, blob=None):
        date = int((self.now - age - sms.COCOA_EPOCH_OFFSET) * 1e9)
        self.conn.execute(
            "INSERT INTO message(ROWID,text,attributedBody,date,is_from_me) VALUES (?,?,?,?,?)",
            (rowid, text, blob, date, int(outgoing)),
        )
        self.conn.commit()

    def read(self, **kwargs):
        return sms.get_latest_duo_sms_sqlite(start_rowid=10, after_unix_timestamp=self.now, **kwargs)

    def test_unrelated_numbers_are_not_auto_submitted(self):
        for text in ("Delivery reference 234567", "Your bank security code: 234567", "1234567"):
            with self.subTest(text=text):
                self.assertIsNone(sms.extract_duo_passcode(text))

    def test_recognizes_supported_lengths_without_truncation(self):
        for code in ("123456", "1234567", "12345678"):
            self.assertEqual(sms.extract_duo_passcode(f"UMBC SMS passcode: {code}"), code)
            self.assertEqual(sms.normalize_passcode(" " + code + " "), code)
        for code in ("12345", "123456789", "１２３４５６", "code 123456"):
            self.assertIsNone(sms.normalize_passcode(code))
        self.assertIsNone(sms.extract_duo_passcode("UMBC SMS passcode: 123456789"))

    def test_prefers_newest_duo_message_over_older_higher_ranked_pattern(self):
        self.insert("UMBC SMS passcode: 1234567")
        self.insert("Your Duo code is 7654321", rowid=12)
        self.assertEqual(self.read()[0], "7654321")

    def test_rejected_code_is_excluded(self):
        self.insert("UMBC SMS passcode: 1234567")
        self.assertIsNone(self.read(excluded_codes={"1234567"}))
        self.insert("UMBC SMS passcode: 7654321", rowid=12)
        self.assertEqual(self.read(excluded_codes={"1234567"})[0], "7654321")

    def test_stale_synced_message_is_rejected_but_rowid_tolerates_clock_drift(self):
        self.insert("UMBC SMS passcode: 1234567", age=3600)
        self.assertIsNone(self.read())
        self.insert("UMBC SMS passcode: 7654321", rowid=12, age=60)
        self.assertEqual(self.read()[0], "7654321")

    def test_outgoing_and_prebaseline_messages_are_ignored(self):
        self.insert("UMBC SMS passcode: 1234567", rowid=9)
        self.insert("UMBC SMS passcode: 7654321", outgoing=True)
        self.assertIsNone(self.read())

    def test_attributed_body_is_supported(self):
        self.insert(None, blob=b"header UMBC SMS passcode: 1234567 footer")
        self.assertEqual(self.read()[0], "1234567")

    def test_access_preflight_succeeds_with_readable_database(self):
        self.assertEqual(sms.check_sms_access(), (True, ""))
        self.assertEqual(sms.get_current_max_rowid(), 10)


class TestSMSFallback(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(sms.sys, "platform", "darwin").start()
        self.now = time.time()
        self.created = datetime.fromtimestamp(self.now, timezone.utc).isoformat()

    def search(self, rows, **kwargs):
        proc = SimpleNamespace(returncode=0, stdout=json.dumps(rows))
        with patch.object(sms, "_search_imsg", return_value=proc):
            return sms.get_latest_duo_sms_imsg(after_unix_timestamp=self.now, **kwargs)

    def message(self, **kwargs):
        return dict(text="Your Duo passcode is: 1234567", created_at=self.created, is_from_me=False, **kwargs)

    def test_requires_incoming_message_and_timestamp(self):
        original = self.message()
        self.assertEqual(self.search(original)[0], "1234567")
        for changes in ({"is_from_me": True}, {"is_from_me": None}, {"created_at": ""}, {"created_at": None}):
            self.assertIsNone(self.search(dict(original, **changes)))

    def test_filters_unrelated_stale_and_rejected_messages(self):
        original = self.message()
        self.assertIsNone(self.search(dict(original, text="Bank passcode: 1234567")))
        self.assertIsNone(self.search(dict(original, created_at="2000-01-01T00:00:00Z")))
        self.assertIsNone(self.search(original, excluded_codes={"1234567"}))

    def test_accepts_json_array_and_jsonl_with_newest_first(self):
        old = dict(self.message(), text="UMBC passcode: 7654321", created_at=datetime.fromtimestamp(self.now - 1, timezone.utc).isoformat())
        self.assertEqual(self.search([old, self.message()])[0], "1234567")
        proc = SimpleNamespace(returncode=0, stdout=json.dumps(old) + "\n" + json.dumps(self.message()))
        with patch.object(sms, "_search_imsg", return_value=proc):
            self.assertEqual(sms.get_latest_duo_sms_imsg(self.now)[0], "1234567")

    def test_permission_failure_is_actionable_and_listener_returns_immediately(self):
        with patch.object(sms, "_messages_connection", side_effect=sqlite3.OperationalError("denied")), \
             patch.object(sms, "_search_imsg", return_value=SimpleNamespace(returncode=1)), \
             patch.object(sms, "get_latest_duo_sms_sqlite") as query, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(sms.wait_for_duo_sms_passcode())
            self.assertIn("Full Disk Access", out.getvalue())
            query.assert_not_called()

    def test_separately_authorized_imsg_is_a_valid_fallback(self):
        with patch.object(sms, "_messages_connection", side_effect=sqlite3.OperationalError("denied")), \
             patch.object(sms, "_search_imsg", return_value=SimpleNamespace(returncode=0)):
            self.assertEqual(sms.check_sms_access(), (True, ""))

    def test_cancelled_listener_does_not_query_messages(self):
        stop = threading.Event()
        stop.set()
        with patch.object(sms, "get_latest_duo_sms_sqlite") as query:
            self.assertIsNone(sms.wait_for_duo_sms_passcode(stop_event=stop, access_checked=True))
            query.assert_not_called()

    def test_listener_does_not_log_passcode_sender_or_body(self):
        record = ("1234567", self.now, "private-sender", "private-message")
        with patch.object(sms, "get_latest_duo_sms_sqlite", return_value=record), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sms.wait_for_duo_sms_passcode(access_checked=True), "1234567")
        for secret in (record[0], record[2], record[3]):
            self.assertNotIn(secret, out.getvalue())


class TestTelegramPasscodes(unittest.TestCase):
    def test_supported_lengths_and_no_partial_long_number(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.enabled = True
        notifier.bot_token = "fixture"
        notifier.admin_chat_id = 42
        notifier.api_base = "https://fixture.invalid"
        for text in ("123456", "1234567", "12345678", "123456789"):
            responses = []
            for payload in (
                {"ok": True, "result": []},
                {"ok": True, "result": [{"update_id": 1, "message": {"chat": {"id": 42}, "text": text}}]},
            ):
                response = MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
                responses.append(response)
            with patch("telegram.notifier.urllib.request.urlopen", side_effect=responses), \
                 patch("telegram.notifier.time.time", side_effect=[0, 0, 121]), \
                 patch("telegram.notifier.time.sleep"):
                expected = text if len(text) <= 8 else None
                self.assertEqual(notifier.poll_for_passcode(), expected)


class TestLoginDeadlines(unittest.TestCase):
    def test_alternative_labels_share_one_deadline(self):
        now = [0.0]
        page = SimpleNamespace(frames=[], wait_for_timeout=lambda ms: now.__setitem__(0, now[0] + ms / 1000))
        with patch.object(session.time, "monotonic", side_effect=lambda: now[0]):
            self.assertFalse(session._click_exact_text_in_any_frame(page, ("missing1", "missing2", "missing3"), timeout=1000))
        self.assertLessEqual(now[0], 1.01)
        self.assertGreaterEqual(now[0], 1.0)

    def test_ready_next_state_skips_optional_search_entirely(self):
        page = MagicMock()
        self.assertFalse(session._click_exact_text_in_any_frame(page, session.TRUST_LABELS, stop_when=lambda: True))
        page.wait_for_timeout.assert_not_called()
        self.assertEqual(page.frames.call_count, 0)

    def test_slow_transition_is_pending_until_success(self):
        now = [0.0]
        page = SimpleNamespace(url="https://fixture.duosecurity.com/")
        def wait(ms):
            now[0] += ms / 1000
            if now[0] >= 3:
                page.url = "https://blackboard.umbc.edu/ultra/course"
        page.wait_for_timeout = wait
        with patch.object(session.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(session, "_trust_prompt_visible", return_value=False), \
             patch.object(session, "_visible_duo_error", return_value=False):
            self.assertEqual(session._wait_for_passcode_outcome(page), "accepted")
        self.assertGreaterEqual(now[0], 3)

    def test_previous_error_does_not_reject_retry_before_response(self):
        now = [0.0]
        page = SimpleNamespace(url="https://fixture.duosecurity.com/")
        def wait(ms):
            now[0] += ms / 1000
            if now[0] >= 0.5:
                page.url = "https://blackboard.umbc.edu/ultra/course"
        page.wait_for_timeout = wait
        with patch.object(session.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(session, "_trust_prompt_visible", return_value=False), \
             patch.object(session, "_visible_duo_error", return_value=True):
            self.assertEqual(session._wait_for_passcode_outcome(page, previous_error=True), "accepted")

    def test_unchanged_input_times_out_without_claiming_rejection(self):
        now = [0.0]
        page = SimpleNamespace(url="https://fixture.duosecurity.com/", wait_for_timeout=lambda ms: now.__setitem__(0, now[0] + ms / 1000))
        with patch.object(session.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(session, "_trust_prompt_visible", return_value=False), \
             patch.object(session, "_visible_duo_error", return_value=False):
            self.assertEqual(session._wait_for_passcode_outcome(page, timeout=500), "timeout")

    def test_invalid_cli_code_never_launches_browser(self):
        with patch.object(session, "ensure_config_exists", return_value=(False, {})), \
             patch.object(session, "sync_playwright") as browser, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(session.login_auto(passcode="123456789"))
        browser.assert_not_called()

    def test_cached_session_does_not_probe_messages(self):
        with patch.object(session, "ensure_config_exists", return_value=(False, {"auto_login": {"username": "fixture", "password": "fixture"}})), \
             patch.object(session, "check_session_state", return_value=("valid", {})), \
             patch.object(session, "check_sms_access") as access, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(session.login_auto())
        access.assert_not_called()


class TestLoginRetry(unittest.TestCase):
    def test_rejected_sms_cannot_be_submitted_again_and_next_code_succeeds(self):
        page = MagicMock()
        page.url = "https://fixture.duosecurity.com/"
        passcode_input = MagicMock()
        context = MagicMock()
        events = []
        exclusions = []
        def sms_worker(**kwargs):
            events.append(kwargs["stop_event"])
            exclusions.append(kwargs["excluded_codes"])
            return "1234567" if not kwargs["excluded_codes"] else "7654321"
        def outcome(*args, **kwargs):
            if passcode_input.fill.call_count == 1:
                return "rejected"
            page.url = "https://blackboard.umbc.edu/ultra/course"
            return "accepted"
        with sso_and_verification(page), patch.object(session, "ensure_config_exists", return_value=(False, {"auto_login": {"username": "fixture", "password": "fixture"}})), \
             patch.object(session, "check_session_state", return_value=("expired", None)), \
             patch.object(session, "check_sms_access", return_value=(True, "")), \
             patch.object(session, "ensure_messages_sync", return_value=True), \
             patch.object(session, "get_current_max_rowid", return_value=10), \
             patch.object(session.sys, "platform", "darwin"), \
             patch.object(session.sys.stdin, "isatty", return_value=False), \
             patch.object(session, "sync_playwright"), \
             patch.object(session, "_launch_context", return_value=(context, page)), \
             patch.object(session, "_click_exact_text_in_any_frame", return_value=True) as clicks, \
             patch.object(session, "_duo_passcode_input", return_value=passcode_input), \
             patch.object(session, "wait_for_duo_sms_passcode", side_effect=sms_worker), \
             patch.object(session, "_wait_for_passcode_outcome", side_effect=outcome), \
             patch.object(session, "_visible_duo_error", return_value=False), \
             patch.object(session, "_save_session"), patch.object(session, "track_session_usage"), \
             patch.object(session, "_wait_for_authenticated_session", return_value=True), \
             patch("telegram.notifier.TelegramNotifier", return_value=SimpleNamespace(enabled=False)), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(session.login_auto(headless=True, auto_exp=True))
        self.assertEqual([call.args[0] for call in passcode_input.fill.call_args_list], ["1234567", "7654321"])
        self.assertEqual(exclusions, [frozenset(), frozenset({"1234567"})])
        self.assertTrue(all(event.is_set() for event in events))
        passcode_input.press.assert_not_called()
        self.assertEqual(sum(call.args[1] == "Verify" for call in clicks.call_args_list), 2)
        context.close.assert_called_once()
        self.assertNotIn("1234567", out.getvalue())
        self.assertNotIn("7654321", out.getvalue())

    @unittest.skipIf(os.name == "nt", "POSIX terminal polling")
    def test_terminal_reader_exits_when_cancelled(self):
        stop = threading.Event()
        with patch.object(session.sys.stdin, "isatty", return_value=True), \
             patch("select.select", return_value=([], [], [])), contextlib.redirect_stdout(io.StringIO()):
            thread = threading.Thread(target=session._read_terminal_passcodes, args=(queue.Queue(), stop))
            thread.start()
            stop.set()
            thread.join(timeout=1)
        self.assertFalse(thread.is_alive())


class TestLoginBrowserHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = session.sync_playwright().start()
        cls.browser = None
        for spec in browser_selection.get_browser_launch_candidates():
            kwargs = {key: spec[key] for key in ("channel", "executable_path") if spec.get(key)}
            try:
                cls.browser = cls.driver.chromium.launch(headless=True, **kwargs)
                break
            except session.PlaywrightError:
                continue
        if cls.browser is None:
            cls.driver.stop()
            raise unittest.SkipTest("No browser available for local login fixtures")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()

    def test_hidden_error_does_not_match_its_visible_ancestor(self):
        self.page.set_content('<div id="root"><input name="passcode"><div class="error-message" hidden>Incorrect passcode</div></div>')
        self.assertFalse(session._visible_duo_error(self.page))
        self.page.locator(".error-message").evaluate("e => e.hidden = false")
        self.assertTrue(session._visible_duo_error(self.page))

    def test_later_alternative_is_clicked_without_waiting_for_first_label(self):
        self.page.set_content('<button onclick="window.clicked=true">UMBC Login</button>')
        self.assertTrue(session._click_exact_text_in_any_frame(self.page, ("absent", "UMBC Login"), timeout=2000))
        self.assertTrue(self.page.evaluate("window.clicked"))

    def test_actual_umbc_portal_label_with_trailing_period(self):
        self.page.set_content('<a href="#sso" onclick="window.clicked=true">Log into Blackboard via myUMBC.</a>')
        self.assertTrue(session._click_exact_text_in_any_frame(
            self.page, ("Log into Blackboard via myUMBC", "UMBC Login", "Sign in"), timeout=2000,
        ))
        self.assertTrue(self.page.evaluate("window.clicked"))

    def test_embedded_passcode_input_and_method_button(self):
        self.page.set_content('<iframe srcdoc="<input name=passcode><button>Text message passcode</button>"></iframe>')
        self.page.frame_locator("iframe").locator("input").wait_for(state="visible")
        self.assertIsNotNone(session._duo_passcode_input(self.page))
        self.assertTrue(session._click_exact_text_in_any_frame(self.page, "Text message passcode", timeout=2000))

    def test_ready_sso_form_skips_absent_portal_links(self):
        self.page.set_content('<input type="text"><input type="password">')
        self.assertFalse(session._click_exact_text_in_any_frame(
            self.page, ("portal1", "portal2", "portal3"),
            stop_when=lambda: self.page.locator("input").first.is_visible(),
        ))


if __name__ == "__main__":
    unittest.main()
