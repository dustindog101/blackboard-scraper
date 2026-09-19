import unittest
from unittest.mock import patch

from scrapers.quiz import (
    _match_gradebook_columns,
    _scrape_assessment_http,
    format_assessment_attempt_cli,
)


class TestDiscussionSubmissions(unittest.TestCase):
    def _api_response(self, path, _cookie):
        if path == "/learn/api/public/v1/courses/_course_1":
            return {"name": "Example Course"}
        if path == "/learn/api/public/v2/courses/_course_1/gradebook/columns":
            return {
                "results": [{
                    "id": "_column_1",
                    "contentId": "_content_1",
                    "name": "Module 3 Discussion",
                    "scoreProviderHandle": "resource/x-bb-forumlink",
                    "score": {"possible": 8},
                }]
            }
        if path == "/learn/api/public/v1/courses/_course_1/discussions":
            return {
                "results": [{
                    "id": "_discussion_1",
                    "title": "Module 3 Discussion",
                    "gradebookColumnId": "_column_1",
                    "groupDiscussion": False,
                    "topic": {"body": "<p>Prompt text</p>"},
                }]
            }
        if path == "/learn/api/v1/users/me":
            return {"id": "_me_1"}
        if "/gradebook/columns/_column_1/grades?userId=_me_1" in path:
            return {"results": [{"status": "GRADED"}]}
        if path.endswith("/discussions/_discussion_1/messages?limit=100"):
            return {
                "results": [
                    {
                        "id": "_mine_1",
                        "userId": "_me_1",
                        "status": "Published",
                        "body": "<p>My submitted response</p>",
                        "postDate": "2026-09-14T02:45:51.434Z",
                    },
                    {
                        "id": "_other_1",
                        "userId": "_other_1",
                        "status": "Published",
                        "body": "<p>Another student's response</p>",
                    },
                ]
            }
        return {}

    @patch("scrapers.quiz.load_courses", return_value={"_course_1": "Example Course"})
    @patch("scrapers.quiz._get_cookie_header", return_value="session=cookie")
    @patch("scrapers.quiz._api_get")
    def test_discussion_inspector_returns_only_current_users_posts(
        self, api_get, _cookie_header, _courses
    ):
        api_get.side_effect = self._api_response

        result = _scrape_assessment_http(
            "_course_1", assessment_id="_content_1"
        )

        self.assertEqual(result["submission_count"], 1)
        self.assertEqual(result["status"], "GRADED")
        self.assertEqual(result["submissions"][0]["body"], "My submitted response")
        self.assertNotIn("Another student's response", str(result))

    def test_cli_formats_submitted_post(self):
        output = format_assessment_attempt_cli({
            "title": "Module 3 Discussion",
            "item_type": "Discussion Board",
            "course_name": "Example Course",
            "submissions": [{
                "status": "Published",
                "post_date": "2026-09-14T02:45:51.434Z",
                "body": "My submitted response",
                "attachments": [],
            }],
        })

        self.assertIn("Your Submitted Post: 1", output)
        self.assertIn("My submitted response", output)
        self.assertIn("Sep 13, 2026 at 10:45 PM EDT", output)

    def test_exact_title_beats_partial_matches(self):
        columns = [
            {"id": "_1", "name": "M3 Online Lesson"},
            {"id": "_2", "name": "M3"},
            {"id": "_3", "name": "M3 Discussion/Application"},
        ]

        matches = _match_gradebook_columns(columns, target="M3")

        self.assertEqual([match["id"] for match in matches], ["_2"])

    def test_multiple_partial_titles_remain_ambiguous(self):
        columns = [
            {"id": "_1", "name": "M3 Online Lesson"},
            {"id": "_2", "name": "M3 Discussion/Application"},
        ]

        matches = _match_gradebook_columns(columns, target="M3")

        self.assertEqual([match["id"] for match in matches], ["_1", "_2"])


if __name__ == "__main__":
    unittest.main()
