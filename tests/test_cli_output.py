"""Output contract for the REST-first read commands: stdout is one JSON document, progress goes to
stderr, announcements are newest first, folders are not downloaded, and nothing needs a live session."""
import asyncio
import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main
from core import rest
from core.display import ago, clip, fmt_when, parse_dt
from core.gradebook import course_label
from scrapers import activity, announcements
from scrapers.search import choose_download_target, files_within, grab_item_async

COURSES = {"_1_1": "IS 410 Intro To Database Design (03.1520) FA2026", "_2_1": "ENGL 100 Composition (12.4799) FA2026"}
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def run_cli(argv, extra_patches=()):
    """Run a bb command in-process; returns (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("main.load_courses", return_value=COURSES))
        stack.enter_context(patch("main._require_session_async", new=AsyncMock(return_value=True)))
        for p in extra_patches:
            stack.enter_context(p)
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        try:
            asyncio.run(main.main_async(main._parse_args(argv)))
        except SystemExit as exc:
            code = exc.code or 0
    return code, out.getvalue(), err.getvalue()


class TestRest(unittest.TestCase):
    def _raises(self, code):
        err = urllib.error.HTTPError("u", code, "x", {}, None)
        with patch("core.rest.cookie_header", return_value="a=b"), patch("core.rest._open", side_effect=err):
            with self.assertRaises(rest.RestError) as cm:
                rest.get_json("/x")
        return cm.exception.kind

    def test_errors_are_distinct(self):
        self.assertEqual(self._raises(401), "auth")
        self.assertEqual(self._raises(403), "forbidden")
        self.assertEqual(self._raises(404), "not_found")
        self.assertEqual(self._raises(503), "unknown")

    def test_network_error_is_unknown_not_auth(self):
        with patch("core.rest.cookie_header", return_value="a=b"), patch("core.rest._open", side_effect=TimeoutError()):
            with self.assertRaises(rest.RestError) as cm:
                rest.get_json("/x")
        self.assertEqual(cm.exception.kind, "unknown")

    def test_missing_session_is_auth(self):
        with patch("core.rest.cookie_header", return_value=None):
            with self.assertRaises(rest.RestError) as cm:
                rest.get_json("/x")
        self.assertEqual(cm.exception.kind, "auth")


class TestDisplay(unittest.TestCase):
    def test_short_course(self):
        self.assertEqual(course_label(COURSES["_1_1"]), "IS 410")
        self.assertEqual(course_label("ECON122 Accounting"), "ECON 122")

    def test_clip_never_cuts_mid_word(self):
        self.assertEqual(clip("alpha beta gamma delta", 14), "alpha beta…")
        self.assertEqual(clip("short", 14), "short")

    def test_ago_and_local_time_with_zone(self):
        self.assertEqual(ago(NOW - timedelta(days=2), NOW), "2d ago")
        self.assertEqual(ago(NOW - timedelta(minutes=5), NOW), "5m ago")
        self.assertRegex(fmt_when(parse_dt("2026-10-21T03:59:00Z"), NOW), r"^\w{3} \w{3} \d+ \d+:\d{2} [AP]M [A-Z]{2,5}$")

    def test_parses_assignment_utc_format(self):
        self.assertEqual(parse_dt("2026-09-08 20:30 UTC"), datetime(2026, 9, 8, 20, 30, tzinfo=timezone.utc))


def ann(title, created, unread=None):
    return {"id": title, "title": title, "created": created, "modified": None, "meta": "", "body": "Body of " + title, "unread": unread}


class TestAnnouncements(unittest.TestCase):
    ITEMS = [
        ann("old", "2026-03-01T12:00:00Z"),
        ann("new", "2026-10-19T12:00:00Z"),
        ann("mid", "2026-09-01T12:00:00Z"),
        ann("older", "2025-09-01T12:00:00Z"),
    ]

    def test_newest_first(self):
        self.assertEqual([a["title"] for a in announcements.filter_announcements(self.ITEMS)], ["new", "mid", "old", "older"])

    def test_since_relative_and_date(self):
        since = announcements.parse_since("60d", NOW)
        self.assertEqual([a["title"] for a in announcements.filter_announcements(self.ITEMS, since=since)], ["new", "mid"])
        self.assertEqual(announcements.parse_since("2w", NOW), NOW - timedelta(days=14))
        self.assertEqual(announcements.parse_since("2026-09-01").year, 2026)
        with self.assertRaises(ValueError):
            announcements.parse_since("yesterday")

    def test_limit_applies_after_sort(self):
        self.assertEqual([a["title"] for a in announcements.filter_announcements(self.ITEMS, limit=2)], ["new", "mid"])

    def test_unread_means_verified_unread_only(self):
        rows = [ann("a", "2026-10-19T12:00:00Z", True), ann("b", "2026-10-18T12:00:00Z", False), ann("c", "2026-10-17T12:00:00Z", None)]
        self.assertEqual([a["title"] for a in announcements.filter_announcements(rows, unread=True)], ["a"])

    def test_rest_rows_never_guess_unread_from_age(self):
        rows = announcements.normalize_announcements([{"title": "Fresh", "created": "2026-10-19T12:00:00Z", "body": "<p>Hi</p><p>there</p>"}])
        self.assertIsNone(rows[0]["unread"])
        self.assertEqual(rows[0]["body"], "Hi\nthere")

    def test_older_term_marker(self):
        rows = [dict(r) for r in self.ITEMS]
        announcements.mark_older_term(rows, datetime(2026, 8, 3, tzinfo=timezone.utc))
        self.assertEqual({r["title"]: r["older_term"] for r in rows}, {"old": True, "new": False, "mid": False, "older": True})
        announcements.mark_older_term(rows, None)
        self.assertTrue(all(r["older_term"] is None for r in rows))

    def test_reconcile_joins_unique_titles_only(self):
        rest_rows = [ann("a", "2026-10-19T12:00:00Z"), ann("dup", "2026-10-18T12:00:00Z"), ann("dup", "2026-10-17T12:00:00Z")]
        browser = [{"title": "a", "unread": True}, {"title": "dup", "unread": False}]
        unknown = announcements.reconcile_read_state(rest_rows, browser)
        self.assertEqual([r["unread"] for r in rest_rows], [True, None, None])
        self.assertEqual(unknown, 2)

    def test_text_is_grouped_sorted_and_clipped(self):
        long_body = "word " * 100
        groups = [{"course_name": COURSES["_2_1"], "total": 1, "announcements": [dict(ann("T", "2026-10-19T12:00:00Z"), body=long_body, older_term=True)]},
                  {"course_name": COURSES["_1_1"], "total": 3, "announcements": [ann("U", "2026-10-19T12:00:00Z", True)]}]
        text = announcements.format_announcements(groups, NOW)
        self.assertLess(text.index("ENGL 100"), text.index("IS 410") + 1000)
        self.assertIn("(1 of 3 announcements)", text)
        self.assertIn("[unread]", text)
        self.assertIn("(older term)", text)
        self.assertTrue(all(len(line) < 200 for line in text.splitlines()))
        self.assertIn("…", text)


class TestActivity(unittest.TestCase):
    def entry(self, **kw):
        base = {"se_id": "x1", "providerId": "bb_mygrades", "se_courseId": "_1_1", "se_timestamp": 1792000000000,
                "itemSpecificData": {"title": "HW 1", "gradeDetails": {"displayGradeScore": 9, "pointsPossible": 10}}}
        base.update(kw)
        return base

    def test_grade_entry(self):
        item = activity.normalize_entry(self.entry(), COURSES, {})
        self.assertEqual((item["category"], item["grade"], item["course"]), ("grade", "9/10 (90%)", COURSES["_1_1"]))
        self.assertTrue(item["at"].startswith("2026-"))

    def test_tidy_merges_duplicates_and_collapses_discussion_threads(self):
        def d(i, ts):
            return activity.normalize_entry({"se_id": f"d{i}", "providerId": "bb_disc", "se_courseId": "_2_1", "se_timestamp": ts,
                                             "itemSpecificData": {"discussionboardDetails": {"forumName": "J4"}, "contentExtract": f"post {i}"}}, COURSES, {}, full=True)
        graded = activity.normalize_entry(self.entry(), COURSES, {})
        ungraded = activity.normalize_entry(self.entry(se_id="x2", itemSpecificData={"title": "HW 1"}), COURSES, {})
        items = activity.tidy([d(1, 1792000000000), d(2, 1792000100000), d(3, 1792000200000), graded, ungraded, dict(graded, id="x3")])
        self.assertEqual(len(items), 2)
        thread = next(i for i in items if i["category"] == "discussion")
        self.assertEqual(thread["count"], 3)
        self.assertEqual(thread["message"], "post 3")

    def test_classmate_names_and_text_hidden_unless_full(self):
        raw = {"se_id": "d", "providerId": "bb_disc", "se_courseId": "_2_1", "se_timestamp": 1792000000000, "se_userId": "u1",
               "itemSpecificData": {"discussionboardDetails": {"forumName": "J4"}, "contentExtract": "secret words"}}
        users = {"u1": {"sx_name": "Some Classmate"}}
        hidden = activity.normalize_entry(raw, COURSES, users)
        self.assertNotIn("author", hidden)
        self.assertNotIn("message", hidden)
        shown = activity.normalize_entry(raw, COURSES, users, full=True)
        self.assertEqual(shown["author"], "Some Classmate")
        self.assertIn("secret words", shown["message"])

    def test_collapsed_thread_shows_count_and_latest_time(self):
        item = {"category": "discussion", "course": COURSES["_2_1"], "title": "J4", "at": "2026-10-19T22:44:00+00:00", "count": 10}
        self.assertRegex(activity.format_activity([item], 1), r"\[10 responses, latest \d+:\d{2} [AP]M")

    def test_text_groups_by_course_without_truncating_mid_line(self):
        items = [activity.normalize_entry(self.entry(), COURSES, {})]
        text = activity.format_activity(items, total=5)
        self.assertIn("IS 410  (1)", text)
        self.assertIn("Showing the newest 1", text)

    def test_rest_pagination_dedupes_and_stops(self):
        pages = [{"sv_streamEntries": [self.entry()], "sv_moreData": True, "sv_providers": [{"sp_provider": "bb_disc"}]},
                 {"sv_streamEntries": [self.entry()], "sv_moreData": True, "sv_providers": [{"sp_provider": "bb_disc"}]}]
        with patch("scrapers.activity.post_json", side_effect=pages) as post, patch("scrapers.activity.load_courses", return_value=COURSES):
            items = activity.fetch_activity_rest()
        self.assertEqual(len(items), 1)
        self.assertEqual(post.call_count, 2)  # second page added nothing new


class TestDownload(unittest.TestCase):
    OUTLINE = [
        {"content_id": "f1", "parent_id": None, "title": "Slides", "content_type": "folder", "has_children": True, "parent_path": []},
        {"content_id": "a", "parent_id": "f1", "title": "Week1.pdf", "content_type": "file", "is_downloadable": True, "parent_path": ["Slides"]},
        {"content_id": "sub", "parent_id": "f1", "title": "More", "content_type": "folder", "has_children": True, "parent_path": ["Slides"]},
        {"content_id": "b", "parent_id": "sub", "title": "Week2.pdf", "content_type": "file", "is_downloadable": True, "parent_path": ["Slides", "More"]},
    ]

    def test_files_within_is_recursive(self):
        self.assertEqual([f["title"] for f in files_within(self.OUTLINE, "f1")], ["Week1.pdf", "Week2.pdf"])

    def test_prefers_files_over_folders(self):
        matches = [dict(self.OUTLINE[0]), dict(self.OUTLINE[1])]
        self.assertEqual(choose_download_target(matches, "slides")["item"]["content_id"], "a")

    def test_folder_name_lists_files_and_creates_nothing(self):
        async def outline(*_a, **_k):
            return self.OUTLINE
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "downloads"
            with patch("scrapers.search.scrape_course_outline_async", side_effect=outline), \
                    patch("scrapers.search.download_content_item_files") as dl, \
                    contextlib.redirect_stderr(io.StringIO()):
                result = asyncio.run(grab_item_async("Slides", {"c": COURSES["_1_1"]}, None, None, out_dir))
            dl.assert_not_called()
            self.assertFalse(out_dir.exists())
        self.assertEqual(result["status"], "folder")
        self.assertEqual(len(result["files"]), 2)

    def test_failed_download_leaves_no_directory(self):
        from scrapers.outline import download_content_item_files
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "downloads" / "IS_410"
            with patch("scrapers.outline.fetch_item_attachments", return_value=[]):
                result = download_content_item_files("c", "i", dest, "x", cookie_header="a=b", allow_default_endpoint=False)
            self.assertEqual(result, [])
            self.assertFalse(dest.exists())


class TestTermsAndDiscover(unittest.TestCase):
    BY_TERM = {"Spring 2026": {"s": "STAT 351 Stats"}, "Fall 2026": {"a": "IS 410 DB", "b": "ENGL 100 Comp"}}

    def test_terms_view_shape_and_order(self):
        from core.course_discovery import terms_view
        view = terms_view(self.BY_TERM, "Fall 2026")
        self.assertEqual([t["term"] for t in view], ["Fall 2026", "Spring 2026"])
        self.assertEqual(view[0], {"term": "Fall 2026", "active": True, "courses": [{"id": "a", "name": "IS 410 DB"}, {"id": "b", "name": "ENGL 100 Comp"}]})

    def test_select_courses(self):
        from core.course_discovery import select_courses
        self.assertEqual(select_courses(self.BY_TERM, "Fall 2026", None)[0], "Fall 2026")
        self.assertEqual(select_courses(self.BY_TERM, "Fall 2026", "sp2026")[1], {"s": "STAT 351 Stats"})
        self.assertEqual(len(select_courses(self.BY_TERM, "Fall 2026", "all")[1]), 3)
        with self.assertRaises(ValueError):
            select_courses(self.BY_TERM, "Fall 2026", "nope")

    def test_terms_is_read_only_and_json_clean(self):
        with patch("core.course_discovery._discover", return_value=(self.BY_TERM, "Fall 2026")), \
                patch("core.course_discovery.save_courses") as save:
            code, out, _err = run_cli(["terms", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["term"], "Fall 2026")
        save.assert_not_called()

    def test_terms_out_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch("core.course_discovery._discover", return_value=(self.BY_TERM, "Fall 2026")):
            target = Path(tmp) / "terms.json"
            code, out, _err = run_cli(["terms", "--out", str(target)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(target.read_text())[0]["active"], True)
            self.assertEqual(out, "")

    def test_discover_saves_explicitly_and_says_so(self):
        with patch("core.course_discovery._discover", return_value=(self.BY_TERM, "Fall 2026")), \
                patch("core.course_discovery.save_courses") as save:
            code, out, err = run_cli(["discover", "--json"])
        self.assertEqual(code, 0)
        save.assert_called_once_with({"a": "IS 410 DB", "b": "ENGL 100 Comp"}, overwrite=True)
        doc = json.loads(out)
        self.assertTrue(doc["saved"])
        self.assertIn("to config.json", err)


class TestJsonStdoutIsOneDocument(unittest.TestCase):
    """stdout must parse as JSON for every command that offers --json; progress stays on stderr."""

    def check(self, argv, patches):
        code, out, err = run_cli(argv + ["--json"], patches)
        self.assertEqual(code, 0, err)
        json.loads(out)
        return out, err

    def test_announcements(self):
        self.check(["announcements"], [
            patch("scrapers.announcements.fetch_course_announcements", return_value=([ann("A", "2026-10-19T12:00:00Z")], None)),
            patch("scrapers.announcements.fetch_term_starts", return_value={}),
        ])

    def test_announcements_error_course_still_json(self):
        out, _ = self.check(["announcements"], [
            patch("scrapers.announcements.fetch_course_announcements", return_value=([], "forbidden")),
            patch("scrapers.announcements.fetch_term_starts", return_value={}),
        ])
        self.assertEqual(json.loads(out)[0]["error"], "forbidden")

    def test_activity(self):
        self.check(["activity"], [patch("scrapers.activity.fetch_activity_rest", return_value=[{"title": "t", "course": "c"}])])

    def test_profile(self):
        self.check(["profile"], [patch("scrapers.profile.scrape_profile_api", return_value={"name": "A B"})])

    def test_search_progress_not_on_stdout(self):
        async def outline(*_a, **_k):
            return [{"title": "Syllabus", "description": "", "content_type": "file", "parent_path": [], "content_id": "f"}]
        _out, err = self.check(["search", "syl"], [
            patch("scrapers.search.scrape_course_outline_async", side_effect=outline),
            patch("scrapers.search._safe_discussions", return_value=None),
        ])
        self.assertIn("Searching", err)

    def test_discussions(self):
        self.check(["discussions", "--all"], [patch("main.scrape_discussions_http", return_value=[])])

    def test_assignments_default_to_all_courses(self):
        calls = []

        def fake(cid):
            calls.append(cid)
            return [{"title": "HW", "due_at": "2026-09-08T20:30:00Z", "status": "graded"}]
        out, _ = self.check(["assignments"], [patch("main.scrape_course_assignments_http", side_effect=fake)])
        self.assertEqual(sorted(calls), sorted(COURSES))
        self.assertEqual(json.loads(out)[0]["assignments"][0]["due_at"], "2026-09-08T20:30:00Z")

    def test_download_not_found_is_json(self):
        async def outline(*_a, **_k):
            return []
        code, out, _ = run_cli(["download", "nothing", "--json"], [patch("scrapers.search.scrape_course_outline_async", side_effect=outline)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["status"], "not_found")

    def test_expired_session_is_a_clear_json_error(self):
        err = rest.RestError("auth", 401)
        code, out, stderr = run_cli(["activity", "--json"], [patch("scrapers.activity.fetch_activity_rest", side_effect=err)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"], "auth")
        self.assertIn("bb login", stderr)


class TestUsageErrors(unittest.TestCase):
    def test_unknown_course_is_short_and_exit_2(self):
        code, out, err = run_cli(["announcements", "NOSUCH101"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("NOSUCH101", err)
        self.assertIn("IS 410", err)
        self.assertLess(len(err.splitlines()), 6)

    def test_outline_without_course_names_a_fix(self):
        code, _out, err = run_cli(["outline"])
        self.assertEqual(code, 2)
        self.assertIn("bb outline IS410", err)

    def test_bad_since_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            main._parse_args(["announcements", "--since", "soon"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()


class TestAnnouncementPreview(unittest.TestCase):
    def test_entities_decoded_and_blocks_separated(self):
        html_in = ('<p><a data-bbtype="attachment" href="x?a=1&amp;b=2">Poof! Gone!</a>Hello all,</p>'
                   '<h2>Rock &amp;amp; Roll</h2><p>Next&nbsp;line</p>')
        text = announcements._clean_html_body(html_in)
        self.assertEqual(text.splitlines(), ["Poof! Gone!", "Hello all,", "Rock & Roll", "Next line"])


class TestIntegratedRequestIdentity(unittest.TestCase):
    def test_rest_reuses_cookie_filter_and_excludes_idp_or_expired_cookies(self):
        cookies = [
            {'name': 'session', 'value': 'synthetic-bb', 'domain': 'blackboard.umbc.edu', 'path': '/'},
            {'name': 'idp', 'value': 'synthetic-idp', 'domain': 'webauth.umbc.edu', 'path': '/'},
            {'name': 'expired', 'value': 'old', 'domain': 'blackboard.umbc.edu', 'path': '/', 'expires': 1},
        ]
        with patch('core.rest._cookies', return_value=cookies):
            self.assertEqual(rest.cookie_header(), 'session=synthetic-bb')

    def test_session_requirement_failure_exits_nonzero_without_json_noise(self):
        from unittest.mock import AsyncMock
        out, err = io.StringIO(), io.StringIO()
        with patch('main.load_courses', return_value=COURSES), \
             patch('main._require_session_async', new=AsyncMock(return_value=False)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                asyncio.run(main.main_async(main._parse_args(['grades', '--json'])))
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(out.getvalue(), '')


class TestSessionCheckExit(unittest.TestCase):
    def test_expired_check_exits_nonzero(self):
        with patch('main.load_courses', return_value=COURSES), \
             patch('main.check_session_async', new=AsyncMock(return_value=False)):
            with self.assertRaises(SystemExit) as caught:
                asyncio.run(main.main_async(main._parse_args(['check'])))
        self.assertEqual(caught.exception.code, 1)


class TestCalendarLazyBrowser(unittest.TestCase):
    def test_calendar_rest_success_never_initializes_browser(self):
        with patch('main.AsyncSessionManager.initialize', new=AsyncMock(side_effect=AssertionError('browser opened'))) as browser:
            code, out, err = run_cli(['calendar', '--json'], [patch('scrapers.calendar.scrape_calendar_api', return_value=[])])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["items"], [])
        browser.assert_not_called()
