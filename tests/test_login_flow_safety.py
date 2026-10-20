"""Persistence, caller cancellation, and login boundary behavior."""

import asyncio
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

import main
from core import login_flow, session, sms_listener


class TestLoginSafety(unittest.TestCase):
    def test_sms_listener_never_prints_passcode_sender_or_message(self):
        for source in ('sqlite', 'imsg'):
            with self.subTest(source=source), contextlib.ExitStack() as stack:
                message = ('1234567', 0, 'private-sender', 'private-message')
                stack.enter_context(patch.object(sms_listener.sys, 'platform', 'darwin'))
                stack.enter_context(patch.object(sms_listener, 'check_sms_access', return_value=(True, '')))
                stack.enter_context(patch.object(sms_listener, 'get_latest_duo_sms_sqlite', return_value=message if source == 'sqlite' else None))
                stack.enter_context(patch.object(sms_listener, 'get_latest_duo_sms_imsg', return_value=message))
                output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                self.assertEqual(sms_listener.wait_for_duo_sms_passcode(timeout_seconds=1), '1234567')
                for secret in message[::2] + ('private-message',):
                    self.assertNotIn(secret, output.getvalue())

    def test_cancelled_login_does_not_launch_browser(self):
        event = threading.Event()
        event.set()
        with patch.object(session, 'sync_playwright') as launch:
            self.assertFalse(session.login_auto(cancel_event=event))
        launch.assert_not_called()

    def invoke_failed_login(self, *, failure=None, status=200, cancel=False):
        context, page = MagicMock(), MagicMock()
        page.url = 'https://blackboard.umbc.edu/ultra/course'
        context.request.get.return_value.status = status
        context.request.get.return_value.json.return_value = {'id': 'fixture-user'}
        event = threading.Event()

        def flow(*args, **kwargs):
            if cancel:
                event.set()
            if failure:
                raise failure
            return 'duo'

        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            cache = root / 'cookies.json'
            cache.write_text('["fixture-previous-cache"]')
            stack.enter_context(patch.object(session, 'SESSION_DIR', root))
            stack.enter_context(patch.object(session, 'ensure_config_exists', return_value=(False, {'auto_login': {'username': 'fixture', 'password': 'fixture'}})))
            stack.enter_context(patch.object(session, 'check_session_state', return_value=('expired', None)))
            stack.enter_context(patch.object(session, 'sync_playwright'))
            launch = stack.enter_context(patch.object(session, '_launch_context', return_value=(context, page)))
            stack.enter_context(patch.object(session, 'wait_for_sso', side_effect=flow))
            stack.enter_context(patch.object(session, '_is_authenticated_session', return_value=True))
            stack.enter_context(patch('telegram.notifier.TelegramNotifier', return_value=SimpleNamespace(enabled=False)))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertFalse(session.login_auto(headless=True, cancel_event=event))
            self.assertEqual(json.loads(cache.read_text()), ['fixture-previous-cache'])
            launch.assert_called_once()
            context.close.assert_called_once()
            self.assertNotIn('private-value', output.getvalue())

    def test_auth_failure_preserves_cache_and_never_switches_browser(self):
        self.invoke_failed_login(failure=login_flow.LoginFlowError('UMBC authentication failed (ACCOUNT_LOCKED).'))

    def test_navigation_failure_preserves_cache_and_redacts_exception(self):
        self.invoke_failed_login(failure=session.PlaywrightError('https://fixture.invalid/?token=private-value'))

    def test_unverified_dashboard_preserves_cache(self):
        self.invoke_failed_login(status=401)

    def test_cancellation_preserves_cache(self):
        self.invoke_failed_login(cancel=True)

    def test_atomic_write_failure_preserves_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / 'cookies.json'
            cache.write_text('["previous-fixture"]')
            context = MagicMock()
            context.cookies.return_value = [{'name': 'fixture', 'value': 'fixture'}]
            with patch.object(session, 'SESSION_DIR', root), patch.object(session.os, 'replace', side_effect=OSError('fixture disk full')), contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(session._save_session(context))
            self.assertEqual(json.loads(cache.read_text()), ['previous-fixture'])
            self.assertEqual(list(root.iterdir()), [cache])

    def test_identity_probe_uses_cookie_snapshot_without_mutating_browser(self):
        for state, expected in [('valid', True), ('expired', False), ('unknown', False)]:
            with self.subTest(state=state):
                context = MagicMock()
                cookies = [{'name': 'fixture', 'value': 'fixture', 'domain': 'fixture.invalid', 'path': '/'}]
                context.cookies.return_value = cookies
                with patch.object(login_flow, 'probe_session', return_value=(state, None)) as probe:
                    self.assertEqual(login_flow.verify_session(context, 'https://fixture.invalid'), expected)
                probe.assert_called_once_with(timeout=2, cookies=cookies, url='https://fixture.invalid/learn/api/public/v1/users/me')
                context.request.get.assert_not_called()
                context.add_cookies.assert_not_called()

    def test_browser_cookie_failure_is_not_verified(self):
        context = MagicMock()
        context.cookies.side_effect = session.PlaywrightError('fixture')
        self.assertFalse(login_flow.is_verified(context, 'https://fixture.invalid'))

    def test_automatic_login_rejects_borrowed_browser_without_launch(self):
        with patch.object(session, 'sync_playwright') as start, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(session.login_auto(cdp_url='http://fixture.invalid'))
        start.assert_not_called()

    def test_plain_login_failure_has_nonzero_exit(self):
        with patch.object(main, '_login_auto_async', new_callable=AsyncMock, return_value=False), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                asyncio.run(main.main_async(main._parse_args(['login'])))
        self.assertEqual(caught.exception.code, 1)

    def test_async_cancellation_signals_worker(self):
        started = threading.Event()
        stopped = threading.Event()

        def worker(cancel_event, **kwargs):
            started.set()
            if cancel_event.wait(3):
                stopped.set()
            return False

        async def run():
            task = asyncio.create_task(session._login_auto_async(headless=True))
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with patch.object(session, 'login_auto', side_effect=worker):
            asyncio.run(run())
        self.assertTrue(stopped.is_set())

    def test_async_refresh_and_sync_refresh_use_login_and_reprobe(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous), contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(session, 'check_session_state', return_value=('expired', None)))
                stack.enter_context(patch.object(session, 'quick_check_session_http', return_value=(True, {'id': 'fixture'})))
                stack.enter_context(patch.object(session, 'ensure_config_exists', return_value=(False, {'auto_login': {'username': 'fixture', 'password': 'fixture'}})))
                login = stack.enter_context(patch.object(session, 'login_auto', return_value=True))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                result = asyncio.run(session._require_session_async()) if asynchronous else session._require_session()
                self.assertTrue(result)
                login.assert_called_once()
                self.assertTrue(login.call_args.kwargs['auto_exp'])

    def test_known_profile_lock_never_tries_second_browser(self):
        for message in ('ProcessSingleton', 'SingletonLock', 'profile is already in use', 'Target directory is in use'):
            with self.subTest(message=message), patch('core.browser.get_browser_launch_candidates', return_value=[{'name': 'first'}, {'name': 'second'}]), contextlib.redirect_stdout(io.StringIO()):
                pw = MagicMock()
                pw.chromium.launch_persistent_context.side_effect = RuntimeError(message)
                with self.assertRaises(SystemExit):
                    session._launch_context(pw)
                self.assertEqual(pw.chromium.launch_persistent_context.call_count, 1)
