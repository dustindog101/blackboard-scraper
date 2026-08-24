"""
Unit and Integration Tests for Blackboard Ultra Scraper v2 features:
- Dynamic Route Optimizer rules
- Task-aware concurrency profile calculation
- Message chunking and HTML escaping for Telegram
- Standardized v2 Composite JSON schema generation
- Dual-channel Duo passcode regex parsing
- Lightweight HTTP session validation
- Native macOS Menubar App structure
- Due Date Aggregation logic and date parsing
"""

import asyncio
import json
import re
import unittest
from unittest.mock import patch

import sys
from core.export_json import build_composite_schema
from core.async_engine import RouteOptimizer, TaskProfile, get_optimal_concurrency
from core.session import quick_check_session_http
from telegram.formatter import escape_html, chunk_message

try:
    import rumps  # noqa: F401
    from ui.menubar import BlackboardMenuBarApp
    HAS_RUMPS = True
except ImportError:
    HAS_RUMPS = False
    BlackboardMenuBarApp = None


class TestSmartConcurrency(unittest.TestCase):
    def test_optimal_concurrency_profiles(self):
        c_light = get_optimal_concurrency(TaskProfile.LIGHT)
        c_heavy = get_optimal_concurrency(TaskProfile.HEAVY)
        c_medium = get_optimal_concurrency(TaskProfile.MEDIUM)

        self.assertGreaterEqual(c_light, c_heavy)
        self.assertGreaterEqual(c_medium, c_heavy)

    def test_user_override_concurrency(self):
        c_custom = get_optimal_concurrency(TaskProfile.HEAVY, user_override=10)
        self.assertEqual(c_custom, 10)


class TestStandardizedJSONSchema(unittest.TestCase):
    def test_build_composite_schema(self):
        bundle = {
            "courses": {
                "_105737_1": {
                    "course_name": "IS 410 Database Design",
                    "outline": [
                        {"content_type": "syllabus", "title": "Course Syllabus", "links": [{"text": "Syllabus.pdf", "url": "https://example.com/s.pdf"}]},
                        {"content_type": "folder", "title": "Week 1", "depth": 0, "links": []}
                    ],
                    "assignments": [
                        {"title": "Project 1", "points_possible": 100, "submission_status": "Unattempted"}
                    ],
                    "grades": [
                        {"name": "Quiz 1", "grade": "90 / 100", "dueDate": "2026-09-10"}
                    ],
                    "announcements": [
                        {"title": "Welcome", "meta": "Aug 15", "unread": True, "body": "Welcome to IS 410"}
                    ]
                }
            },
            "calendar": [
                {"title": "Project 1", "course": "IS 410", "due": "Sep 15, 2026"}
            ],
            "activity": []
        }

        json_str = build_composite_schema(bundle, user_info={"name": "Amanuel Hailie", "username": "BH69617"})
        data = json.loads(json_str)

        self.assertEqual(data["version"], "2.0")
        self.assertEqual(data["summary"]["total_courses"], 1)
        self.assertEqual(data["summary"]["upcoming_deadlines_count"], 1)
        self.assertEqual(data["summary"]["unread_announcements_count"], 1)
        self.assertEqual(data["user"]["username"], "BH69617")

        course = data["courses"][0]
        self.assertEqual(course["course_id"], "_105737_1")
        self.assertIsNotNone(course["syllabus"])
        self.assertEqual(course["syllabus"]["title"], "Course Syllabus")
        self.assertEqual(len(course["assignments"]), 1)
        self.assertEqual(len(course["grades"]), 1)
        self.assertEqual(len(course["announcements"]), 1)


class TestTelegramFormatter(unittest.TestCase):
    def test_escape_html(self):
        raw = "<script>alert('test & review')</script>"
        escaped = escape_html(raw)
        self.assertEqual(escaped, "&lt;script&gt;alert('test &amp; review')&lt;/script&gt;")

    def test_chunk_message_short(self):
        msg = "Short message"
        chunks = chunk_message(msg, max_length=100)
        self.assertEqual(chunks, ["Short message"])

    def test_chunk_message_split(self):
        long_msg = "\n".join([f"Line {i}: Some detailed content for testing" for i in range(100)])
        chunks = chunk_message(long_msg, max_length=200)
        self.assertTrue(len(chunks) > 1)
        for chunk in chunks:
            self.assertTrue(len(chunk) <= 200)

    def test_passcode_regex_matching(self):
        sample_messages = [
            ("123456", "123456"),
            ("My code is 654321", "654321"),
            ("987654 thanks!", "987654"),
            ("Invalid 123", None),
        ]
        for text, expected in sample_messages:
            match = re.search(r"\b(\d{6})\b", text)
            extracted = match.group(1) if match else None
            self.assertEqual(extracted, expected)


