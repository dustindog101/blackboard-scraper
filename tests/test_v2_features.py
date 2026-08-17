"""
Unit and Integration Tests for Blackboard Ultra Scraper v2 features:
- Async Route Optimizer rules
- Message chunking and HTML escaping for Telegram
- Due Date Aggregation logic and date parsing
- Envelope Export formatting for new content kinds
"""

import unittest
from datetime import datetime
from telegram.formatter import escape_html, chunk_message, format_due_dates_list, format_grade_alert, format_announcement_alert
from core.export_json import build_item, _to_unix_timestamp
from core.async_engine import RouteOptimizer


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

    def test_format_grade_alert(self):
        grade_item = {"name": "Midterm Exam", "grade": "95 / 100", "status": "Graded"}
        formatted = format_grade_alert(grade_item, "IS 410 Database Design")
        self.assertIn("NEW GRADE POSTED", formatted)
        self.assertIn("IS 410 Database Design", formatted)
        self.assertIn("Midterm Exam", formatted)
        self.assertIn("95 / 100", formatted)


class TestExportEnvelope(unittest.TestCase):
    def test_build_outline_item(self):
        item = build_item(
            kind="content_item",
            course_id="_100001_1",
            course_name="IS 410",
            title="Lecture Notes Week 1",
            notes="Introduction to Relational Algebra",
            metadata={"content_type": "document", "depth": 1},
        )
        self.assertEqual(item["kind"], "content_item")
        self.assertEqual(item["course_id"], "_100001_1")
        self.assertEqual(item["metadata"]["content_type"], "document")

    def test_build_assignment_item(self):
        item = build_item(
            kind="assignment",
            course_id="_100001_1",
            course_name="IS 410",
            title="Project Milestone 1",
            due_text="2026-09-15 23:59",
            metadata={"points_possible": "100", "submission_status": "Unattempted"},
        )
        self.assertEqual(item["kind"], "assignment")
        self.assertIsNotNone(item["due_at"])
        self.assertEqual(item["metadata"]["points_possible"], "100")


class TestRouteOptimizer(unittest.TestCase):
    def test_blocked_extensions(self):
        self.assertIn("png", RouteOptimizer.BLOCKED_EXTENSIONS)
        self.assertIn("woff2", RouteOptimizer.BLOCKED_EXTENSIONS)
        self.assertIn("mp4", RouteOptimizer.BLOCKED_EXTENSIONS)

    def test_blocked_domains(self):
        self.assertIn("telemetry.blackboard.com", RouteOptimizer.BLOCKED_DOMAINS)
        self.assertIn("google-analytics.com", RouteOptimizer.BLOCKED_DOMAINS)


if __name__ == "__main__":
    unittest.main()
