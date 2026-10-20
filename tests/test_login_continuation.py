"""Reproductions from the interrupted login audit; no live authentication."""

import asyncio
import contextlib
import io
import threading
import time
import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import http_refresh, session
from core.passcodes import Source, collect_passcode
from core.session_state import EXPIRED, UNKNOWN, VALID
from telegram.notifier import TelegramNotifier


class TestUnknownRecovery(unittest.TestCase):
    def test_check_output_does_not_print_identity_or_fixed_latency(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous), patch.object(session, 'check_session_state', return_value=(VALID, {'id': 'private-identity', 'studentId': 'private-student', 'userName': 'private-user'})), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                result = asyncio.run(session.check_session_async()) if asynchronous else session.check_session()
                self.assertTrue(result)
                self.assertIn('identity verified', out.getvalue())
                self.assertNotIn('private-', out.getvalue())
                self.assertNotIn('150ms', out.getvalue())

    def test_unknown_https_refresh_stops_both_session_guards(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous), contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(session, 'check_session_state', return_value=(EXPIRED, None)))
                stack.enter_context(patch.object(session, '_http_refresh', return_value=None))
                config = stack.enter_context(patch.object(session, 'ensure_config_exists', return_value=(False, {})))
                login = stack.enter_context(patch.object(session, 'login_auto'))
                stack.enter_context(patch.object(session.sys.stdin, 'isatty', return_value=False))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                result = asyncio.run(session._require_session_async()) if asynchronous else session._require_session()
                self.assertFalse(result)
                config.assert_not_called()
                login.assert_not_called()

    def test_supplied_state_skips_duplicate_probe(self):
        for asynchronous in (False, True):
            for state in (VALID, EXPIRED, UNKNOWN):
                with self.subTest(asynchronous=asynchronous, state=state), contextlib.ExitStack() as stack:
                    probe = stack.enter_context(patch.object(session, 'check_session_state'))
                    refresh = stack.enter_context(patch.object(session, '_http_refresh', return_value=None))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                    result = asyncio.run(session._require_session_async(checked_state=state)) if asynchronous else session._require_session(checked_state=state)
                    self.assertEqual(result, state == VALID)
                    probe.assert_not_called()
                    self.assertEqual(refresh.call_count, int(state == EXPIRED))

    def test_login_auto_unknown_probe_stops_before_refresh_or_browser(self):
        cfg = {'auto_login': {'username': 'fixture', 'password': 'fixture'}}
        with patch.object(session, 'ensure_config_exists', return_value=(False, cfg)), \
             patch.object(session, 'check_session_state', return_value=(UNKNOWN, None)), \
             patch.object(session, 'quick_check_session_http', return_value=(False, None)), \
             patch.object(session, '_http_refresh', return_value=False) as refresh, \
             patch.object(session, 'sync_playwright') as launch, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(session.login_auto.__wrapped__())
        refresh.assert_not_called()
        launch.assert_not_called()


class TestRaceLifetime(unittest.TestCase):
    def test_cancelled_race_starts_no_sources(self):
        cancel = threading.Event()
        cancel.set()
        run = MagicMock()
        collect_passcode([Source('fixture', 'fixture', run)], sent_at=0, timeout=1, cancel=cancel)
        run.assert_not_called()

    def test_cooperative_source_exits_before_race_returns(self):
        exited = threading.Event()
        entered = threading.Event()
        def slow(emit, stop):
            entered.set()
            stop.wait(2)
            time.sleep(0.03)  # cleanup, like a queued terminal read
            exited.set()
        def fast(emit, stop):
            entered.wait(1)
            emit('123456', None)
        result = collect_passcode([Source('slow', 'slow', slow), Source('fast', 'fast', fast)], sent_at=0, timeout=1)
        self.assertEqual(result.code, '123456')
        self.assertTrue(exited.is_set())
        self.assertTrue(exited.wait(1))


