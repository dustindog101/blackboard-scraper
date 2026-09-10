"""
Unit tests for Blackboard Scraper Natural CLI Subcommands and Legacy Flag Interceptor.
Tests both canonical modern subcommands (bb login, bb due, bb outline)
and transparent redirection for legacy root flags (bb --due, bb --auto-exp).
"""

import unittest
from main import _intercept_legacy_args, _parse_args
from scrapers.quiz import format_assessment_attempt_cli


class TestLegacyInterceptor(unittest.TestCase):
    def test_legacy_due(self):
        res, hint = _intercept_legacy_args(["--due", "14d"])
        self.assertEqual(res, ["due", "14d"])
        self.assertEqual(hint, "due 14d")

    def test_legacy_due_default_window(self):
        res, hint = _intercept_legacy_args(["--due", "--json"])
        self.assertEqual(res, ["due", "7d", "--json"])
        self.assertEqual(hint, "due 7d")

    def test_legacy_upcoming(self):
        res, hint = _intercept_legacy_args(["--upcoming", "10"])
        self.assertEqual(res, ["due", "10d"])
        self.assertEqual(hint, "due 10d")

    def test_legacy_auto_exp(self):
        res, hint = _intercept_legacy_args(["--auto-exp"])
        self.assertEqual(res, ["login"])
        self.assertEqual(hint, "login")

    def test_legacy_auto_exp_force(self):
        res, hint = _intercept_legacy_args(["--auto-exp", "--force"])
        self.assertEqual(res, ["login", "--force"])
        self.assertEqual(hint, "login --force")

    def test_legacy_login_auto(self):
        res, hint = _intercept_legacy_args(["--login", "--auto"])
        self.assertEqual(res, ["login", "auto"])
        self.assertEqual(hint, "login auto")

    def test_legacy_login_visible(self):
        res, hint = _intercept_legacy_args(["--login", "--visible"])
        self.assertEqual(res, ["login", "--manual"])
        self.assertEqual(hint, "login --manual")

    def test_legacy_check_session(self):
        res, hint = _intercept_legacy_args(["--check-session"])
        self.assertEqual(res, ["session", "check"])
        self.assertEqual(hint, "session check")

    def test_legacy_bot_status(self):
        res, hint = _intercept_legacy_args(["--bot-status"])
        self.assertEqual(res, ["bot", "status"])
        self.assertEqual(hint, "bot status")

    def test_legacy_bot_daemon(self):
        res, hint = _intercept_legacy_args(["--bot", "-d"])
        self.assertEqual(res, ["bot", "start"])
        self.assertEqual(hint, "bot start")

    def test_legacy_search_and_download(self):
        res_s, hint_s = _intercept_legacy_args(["--search", "Syllabus"])
        self.assertEqual(res_s, ["search", "Syllabus"])
        self.assertEqual(hint_s, "search Syllabus")

        res_d, hint_d = _intercept_legacy_args(["--download", "file.pdf"])
        self.assertEqual(res_d, ["download", "file.pdf"])
        self.assertEqual(hint_d, "download file.pdf")

    def test_modern_args_untouched(self):
        modern_cases = [
            ["login"],
            ["login", "auto"],
            ["login", "--manual"],
            ["due"],
            ["due", "14d"],
            ["outline", "IS410", "-f", "Homework"],
            ["bot", "start"],
            ["session", "check"],
            ["check"],
            ["whoami"],
        ]
        for c in modern_cases:
            res, hint = _intercept_legacy_args(c)
            self.assertEqual(res, c)
            self.assertIsNone(hint)