class TestSMSListener(unittest.TestCase):
    def test_umbc_duo_sms_patterns(self):
        from core.sms_listener import PASSCODE_REGEXES
        sample_texts = [
            ("UMBC SMS passcode (will expire in 5 minutes): 1191652", "1191652"),
            ("UMBC SMS passcode (will expire in 5 minutes): 1985729", "1985729"),
            ("Your Duo passcode is: 654321", "654321"),
            ("Use passcode 885259 to log in", "885259"),
            ("UMBC code: 1234567", "1234567"),
        ]
        for msg, expected in sample_texts:
            matched_code = None
            for pattern in PASSCODE_REGEXES:
                m = pattern.search(msg)
                if m:
                    matched_code = m.group(1)
                    break
            self.assertEqual(matched_code, expected)



class TestRouteOptimizer(unittest.TestCase):
    def test_blocked_extensions(self):
        self.assertIn("png", RouteOptimizer.BLOCKED_EXTENSIONS)
        self.assertIn("woff2", RouteOptimizer.BLOCKED_EXTENSIONS)
        self.assertIn("mp4", RouteOptimizer.BLOCKED_EXTENSIONS)

    def test_blocked_domains(self):
        self.assertIn("telemetry.blackboard.com", RouteOptimizer.BLOCKED_DOMAINS)
        self.assertIn("google-analytics.com", RouteOptimizer.BLOCKED_DOMAINS)


class TestSessionValidation(unittest.TestCase):
    def test_http_probe_live(self):
        is_valid, user_data = quick_check_session_http()
        if is_valid:
            self.assertIsNotNone(user_data)
            self.assertTrue("id" in user_data or "studentId" in user_data or "userName" in user_data)


@unittest.skipUnless(sys.platform == "darwin" and HAS_RUMPS, "Menubar tests require macOS and rumps package")
class TestMenuBarApp(unittest.TestCase):
    def test_menubar_instantiation(self):
        app = BlackboardMenuBarApp()
        self.assertIn("BB", app.title)
        self.assertIsNotNone(app.item_user)
        self.assertIsNotNone(app.menu_session_actions)
        self.assertIsNotNone(app.menu_bot_controls)
        self.assertIsNotNone(app.item_run_briefing)
        self.assertIsNotNone(app.menu_due_dates)
        self.assertIsNotNone(app.menu_grades)


class TestSessionTracker(unittest.TestCase):

    def test_tracker_lifecycle_and_averages(self):
        import tempfile
        from pathlib import Path
        from core.session_tracker import SessionTracker

        with tempfile.TemporaryDirectory() as tmpdir:
            test_path = Path(tmpdir) / "test_telemetry.json"
            tracker = SessionTracker(test_path)

            # 1. New session probe
            changed, alert = tracker.record_probe(True, {"studentId": "BH69617"})
            self.assertTrue(changed)
            self.assertIn("session established", alert)
            self.assertEqual(tracker.data["current_session"]["status"], "VALID")

            # 2. Re-probe while still valid -> no state change
            changed2, alert2 = tracker.record_probe(True, {"studentId": "BH69617"})
            self.assertFalse(changed2)
            self.assertIsNone(alert2)

            # 3. Simulate passage of time and session expiration
            tracker.data["current_session"]["login_time"] -= 36000  # 10 hours ago
            changed3, alert3 = tracker.record_probe(False)
            self.assertTrue(changed3)
            self.assertIn("Session Expired", alert3)
            self.assertIn("Lifespan", alert3)
            self.assertEqual(len(tracker.data["history"]), 1)
            self.assertEqual(tracker.data["current_session"]["status"], "EXPIRED")

            # 4. Check statistics calculation
            stats = tracker.data["stats"]
            self.assertEqual(stats["total_recorded_sessions"], 1)
            self.assertIn("h", stats["average_lifespan_human"])

            # 5. Format CLI summary verification
            cli_text = tracker.format_cli_summary()
            self.assertIn("BLACKBOARD SESSION LIFESPAN TELEMETRY", cli_text)


