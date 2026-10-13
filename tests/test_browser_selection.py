"""Browser selection across OS layouts and shared sync/async launch behavior."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from core import async_engine, browser, session


class TestBrowserSelection(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.which = patch("core.browser.shutil.which", return_value=None)
        self.which.start()
        self.addCleanup(self.which.stop)

    def test_windows_prefers_edge_and_finds_all_install_scopes(self):
        with patch("core.browser.sys.platform", "win32"), patch.dict(os.environ, {
            "ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)",
            "LOCALAPPDATA": r"C:\Users\Student\AppData\Local",
        }), patch("core.browser.Path.is_file", return_value=True):
            candidates = browser.get_browser_launch_candidates()
            self.assertEqual(candidates[0]["channel"], "msedge")
            edge_paths = [c["executable_path"] for c in candidates
                          if c["name"] == "Microsoft Edge" and "executable_path" in c]
            self.assertEqual(edge_paths, [
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Users\Student\AppData\Local\Microsoft\Edge\Application\msedge.exe",
            ])

    def test_windows_missing_environment_does_not_create_literal_variable_paths(self):
        with patch("core.browser.sys.platform", "win32"):
            self.assertEqual(browser._browser_paths("msedge"), [])

    def test_macos_user_applications_are_detected(self):
        home = Path(tempfile.gettempdir()) / "Student Home"
        expected = str(home / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        with patch("core.browser.sys.platform", "darwin"), patch("core.browser.Path.home", return_value=home), \
             patch("core.browser.Path.is_file", autospec=True, side_effect=lambda p: str(p) == expected):
            candidates = browser.get_browser_launch_candidates()
            self.assertEqual(candidates[0]["channel"], "chrome")
            self.assertIn({"name": "Google Chrome", "executable_path": expected}, candidates)
            self.assertFalse(any("Microsoft Edge" in c.get("executable_path", "") for c in candidates))

    def test_linux_path_browser_precedes_bundled_chromium(self):
        with patch("core.browser.sys.platform", "linux"), \
             patch("core.browser.shutil.which", side_effect=lambda name: "/opt/bin/chromium" if name == "chromium" else None):
            candidates = browser.get_browser_launch_candidates()
            idx = next(i for i, c in enumerate(candidates) if c.get("executable_path") == "/opt/bin/chromium")
            bundled = next(i for i, c in enumerate(candidates) if c.get("channel") == "chromium")
            self.assertLess(idx, bundled)

    def test_pinned_channel_does_not_fall_back_to_other_browsers(self):
        with patch.dict(os.environ, {"BLACKBOARD_BROWSER": "msedge"}), patch("core.browser.Path.is_file", return_value=False):
            self.assertEqual(browser.get_browser_launch_candidates(), [{"name": "Microsoft Edge", "channel": "msedge"}])

    def test_full_chromium_before_legacy_headless_shell(self):
        with patch.dict(os.environ, {"BLACKBOARD_BROWSER": "chromium"}):
            self.assertEqual([c["channel"] for c in browser.get_browser_launch_candidates()], ["chromium", None])

    def test_custom_executable_takes_precedence_and_preserves_spaces(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "browser with spaces.exe"
            executable.touch()
            with patch.dict(os.environ, {"BLACKBOARD_BROWSER": "invalid", "BLACKBOARD_BROWSER_PATH": str(executable)}):
                self.assertEqual(browser.get_browser_launch_candidates(), [
                    {"name": "Configured browser", "executable_path": str(executable)},
                ])

    def test_bad_override_reports_configuration_error(self):
        with patch.dict(os.environ, {"BLACKBOARD_BROWSER": "safari"}):
            with self.assertRaisesRegex(ValueError, "BLACKBOARD_BROWSER must"):
                browser.get_browser_launch_candidates()
        with patch.dict(os.environ, {"BLACKBOARD_BROWSER_PATH": "relative.exe"}):
            with self.assertRaisesRegex(ValueError, "absolute path"):
                browser.get_browser_launch_candidates()

    def test_sync_missing_browsers_reports_optional_install(self):
        pw = MagicMock()
        pw.chromium.launch_persistent_context.side_effect = RuntimeError("missing executable")
        with patch("core.session.get_browser_launch_candidates", return_value=[{"name": "Chrome", "channel": "chrome"}]):
            with self.assertRaisesRegex(RuntimeError, "install chromium --no-shell"):
                session._launch_context(pw)

    def test_async_uses_same_selection_and_preserves_launch_options(self):
        async def check():
            pw = MagicMock()
            context = MagicMock()
            context.route = AsyncMock()
            context.close = AsyncMock()
            pw.stop = AsyncMock()
            pw.chromium.launch_persistent_context = AsyncMock(side_effect=[RuntimeError("missing"), context])
            starter = MagicMock()
            starter.start = AsyncMock(return_value=pw)
            candidates = [{"name": "Chrome", "channel": "chrome"}, {"name": "Edge", "channel": "msedge"}]
            with tempfile.TemporaryDirectory() as temp, patch("core.async_engine.SESSION_DIR", Path(temp)), \
                 patch("core.async_engine.async_playwright", return_value=starter), \
                 patch("core.async_engine.get_browser_launch_candidates", return_value=candidates):
                manager = async_engine.AsyncSessionManager(async_engine.EngineConfig(headless=False))
                try:
                    await manager.initialize()
                    launch = pw.chromium.launch_persistent_context.call_args.kwargs
                    self.assertEqual(launch["channel"], "msedge")
                    self.assertFalse(launch["headless"])
                    self.assertEqual(launch["user_data_dir"], temp)
                finally:
                    await manager.close()
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