class TestSubcommandParsing(unittest.TestCase):
    def test_login_subcommand_defaults(self):
        args = _parse_args(["login"])
        self.assertEqual(args.subcommand, "login")
        self.assertEqual(args.mode, "auto")
        self.assertFalse(args.force)
        self.assertFalse(args.manual)

    def test_login_subcommand_manual(self):
        args = _parse_args(["login", "manual"])
        self.assertEqual(args.subcommand, "login")
        self.assertEqual(args.mode, "manual")

        args_flag = _parse_args(["login", "--manual"])
        self.assertEqual(args_flag.subcommand, "login")
        self.assertTrue(args_flag.manual)

    def test_login_subcommand_force(self):
        args = _parse_args(["login", "--force"])
        self.assertEqual(args.subcommand, "login")
        self.assertTrue(args.force)

    def test_due_subcommand(self):
        args_default = _parse_args(["due"])
        self.assertEqual(args_default.subcommand, "due")
        self.assertEqual(args_default.window, "7d")

        args_custom = _parse_args(["due", "14d", "--json"])
        self.assertEqual(args_custom.subcommand, "due")
        self.assertEqual(args_custom.window, "14d")
        self.assertTrue(args_custom.json)

    def test_outline_positional_course(self):
        args = _parse_args(["outline", "IS410", "-f", "Homework", "--expand-all"])
        self.assertEqual(args.subcommand, "outline")
        self.assertEqual(args.course_pos, "IS410")
        self.assertEqual(args.folder, "Homework")
        self.assertTrue(args.expand_all)

    def test_grades_and_announcements(self):
        args_g = _parse_args(["grades", "MATH215"])
        self.assertEqual(args_g.subcommand, "grades")
        self.assertEqual(args_g.course_pos, "MATH215")

        args_a = _parse_args(["announcements", "--all"])
        self.assertEqual(args_a.subcommand, "announcements")
        self.assertTrue(args_a.all)

    def test_search_and_download(self):
        args_s = _parse_args(["search", "Python"])
        self.assertEqual(args_s.subcommand, "search")
        self.assertEqual(args_s.query, "Python")

        args_d = _parse_args(["download", "hw1.pdf", "--out-dir", "/tmp/dl"])
        self.assertEqual(args_d.subcommand, "download")
        self.assertEqual(args_d.item, "hw1.pdf")
        self.assertEqual(args_d.out_dir, "/tmp/dl")

    def test_bot_daemon_subcommands(self):
        for act in ["start", "stop", "restart", "status", "run"]:
            args = _parse_args(["bot", act])
            self.assertEqual(args.subcommand, "bot")
            self.assertEqual(args.action, act)

    def test_session_subcommands_and_check_shortcut(self):
        args_s = _parse_args(["session", "stats"])
        self.assertEqual(args_s.subcommand, "session")
        self.assertEqual(args_s.action, "stats")

        args_c = _parse_args(["check", "--debug"])
        self.assertEqual(args_c.subcommand, "check")
        self.assertTrue(args_c.debug)

    def test_subcommand_aliases(self):
        self.assertEqual(_parse_args(["brief"]).subcommand, "brief")
        self.assertEqual(_parse_args(["cal"]).subcommand, "cal")
        self.assertEqual(_parse_args(["whoami"]).subcommand, "whoami")
        self.assertEqual(_parse_args(["find", "Exam"]).query, "Exam")
        self.assertEqual(_parse_args(["grab", "quiz.pdf"]).item, "quiz.pdf")
        self.assertEqual(_parse_args(["app"]).subcommand, "app")


class TestLegacyEndToEndParsing(unittest.TestCase):
    def test_legacy_due_redirection(self):
        args = _parse_args(["--due", "7d", "--json"])
        self.assertEqual(args.subcommand, "due")
        self.assertEqual(args.window, "7d")
        self.assertTrue(args.json)

    def test_legacy_auto_exp_redirection(self):
        args = _parse_args(["--auto-exp", "--force"])
        self.assertEqual(args.subcommand, "login")
        self.assertTrue(args.force)

    def test_legacy_bot_status_redirection(self):
        args = _parse_args(["--bot-status"])
        self.assertEqual(args.subcommand, "bot")
        self.assertEqual(args.action, "status")

    def test_legacy_check_session_redirection(self):
        args = _parse_args(["--check-session"])
        self.assertEqual(args.subcommand, "session")
        self.assertEqual(args.action, "check")


