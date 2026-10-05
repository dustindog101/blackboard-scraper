import asyncio
import unittest
from unittest.mock import patch

from core.config import BLACKBOARD_BASE
import contextlib
import io

import main
from scrapers.discussions import scrape_discussions_http
from scrapers.quiz import (
    _clean_html_text,
    _extract_links,
    _scrape_assessment_http,
    format_assessment_attempt_cli,
)
from scrapers.search import _match_discussions, find_items_async

VIDEO_URL = "https://mediaplayer.pearsoncmg.com/assets/pearson_rw_example"
PROMPT_HTML = (
    '<p>First, watch the application <a href="' + VIDEO_URL + '" target="_blank">video clip</a>.'
    " Next, summarize the concepts.</p><p>Write 3 paragraphs.</p>"
)


class TestLinkPreservation(unittest.TestCase):
    def test_link_url_kept_inline(self):
        text = _clean_html_text(PROMPT_HTML)
        self.assertIn(f"video clip ({VIDEO_URL})", text)
        self.assertIn("Write 3 paragraphs.", text)

    def test_link_whose_text_is_the_url_is_not_doubled(self):
        text = _clean_html_text(f'<p>See <a href="{VIDEO_URL}">{VIDEO_URL}</a></p>')
        self.assertEqual(text, f"See {VIDEO_URL}")

    def test_relative_href_resolves_to_blackboard(self):
        links = _extract_links('<a href="/bbcswebdav/xid-1_1">Syllabus</a>')
        self.assertEqual(links, [{"text": "Syllabus", "url": f"{BLACKBOARD_BASE}/bbcswebdav/xid-1_1"}])

    def test_anchor_and_javascript_hrefs_are_dropped(self):
        html_in = '<a href="#top">Top</a> <a href="javascript:void(0)">Click</a>'
        self.assertEqual(_clean_html_text(html_in), "Top Click")
        self.assertEqual(_extract_links(html_in), [])

    def test_entities_and_duplicates(self):
        html_in = (
            '<a href="https://x.test/?a=1&amp;b=2">One &amp; Two</a>'
            '<a href="https://x.test/?a=1&amp;b=2">again</a>'
        )
        self.assertEqual(_extract_links(html_in), [{"text": "One & Two", "url": "https://x.test/?a=1&b=2"}])

    def test_plain_text_and_empty_input_unchanged(self):
        self.assertEqual(_clean_html_text("<p>No links here</p>"), "No links here")
        self.assertEqual(_clean_html_text(None), "")
        self.assertEqual(_extract_links(None), [])


class TestDiscussionInspectLinks(unittest.TestCase):
    def _api(self, path, _cookie):
        if path == "/learn/api/public/v1/courses/_c1":
            return {"name": "Example Course"}
        if path == "/learn/api/public/v2/courses/_c1/gradebook/columns":
            return {"results": [{
                "id": "_col1", "contentId": "_content1", "name": "M5 Discussion",
                "scoreProviderHandle": "resource/x-bb-forumlink", "score": {"possible": 8},
            }]}
        if path == "/learn/api/public/v1/courses/_c1/discussions":
            return {"results": [{
                "id": "_d1", "title": "M5 Discussion", "gradebookColumnId": "_col1",
                "topic": {"body": PROMPT_HTML},
            }]}
        if path == "/learn/api/v1/users/me":
            return {"id": "_me"}
        return {}

    @patch("scrapers.quiz.load_courses", return_value={"_c1": "Example Course"})
    @patch("scrapers.quiz._get_cookie_header", return_value="session=cookie")
    @patch("scrapers.quiz._api_get")
    def test_inspect_returns_links_and_cli_shows_them(self, api_get, _cookie, _courses):
        api_get.side_effect = self._api
        result = _scrape_assessment_http("_c1", assessment_id="_content1")
        self.assertEqual(result["links"], [{"text": "video clip", "url": VIDEO_URL}])
        self.assertIn(VIDEO_URL, result["instructions"])
        self.assertIn(f"video clip: {VIDEO_URL}", format_assessment_attempt_cli(result))


