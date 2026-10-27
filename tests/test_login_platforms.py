"""Console, completion, and CLI regressions; no live authentication."""
import asyncio
import contextlib
import io
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from login_fakes import sso_and_verification
from core import session
import main


class TestWindowsConsole(unittest.TestCase):
    def run_keys(self, keys):
        keys = list(keys)
        console = SimpleNamespace(kbhit=lambda: bool(keys), getwch=lambda: keys.pop(0))
        event = threading.Event()
        results = queue.Queue()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            session._read_windows_passcodes(results, event, console)
        return list(results.queue), out.getvalue(), event

    def test_seven_digit_code_and_backspace_are_accepted_without_echoing_code(self):
        results, output, _ = self.run_keys("1234568\b7\r\x03")
        self.assertEqual(results, [("cli", "1234567")])
        self.assertNotIn("1234567", output)
        self.assertIn("\b \b", output)

    def test_terminal_delete_byte_also_edits_the_pending_code(self):
        results, output, _ = self.run_keys("1234568\x7f7\r\x03")
        self.assertEqual(results, [("cli", "1234567")])
        self.assertIn("\b \b", output)

    def test_extended_key_scan_codes_do_not_enter_digits(self):
        results, _, _ = self.run_keys("123\x0054567\r\x03")
        self.assertEqual(results, [("cli", "1234567")])

    def test_reader_keeps_reading_after_invalid_length(self):
        results, _, _ = self.run_keys("12345\r1234567\r\x03")
        self.assertEqual(results, [("cli", "12345"), ("cli", "1234567")])

    def test_ctrl_c_and_ctrl_z_cancel(self):
        for char in ("\x03", "\x1a"):
            results, _, event = self.run_keys(char)
            self.assertEqual(results, [])
            self.assertTrue(event.is_set())

    def test_idle_reader_is_cancellable(self):
        event = threading.Event()
        console = SimpleNamespace(kbhit=lambda: False)
        thread = threading.Thread(target=session._read_windows_passcodes, args=(queue.Queue(), event, console))
        thread.start()
        event.set()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())


class TestAuthenticatedCompletion(unittest.TestCase):
    def test_verifies_http_session_on_root_without_waiting_for_ultra_assets(self):
        response = MagicMock(status=200)
        response.json.return_value = {"id": "fixture"}
        context = MagicMock()
        context.request.get.return_value = response
        page = SimpleNamespace(url="https://blackboard.umbc.edu/?new_loc=fixture", wait_for_timeout=MagicMock())
        self.assertTrue(session._wait_for_authenticated_session(context, page))
        response.dispose.assert_called_once()
        page.wait_for_timeout.assert_not_called()

    def test_invalid_or_redirected_probe_is_not_success(self):
        for status, data in ((302, {}), (401, {}), (200, {"error": "unauthorized"})):
            now = [0.0]
            response = MagicMock(status=status)
            response.json.return_value = data
            context = MagicMock()
            context.request.get.return_value = response
            page = SimpleNamespace(url="https://blackboard.umbc.edu/", wait_for_timeout=lambda ms: now.__setitem__(0, now[0] + ms / 1000))
            with patch.object(session.time, "monotonic", side_effect=lambda: now[0]):
                self.assertFalse(session._wait_for_authenticated_session(context, page, timeout=100))
            response.dispose.assert_called_once()

    def test_transient_network_failure_can_recover(self):
        response = MagicMock(status=200)
        response.json.return_value = {"userName": "fixture"}
        context = MagicMock()
        context.request.get.side_effect = [session.PlaywrightError("temporary"), response]
        page = SimpleNamespace(url="https://blackboard.umbc.edu/", wait_for_timeout=lambda ms: None)
        self.assertTrue(session._wait_for_authenticated_session(context, page))
        self.assertEqual(context.request.get.call_count, 2)

    def test_chrome_profile_lock_does_not_try_other_browsers(self):
        driver = MagicMock()
        driver.chromium.launch_persistent_context.side_effect = session.PlaywrightError("Failed to create a ProcessSingleton for your profile directory.")
        with patch("core.browser.get_browser_launch_candidates", return_value=[{"name": "c", "executable_path": "/c"}, {"name": "e", "executable_path": "/e"}]), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            session._launch_context(driver)
        self.assertEqual(driver.chromium.launch_persistent_context.call_count, 1)


class TestLoginExitCodes(unittest.TestCase):
    def test_failed_auto_login_exits_nonzero(self):
        args = main._parse_args(["login"])
        with patch.object(main, "load_courses", return_value={}), \
             patch.object(main, "_login_auto_async", new_callable=AsyncMock, return_value=False), self.assertRaises(SystemExit) as error:
            asyncio.run(main.main_async(args))
        self.assertEqual(error.exception.code, 1)

    def test_successful_auto_login_returns_normally(self):
        args = main._parse_args(["login"])
        with patch.object(main, "load_courses", return_value={}), patch.object(main, "_login_auto_async", new_callable=AsyncMock, return_value=True):
            asyncio.run(main.main_async(args))


class TestLoginCancellation(unittest.TestCase):
    def test_cancelled_cli_stops_the_login_worker(self):
        stopped = threading.Event()
        entered = threading.Event()
        def login(cancel_event, **kwargs):
            entered.set()
            if cancel_event.wait(1):
                stopped.set()
            return False
        async def exercise():
            task = asyncio.create_task(main.main_async(main._parse_args(["login"])))
            while not entered.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with patch.object(main, "load_courses", return_value={}), patch.object(session, "login_auto", side_effect=login):
            asyncio.run(exercise())
        self.assertTrue(stopped.is_set())


