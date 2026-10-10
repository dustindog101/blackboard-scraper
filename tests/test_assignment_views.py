import asyncio
import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import main
from scrapers.assignment_view import (
    DEFAULT_TEXT_LIMIT,
    _DocumentRedirects,
    _read_document,
    _related_resources,
    format_assignment_view,
    prepare_assignment_view,
)
from scrapers.quiz import _scrape_assessment_http, save_assessment_attempt


def association(visible=True):
    return {"displayBefore": visible, "rubricDefinition": {
        "title": "Writing rubric", "columnHeaders": [{"id": "level", "header": "Excellent"}],
        "rows": [{"header": "Evidence", "percentage": 100, "position": 0,
                  "cells": [{"rubricColumnId": "level", "description": "PRIVATE_RUBRIC_DETAILS",
                             "percentageMin": 85, "percentageMax": 100,
                             "normalizedPointRange": [1, 2, 3], "weightedPercentRange": [0.1]}]}],
    }}


BASE = {"title": "Example assignment", "item_type": "Assignment", "course_id": "course",
        "course_name": "Example Course", "assessment_id": "content", "column_id": "column",
        "instructions": "Explain your reasoning.", "due_date": "2026-10-12T03:59:00Z",
        "questions": [{"prompt": "PREVIOUS_QUESTION", "selected_answer": "PREVIOUS_ANSWER"}],
        "submissions": [{"body": "PREVIOUS_POST"}], "attempt_id": "attempt", "status": "COMPLETED"}