class TestAssignmentInterceptorAndSubcommand(unittest.TestCase):
    """Regression coverage for the single-assessment inspector (ADR-0003):
    every historical spelling must survive the subcommand migration."""

    def test_legacy_assignment_flag(self):
        res, hint = _intercept_legacy_args(["--assignment", "_123_1"])
        self.assertEqual(res, ["assignment", "_123_1"])
        self.assertEqual(hint, "assignment _123_1")

    def test_legacy_quiz_asmt_assessment_flags(self):
        for flag in ("--quiz", "--asmt", "--assessment"):
            res, hint = _intercept_legacy_args([flag, "Midterm"])
            self.assertEqual(res, ["assignment", "Midterm"], msg=flag)
            self.assertEqual(hint, "assignment Midterm", msg=flag)

    def test_legacy_assignment_with_attempt_flags(self):
        res, hint = _intercept_legacy_args(
            ["--assignment", "_1_1", "--start-attempt", "--force-start", "-c", "IS410"]
        )
        self.assertEqual(
            res, ["assignment", "_1_1", "--start-attempt", "--force-start", "-c", "IS410"]
        )
        self.assertEqual(hint, "assignment _1_1")

    def test_legacy_begin_attempt_normalized(self):
        res, _ = _intercept_legacy_args(["--assignment", "_1_1", "--begin-attempt"])
        self.assertEqual(res, ["assignment", "_1_1", "--start-attempt"])

    def test_assignment_subcommand_parsing(self):
        args = _parse_args(["assignment", "Homework 1", "-c", "IS410"])
        self.assertEqual(args.subcommand, "assignment")
        self.assertEqual(args.target, "Homework 1")
        self.assertEqual(args.course, "IS410")
        self.assertFalse(args.start_attempt)
        self.assertFalse(args.force_start)

    def test_assignment_positional_course(self):
        # Single-assessment keeps the historical `-c` course flag (no bare
        # positional course) — matching the legacy `--assignment X -c C` form.
        args = _parse_args(["assignment", "Homework 1", "-c", "IS410"])
        self.assertEqual(args.target, "Homework 1")
        self.assertEqual(args.course, "IS410")

    def test_assignment_attempt_flags(self):
        args = _parse_args(["assignment", "_1_1", "--start-attempt", "--force-start"])
        self.assertTrue(args.start_attempt)
        self.assertTrue(args.force_start)

    def test_assignment_aliases(self):
        for alias in ("quiz", "asmt", "assessment"):
            args = _parse_args([alias, "_1_1"])
            self.assertEqual(args.target, "_1_1", msg=alias)

    def test_assignment_missing_target_parses(self):
        args = _parse_args(["assignment"])
        self.assertEqual(args.subcommand, "assignment")
        self.assertIsNone(args.target)

    def test_modern_assignment_untouched(self):
        res, hint = _intercept_legacy_args(["assignment", "Homework 1", "-c", "IS410"])
        self.assertEqual(res, ["assignment", "Homework 1", "-c", "IS410"])
        self.assertIsNone(hint)

    def test_legacy_assignment_end_to_end(self):
        args = _parse_args(["--assignment", "_1_1", "--start-attempt", "-c", "IS410"])
        self.assertEqual(args.subcommand, "assignment")
        self.assertEqual(args.target, "_1_1")
        self.assertTrue(args.start_attempt)
        self.assertEqual(args.course, "IS410")