class TestHttpValidation(unittest.TestCase):
    def test_verify_server_error_is_unknown(self):
        with patch.object(http_refresh, '_request', return_value=(503, {}, 'unavailable')):
            self.assertEqual(http_refresh._verify(MagicMock()), UNKNOWN)

    def test_deleted_cookie_is_not_resurrected_when_merging(self):
        old = [{'name': 'expired', 'value': 'old', 'domain': 'blackboard.umbc.edu', 'path': '/'}]
        # CookieJar removes a cookie on an expired Set-Cookie response.
        self.assertEqual(http_refresh._merge_cookies(old, http_refresh._jar_from([])), [])


class TestTelegramRace(unittest.TestCase):
    def notifier(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.enabled = True
        notifier.bot_token = 'fixture'
        notifier.admin_chat_id = 42
        notifier.api_base = 'https://fixture.invalid'
        return notifier

    def response(self, updates):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'ok': True, 'result': updates}).encode()
        return response

    def update(self, code, update_id=10):
        return {'update_id': update_id, 'message': {'chat': {'id': 42}, 'text': code}}

    def test_immediate_reply_after_prompt_is_not_discarded_by_initial_offset(self):
        notifier = self.notifier()
        updates = []
        notifier.send_raw_message = lambda _: updates.append(self.update('1234567'))
        def request(url, **kwargs):
            if 'offset=-1' in url:
                return self.response(updates[-1:])
            offset = int(url.split('offset=')[1].split('&')[0])
            return self.response([u for u in updates if u['update_id'] >= offset])
        emitted = []
        with patch('telegram.notifier.urllib.request.urlopen', side_effect=request), \
             patch('telegram.notifier.time.time', side_effect=[0, 0, 121]), \
             patch('telegram.notifier.time.sleep'):
            session._telegram_passcode_source(notifier, 1, 3, '').run(lambda code, _: emitted.append(code), threading.Event())
        self.assertEqual(emitted, ['1234567'])

    def test_rejected_reply_keeps_polling_for_a_different_code(self):
        notifier = self.notifier()
        responses = [self.response([]), self.response([self.update('1234567')]), self.response([self.update('7654321', 11)])]
        with patch('telegram.notifier.urllib.request.urlopen', side_effect=responses), \
             patch('telegram.notifier.time.sleep'):
            self.assertEqual(notifier.poll_for_passcode(timeout_sec=1, excluded_codes={'1234567'}), '7654321')

    def test_cancelled_network_poll_does_not_publish_code(self):
        notifier = self.notifier()
        stop = threading.Event()
        responses = [self.response([]), self.response([self.update('1234567')])]
        def request(*args, **kwargs):
            response = responses.pop(0)
            if not responses:
                stop.set()
            return response
        with patch('telegram.notifier.urllib.request.urlopen', side_effect=request):
            self.assertIsNone(notifier.poll_for_passcode(stop_event=stop))