class TestDiscussionsRestFastPath(unittest.TestCase):
    def _api(self, path, _cookie):
        if path == "/learn/api/public/v1/courses/_c1/discussions":
            return {
                "results": [{"id": "_d1", "title": "M5 Discussion", "gradebookColumnId": "_col1",
                             "topic": {"body": PROMPT_HTML}}],
                "paging": {"nextPage": "/learn/api/public/v1/courses/_c1/discussions?offset=1"},
            }
        if path == "/learn/api/public/v1/courses/_c1/discussions?offset=1":
            return {"results": [{"id": "_d2", "title": None, "topic": None}]}
        if path == "/learn/api/public/v2/courses/_c1/gradebook/columns":
            return {"results": [{"id": "_col1", "dueDate": None, "grading": {"due": "2026-10-05T03:59:00.000Z"}}]}
        return {}

    @patch("scrapers.discussions._get_cookie_header", return_value="session=cookie")
    @patch("scrapers.discussions._api_get")
    def test_topics_prompts_links_due_dates_and_paging(self, api_get, _cookie):
        api_get.side_effect = self._api
        topics = scrape_discussions_http("_c1")
        self.assertEqual(len(topics), 2)
        first = topics[0]
        self.assertEqual(first["title"], "M5 Discussion")
        self.assertEqual(first["links"][0]["url"], VIDEO_URL)
        self.assertIn("Write 3 paragraphs.", first["prompt"])
        self.assertIn("Oct 4, 2026", first["due_date"])
        self.assertEqual(first["posts"], [])
        self.assertEqual(topics[1]["title"], "")
        self.assertEqual(topics[1]["prompt"], "")

    @patch("scrapers.discussions._get_cookie_header", return_value=None)
    def test_no_session_returns_none_for_browser_fallback(self, _cookie):
        self.assertIsNone(scrape_discussions_http("_c1"))

    @patch("scrapers.discussions._get_cookie_header", return_value="session=cookie")
    @patch("scrapers.discussions._api_get", return_value={"_http_status": 401, "error": "Unauthorized"})
    def test_expired_session_returns_none(self, _api, _cookie):
        self.assertIsNone(scrape_discussions_http("_c1"))

    @patch("scrapers.discussions._get_cookie_header", return_value="session=cookie")
    @patch("scrapers.discussions._api_get")
    def test_gradebook_failure_still_returns_topics(self, api_get, _cookie):
        def api(path, cookie):
            if "gradebook" in path:
                return None
            return self._api(path, cookie)
        api_get.side_effect = api
        topics = scrape_discussions_http("_c1")
        self.assertEqual(topics[0]["due_date"], "")
        self.assertEqual(topics[0]["links"][0]["url"], VIDEO_URL)


class TestDiscussionPostsRest(unittest.TestCase):
    BASE = "/learn/api/public/v1/courses/_c1/discussions"

    def _api(self, path, _cookie):
        if path == self.BASE:
            return {"results": [
                {"id": "_d1", "title": "M4 Discussion", "topic": {"body": PROMPT_HTML}},
                {"id": "_d2", "title": "Broken Thread", "topic": {"body": "<p>x</p>"}},
            ]}
        if "gradebook" in path:
            return {"results": []}
        if path == f"{self.BASE}/_d1/messages?limit=100":
            return {
                "results": [{"id": "_m1", "givenName": "Pat", "familyName": "Lee", "userId": "_u1",
                             "body": "<p>First post</p>", "postDate": "2026-09-21T03:55:21.027Z",
                             "parentId": "_root", "threadId": "_root"}],
                "paging": {"nextPage": f"{self.BASE}/_d1/messages?limit=100&offset=1"},
            }
        if path == f"{self.BASE}/_d1/messages?limit=100&offset=1":
            return {"results": [{"id": "_m2", "givenName": "Sam", "familyName": "", "userId": "_u2",
                                 "body": "<p>Second post</p>", "parentId": "_root", "threadId": "_root"}]}
        if path == f"{self.BASE}/_d1/messages/_m1/replies?limit=100":
            return {"results": [{"id": "_r1", "givenName": "Sam", "familyName": "", "userId": "_u2",
                                 "body": "<p>Nice point</p>", "parentId": "_m1", "threadId": "_root"}]}
        if path == f"{self.BASE}/_d1/messages/_m2/replies?limit=100":
            return {"results": []}
        if path == f"{self.BASE}/_d2/messages?limit=100":
            return {"_http_status": 403, "error": "Forbidden"}
        return {}

    def _run(self, **kwargs):
        with patch("scrapers.discussions._get_cookie_header", return_value="session=cookie"), \
                patch("scrapers.discussions._api_get", side_effect=self._api):
            return scrape_discussions_http("_c1", include_posts=True, **kwargs)

    def test_posts_replies_and_participants(self):
        thread = self._run()[0]
        self.assertEqual([p["text"] for p in thread["posts"]], ["First post", "Nice point", "Second post"])
        self.assertEqual(thread["posts"][0]["author"], "Pat Lee")
        self.assertTrue(thread["posts"][1]["is_reply"])
        self.assertIn("Sep 20, 2026", thread["posts"][0]["date"])
        self.assertEqual(
            {p["name"]: p["stats"] for p in thread["participants"]},
            {"Pat Lee": "1 responses, 0 replies", "Sam": "1 responses, 1 replies"},
        )
        self.assertEqual(thread["links"][0]["url"], VIDEO_URL)

    def test_one_failing_thread_keeps_the_rest(self):
        broken = self._run()[1]
        self.assertEqual(broken["posts"], [])
        self.assertIn("posts_error", broken)
        self.assertEqual(broken["prompt"], "x")

    def test_posts_only_and_participants_only(self):
        self.assertEqual(self._run(posts_only=True)[0]["participants"], [])
        participants_only = self._run(participants_only=True)[0]
        self.assertEqual(participants_only["posts"], [])
        self.assertEqual(len(participants_only["participants"]), 2)

    def test_titles_only_makes_no_message_requests(self):
        with patch("scrapers.discussions._get_cookie_header", return_value="session=cookie"), \
                patch("scrapers.discussions._api_get", side_effect=self._api) as api:
            scrape_discussions_http("_c1")
        self.assertFalse([c for c in api.call_args_list if "/messages" in c.args[0]])


