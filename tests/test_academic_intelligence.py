"""
Unit and integration tests for Academic Intelligence features:
- Assessment & Quiz Inspector CLI (Ticket 01 / Issue #2)
- Syllabus Auto-Discovery & Content Viewer (Ticket 02 / Issue #3)
- In-Progress Attempt Alerts in Briefing & Due Dates (Ticket 03 / Issue #4)
- Syllabus Local Mirroring & Source Sync Engine (Ticket 04 / Issue #5)
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from scrapers.assessment import (
    inspect_assessment_api,
    format_assessment_cli,
    resolve_assessment_course,
)
from scrapers.syllabus import (
    discover_syllabi_api,
    format_syllabi_cli,
    rewrite_google_doc_url,
    sync_syllabus_file,
    is_syllabus_item,
)
from scrapers.due_dates import (
    get_in_progress_attempts_api,
    format_in_progress_alert_cli,
)


class TestAssessmentInspector(unittest.TestCase):
    @patch("scrapers.assessment.get_cookie_header", return_value="dummy_cookie=123")
    @patch("scrapers.assessment._api_get")
    def test_inspect_assessment_success(self, mock_api_get, mock_cookie):
        def side_effect(url, *args, **kwargs):
            if "contents/_8936566_1" in url:
                return {
                    "id": "_8936566_1",
                    "title": "Engl 100 Common Policies Quiz",
                    "description": "Short quiz about policies.",
                    "contentHandler": {
                        "id": "resource/x-bb-asmt-test-link",
                        "assessmentId": "_23215890_1",
                        "gradeColumnId": "_2071123_1",
                        "proctoring": {
                            "secureBrowserRequiredToTake": False,
                            "secureBrowserRequiredToReview": False,
                            "webcamRequired": False,
                        },
                        "password": {"enabled": False},
                        "isLateAttemptCreationDisallowed": False,
                    },
                }
            elif "gradebook/columns/_2071123_1/attempts" in url:
                return {
                    "results": [
                        {
                            "id": "_35975518_1",
                            "userId": "_235132_1",
                            "status": "InProgress",
                            "exempt": False,
                            "created": "2026-08-27T23:29:10.561Z",
                        }
                    ]
                }
            elif "gradebook/columns/_2071123_1" in url:
                return {
                    "id": "_2071123_1",
                    "name": "Engl 100 Common Policies Quiz",
                    "score": {"possible": 100.0},
                    "grading": {
                        "type": "Attempts",
                        "attemptsAllowed": 2,
                        "scoringModel": "Last",
                        "due": "2026-09-05T23:59:00Z",
                    },
                }
            return None

        mock_api_get.side_effect = side_effect

        record = inspect_assessment_api(
            content_id="_8936566_1",
            course_id="_108410_1",
            courses={"_108410_1": "ENGL 100 Composition"},
        )

        self.assertIsNotNone(record)
        self.assertEqual(record["content_id"], "_8936566_1")
        self.assertEqual(record["course_id"], "_108410_1")
        self.assertEqual(record["title"], "Engl 100 Common Policies Quiz")
        self.assertEqual(record["points_possible"], 100.0)
        self.assertEqual(record["attempts_allowed"], 2)
        self.assertEqual(record["attempts_used"], 1)
        self.assertEqual(record["scoring_model"], "Last")
        self.assertEqual(len(record["attempts"]), 1)
        self.assertEqual(record["attempts"][0]["status"], "InProgress")
        self.assertIn("ultra/courses/_108410_1/outline/assessment/_8936566_1", record["launcher_url"])

        # Test CLI formatting
        cli_text = format_assessment_cli(record)
        self.assertIn("Engl 100 Common Policies Quiz", cli_text)
        self.assertIn("100", cli_text)
        self.assertIn("InProgress", cli_text)

    @patch("scrapers.assessment.get_cookie_header", return_value="dummy_cookie=123")
    @patch("scrapers.assessment._api_get")
    def test_auto_resolve_course(self, mock_api_get, mock_cookie):
        def side_effect(url, *args, **kwargs):
            if "_108410_1/contents/_8936566_1" in url:
                return {"id": "_8936566_1", "title": "Engl 100 Common Policies Quiz"}
            return None

        mock_api_get.side_effect = side_effect

        courses = {
            "_105737_1": "IS 410 Database Design",
            "_108410_1": "ENGL 100 Composition",
        }
        resolved = resolve_assessment_course("_8936566_1", courses, "dummy_cookie=123")
        self.assertEqual(resolved, "_108410_1")


class TestSyllabusDiscoveryAndSync(unittest.TestCase):
    def test_rewrite_google_doc_url(self):
        url = "https://docs.google.com/document/d/1-zYc1McrAR1-27jWeNwn6gTbcvh6U2oe/edit?tab=t.0"
        pdf_url, txt_url = rewrite_google_doc_url(url)
        self.assertEqual(pdf_url, "https://docs.google.com/document/d/1-zYc1McrAR1-27jWeNwn6gTbcvh6U2oe/export?format=pdf")
        self.assertEqual(txt_url, "https://docs.google.com/document/d/1-zYc1McrAR1-27jWeNwn6gTbcvh6U2oe/export?format=txt")

    def test_is_syllabus_item(self):
        self.assertTrue(is_syllabus_item("syllabus", "Course Overview", ""))
        self.assertTrue(is_syllabus_item("document", "Fall 2026 Syllabus", ""))
        self.assertTrue(is_syllabus_item("file", "Syllabus_ECON122.pdf", ""))
        self.assertFalse(is_syllabus_item("assignment", "Homework 1", "Regular homework"))

    def test_sync_syllabus_file_idempotent(self):
        tmp_dir = tempfile.mkdtemp()
        try:
            target_dir = os.path.join(tmp_dir, "courses", "ENGL100", "syllabus")
            record = {
                "course_id": "_108410_1",
                "course_name": "ENGL 100 Composition",
                "content_id": "_8936565_1",
                "title": "ENGL 100 Syllabus",
                "source_type": "google_doc",
                "source_url": "https://docs.google.com/document/d/1-zYc1McrAR1-27jWeNwn6gTbcvh6U2oe/edit",
                "body_text": "UMBC English Composition Fall 2026\nInstructor: Shane Moritz",
            }
            # First sync -> creates file
            saved_path, modified = sync_syllabus_file(record, base_dir=tmp_dir)
            self.assertTrue(modified)
            self.assertTrue(os.path.exists(saved_path))
            with open(saved_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("UMBC English Composition", content)
            self.assertIn("course_id: '_108410_1'", content)

            # Second sync with same content -> idempotent (not modified)
            saved_path_2, modified_2 = sync_syllabus_file(record, base_dir=tmp_dir)
            self.assertFalse(modified_2)
            self.assertEqual(saved_path, saved_path_2)
        finally:
            shutil.rmtree(tmp_dir)


class TestInProgressAttemptAlerts(unittest.TestCase):
    @patch("scrapers.assessment.get_cookie_header", return_value="dummy_cookie=123")
    @patch("scrapers.assessment._api_get")
    def test_get_in_progress_attempts(self, mock_api_get, mock_cookie):
        def side_effect(url, *args, **kwargs):
            if "gradebook/columns" in url and "attempts" not in url:
                return {
                    "results": [
                        {
                            "id": "_2071123_1",
                            "name": "Engl 100 Common Policies Quiz",
                            "contentId": "_8936566_1",
                            "grading": {"due": "2026-09-05T23:59:00Z"},
                        }
                    ]
                }
            elif "gradebook/columns/_2071123_1/attempts" in url:
                # 3 hours ago
                created_iso = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
                return {
                    "results": [
                        {
                            "id": "_35975518_1",
                            "status": "InProgress",
                            "created": created_iso,
                        }
                    ]
                }
            return None

        mock_api_get.side_effect = side_effect

        courses = {"_108410_1": "ENGL 100 Composition"}
        open_attempts = get_in_progress_attempts_api(courses=courses)

        self.assertEqual(len(open_attempts), 1)
        att = open_attempts[0]
        self.assertEqual(att["status"], "InProgress")
        self.assertGreaterEqual(att["elapsed_minutes"], 170)
        self.assertIn("ago", att["elapsed_time_human"])

        alert_text = format_in_progress_alert_cli(open_attempts)
        self.assertIn("IN-PROGRESS", alert_text)
        self.assertIn("Engl 100 Common Policies Quiz", alert_text)

    @patch("scrapers.assessment.inspect_assessment_playwright_async")
    @patch("scrapers.assessment.inspect_assessment_api")
    def test_inspect_assessment_dual_engine_fallback_on_403(self, mock_api, mock_pw):
        # 1. REST endpoint returns 403 Forbidden
        mock_api.return_value = {"_http_status": 403, "error": "Forbidden"}

        async def dummy_pw(*args, **kwargs):
            return {
                "content_id": "_8936566_1",
                "course_id": "_108410_1",
                "title": "Engl 100 Common Policies Quiz (Fallback)",
                "points_possible": "100",
                "attempts_allowed": "2",
                "attempts_used": 0,
                "due_date": "",
                "is_timed": False,
                "attempts": [],
                "launcher_url": "https://blackboard.umbc.edu/test",
            }

        mock_pw.side_effect = dummy_pw

        from scrapers.assessment import inspect_assessment
        import asyncio
        record = asyncio.run(inspect_assessment(
            content_id="_8936566_1",
            course_id="_108410_1",
            courses={"_108410_1": "ENGL 100 Composition"},
        ))

        self.assertIsNotNone(record)
        self.assertEqual(record["title"], "Engl 100 Common Policies Quiz (Fallback)")
        mock_pw.assert_called_once()

    @patch("scrapers.assessment.get_cookie_header", return_value="dummy_cookie=123")
    @patch("scrapers.assessment._api_get")
    def test_assessment_time_limit_and_overdue(self, mock_api_get, mock_cookie):
        def side_effect(url, *args, **kwargs):
            if "contents/_999" in url:
                return {
                    "id": "_999",
                    "title": "Timed Midterm",
                    "contentHandler": {"id": "resource/x-bb-asmt-test-link", "gradeColumnId": "_col999"},
                }
            elif "gradebook/columns/_col999/attempts" in url:
                # Started 90 minutes ago
                created_iso = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat().replace("+00:00", "Z")
                return {
                    "results": [
                        {
                            "id": "_att999",
                            "status": "InProgress",
                            "created": created_iso,
                        }
                    ]
                }
            elif "gradebook/columns/_col999" in url:
                return {
                    "id": "_col999",
                    "score": {"possible": 50.0},
                    "grading": {"timeLimit": 60},  # 60 minute limit
                }
            return None

        mock_api_get.side_effect = side_effect

        record = inspect_assessment_api(content_id="_999", course_id="_108410_1", courses={"_108410_1": "ENGL 100"})
        self.assertIsNotNone(record)
        self.assertTrue(record["is_timed"])
        self.assertEqual(record["time_limit_minutes"], 60)
        self.assertEqual(len(record["attempts"]), 1)
        # Started 90 mins ago with 60 min limit -> overdue
        self.assertTrue(record["attempts"][0]["is_overdue"])


class TestSyllabusConcurrentAndBinary(unittest.TestCase):
    @patch("scrapers.syllabus.get_cookie_header", return_value="dummy_cookie=123")
    @patch("scrapers.syllabus._crawl_api_tree")
    def test_discover_syllabi_api_all_concurrent(self, mock_tree, mock_cookie):
        mock_tree.return_value = [
            {
                "content_id": "_s1",
                "title": "Syllabus File",
                "content_type": "syllabus",
                "description": "Course Syllabus",
                "children": [],
            }
        ]

        courses = {
            "_c1": "Course 1",
            "_c2": "Course 2",
            "_c3": "Course 3",
        }

        results = discover_syllabi_api(all_courses=True, courses=courses)
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertTrue(r["syllabus_found"])
            self.assertEqual(len(r["items"]), 1)
            self.assertIn("course_id", r)
            self.assertIn("course_name", r)

    def test_sync_syllabus_binary_pdf_and_sidecar(self):
        tmp_dir = tempfile.mkdtemp()
        try:
            record = {
                "course_id": "_108410_1",
                "course_name": "ENGL 100 Composition",
                "content_id": "_file123",
                "title": "Syllabus Document",
                "source_type": "file",
                "source_url": "https://example.com/syllabus.pdf",
                "body_text": "",
            }

            fake_pdf_bytes = b"%PDF-1.4 Fake PDF Content for Unit Test"
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.read.return_value = fake_pdf_bytes
                mock_resp.__enter__.return_value = mock_resp
                mock_urlopen.return_value = mock_resp

                saved_path, modified = sync_syllabus_file(record, base_dir=tmp_dir)

                self.assertTrue(modified)
                self.assertTrue(os.path.exists(saved_path))
                self.assertTrue(saved_path.endswith(".pdf"))
                with open(saved_path, "rb") as f:
                    self.assertEqual(f.read(), fake_pdf_bytes)

                # Check metadata sidecar
                sidecar_path = saved_path.replace(".pdf", ".meta.json")
                self.assertTrue(os.path.exists(sidecar_path))
                with open(sidecar_path, "r", encoding="utf-8") as f:
                    meta = json.loads(f.read())
                self.assertEqual(meta["title"], "Syllabus Document")
                self.assertEqual(meta["course_id"], "_108410_1")
        finally:
            shutil.rmtree(tmp_dir)


if __name__ == "__main__":
    unittest.main()