class TestViews(unittest.TestCase):
    def prepare(self, mode="default", visible=True, data=None, response=None):
        col = response if response is not None else {"rubricAssociations": [association(visible)], "possible": 100}
        with patch("scrapers.assignment_view._get_cookie_header", return_value="SECRET_COOKIE"), \
             patch("scrapers.assignment_view._api_get", return_value=col):
            return prepare_assignment_view(data or BASE, mode)

    def test_default_hints_without_work_or_rubric_details(self):
        result = self.prepare()
        output = format_assignment_view(result)
        self.assertIn("Rubric: available (1 criterion)", output)
        self.assertIn("Existing submission/attempt available", output)
        self.assertIn("Oct 11, 2026 at 11:59 PM EDT", output)
        for private in ("PRIVATE_RUBRIC_DETAILS", "PREVIOUS_QUESTION", "PREVIOUS_ANSWER", "PREVIOUS_POST"):
            self.assertNotIn(private, json.dumps(result))
        self.assertNotIn("attempt_id", result)
        self.assertTrue(result["work_available"])
        self.assertIn("--rubric", result["rubric"]["hint"])
        self.assertIn("--full", result["work_hint"])

    def test_instructions_view_has_hint_but_no_details(self):
        result = self.prepare("instructions")
        self.assertEqual(result["instructions"], BASE["instructions"])
        self.assertNotIn("status", result)
        self.assertNotIn("questions", result)
        self.assertNotIn("PRIVATE_RUBRIC_DETAILS", json.dumps(result))
        self.assertIn("--rubric", format_assignment_view(result))

    def test_rubric_view_has_no_instructions_or_work(self):
        result = self.prepare("rubric")
        output = format_assignment_view(result)
        self.assertIn("PRIVATE_RUBRIC_DETAILS", output)
        self.assertNotIn("instructions", result)
        self.assertNotIn("Explain your reasoning", output)
        self.assertNotIn("PREVIOUS_ANSWER", json.dumps(result))
        self.assertNotIn("normalizedPointRange", json.dumps(result))
        self.assertNotIn("weightedPercentRange", json.dumps(result))

    def test_point_ranges_are_rendered_without_percentage_units(self):
        raw = association()
        cell = raw["rubricDefinition"]["rows"][0]["cells"][0]
        cell.pop("percentageMin")
        cell.pop("percentageMax")
        cell.update(pointsMin=8, pointsMax=10)
        result = self.prepare("rubric", response={"rubricAssociations": [raw]})
        self.assertIn("Excellent (8–10 points)", format_assignment_view(result))

    def test_hidden_or_unspecified_visibility_never_exposes_definition(self):
        for visible in (False, None, "true"):
            for mode in ("default", "instructions", "rubric", "full"):
                result = self.prepare(mode, visible=visible)
                self.assertEqual(result["rubric"]["status"], "hidden")
                self.assertNotIn("PRIVATE_RUBRIC_DETAILS", json.dumps(result))
                self.assertIn("not available before starting", format_assignment_view(result))

    def test_hidden_native_rubric_cannot_be_bypassed_by_a_link(self):
        data = {**BASE, "links": [{"text": "Rubric", "url": "https://docs.google.com/document/d/r/edit"}]}
        with patch("scrapers.assignment_view._get_cookie_header", return_value="cookie"), \
             patch("scrapers.assignment_view._api_get", return_value={"rubricAssociations": [association(False)]}), \
             patch("scrapers.assignment_view._read_document") as read:
            result = prepare_assignment_view(data, "full")
        read.assert_not_called()
        self.assertEqual(result["rubric"]["status"], "hidden")
        self.assertNotIn("PRIVATE_RUBRIC_DETAILS", json.dumps(result))

    def test_full_contains_instructions_rubric_and_existing_work(self):
        result = self.prepare("full")
        output = format_assignment_view(result)
        for expected in ("Explain your reasoning", "PRIVATE_RUBRIC_DETAILS", "PREVIOUS_QUESTION", "PREVIOUS_POST"):
            self.assertIn(expected, output)

    def test_failed_rubric_request_is_unknown_not_none(self):
        for response in ({"_http_status": 403}, {}):
            result = self.prepare("rubric", response=response)
            self.assertEqual(result["rubric"]["status"], "unknown")
            self.assertIn("Could not check", format_assignment_view(result))

    def test_default_truncation_is_explicit_instructions_mode_is_complete(self):
        data = {**BASE, "instructions": "x" * (DEFAULT_TEXT_LIMIT + 100)}
        default = self.prepare(data=data)
        self.assertEqual(len(default["instructions"]), DEFAULT_TEXT_LIMIT)
        self.assertTrue(default["instructions_truncated"])
        self.assertIn("truncated", format_assignment_view(default))
        self.assertEqual(self.prepare("instructions", data=data)["instructions"], data["instructions"])

    def test_external_provider_is_identified_not_unattempted(self):
        col = {"scoreProviderHandle": "resource/x-bb-blti-link", "possible": 1, "rubricAssociations": []}
        content = {"description": "Gradescope link", "contentDetail": {"resource/x-bb-blti-link": {
            "domainConfig": {"name": "Gradescope"}}}}
        with patch("scrapers.assignment_view._get_cookie_header", return_value="cookie"), \
             patch("scrapers.assignment_view._api_get", side_effect=[col, content]):
            result = prepare_assignment_view({**BASE, "status": "NOT_ATTEMPTED"}, "rubric")
        self.assertEqual(result["rubric"]["status"], "external")
        self.assertIn("Gradescope", format_assignment_view(result))
        self.assertNotIn("No rubric", format_assignment_view(result))

    def test_linked_prompt_read_but_linked_rubric_only_read_when_requested(self):
        resources = [{"kind": "instructions", "title": "Essay Prompt", "url": "https://docs.google.com/document/d/prompt/edit"},
                     {"kind": "rubric", "title": "Rubric!", "url": "https://docs.google.com/document/d/rubric/edit"}]
        for mode, expected_reads in (("default", 1), ("instructions", 1), ("rubric", 1), ("full", 2)):
            with patch("scrapers.assignment_view._get_cookie_header", return_value="cookie"), \
                 patch("scrapers.assignment_view._api_get", side_effect=[{"rubricAssociations": []}, {"parentId": "folder"}]), \
                 patch("scrapers.assignment_view._related_resources", return_value=resources), \
                 patch("scrapers.assignment_view._read_document", return_value={"status": "available", "text": "DOCUMENT_TEXT", "truncated": False}) as read:
                result = prepare_assignment_view({**BASE, "instructions": "", "links": []}, mode)
            self.assertEqual(read.call_count, expected_reads)
            if mode in ("default", "instructions"):
                self.assertEqual(read.call_args.args[0]["kind"], "instructions")
                self.assertNotIn("documents", result["rubric"])
            if mode == "rubric":
                self.assertEqual(read.call_args.args[0]["kind"], "rubric")
                self.assertNotIn("instructions", result)

    def test_rubric_link_not_read_as_instruction_attachment(self):
        data = {**BASE, "instructions": "Download the instructions and rubric.",
                "links": [{"text": "rubric.pdf", "url": "https://blackboard.umbc.edu/bbcswebdav/xid-1"}]}
        with patch("scrapers.assignment_view._get_cookie_header", return_value="cookie"), \
             patch("scrapers.assignment_view._api_get", side_effect=[{"rubricAssociations": []}, {}]), \
             patch("scrapers.assignment_view._read_document") as read:
            result = prepare_assignment_view(data)
        read.assert_not_called()
        self.assertEqual(result["rubric"]["status"], "linked")

    def test_attachment_only_instructions_are_read_and_link_not_duplicated(self):
        url = "https://blackboard.umbc.edu/bbcswebdav/xid-1"
        data = {**BASE, "instructions": f"HW1.docx ({url})", "links": [{"text": "HW1.docx", "url": url}]}
        with patch("scrapers.assignment_view._get_cookie_header", return_value="cookie"), \
             patch("scrapers.assignment_view._api_get", return_value={"rubricAssociations": [association()]}), \
             patch("scrapers.assignment_view._read_document", return_value={"status": "available", "text": "Actual exercise", "truncated": False}):
            result = prepare_assignment_view(data, "instructions")
        self.assertIn("Actual exercise", result["instructions"])
        self.assertNotIn(url, result["instructions"])
        self.assertNotIn("links", result)

    def test_markdown_uses_same_projection_as_text(self):
        data = self.prepare("rubric")
        with tempfile.TemporaryDirectory() as directory, patch("scrapers.quiz.ensure_output_dir", return_value=Path(directory)):
            path = save_assessment_attempt(data, Path(directory) / "report.md")
            text = path.read_text()
        self.assertIn("PRIVATE_RUBRIC_DETAILS", text)
        self.assertNotIn("PREVIOUS_ANSWER", text)
        self.assertNotIn("Explain your reasoning", text)


