"""Real browser SSO fixtures; all requests intercepted, no UMBC/Duo traffic."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import login_flow, session


BB = 'https://blackboard.umbc.edu'
SSO = 'https://webauth.umbc.edu/login'
DUO = 'https://fixture.duosecurity.com/auth'
FORM = '''<form action="https://fixture.duosecurity.com/auth">
    <input type="text" name="hidden-decoy" style="display:none">
    <input name="j_username" type="text"><input name="j_password" type="password">
    <button>Log In</button></form>'''


class TestSSOBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='bb sso fixture ')
        cls.pw = session.sync_playwright().start()
        try:
            with patch.object(session, 'SESSION_DIR', Path(cls.temp.name)):
                cls.context, _ = session._launch_context(cls.pw, headless=True)
        except BaseException:
            cls.pw.stop()
            cls.temp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.context.close()
        cls.pw.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.page = self.context.new_page()
        self.addCleanup(self.page.close)
        self.documents = {}
        self.submissions = []

        def route(request_route):
            request = request_route.request
            url = request.url.split('?')[0]
            if url.startswith(DUO):
                self.submissions.append(request.post_data or request.url)
            request_route.fulfill(status=200, content_type='text/html', body=self.documents.get(url, '<html>Fixture</html>'))

        self.page.route('**/*', route)

    def run_flow(self, start, documents, *, verified=False, timeout=2500, cancel_event=None):
        self.documents.update(documents)
        self.page.goto(start)
        with patch.object(login_flow, 'verify_session', return_value=verified):
            return login_flow.wait_for_sso(
                self.context, self.page, 'fixture-user', 'fixture-password', BB,
                session.detect_sso_error, timeout=timeout, cancel_event=cancel_event,
            )

    def test_portal_redirect_bypasses_form_and_verifies_session(self):
        portal = '<a href="' + SSO + '">Log into Blackboard via myUMBC.</a>'
        redirect = '<script>setTimeout(()=>location.href="' + BB + '/ultra/course",100)</script>'
        self.assertEqual(self.run_flow(BB + '/login', {BB + '/login': portal, SSO: redirect}, verified=True), 'authenticated')
        self.assertEqual(self.submissions, [])

    def test_sso_to_duo_without_form(self):
        redirect = '<script>setTimeout(()=>location.href="' + DUO + '",100)</script>'
        self.assertEqual(self.run_flow(SSO, {SSO: redirect}), 'duo')

    def test_delayed_form_ignores_hidden_decoy(self):
        html = '<div id="form"></div><script>setTimeout(()=>document.getElementById("form").innerHTML=' + repr(FORM) + ',150)</script>'
        self.assertEqual(self.run_flow(SSO, {SSO: html}), 'duo')
        self.assertEqual(len(self.submissions), 1)
        self.assertIn('j_username=fixture-user', self.submissions[0])
        self.assertIn('j_password=fixture-password', self.submissions[0])

    def test_password_only_remembered_username(self):
        html = FORM.replace('<input name="j_username" type="text">', '')
        self.assertEqual(self.run_flow(SSO, {SSO: html}), 'duo')
        self.assertEqual(len(self.submissions), 1)

    def test_split_username_and_password_steps(self):
        html = '<form action="/password"><input name="j_username"><button>Next</button></form>'
        self.assertEqual(self.run_flow(SSO, {SSO: html, 'https://webauth.umbc.edu/password': FORM}), 'duo')
        self.assertEqual(len(self.submissions), 1)

    def test_trusted_iframe_form(self):
        html = '<iframe src="https://webauth.umbc.edu/form"></iframe>'
        form = FORM.replace('<form ', '<form target="_top" ')
        self.assertEqual(self.run_flow(SSO, {SSO: html, 'https://webauth.umbc.edu/form': form}), 'duo')

    def test_missing_form_has_bounded_safe_diagnostic(self):
        start = time.monotonic()
        with self.assertRaisesRegex(login_flow.LoginFlowError, 'UMBC redirect or credential form') as caught:
            self.run_flow(SSO + '?execution=private-saml', {SSO: '<div>Changed layout</div>'}, timeout=350)
        self.assertLess(time.monotonic() - start, 2)
        self.assertNotIn('private-saml', str(caught.exception))

    def test_invalid_credentials_are_not_resubmitted(self):
        html = FORM.replace('https://fixture.duosecurity.com/auth', '/rejected')
        error = '<div role="alert">Invalid credentials for private-account</div>'
        with self.assertRaisesRegex(login_flow.LoginFlowError, 'INVALID_CREDENTIALS') as caught:
            self.run_flow(SSO, {SSO: html, 'https://webauth.umbc.edu/rejected': error})
        self.assertNotIn('private-account', str(caught.exception))

    def test_locked_account_stops_before_any_submission(self):
        with self.assertRaisesRegex(login_flow.LoginFlowError, 'ACCOUNT_LOCKED'):
            self.run_flow(SSO, {SSO: '<div role="alert">Account locked</div>' + FORM})
        self.assertEqual(self.submissions, [])

    def test_informational_alert_does_not_block_form(self):
        self.assertEqual(self.run_flow(SSO, {SSO: '<div role="alert">Scheduled maintenance on Sunday</div>' + FORM}), 'duo')

    def test_ultra_url_without_valid_http_session_is_not_success(self):
        with self.assertRaisesRegex(login_flow.LoginFlowError, 'Blackboard session verification'):
            self.run_flow(BB + '/ultra/course', {}, timeout=350)

    def test_unexpected_host_never_receives_credentials(self):
        with self.assertRaisesRegex(login_flow.LoginFlowError, 'unexpected host'):
            self.run_flow('https://webauth.umbc.edu.attacker.invalid/login', {'https://webauth.umbc.edu.attacker.invalid/login': FORM})
        self.assertEqual(self.submissions, [])

    def test_duo_substring_is_not_a_trusted_host(self):
        with self.assertRaisesRegex(login_flow.LoginFlowError, 'unexpected host'):
            self.run_flow('https://fixture.invalid/duosecurity.com/', {})

    def test_cancelled_before_fill(self):
        event = threading.Event()
        event.set()
        with self.assertRaisesRegex(login_flow.LoginFlowError, 'cancelled'):
            self.run_flow(SSO, {SSO: FORM}, cancel_event=event)
        self.assertEqual(self.submissions, [])

    def test_cancellation_during_redirect_wait(self):
        event = threading.Event()
        timer = threading.Timer(0.15, event.set)
        timer.start()
        self.addCleanup(timer.join)
        with self.assertRaisesRegex(login_flow.LoginFlowError, 'cancelled'):
            self.run_flow(SSO, {}, cancel_event=event)

    def test_closed_browser_is_actionable(self):
        self.page.close()
        with self.assertRaisesRegex(login_flow.LoginFlowError, 'closed'):
            login_flow.wait_for_sso(self.context, self.page, '', '', BB, session.detect_sso_error)