class TestManualCache(unittest.TestCase):
    def test_tui_manual_fallback_does_not_reacquire_owned_login_lock(self):
        held = [False]
        @contextlib.contextmanager
        def lock():
            if held[0]:
                raise session.LoginInProgress('nested login lock')
            held[0] = True
            try:
                yield
            finally:
                held[0] = False
        context, page = MagicMock(), MagicMock()
        with patch.object(session, 'login_lock', side_effect=lock), \
             patch.object(session, 'ensure_config_exists', return_value=(False, {})), \
             patch.object(session, 'load_config', return_value={}), \
             patch.object(session.sys.stdin, 'isatty', return_value=True), \
             patch.object(session, 'prompt_credentials_tui', return_value=('fixture', 'fixture', False, 'manual')), \
             patch.object(session, 'quick_check_session_http', return_value=(True, {})), \
             patch.object(session, 'sync_playwright'), \
             patch.object(session, '_launch_context', return_value=(context, page)) as launch, \
             patch.object(session, 'wait_for_blackboard'), \
             patch.object(session, '_persist_login', return_value=True), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(session.login_auto(force=True))
        launch.assert_called_once()
        self.assertFalse(held[0])

    def test_unverified_manual_ultra_page_preserves_cache_and_closes(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            cache = root / 'cookies.json'
            cache.write_text('["previous"]')
            context, page = MagicMock(), MagicMock()
            context.pages = [page]
            page.url = 'https://blackboard.umbc.edu/ultra/course'
            context.cookies.return_value = [{'name': 'unverified', 'value': 'fixture'}]
            stack.enter_context(patch.object(session, 'SESSION_DIR', root))
            stack.enter_context(patch.object(session, 'ensure_config_exists'))
            stack.enter_context(patch.object(session, 'load_config', return_value={}))
            stack.enter_context(patch.object(session, 'check_session', return_value=False))
            stack.enter_context(patch.object(session, 'sync_playwright'))
            stack.enter_context(patch.object(session, '_launch_context', return_value=(context, page)))
            stack.enter_context(patch.object(session, 'wait_for_blackboard', side_effect=session.LoginFlowError('not verified')))
            stack.enter_context(patch.object(session, 'track_session_usage'))
            stack.enter_context(patch.object(session, 'record_idp_storage'))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertIs(session.login.__wrapped__(), False)
            self.assertEqual(cache.read_text(), '["previous"]')
            context.close.assert_called_once()


class TestLoginContinuationBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from core.browser import launch_persistent_context
        cls.profile = tempfile.TemporaryDirectory(prefix='bb continuation fixtures ')
        cls.driver = session.sync_playwright().start()
        try:
            cls.browser_context, _ = launch_persistent_context(cls.driver, cls.profile.name, headless=True)
            cls.browser = cls.browser_context.browser
        except BaseException:
            cls.driver.stop()
            cls.profile.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser_context.close()
        cls.driver.stop()
        cls.profile.cleanup()

    def test_push_menu_option_is_selected_and_approved(self):
        page = self.browser_context.new_page()
        self.addCleanup(page.close)
        html = '<button onclick="document.body.innerHTML=\'<button>Yes, this is my device</button>\'">Duo Push</button>'
        page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))
        page.goto('https://fixture.duosecurity.com/auth')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(session._approve_duo_push(page, None, lambda: False, timeout=0.2))
        self.assertTrue(page.get_by_text('Yes, this is my device', exact=True).is_visible())

    def invoke(self, *, methods='passcode', telegram=False, cancel=False):
        from core import login_flow
        context = self.browser.new_context()
        page = context.new_page()
        self.addCleanup(context.close)
        authenticated = [False]
        menu = ('<button onclick="this.remove()">Other options</button>'
                '<button onclick="location.href=\'/input\'">Duo Mobile passcode</button>'
                + ('<button>Duo Push</button>' if methods == 'push+passcode' else ''))
        form = """<form onsubmit="event.preventDefault();if(document.querySelector('input').value==='246810'){location.href='/trust'}else{document.querySelector('div').innerText='Incorrect passcode'}">
        <input name="passcode"><button>Verify</button><div role="alert"></div></form>"""
        trust = '<button onclick="location.href=\'https://blackboard.umbc.edu/ultra/ready\'">Yes, this is my device</button>'
        def route(request_route):
            url = request_route.request.url
            if url == 'https://blackboard.umbc.edu/ultra/course':
                body = '<script>location.href="https://fixture.duosecurity.com/menu"</script>'
            elif url.endswith('/menu'):
                body = menu
            elif url.endswith('/input'):
                body = form
            elif url.endswith('/trust'):
                body = trust
            else:
                authenticated[0] = True
                body = '<h1>Ready</h1>'
            request_route.fulfill(content_type='text/html', body=body)
        page.route('**/*', route)
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            code_path = root / 'code.txt'
            code_path.write_text('000000')  # stale input is removed before SSO starts
            cache = root / 'cookies.json'
            cache.write_text('["previous"]')
            stop = threading.Event()
            notified = []
            stream = io.StringIO()
            class Output:
                def write(self, value):
                    if 'WAITING_FOR_CODE' in value:
                        if cancel:
                            stop.set()
                        else:
                            code_path.write_text('246810\n', encoding='utf-8-sig')
                    return stream.write(value)
                def flush(self):
                    pass
            notifier = SimpleNamespace(enabled=telegram)
            if telegram:
                notifier.send_raw_message = lambda text: notified.append(text)
                def poll(**kwargs):
                    kwargs['on_ready']()
                    return '246810'
                notifier.poll_for_passcode = poll
            stack.enter_context(patch.object(session, 'SESSION_DIR', root))
            stack.enter_context(patch.object(session, 'ensure_config_exists', return_value=(False, {'auto_login': {'username': 'fixture', 'password': 'fixture'}})))
            stack.enter_context(patch.object(session, 'check_session_state', return_value=(EXPIRED, None)))
            stack.enter_context(patch.object(session, 'sync_playwright'))
            stack.enter_context(patch.object(session, '_launch_context', return_value=(context, page)))
            stack.enter_context(patch.object(session, 'check_sms_access', return_value=(False, 'fixture: unavailable')))
            stack.enter_context(patch.object(session.sys.stdin, 'isatty', return_value=False))
            stack.enter_context(patch('telegram.notifier.TelegramNotifier', return_value=notifier))
            stack.enter_context(patch.object(login_flow, 'verify_session', side_effect=lambda *a, **k: authenticated[0]))
            stack.enter_context(patch.object(session, 'record_idp_storage'))
            stack.enter_context(patch.object(session, 'record_user_agent'))
            stack.enter_context(contextlib.redirect_stdout(Output()))
            result = session.login_auto.__wrapped__(headless=True, cancel_event=stop, code_file=None if telegram else str(code_path), code_timeout=1)
            self.assertEqual(result, not cancel)
            self.assertTrue(page.is_closed())
            if cancel:
                self.assertEqual(cache.read_text(), '["previous"]')
            elif not telegram:
                self.assertFalse(code_path.exists())
                self.assertEqual(stream.getvalue().count('WAITING_FOR_CODE'), 1)
            if telegram:
                self.assertTrue(any('Open Duo Mobile' in text for text in notified))
            self.assertNotIn('being sent', stream.getvalue())

    def test_code_file_arrives_mid_flow_with_bom_and_no_sms(self):
        self.invoke()

    def test_supplied_code_prefers_mobile_over_push_when_sms_is_absent(self):
        self.invoke(methods='push+passcode')

    def test_cancel_during_code_file_wait_preserves_cache(self):
        self.invoke(cancel=True)

    def test_telegram_is_available_when_messages_is_unavailable(self):
        self.invoke(telegram=True)


