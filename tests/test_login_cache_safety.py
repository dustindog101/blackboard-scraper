"""Cache preservation and cancellation regressions for automated login."""

import contextlib
import io
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import session


class TestLoginCacheSafety(unittest.TestCase):
    def invoke_login(
        self,
        *,
        authenticated=False,
        initial_url="https://fixture.duosecurity.com/",
        auth_markers=False,
        cancel_event=None,
        cancel_on_goto=False,
        cookie_error=None,
        quick_checks=None,
        outcome_results=None,
    ):
        temp = tempfile.TemporaryDirectory(prefix="bb login cache safety ")
        self.addCleanup(temp.cleanup)
        session_dir = Path(temp.name)
        cache = session_dir / "cookies.json"
        original_cache = b'[{"name":"old-session","value":"preserve-me"}]\n'
        cache.write_bytes(original_cache)

        context, page, code_input = MagicMock(), MagicMock(), MagicMock()
        context.pages = [page]
        if cookie_error is not None:
            context.cookies.side_effect = cookie_error
        else:
            context.cookies.return_value = [{"name": "new-session", "value": "fixture"}]
        page.url = initial_url
        cancel_event = cancel_event or threading.Event()

        def goto(*_args, **_kwargs):
            if cancel_on_goto:
                cancel_event.set()

        page.goto.side_effect = goto
        playwright = MagicMock()
        playwright.__enter__.return_value = MagicMock()

        def click(_page, target, *_args, **_kwargs):
            return target in ("Verify", session.TRUST_LABELS)

        def outcome(*_args, **_kwargs):
            result = outcome_results.pop(0) if outcome_results else "accepted"
            if result == "accepted":
                page.url = "https://blackboard.umbc.edu/ultra/course"
            return result

        out = io.StringIO()
        extras = contextlib.ExitStack()
        self.addCleanup(extras.close)
        for name, kwargs in (
            ("wait_for_sso", {"side_effect": lambda *a, **k: "duo" if "duosecurity" in page.url else "authenticated"}),
            ("duo_methods", {"return_value": ["sms"]}),
            ("is_verified", {"side_effect": lambda *a, **k: authenticated}),
            ("wait_for_blackboard", {"side_effect": lambda *a, **k: None if authenticated else (_ for _ in ()).throw(session.LoginFlowError("not verified"))}),
            ("record_user_agent", {}),
        ):
            extras.enter_context(patch.object(session, name, **kwargs))
        checks = quick_checks or [(False, None)]
        with patch.object(session, "SESSION_DIR", session_dir), \
             patch.object(session, "ensure_config_exists", return_value=(False, {"auto_login": {"username": "fixture", "password": "fixture"}})), \
             patch.object(session, "quick_check_session_http", side_effect=checks), \
             patch.object(session.sys, "platform", "win32"), \
             patch.object(session.sys.stdin, "isatty", return_value=False), \
             patch.object(session, "sync_playwright", return_value=playwright), \
             patch.object(session, "_launch_context", return_value=(context, page)), \
             patch.object(session, "_click_exact_text_in_any_frame", side_effect=click), \
             patch.object(session, "_duo_passcode_input", return_value=code_input), \
             patch.object(session, "_visible_duo_error", return_value=False), \
             patch.object(session, "_is_authenticated_session", return_value=False), \
             patch.object(session, "_has_auth_markers", return_value=auth_markers), \
             patch.object(session, "_is_clearly_authenticated_url", side_effect=lambda url: "blackboard.umbc.edu/ultra" in url), \
             patch.object(session, "_wait_for_passcode_outcome", side_effect=outcome), \
             patch.object(session, "_wait_for_authenticated_session", return_value=authenticated), \
             patch.object(session, "track_session_usage"), \
             patch("telegram.notifier.TelegramNotifier", return_value=SimpleNamespace(enabled=False)), \
             contextlib.redirect_stdout(out):
            result = session.login_auto(passcode="1234567", cancel_event=cancel_event)

        return result, out.getvalue(), cache, original_cache, context, code_input

    def test_final_auth_verification_failure_preserves_existing_cache(self):
        result, _, cache, original, context, _ = self.invoke_login(authenticated=False)

        self.assertFalse(result)
        self.assertEqual(cache.read_bytes(), original)
        context.cookies.assert_not_called()
        context.close.assert_called_once()

    def test_early_auth_marker_context_failure_preserves_existing_cache(self):
        result, _, cache, original, context, _ = self.invoke_login(
            initial_url="https://blackboard.umbc.edu/ultra/course",
            auth_markers=True,
            authenticated=False,
        )

        self.assertFalse(result)
        self.assertEqual(cache.read_bytes(), original)
        context.cookies.assert_not_called()

    def test_early_context_auth_succeeds_with_expired_disk_cache(self):
        result, _, cache, original, context, _ = self.invoke_login(
            initial_url="https://blackboard.umbc.edu/ultra/course",
            auth_markers=True,
            authenticated=True,
            quick_checks=[(False, None)],
        )

        self.assertTrue(result)
        self.assertNotEqual(cache.read_bytes(), original)
        context.cookies.assert_called_once()

    def test_cancel_after_navigation_prevents_passcode_submission_and_save(self):
        result, output, cache, original, context, code_input = self.invoke_login(cancel_on_goto=True)

        self.assertFalse(result)
        self.assertIn("Login cancelled", output)
        code_input.fill.assert_not_called()
        context.cookies.assert_not_called()
        self.assertEqual(cache.read_bytes(), original)

    def test_verified_login_reports_cookie_save_failure_without_replacing_cache(self):
        result, output, cache, original, context, _ = self.invoke_login(
            authenticated=True,
            cookie_error=OSError("fixture save failure"),
        )

        self.assertFalse(result)
        self.assertIn("Could not save session cookies", output)
        self.assertNotIn("Auto-Login Successful", output)
        self.assertEqual(cache.read_bytes(), original)
        self.assertEqual(list(cache.parent.glob(".cookies.json.*.tmp")), [])

    def test_rejected_cli_code_without_retry_source_does_not_wait_on_empty_queue(self):
        waiting_queue = MagicMock()
        waiting_queue.get.side_effect = queue.Empty
        with patch("queue.Queue", return_value=waiting_queue):
            result, output, cache, original, context, _ = self.invoke_login(
                outcome_results=["rejected"],
            )

        self.assertFalse(result)
        self.assertIn("No passcode input is available for another attempt", output)
        waiting_queue.get.assert_not_called()
        context.cookies.assert_not_called()
        self.assertEqual(cache.read_bytes(), original)

    def test_cookie_cache_replace_is_atomic_on_commit_failure(self):
        with tempfile.TemporaryDirectory(prefix="bb cookie replace ") as temp:
            session_dir = Path(temp)
            cache = session_dir / "cookies.json"
            original = b'[{"name":"old-session","value":"preserve-me"}]\n'
            cache.write_bytes(original)
            context = MagicMock()
            context.cookies.return_value = [{"name": "new-session", "value": "fixture"}]

            with patch.object(session, "SESSION_DIR", session_dir), \
                 patch.object(session.os, "replace", side_effect=OSError("fixture replace failure")), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(session._save_session(context))

            self.assertEqual(cache.read_bytes(), original)
            self.assertEqual(list(session_dir.glob(".cookies.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