class TestConfigInitializationAndCredentials(unittest.TestCase):
    def test_ensure_config_exists_and_blank_creation(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        import core.config as config_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            test_cfg_path = Path(tmpdir) / "config.json"
            with patch.object(config_mod, "CONFIG_FILE", test_cfg_path):
                self.assertFalse(test_cfg_path.exists())

                # Should create blank config
                created, cfg = config_mod.ensure_config_exists(notify=False)
                self.assertTrue(created)
                self.assertTrue(test_cfg_path.exists())
                self.assertIn("courses", cfg)
                self.assertIn("auto_login", cfg)
                self.assertEqual(cfg["auto_login"]["username"], "")
                self.assertEqual(cfg["auto_login"]["password"], "")

                # Second call should not re-create
                created2, cfg2 = config_mod.ensure_config_exists(notify=False)
                self.assertFalse(created2)

    def test_has_auto_login_credentials(self):
        from core.config import has_auto_login_credentials

        self.assertFalse(has_auto_login_credentials({}))
        self.assertFalse(has_auto_login_credentials({"auto_login": {"username": "", "password": ""}}))
        self.assertFalse(has_auto_login_credentials({"auto_login": {"username": "user123", "password": ""}}))
        self.assertTrue(has_auto_login_credentials({"auto_login": {"username": "user123", "password": "securepassword"}}))

    def test_save_auto_login_credentials(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        import core.config as config_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            test_cfg_path = Path(tmpdir) / "config.json"
            test_cfg_path.write_text(json.dumps({"courses": {"_1001_1": "Test Course"}}))
            with patch.object(config_mod, "CONFIG_FILE", test_cfg_path):
                config_mod.save_auto_login_credentials("student1", "secret123")
                saved = json.loads(test_cfg_path.read_text())
                self.assertEqual(saved["auto_login"]["username"], "student1")
                self.assertEqual(saved["auto_login"]["password"], "secret123")
                # Ensure existing courses preserved
                self.assertIn("_1001_1", saved["courses"])


class TestTUIAuthPromptAndAutoRecovery(unittest.TestCase):
    @patch("builtins.input", side_effect=["testuser@umbc.edu", "y", "1"])
    @patch("core.session.getpass", return_value="mypassword123")
    def test_prompt_credentials_tui_success(self, mock_getpass, mock_input):
        from core.session import prompt_credentials_tui

        usr, pwd, save_creds, mode = prompt_credentials_tui(default_auto_exp=True)
        self.assertEqual(usr, "testuser@umbc.edu")
        self.assertEqual(pwd, "mypassword123")
        self.assertTrue(save_creds)
        self.assertEqual(mode, "auto_exp")

    @patch("builtins.input", side_effect=KeyboardInterrupt)
    def test_prompt_credentials_tui_cancel(self, mock_input):
        from core.session import prompt_credentials_tui

        usr, pwd, save_creds, mode = prompt_credentials_tui(default_auto_exp=True)
        self.assertIsNone(usr)
        self.assertIsNone(pwd)
        self.assertFalse(save_creds)
        self.assertEqual(mode, "")

    @patch("sys.stdin.isatty", return_value=False)
    @patch("core.session.ensure_config_exists", return_value=(True, {"courses": {}, "auto_login": {}}))
    def test_login_auto_non_interactive_no_credentials(self, mock_ensure, mock_isatty):
        import io
        from contextlib import redirect_stdout
        from core.session import login_auto

        out = io.StringIO()
        with redirect_stdout(out):
            login_auto(username=None, password=None, headless=True)
        output_str = out.getvalue()
        self.assertIn("No login detected and no credentials found in config.json", output_str)

    @patch("core.session.quick_check_session_http", side_effect=[(False, None), (True, {"studentId": "BH69617"})])
    @patch("core.session.check_session", return_value=False)
    @patch("core.session.ensure_config_exists", return_value=(False, {"auto_login": {"username": "BH69617", "password": "pass"}}))
    @patch("core.session.has_auto_login_credentials", return_value=True)
    @patch("core.session.login_auto")
    def test_require_session_auto_recovery_with_credentials(self, mock_login_auto, mock_has_creds, mock_ensure, mock_check, mock_http):
        from core.session import _require_session

        res = _require_session()
        self.assertTrue(res)
        mock_login_auto.assert_called_once_with(username=None, password=None, headless=True, cdp_url=None, auto_exp=True, force=False)


class TestSSOErrorDetection(unittest.TestCase):
    def test_detect_sso_error_patterns(self):
        from unittest.mock import MagicMock
        from core.session import detect_sso_error

        # 1. Invalid credentials
        mock_page = MagicMock()
        mock_page.url = "https://webauth.umbc.edu/idp/profile/cas/login"
        mock_loc = MagicMock()
        mock_loc.count.return_value = 1
        mock_item = MagicMock()
        mock_item.is_visible.return_value = True
        mock_item.inner_text.return_value = "The username or password you entered was incorrect."
        mock_loc.nth.return_value = mock_item
        mock_page.locator.return_value = mock_loc

        has_err, err_type, err_txt = detect_sso_error(mock_page)
        self.assertTrue(has_err)
        self.assertEqual(err_type, "INVALID_CREDENTIALS")
        self.assertIn("incorrect", err_txt)

        # 2. Account locked
        mock_item.inner_text.return_value = "Your account is locked due to too many failed attempts."
        has_err2, err_type2, err_txt2 = detect_sso_error(mock_page)
        self.assertTrue(has_err2)
        self.assertEqual(err_type2, "ACCOUNT_LOCKED")

        # 3. Password expired
        mock_item.inner_text.return_value = "Your password has expired. Please reset your password."
        has_err3, err_type3, err_txt3 = detect_sso_error(mock_page)
        self.assertTrue(has_err3)
        self.assertEqual(err_type3, "PASSWORD_EXPIRED")


class TestWindowsCrossPlatformBehaviors(unittest.TestCase):
    @patch("sys.platform", "win32")
    def test_sms_listener_windows_safeguards(self):
        from core import sms_listener
        self.assertEqual(sms_listener.get_current_max_rowid(), 0)
        self.assertIsNone(sms_listener.get_latest_duo_sms_sqlite())
        self.assertIsNone(sms_listener.get_latest_duo_sms_imsg())
        self.assertIsNone(sms_listener.wait_for_duo_sms_passcode(timeout_seconds=1))

    @patch("sys.platform", "win32")
    @patch("builtins.input", side_effect=["testuser@umbc.edu", "y", "1"])
    @patch("core.session.getpass", return_value="mypassword123")
    def test_prompt_credentials_tui_windows_mode(self, mock_getpass, mock_input):
        from core.session import prompt_credentials_tui

        usr, pwd, save_creds, mode = prompt_credentials_tui(default_auto_exp=True)
        self.assertEqual(usr, "testuser@umbc.edu")
        self.assertEqual(pwd, "mypassword123")
        self.assertTrue(save_creds)
        self.assertEqual(mode, "auto")

class TestBrowserLaunchCandidates(unittest.TestCase):
    def test_candidates_order_and_structure(self):
        from core.session import get_browser_launch_candidates
        candidates = get_browser_launch_candidates()
        self.assertTrue(len(candidates) >= 5)

        # 1. Primary candidates should include Google Chrome, Microsoft Edge, and Bundled Chromium fallback
        names = [c["name"] for c in candidates]
        self.assertTrue(any("Google Chrome" in n for n in names))
        self.assertTrue(any("Microsoft Edge" in n for n in names))
        self.assertTrue(any("Playwright Bundled Chromium" in n for n in names))

        # 2. Last candidate must be the bundled Chromium fallback
        self.assertIsNone(candidates[-1]["channel"])
        self.assertEqual(candidates[-1]["name"], "Playwright Bundled Chromium")

    @patch("core.session.get_browser_launch_candidates")
    def test_launch_context_candidate_fallback(self, mock_candidates):
        from unittest.mock import MagicMock
        from core.session import _launch_context

        mock_candidates.return_value = [
            {"name": "Missing Chrome", "channel": "chrome"},
            {"name": "Working Edge", "channel": "msedge"},
        ]

        mock_pw = MagicMock()
        mock_ctx = MagicMock()
        mock_page = MagicMock()
        mock_ctx.pages = [mock_page]

        # First call fails (missing executable), second call succeeds
        mock_pw.chromium.launch_persistent_context.side_effect = [
            Exception("Executable doesn't exist"),
            mock_ctx,
        ]

        ctx, page = _launch_context(mock_pw, headless=True)
        self.assertEqual(ctx, mock_ctx)
        self.assertEqual(page, mock_page)
        self.assertEqual(mock_pw.chromium.launch_persistent_context.call_count, 2)


class TestPureHTTPSessionCheck(unittest.TestCase):
    @patch("core.session.quick_check_session_http", return_value=(True, {"studentId": "BH69617"}))
    def test_check_session_async_active(self, mock_http):
        from core.session import check_session_async
        res = asyncio.run(check_session_async(quiet=True, fast_only=True))
        self.assertTrue(res)

    @patch("core.session.quick_check_session_http", return_value=(False, None))
    @patch("core.async_engine.AsyncSessionManager")
    def test_check_session_async_expired_pure_http(self, mock_session_mgr, mock_http):
        from core.session import check_session_async
        res = asyncio.run(check_session_async(quiet=True, fast_only=True))
        self.assertFalse(res)
        # Verify no browser session manager was initialized
        mock_session_mgr.assert_not_called()

    @patch("core.session.quick_check_session_http", return_value=(True, {"studentId": "BH69617"}))
    def test_check_session_sync_active(self, mock_http):
        from core.session import check_session
        res = check_session(quiet=True, fast_only=True)
        self.assertTrue(res)

    @patch("core.session.quick_check_session_http", return_value=(False, None))
    def test_check_session_sync_expired_pure_http(self, mock_http):
        from core.session import check_session
        res = check_session(quiet=True, fast_only=True)
        self.assertFalse(res)


if __name__ == "__main__":
    unittest.main()