class TestGuideAndCompatDetails(unittest.TestCase):
    def test_guide_no_topic_parses(self):
        for cmd in ("guide", "help"):
            args = _parse_args([cmd])
            self.assertIsNone(args.topic)

    def test_typing_names_importable(self):
        # Guards the v1 `Tuple` NameError on Python 3.10-3.13 (ruff F821).
        import main as main_module

        self.assertTrue(hasattr(main_module, "Tuple"))

    def test_discover_tolerates_output_flags(self):
        args = _parse_args(["discover", "--term", "FA2026"])
        self.assertEqual(args.subcommand, "discover")

    def test_due_upcoming_legacy(self):
        res, hint = _intercept_legacy_args(["--upcoming", "10"])
        self.assertEqual(res, ["due", "10d"])
        self.assertEqual(hint, "due 10d")

    def test_legacy_guide_no_topic_interceptor_and_parsing(self):
        res, hint = _intercept_legacy_args(["--guide"])
        self.assertEqual(res, ["guide"])
        self.assertEqual(hint, "guide")
        args = _parse_args(["--guide"])
        self.assertEqual(args.subcommand, "guide")
        self.assertIsNone(args.topic)

        res_topic, hint_topic = _intercept_legacy_args(["--guide", "auth"])
        self.assertEqual(res_topic, ["guide", "auth"])
        self.assertEqual(hint_topic, "guide auth")
        args_topic = _parse_args(["--guide", "auth"])
        self.assertEqual(args_topic.subcommand, "guide")
        self.assertEqual(args_topic.topic, "auth")

    def test_search_and_download_with_course_flag(self):
        args_s1 = _parse_args(["search", "test", "-c", "IS410"])
        self.assertEqual(args_s1.query, "test")
        self.assertEqual(args_s1.course, "IS410")

        args_s2 = _parse_args(["search", "test", "--course", "IS410", "--all"])
        self.assertEqual(args_s2.query, "test")
        self.assertEqual(args_s2.course, "IS410")
        self.assertTrue(args_s2.all)

        args_d1 = _parse_args(["download", "file.pdf", "-c", "IS410"])
        self.assertEqual(args_d1.item, "file.pdf")
        self.assertEqual(args_d1.course, "IS410")

        args_d2 = _parse_args(["download", "file.pdf", "--course", "IS410", "--all"])
        self.assertEqual(args_d2.item, "file.pdf")
        self.assertEqual(args_d2.course, "IS410")
        self.assertTrue(args_d2.all)

    def test_leading_boolean_flags_reordering(self):
        res1, _ = _intercept_legacy_args(["--json", "briefing"])
        self.assertEqual(res1[0], "briefing")
        self.assertIn("--json", res1)

        res2, _ = _intercept_legacy_args(["-v", "outline", "IS410"])
        self.assertEqual(res2[0], "outline")
        self.assertIn("-v", res2)
        self.assertIn("IS410", res2)

        res3, _ = _intercept_legacy_args(["--raw", "--json", "grades"])
        self.assertEqual(res3[0], "grades")
        self.assertIn("--raw", res3)
        self.assertIn("--json", res3)

    def test_standalone_begin_attempt_interceptor(self):
        res, hint = _intercept_legacy_args(["--begin-attempt", "_123_1"])
        self.assertEqual(res, ["assignment", "_123_1", "--start-attempt"])
        self.assertEqual(hint, "assignment _123_1 --start-attempt")

        res_bare, hint_bare = _intercept_legacy_args(["--begin-attempt"])
        self.assertEqual(res_bare, ["assignment", "--start-attempt"])
        self.assertEqual(hint_bare, "assignment --start-attempt")

    def test_login_passcode_flag(self):
        args1 = _parse_args(["login", "--passcode", "123456"])
        self.assertEqual(args1.duo_passcode, "123456")

        args2 = _parse_args(["login", "--duo-passcode", "654321"])
        self.assertEqual(args2.duo_passcode, "654321")

    def test_assignment_help_interception(self):
        # 'bb assignment help' -> 'bb assignment --help'
        res, hint = _intercept_legacy_args(["assignment", "help"])
        self.assertEqual(res, ["assignment", "--help"])
        self.assertIsNone(hint)

        # 'bb assignment -h' -> 'bb assignment --help'
        res, hint = _intercept_legacy_args(["assignment", "-h"])
        self.assertEqual(res, ["assignment", "--help"])
        self.assertIsNone(hint)

        # 'bb quiz help' -> 'bb quiz --help'
        res, hint = _intercept_legacy_args(["quiz", "help"])
        self.assertEqual(res, ["quiz", "--help"])
        self.assertIsNone(hint)

        # 'bb help assignment' -> 'bb assignment --help'
        res, hint = _intercept_legacy_args(["help", "assignment"])
        self.assertEqual(res, ["assignment", "--help"])
        self.assertIsNone(hint)

        # 'bb help due' -> 'bb due --help'
        res, hint = _intercept_legacy_args(["help", "due"])
        self.assertEqual(res, ["due", "--help"])
        self.assertIsNone(hint)

    def test_search_help_not_intercepted(self):
        # Free-text search for the query 'help' must NOT be converted to --help
        res, hint = _intercept_legacy_args(["search", "help"])
        self.assertEqual(res, ["search", "help"])
        self.assertIsNone(hint)

        res, hint = _intercept_legacy_args(["find", "help"])
        self.assertEqual(res, ["find", "help"])
        self.assertIsNone(hint)

    def test_guide_topics_not_redirected_to_subcommand_help(self):
        # Guide topics ('auth', 'courses', etc.) must remain guide topics
        res, hint = _intercept_legacy_args(["help", "auth"])
        self.assertEqual(res, ["help", "auth"])
        self.assertIsNone(hint)

        res, hint = _intercept_legacy_args(["help", "courses"])
        self.assertEqual(res, ["help", "courses"])
        self.assertIsNone(hint)

    def test_assignment_not_found_cli_formatting(self):
        data = {
            "status": "NOT_FOUND",
            "error": "No assignment or quiz matching 'Nonexistent Homework' was found in course 'IS410'.",
            "target": "Nonexistent Homework",
            "course_id": "IS410",
        }
        formatted = format_assessment_attempt_cli(data)
        self.assertIn("❌ No assignment or quiz matching 'Nonexistent Homework' was found in course 'IS410'.", formatted)
        self.assertIn("💡 Tip: Run 'bb assignments -c IS410' or 'bb due'", formatted)


if __name__ == "__main__":
    unittest.main()
