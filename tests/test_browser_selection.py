"""Shared launcher: candidate order per OS, default browser, lock stop, cleanup."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from core import async_engine, browser, session


def names(candidates):
    return [c["name"] for c in candidates]


class BrowserTestCase(unittest.TestCase):
    def setUp(self):
        for target in (patch.dict(os.environ, {}, clear=True),
                       # Simulated OS discovery must not need the host's HOME/USERPROFILE.
                       patch("core.browser.Path.home", return_value=Path("/fixture-home")),
                       patch("core.browser.shutil.which", return_value=None),
                       patch("core.browser._winner", None),
                       patch("core.browser._announced", False),
                       patch("core.browser._windows_registered_path", return_value=None)):
            target.start()
            self.addCleanup(target.stop)


class TestCandidateOrder(BrowserTestCase):
    def installed(self, *present):
        """Match fixture paths despite the host Path's separator convention."""
        return patch("core.browser.Path.is_file", autospec=True,
                     side_effect=lambda p: any(fragment.replace("\\", "/") in str(p).replace("\\", "/")
                                               for fragment in present))

    def test_macos_chrome_first_then_edge_brave_then_bundled_last(self):
        with patch("core.browser.sys.platform", "darwin"), patch("core.browser.default_browser", return_value=(None, "Safari")), \
             self.installed("Brave Browser.app", "Google Chrome.app", "Microsoft Edge.app"):
            candidates = browser.get_browser_launch_candidates()
        self.assertEqual(list(dict.fromkeys(names(candidates)))[:3], ["Google Chrome", "Microsoft Edge", "Brave Browser"])
        self.assertEqual(names(candidates)[-2:], ["Playwright Chromium", "Playwright Bundled Chromium"])
        self.assertTrue(all(c.get("bundled") for c in candidates[-2:]))

    def test_windows_edge_first_and_finds_all_install_scopes(self):
        env = {"ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)",
               "LOCALAPPDATA": r"C:\Users\Student\AppData\Local"}
        with patch("core.browser.sys.platform", "win32"), patch.dict(os.environ, env), \
             patch("core.browser.default_browser", return_value=(None, "Firefox")), self.installed("Edge", "Chrome"):
            candidates = browser.get_browser_launch_candidates()
        self.assertEqual(candidates[0]["name"], "Microsoft Edge")
        edge = [c["executable_path"] for c in candidates if c["name"] == "Microsoft Edge"]
        self.assertEqual(edge, [
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Users\Student\AppData\Local\Microsoft\Edge\Application\msedge.exe",
        ])
        self.assertIn("Google Chrome", names(candidates))

    def test_windows_registry_path_is_a_candidate(self):
        custom = r"D:\Apps\chrome.exe"
        with patch("core.browser.sys.platform", "win32"), patch("core.browser.default_browser", return_value=(None, "x")), \
             patch("core.browser._windows_registered_path", side_effect=lambda exe: custom if exe == "chrome.exe" else None), \
             self.installed("chrome.exe"):
            paths = [c.get("executable_path") for c in browser.get_browser_launch_candidates()]
        self.assertIn(custom, paths)

    def test_missing_environment_does_not_create_literal_variable_paths(self):
        with patch("core.browser.sys.platform", "win32"):
            self.assertEqual(browser._browser_paths("msedge"), [])

    def test_default_chromium_browser_goes_first(self):
        with patch("core.browser.sys.platform", "win32"), patch.dict(os.environ, {"ProgramFiles": r"C:\PF"}), \
             patch("core.browser.default_browser", return_value=("chrome", "ChromeHTML")), self.installed("Chrome", "Edge"):
            self.assertEqual(names(browser.get_browser_launch_candidates())[:2], ["Google Chrome", "Microsoft Edge"])

    def test_uninstalled_default_is_skipped(self):
        with patch("core.browser.sys.platform", "darwin"), patch("core.browser.default_browser", return_value=("brave", "x")), \
             self.installed("Google Chrome.app"):
            self.assertEqual(names(browser.get_browser_launch_candidates())[0], "Google Chrome")

    def test_linux_path_chromium_precedes_bundled(self):
        with patch("core.browser.sys.platform", "linux"), patch("core.browser.default_browser", return_value=(None, "x")), \
             patch("core.browser.shutil.which", side_effect=lambda n: "/opt/bin/chromium" if n == "chromium" else None), \
             self.installed("/opt/bin/chromium"):
            candidates = browser.get_browser_launch_candidates()
        self.assertEqual(candidates[0]["executable_path"], "/opt/bin/chromium")
        self.assertTrue(candidates[-1]["bundled"])

    def test_pinned_browser_never_falls_back_to_another(self):
        with patch.dict(os.environ, {"BLACKBOARD_BROWSER": "msedge"}), patch("core.browser.Path.is_file", return_value=False):
            self.assertEqual(browser.get_browser_launch_candidates(), [])

    def test_pinned_chromium_is_bundled_only(self):
        with patch.dict(os.environ, {"BLACKBOARD_BROWSER": "chromium"}), patch("core.browser.Path.is_file", return_value=False):
            self.assertTrue(all(c["bundled"] for c in browser.get_browser_launch_candidates()))

    def test_custom_executable_takes_precedence_and_preserves_spaces(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "browser with spaces.exe"
            executable.touch()
            with patch.dict(os.environ, {"BLACKBOARD_BROWSER": "invalid", "BLACKBOARD_BROWSER_PATH": str(executable)}):
                self.assertEqual(browser.get_browser_launch_candidates(),
                                 [{"name": "Configured browser", "executable_path": str(executable)}])

    def test_config_json_override(self):
        with patch("core.config.load_config", return_value={"browser": "brave"}), patch("core.browser.Path.is_file", return_value=False):
            self.assertEqual(browser.get_browser_launch_candidates(), [])
        with patch("core.config.load_config", return_value={"browser": "safari"}):
            with self.assertRaisesRegex(ValueError, "BLACKBOARD_BROWSER must"):
                browser.get_browser_launch_candidates()

    def test_bad_override_reports_configuration_error(self):
        with patch.dict(os.environ, {"BLACKBOARD_BROWSER": "safari"}):
            with self.assertRaisesRegex(ValueError, "BLACKBOARD_BROWSER must"):
                browser.get_browser_launch_candidates()
        with patch.dict(os.environ, {"BLACKBOARD_BROWSER_PATH": "relative.exe"}):
            with self.assertRaisesRegex(ValueError, "absolute path"):
                browser.get_browser_launch_candidates()

    def test_default_browser_mapping(self):
        with patch("core.browser.sys.platform", "darwin"):
            self.assertEqual(browser._MAC_BUNDLES["com.google.chrome"], "chrome")
        self.assertEqual(browser._WINDOWS_PROGIDS["msedgehtm"], "msedge")
        self.assertNotIn("firefoxurl", browser._WINDOWS_PROGIDS)


class TestLauncher(BrowserTestCase):
    SPECS = [{"name": "A", "executable_path": "/a"}, {"name": "B", "executable_path": "/b"}]

    def test_missing_binary_falls_through_and_winner_is_cached_first(self):
        pw = MagicMock()
        pw.chromium.launch_persistent_context.side_effect = [RuntimeError("Executable doesn't exist"), "ctx"]
        with patch("core.browser.get_browser_launch_candidates", return_value=self.SPECS):
            context, spec = browser.launch_persistent_context(pw, "/p", headless=False)
            self.assertEqual((context, spec["name"]), ("ctx", "B"))
            pw.chromium.launch_persistent_context.side_effect = ["again"]
            browser.launch_persistent_context(pw, "/p")
        self.assertEqual(pw.chromium.launch_persistent_context.call_args.kwargs["executable_path"], "/b")

    def test_profile_lock_stops_without_trying_next_browser_sync(self):
        for message in ("ProcessSingleton", "SingletonLock", "profile is already in use", "Target directory is in use",
                        "Opening in existing browser session."):
            pw = MagicMock()
            pw.chromium.launch_persistent_context.side_effect = RuntimeError(message)
            with self.subTest(message), patch("core.browser.get_browser_launch_candidates", return_value=self.SPECS):
                with self.assertRaises(browser.BrowserProfileLockedError):
                    browser.launch_persistent_context(pw, "/p")
                self.assertEqual(pw.chromium.launch_persistent_context.call_count, 1)

    def test_sync_session_launcher_exits_with_guidance_on_lock(self):
        pw = MagicMock()
        pw.chromium.launch_persistent_context.side_effect = RuntimeError("ProcessSingleton")
        with patch("core.browser.get_browser_launch_candidates", return_value=self.SPECS):
            with self.assertRaises(SystemExit):
                session._launch_context(pw)
        self.assertEqual(pw.chromium.launch_persistent_context.call_count, 1)

    def test_nothing_launchable_reports_browser_guidance(self):
        pw = MagicMock()
        pw.chromium.launch_persistent_context.side_effect = RuntimeError("missing")
        with patch("core.browser.get_browser_launch_candidates", return_value=self.SPECS):
            with self.assertRaisesRegex(browser.BrowserUnavailableError, "No usable browser"):
                browser.launch_persistent_context(pw, "/p")

    def test_bundled_candidate_skipped_when_not_downloaded(self):
        pw = MagicMock()
        pw.chromium.executable_path = "/nowhere/chrome"
        pw.chromium.launch_persistent_context.return_value = "ctx"
        specs = [{"name": "Playwright Chromium", "channel": "chromium", "bundled": True}, self.SPECS[0]]
        with patch("core.browser.get_browser_launch_candidates", return_value=specs):
            browser.launch_persistent_context(pw, "/p")
        self.assertEqual(pw.chromium.launch_persistent_context.call_args.kwargs["executable_path"], "/a")

    def test_one_stderr_line_when_default_browser_not_usable(self):
        pw = MagicMock()
        pw.chromium.launch_persistent_context.return_value = "ctx"
        spec = [{"name": "Google Chrome", "executable_path": "/c"}]
        with patch("core.browser.get_browser_launch_candidates", return_value=spec), \
             patch("core.browser.default_browser", return_value=(None, "Firefox")), patch("builtins.print") as out:
            browser.launch_persistent_context(pw, "/p")
            browser.launch_persistent_context(pw, "/p")
        self.assertEqual(out.call_count, 1)
        self.assertIn("Firefox", out.call_args.args[0])


def async_playwright_double(launch_side_effect):
    pw = MagicMock()
    pw.stop = AsyncMock()
    pw.chromium.launch_persistent_context = AsyncMock(side_effect=launch_side_effect)
    starter = MagicMock()
    starter.start = AsyncMock(return_value=pw)
    return pw, starter


class TestAsyncLauncher(BrowserTestCase):
    SPECS = TestLauncher.SPECS

    def run_init(self, launch_side_effect, **config):
        async def check():
            pw, starter = async_playwright_double(launch_side_effect)
            with tempfile.TemporaryDirectory() as temp, patch("core.async_engine.SESSION_DIR", Path(temp)), \
                 patch("core.async_engine.async_playwright", return_value=starter), \
                 patch("core.browser.get_browser_launch_candidates", return_value=self.SPECS), \
                 patch("core.async_engine.get_browser_launch_candidates", return_value=self.SPECS):
                manager = async_engine.AsyncSessionManager(async_engine.EngineConfig(**config))
                try:
                    await manager.initialize()
                except Exception as error:
                    return pw, manager, error
                await manager.close()
                return pw, manager, None
        return asyncio.run(check())

    def test_profile_lock_stops_immediately_and_releases_driver(self):
        pw, manager, error = self.run_init(RuntimeError("ProcessSingleton"))
        self.assertIsInstance(error, browser.BrowserProfileLockedError)
        self.assertEqual(pw.chromium.launch_persistent_context.await_count, 1)
        pw.stop.assert_awaited_once()
        self.assertIsNone(manager._pw)
        self.assertFalse(manager._initialized)

    def test_no_browser_releases_driver(self):
        pw, manager, error = self.run_init(RuntimeError("missing"))
        self.assertIsInstance(error, browser.BrowserUnavailableError)
        self.assertEqual(pw.chromium.launch_persistent_context.await_count, 2)
        pw.stop.assert_awaited_once()

    def test_failure_after_launch_closes_context_and_driver(self):
        async def check():
            pw, starter = async_playwright_double(None)
            context = MagicMock()
            context.close = AsyncMock()
            context.route = AsyncMock(side_effect=RuntimeError("route failed"))
            pw.chromium.launch_persistent_context = AsyncMock(return_value=context)
            with tempfile.TemporaryDirectory() as temp, patch("core.async_engine.SESSION_DIR", Path(temp)), \
                 patch("core.async_engine.async_playwright", return_value=starter), \
                 patch("core.browser.get_browser_launch_candidates", return_value=self.SPECS), \
                 patch("core.async_engine.get_browser_launch_candidates", return_value=self.SPECS):
                manager = async_engine.AsyncSessionManager()
                with self.assertRaises(RuntimeError):
                    await manager.initialize()
            context.close.assert_awaited_once()
            pw.stop.assert_awaited_once()
        asyncio.run(check())

    def test_retry_after_failed_init_does_not_leak_first_driver(self):
        async def check():
            first, starter1 = async_playwright_double(RuntimeError("ProcessSingleton"))
            second, starter2 = async_playwright_double(["ctx"])
            second.chromium.launch_persistent_context = AsyncMock(return_value=MagicMock(route=AsyncMock(), close=AsyncMock()))
            with tempfile.TemporaryDirectory() as temp, patch("core.async_engine.SESSION_DIR", Path(temp)), \
                 patch("core.async_engine.async_playwright", side_effect=[starter1, starter2]), \
                 patch("core.browser.get_browser_launch_candidates", return_value=self.SPECS), \
                 patch("core.async_engine.get_browser_launch_candidates", return_value=self.SPECS):
                manager = async_engine.AsyncSessionManager()
                with self.assertRaises(browser.BrowserProfileLockedError):
                    await manager.initialize()
                await manager.initialize()
                await manager.close()
            first.stop.assert_awaited_once()
            second.stop.assert_awaited_once()
        asyncio.run(check())

    def test_bad_override_does_not_start_driver(self):
        async def check():
            with patch.dict(os.environ, {"BLACKBOARD_BROWSER": "invalid"}), \
                 patch("core.async_engine.async_playwright") as starter:
                manager = async_engine.AsyncSessionManager()
                with self.assertRaisesRegex(ValueError, "BLACKBOARD_BROWSER must"):
                    await manager.initialize()
                starter.assert_not_called()
        asyncio.run(check())

    def test_async_uses_same_selection_and_launch_options(self):
        async def check():
            pw, starter = async_playwright_double([RuntimeError("missing"), MagicMock(route=AsyncMock(), close=AsyncMock())])
            with tempfile.TemporaryDirectory() as temp, patch("core.async_engine.SESSION_DIR", Path(temp)), \
                 patch("core.async_engine.async_playwright", return_value=starter), \
                 patch("core.browser.get_browser_launch_candidates", return_value=self.SPECS), \
                 patch("core.async_engine.get_browser_launch_candidates", return_value=self.SPECS):
                manager = async_engine.AsyncSessionManager(async_engine.EngineConfig(headless=False))
                await manager.initialize()
                launch = pw.chromium.launch_persistent_context.call_args.kwargs
                self.assertEqual(launch["executable_path"], "/b")
                self.assertFalse(launch["headless"])
                self.assertEqual(launch["user_data_dir"], temp)
                await manager.close()
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