class TestDiscussionsRunner(unittest.TestCase):
    TOPICS = [{"id": "_d1", "title": "M5", "posts": [{"text": "hi"}], "participants": []}]

    def _run(self, rest_value, browser_value=None):
        out, err = io.StringIO(), io.StringIO()
        with patch("main.scrape_discussions_http", return_value=rest_value) as rest, \
                patch("main.scrape_discussions", return_value=browser_value or []) as browser, \
                patch("main._launch_context", return_value=(unittest.mock.MagicMock(), None)), \
                patch("playwright.sync_api.sync_playwright"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            data = main._run_discussions_sync(["_c1"], {"_c1": "Course"}, True, None, {}, False, False)
        return data, out.getvalue(), rest, browser

    def test_rest_success_skips_browser_and_keeps_stdout_clean(self):
        data, stdout, rest, browser = self._run(self.TOPICS)
        self.assertEqual(data[0]["discussions"], self.TOPICS)
        self.assertEqual(stdout, "")
        browser.assert_not_called()
        self.assertTrue(rest.call_args.kwargs["include_posts"])

    def test_rest_unavailable_falls_back_to_browser(self):
        data, stdout, _rest, browser = self._run(None, browser_value=[{"title": "From browser"}])
        browser.assert_called_once()
        self.assertEqual(data[0]["discussions"], [{"title": "From browser"}])
        self.assertEqual(stdout, "")


class TestSearchDiscussionPrompts(unittest.TestCase):
    DISCS = [{"id": "_d1", "title": "M5 Discussion", "prompt": "Watch the video clip about Example Co.",
              "links": [{"text": "video clip", "url": VIDEO_URL}], "due_date": ""}]

    def test_matches_link_url(self):
        hits = _match_discussions(self.DISCS, "pearson_rw_example", "_c1", "Course", set())
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["external_url"], VIDEO_URL)
        self.assertEqual(hits[0]["content_type"], "discussion")

    def test_matches_prompt_text_with_snippet(self):
        hits = _match_discussions(self.DISCS, "example co", "_c1", "Course", set())
        self.assertIn("Example Co", hits[0]["description"])

    def test_skips_titles_already_matched_by_outline(self):
        self.assertEqual(_match_discussions(self.DISCS, "m5", "_c1", "Course", {"m5 discussion"}), [])

    def test_no_match(self):
        self.assertEqual(_match_discussions(self.DISCS, "nothing", "_c1", "Course", set()), [])

    @patch("scrapers.search.scrape_course_outline_async")
    @patch("scrapers.search.scrape_discussions_http", side_effect=RuntimeError("boom"))
    def test_discussion_failure_never_breaks_outline_search(self, _disc, outline):
        async def fake_outline(*_a, **_k):
            return [{"title": "Example notes", "description": "", "content_type": "file",
                     "parent_path": [], "content_id": "_f1"}]
        outline.side_effect = fake_outline
        hits = asyncio.run(find_items_async("example", {"_c1": "Course"}))
        self.assertEqual([h["content_id"] for h in hits], ["_f1"])

    @patch("scrapers.search.scrape_course_outline_async")
    @patch("scrapers.search.scrape_discussions_http")
    def test_search_finds_video_only_mentioned_in_prompt_link(self, disc, outline):
        async def fake_outline(*_a, **_k):
            return []
        outline.side_effect = fake_outline
        disc.return_value = self.DISCS
        hits = asyncio.run(find_items_async("pearson_rw_example", {"_c1": "Course"}))
        self.assertEqual(hits[0]["title"], "M5 Discussion")


if __name__ == "__main__":
    unittest.main()
