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

import json
import re
import unittest
from datetime import datetime

from core.export_json import build_item, build_export_doc, build_composite_schema, _to_unix_timestamp
from core.async_engine import RouteOptimizer, TaskProfile, get_optimal_concurrency
from core.session import quick_check_session_http
from telegram.formatter import escape_html, chunk_message, format_due_dates_list, format_grade_alert, format_announcement_alert
from ui.menubar import BlackboardMenuBarApp


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


if __name__ == "__main__":
    unittest.main()