class TestMessagesStartup(unittest.TestCase):
    def test_starts_messages_hidden_only_when_needed(self):
        from core import sms_listener as sms
        with patch.object(sms.sys, "platform", "darwin"), \
             patch.object(sms.subprocess, "run", side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]) as run:
            self.assertTrue(sms.ensure_messages_sync())
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/open", "-g", "-j", "-a", "Messages"])

    def test_running_messages_is_not_relaunched(self):
        from core import sms_listener as sms
        with patch.object(sms.sys, "platform", "darwin"), \
             patch.object(sms.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(sms.ensure_messages_sync())
        self.assertEqual(run.call_count, 1)

    def test_windows_never_launches_messages(self):
        from core import sms_listener as sms
        with patch.object(sms.sys, "platform", "win32"), patch.object(sms.subprocess, "run") as run:
            self.assertFalse(sms.ensure_messages_sync())
        run.assert_not_called()


class TestSMSRecovery(unittest.TestCase):
    def invoke(self, captured, platform="darwin", terminal=None, resend_available=True):
        page, context, code_input = MagicMock(), MagicMock(), MagicMock()
        page.url = "https://fixture.duosecurity.com/"
        def click(page, label, *args, **kwargs):
            if isinstance(label, tuple) and label[0] == "Send a new passcode":
                return resend_available
            return True
        def outcome(*args, **kwargs):
            page.url = "https://blackboard.umbc.edu/ultra/course"
            return "accepted"
        # ExitStack: a single 21-item `with` exceeds the static block limit on Python < 3.12.
        with contextlib.ExitStack() as stack:
            for cm in (
                sso_and_verification(page),
                patch.object(session, "ensure_config_exists", return_value=(False, {"auto_login": {"username": "fixture", "password": "fixture"}})),
                patch.object(session.sys, "platform", platform),
                patch.object(session.sys.stdin, "isatty", return_value=terminal is not None),
                patch.object(session, "check_session_state", return_value=("expired", None)),
                patch.object(session, "check_sms_access", return_value=(True, "")),
                patch.object(session, "ensure_messages_sync", return_value=True),
                patch.object(session, "get_current_max_rowid", return_value=10),
                patch.object(session, "sync_playwright"),
                patch.object(session, "_launch_context", return_value=(context, page)),
                patch.object(session, "_duo_passcode_input", return_value=code_input),
                patch.object(session, "_visible_duo_error", return_value=False),
                patch.object(session, "wait_for_duo_sms_passcode", side_effect=captured),
                patch.object(session, "_read_terminal_passcodes", side_effect=terminal),
                patch.object(session, "_wait_for_passcode_outcome", side_effect=outcome),
                patch.object(session, "_wait_for_authenticated_session", return_value=True),
                patch.object(session, "_save_session"),
                patch.object(session, "track_session_usage"),
                patch("telegram.notifier.TelegramNotifier", return_value=SimpleNamespace(enabled=False)),
            ):
                stack.enter_context(cm)
            clicks = stack.enter_context(patch.object(session, "_click_exact_text_in_any_frame", side_effect=click))
            out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            success = session.login_auto(headless=True, auto_exp=True)
        resends = [call for call in clicks.call_args_list if isinstance(call.args[1], tuple) and call.args[1][0] == "Send a new passcode"]
        context.close.assert_called_once()
        return success, code_input.fill.call_count, len(resends), out.getvalue()

    def test_delayed_sms_gets_one_replacement_and_can_succeed(self):
        success, submissions, resends, _ = self.invoke([None, "1234567"])
        self.assertTrue(success)
        self.assertEqual(submissions, 1)
        self.assertEqual(resends, 1)

    def test_resend_cooldown_keeps_listening_for_original_sms(self):
        success, submissions, _, output = self.invoke([None, "1234567"], resend_available=False)
        self.assertTrue(success)
        self.assertEqual(submissions, 1)
        self.assertIn("original SMS", output)

    def test_missing_sms_stops_after_one_replacement(self):
        success, submissions, resends, output = self.invoke([None, None])
        self.assertFalse(success)
        self.assertEqual(submissions, 0)
        self.assertEqual(resends, 1)
        self.assertIn("bb login --manual", output)

    def test_windows_closed_input_does_not_wait_for_timeout(self):
        def closed(results, event, *args):
            results.put(("terminal-closed", None))
        success, submissions, resends, _ = self.invoke([], platform="win32", terminal=closed)
        self.assertFalse(success)
        self.assertEqual(submissions, 0)
        self.assertEqual(resends, 0)

    def test_windows_cancellation_is_explicit(self):
        def cancel(results, event, *args):
            event.set()
        success, submissions, _, output = self.invoke([], platform="win32", terminal=cancel)
        self.assertFalse(success)
        self.assertEqual(submissions, 0)
        self.assertIn("Login cancelled", output)


class TestOptionalNotifications(unittest.TestCase):
    def test_network_notification_does_not_hold_up_login(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def send(text):
            started.set()
            release.wait(1)
            finished.set()
        session._notify_login(SimpleNamespace(send_raw_message=send), "fixture status")
        self.assertTrue(started.wait(1))
        self.assertFalse(finished.is_set())
        release.set()
        self.assertTrue(finished.wait(1))


if __name__ == "__main__":
    unittest.main()
