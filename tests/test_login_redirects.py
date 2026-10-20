"""SSO can bypass the credential form while the portal click is returning."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import session


class TestLoginRedirects(unittest.TestCase):
    def test_portal_sso_redirect_never_requires_username_or_enter(self):
        page, context = MagicMock(), MagicMock()
        page.url = 'https://blackboard.umbc.edu/webapps/login/'
        page.frames = [page]
        page.is_closed.return_value = False
        page.locator.return_value.count.return_value = 0
        page.locator.return_value.first.is_visible.return_value = False
        page.get_by_label.return_value.count.return_value = 0
        page.get_by_role.return_value.count.return_value = 0
        page.get_by_text.return_value.count.return_value = 0

        def click(*args, **kwargs):
            page.url = 'https://webauth.umbc.edu/idp/profile/SAML2/POST/SSO?private=omitted'
            return True

        def tick(*args):
            page.url = 'https://blackboard.umbc.edu/ultra/course'

        page.wait_for_timeout.side_effect = tick
        context.cookies.return_value = []
        response = context.request.get.return_value
        response.status = 200
        response.json.return_value = {'id': 'fixture-user'}
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(session, 'SESSION_DIR', Path(tmp)))
            stack.enter_context(patch.object(session, 'ensure_config_exists', return_value=(False, {'auto_login': {'username': 'fixture', 'password': 'fixture'}})))
            stack.enter_context(patch.object(session, 'check_session_state', return_value=('expired', None)))
            stack.enter_context(patch.object(session, 'sync_playwright'))
            stack.enter_context(patch.object(session, '_launch_context', return_value=(context, page)))
            stack.enter_context(patch.object(session, '_click_exact_text_in_any_frame', side_effect=click))
            stack.enter_context(patch('core.login_flow.probe_session', return_value=('valid', {'id': 'fixture'})))
            stack.enter_context(patch.object(session, 'track_session_usage'))
            stack.enter_context(patch('telegram.notifier.TelegramNotifier', return_value=SimpleNamespace(enabled=False)))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertTrue(session.login_auto(headless=True, auto_exp=True))
        page.keyboard.press.assert_not_called()
        page.locator.return_value.first.fill.assert_not_called()
        context.close.assert_called_once()