class TestProbeCookieIsolation(unittest.TestCase):
    def test_early_identity_probe_cannot_shadow_later_saml_root_cookie(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from core import login_flow
        from core.browser import launch_persistent_context
        probed = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                if self.path == '/pending':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.send_header('Set-Cookie', 'JSESSIONID=fixture-pending; Path=/')
                    self.end_headers()
                    self.wfile.write(b'<script>fetch("/begin").then(()=>location.href="/finish")</script>')
                elif self.path == '/begin':
                    probed.wait(3)
                    self.send_response(204)
                    self.end_headers()
                elif self.path == '/finish':
                    self.send_response(200)
                    self.send_header('Set-Cookie', 'JSESSIONID=fixture-authenticated; Path=/')
                    self.end_headers()
                elif self.path.startswith('/learn/api/'):
                    probed.set()
                    # Servlet-style selection: the path-specific cookie is first.
                    cookie = self.headers.get('Cookie', '')
                    first = next((part.strip() for part in cookie.split(';') if part.strip().startswith('JSESSIONID=')), '')
                    valid = first == 'JSESSIONID=fixture-authenticated'
                    self.send_response(200 if valid else 401)
                    if not valid:
                        self.send_header('Set-Cookie', 'JSESSIONID=fixture-unauthenticated; Path=/learn/api')
                    self.end_headers()
                    self.wfile.write(b'{"id":"fixture"}' if valid else b'{}')
                else:
                    self.send_response(204)
                    self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as profile, session.sync_playwright() as pw:
                context, _ = launch_persistent_context(pw, profile, headless=True)
                try:
                    base = 'http://127.0.0.1:' + str(server.server_port)
                    page = context.new_page()
                    page.goto(base + '/pending')
                    login_flow.wait_for_blackboard(context, page, base, timeout=2500)
                    self.assertFalse(any(c['name'] == 'JSESSIONID' and c['path'] == '/learn/api' for c in context.cookies()))
                finally:
                    context.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)