class TestResourceReading(unittest.TestCase):
    def opener(self, body, content_type="text/plain"):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = body
        response.headers = {"Content-Type": content_type}
        opener = MagicMock()
        opener.open.return_value = response
        return opener

    def test_google_doc_export_never_receives_blackboard_cookie(self):
        opener = self.opener(b"Prompt text")
        with patch("scrapers.assignment_view.urllib.request.build_opener", return_value=opener):
            result = _read_document({"title": "Prompt", "url": "https://docs.google.com/document/d/example/edit"}, "SECRET_COOKIE", 100)
        request = opener.open.call_args.args[0]
        self.assertNotIn("Cookie", request.headers)
        self.assertTrue(request.full_url.endswith("/export?format=txt"))
        self.assertEqual(result["text"], "Prompt text")

    def test_docx_extraction_and_blackboard_cookie_scope(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Homework instructions</w:t></w:r></w:p></w:body></w:document>')
        opener = self.opener(buffer.getvalue(), "application/octet-stream")
        with patch("scrapers.assignment_view.urllib.request.build_opener", return_value=opener):
            result = _read_document({"title": "HW.docx", "url": "https://blackboard.umbc.edu/bbcswebdav/xid-1"}, "SECRET_COOKIE", 100)
        self.assertEqual(result["text"], "Homework instructions")
        self.assertEqual(opener.open.call_args.args[0].headers["Cookie"], "SECRET_COOKIE")

    def test_pdf_instruction_extraction(self):
        opener = self.opener(b"PDF bytes", "application/pdf")
        reader = MagicMock()
        reader.pages = [MagicMock(), MagicMock()]
        reader.pages[0].extract_text.return_value = "First instruction"
        reader.pages[1].extract_text.return_value = "Second instruction"
        with patch("scrapers.assignment_view.urllib.request.build_opener", return_value=opener), \
             patch("pypdf.PdfReader", return_value=reader):
            result = _read_document({"title": "Instructions.pdf", "url": "https://blackboard.umbc.edu/bbcswebdav/xid-1"}, "cookie", 100)
        self.assertEqual(result["text"], "First instruction\n\nSecond instruction")

    def test_oversized_document_is_left_as_a_link(self):
        opener = self.opener(b"x" * 20)
        with patch("scrapers.assignment_view.urllib.request.build_opener", return_value=opener), \
             patch("scrapers.assignment_view.MAX_DOCUMENT_BYTES", 10):
            result = _read_document({"title": "Prompt", "url": "https://docs.google.com/document/d/example/edit"}, "cookie", 100)
        self.assertEqual(result["status"], "linked")
        self.assertNotIn("text", result)
        opener.open.return_value.__enter__.return_value.read.assert_called_once_with(11)

    def test_unsupported_urls_are_not_fetched(self):
        for url in ("https://other.example/file.pdf", "http://docs.google.com/document/d/example/edit", "https://docs.google.com/evil"):
            with patch("scrapers.assignment_view.urllib.request.build_opener") as opener:
                result = _read_document({"title": "file.pdf", "url": url}, "cookie", 100)
            opener.assert_not_called()
            self.assertEqual(result["status"], "linked")

    def test_blackboard_cookie_is_not_forwarded_on_foreign_redirect(self):
        import urllib.error
        import urllib.request
        request = urllib.request.Request("https://blackboard.umbc.edu/bbcswebdav/xid-1", headers={"Cookie": "secret"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _DocumentRedirects(True).redirect_request(request, None, 302, "Found", {}, "https://other.example/file.pdf")
        raised.exception.close()

    def test_blackboard_download_redirect_drops_cookies_on_vendor_host(self):
        import urllib.request
        request = urllib.request.Request("https://blackboard.umbc.edu/bbcswebdav/xid-1", headers={"Cookie": "secret"})
        redirected = _DocumentRedirects(True).redirect_request(request, None, 302, "Found", {}, "https://alt-example.blackboard.com/download")
        self.assertNotIn("Cookie", redirected.headers)
        cdn = _DocumentRedirects(True).redirect_request(request, None, 302, "Found", {}, "https://learn.example.content.blackboardcdn.com/file")
        self.assertNotIn("Cookie", cdn.headers)

    def test_document_access_failure_keeps_link_and_no_secret_error(self):
        with patch("scrapers.assignment_view.urllib.request.build_opener", side_effect=OSError("SECRET_COOKIE")):
            result = _read_document({"title": "Prompt", "url": "https://docs.google.com/document/d/example/edit"}, "SECRET_COOKIE", 100)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("SECRET_COOKIE", json.dumps(result))

    def test_sibling_matching_requires_assignment_folder_and_visible_resource(self):
        content = {"title": "Ghost Dog Final", "parentId": "folder"}
        children = {"results": [{"title": "Rubric!", "availability": {"available": "No"}, "contentHandler": {"url": "https://docs.google.com/document/d/r/edit"}},
                                {"title": "Essay Prompt", "contentHandler": {"url": "https://docs.google.com/document/d/p/edit"}},
                                {"title": "Reading list", "contentHandler": {"url": "https://example.com"}}]}
        with patch("scrapers.assignment_view._api_get", side_effect=[{"title": "Ghost Dog"}, children]):
            resources = _related_resources("course", content, "cookie")
        self.assertEqual([r["title"] for r in resources], ["Essay Prompt"])
        with patch("scrapers.assignment_view._api_get", return_value={"title": "Assignments"}) as api:
            self.assertEqual(_related_resources("course", content, "cookie"), [])
            self.assertEqual(api.call_count, 1)


class TestLeanReads(unittest.TestCase):
    def api(self, path, _cookie):
        if path.endswith("/users/me"):
            return {"id": "me"}
        if path.endswith("gradebook/columns"):
            return {"results": [{"id": "column", "contentId": "content", "name": "Test", "scoreProviderHandle": "resource/x-bb-assessment"}]}
        if "/contents/content" in path:
            return {"contentDetail": {"resource/x-bb-asmt-test-link": {"test": {"deployedAssessmentType": "test", "assessment": {"questionCount": 3}}}}}
        if "/grades?" in path:
            return {"results": [{"id": "grade", "status": "COMPLETED"}]}
        if path.endswith("/attempts"):
            return {"results": [{"id": "attempt", "status": "COMPLETED"}]}
        if "/gradebook/attempts/" in path:
            self.fail("Lean reads must not fetch previous questions or answers")
        return {}

    def test_lean_test_read_discovers_attempt_without_fetching_answers(self):
        with patch("scrapers.quiz._get_cookie_header", return_value="cookie"), \
             patch("scrapers.quiz.load_courses", return_value={"course": "Course"}), \
             patch("scrapers.quiz._api_get", side_effect=self.api):
            data = _scrape_assessment_http("course", assessment_id="content", include_work=False)
        self.assertEqual(data["attempt_id"], "attempt")
        self.assertEqual(data["question_count"], 3)
        self.assertEqual(data["questions"], [])

    def test_lean_discussion_does_not_fetch_post_bodies(self):
        def api(path, cookie):
            if path.endswith("gradebook/columns"):
                return {"results": [{"id": "column", "contentId": "content", "name": "Discussion", "scoreProviderHandle": "resource/x-bb-forumlink"}]}
            if path.endswith("/discussions"):
                return {"results": [{"id": "discussion", "gradebookColumnId": "column", "topic": {"body": "Prompt"}}]}
            if "/messages" in path:
                self.fail("Lean reads must not fetch discussion post bodies")
            return self.api(path, cookie)
        with patch("scrapers.quiz._get_cookie_header", return_value="cookie"), \
             patch("scrapers.quiz.load_courses", return_value={"course": "Course"}), \
             patch("scrapers.quiz._api_get", side_effect=api):
            data = _scrape_assessment_http("course", assessment_id="content", include_work=False)
        self.assertEqual(data["instructions"], "Prompt")


class TestCLIViews(unittest.TestCase):
    def test_modes_are_exclusive_and_apply_to_aliases(self):
        for alias in ("assignment", "quiz", "asmt", "assessment"):
            self.assertEqual(main._parse_args([alias, "Example"]).assignment_view, "default")
            for flag in ("instructions", "rubric", "full"):
                self.assertEqual(main._parse_args([alias, "Example", "--" + flag]).assignment_view, flag)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main._parse_args(["assignment", "Example", "--rubric", "--instructions"])

    def test_legacy_flags_accept_view_modes(self):
        for alias in ("--assignment", "--quiz", "--asmt", "--assessment"):
            args = main._parse_args([alias, "Example", "--rubric"])
            self.assertEqual(args.assignment_view, "rubric")

    def test_focused_modes_cannot_start_an_attempt(self):
        for flag in ("--instructions", "--rubric"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main._parse_args(["assignment", "Example", flag, "--start-attempt"])

    def test_cli_passes_read_scope_and_projects_json(self):
        for flag, include_work in ((None, False), ("--instructions", False), ("--rubric", False), ("--full", True)):
            args = main._parse_args(["assignment", "Example", "-c", "EXAMPLE", "--json"] + ([flag] if flag else []))
            with patch("main.load_courses", return_value={"course": "EXAMPLE 100"}), \
                 patch("main._require_session_async", new=AsyncMock(return_value=True)), \
                 patch("main.scrape_assessment_attempt_async", new=AsyncMock(return_value={"title": "Example"})) as scrape, \
                 patch("main.prepare_assignment_view", return_value={"title": "Example", "view": flag or "default"}), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                asyncio.run(main.main_async(args))
            self.assertEqual(scrape.call_args.kwargs["include_work"], include_work)
            self.assertFalse(scrape.call_args.kwargs["allow_start"])
            self.assertEqual(json.loads(output.getvalue())["title"], "Example")


if __name__ == "__main__":
    unittest.main()
