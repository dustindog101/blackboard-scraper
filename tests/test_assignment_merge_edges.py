import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tests import test_assignment_views as fixtures

BASE = fixtures.BASE
association = fixtures.association
from scrapers.assignment_view import prepare_assignment_view, format_assignment_view, _related_resources
from scrapers.quiz import scrape_assessment_attempt_async, _scrape_assessment_playwright


class TestRubricEdgeCases(unittest.TestCase):
    prepare = fixtures.TestViews.prepare
    def test_null_optional_rubric_collections_do_not_crash(self):
        for value in (None, [], [None, "bad"], "bad"):
            result = self.prepare("rubric", response={"rubricAssociations": value})
            self.assertEqual(result["rubric"]["status"], "none")

    def test_visible_rubric_without_definition_reports_unavailable_details(self):
        for definition in (None, {}, {"rows": None}, {"rows": []}):
            column = {"rubricAssociations": [{"displayBefore": True, "rubricDefinition": definition}]}
            result = self.prepare("rubric", response=column)
            self.assertEqual(result["rubric"]["status"], "unknown")
            self.assertTrue(result["rubric"]["display_before"])
            self.assertIn("scoring details could not be retrieved", format_assignment_view(result))

    def test_null_cells_headers_positions_and_scores_remain_readable(self):
        raw = association()
        raw["rubricDefinition"]["columnHeaders"] = None
        raw["rubricDefinition"]["rows"][0].update(cells=None, position=None, percentage="invalid")
        result = self.prepare("rubric", response={"rubricAssociations": [raw]})
        self.assertIn("scoring details could not be retrieved", format_assignment_view(result))
        self.assertNotIn("weight_percent", result["rubric"]["definitions"][0]["criteria"][0])

    def test_partial_definitions_keep_good_details_and_report_missing_part(self):
        result = self.prepare("rubric", response={"rubricAssociations": [association(), {"displayBefore": True}]})
        output = format_assignment_view(result)
        self.assertIn("PRIVATE_RUBRIC_DETAILS", output)
        self.assertIn("scoring details could not be retrieved", output)

    def test_standalone_links_are_rendered_in_instructions_and_full(self):
        data = {**BASE, "links": [{"text": "Resource", "url": "https://example.com/resource"}]}
        for mode in ("default", "instructions", "full"):
            self.assertIn("Resource — https://example.com/resource", format_assignment_view(self.prepare(mode, data=data)))


class TestCompleteResourceReads(unittest.TestCase):
    def prepare(self, mode, kind="instructions"):
        resources = [{"kind": kind, "title": f"Document {i}", "url": f"https://docs.google.com/document/d/doc{i}/edit"} for i in range(4)]
        with patch("scrapers.assignment_view._get_cookie_header", return_value="cookie"), \
             patch("scrapers.assignment_view._api_get", side_effect=[{"rubricAssociations": []}, {"parentId": "folder"}]), \
             patch("scrapers.assignment_view._related_resources", return_value=resources), \
             patch("scrapers.assignment_view._read_document", side_effect=lambda resource, cookie, limit: {"status": "available", "text": resource["title"], "truncated": False}) as read:
            data = prepare_assignment_view({**BASE, "instructions": "", "links": []}, mode)
        return data, read.call_count

    def test_default_documents_are_bounded_but_omissions_are_explicit(self):
        result, reads = self.prepare("default")
        self.assertEqual(reads, 2)
        self.assertEqual(len(result["instruction_sources"]), 4)
        self.assertIn("Additional instruction document", format_assignment_view(result))

    def test_focused_and_full_instruction_views_read_all_explicit_documents(self):
        for mode in ("instructions", "full"):
            result, reads = self.prepare(mode)
            self.assertEqual(reads, 4)
            for i in range(4):
                self.assertIn(f"Document {i}", result["instructions"])

    def test_focused_and_full_rubric_views_read_all_explicit_documents(self):
        for mode in ("rubric", "full"):
            result, reads = self.prepare(mode, "rubric")
            self.assertEqual(reads, 4)
            self.assertEqual(len(result["rubric"]["documents"]), 4)

    def test_duplicate_resources_are_read_once(self):
        resource = {"kind": "rubric", "title": "Rubric", "url": "https://docs.google.com/document/d/doc/edit"}
        with patch("scrapers.assignment_view._get_cookie_header", return_value="cookie"), \
             patch("scrapers.assignment_view._api_get", side_effect=[{"rubricAssociations": []}, {"parentId": "folder"}]), \
             patch("scrapers.assignment_view._related_resources", return_value=[resource, resource]), \
             patch("scrapers.assignment_view._read_document", return_value={"status": "available", "text": "Rubric"}) as read:
            prepare_assignment_view({**BASE, "instructions": "", "links": []}, "rubric")
        self.assertEqual(read.call_count, 1)

    def test_matching_folder_resources_follow_pagination(self):
        with patch("scrapers.assignment_view._api_get", side_effect=[
                {"title": "Ghost Dog"},
                {"results": [{"title": "Essay Prompt", "availability": None, "contentHandler": {"url": "https://example.com/p"}}], "paging": {"nextPage": "/page2"}},
                {"results": [{"title": "Rubric!", "contentHandler": {"url": "https://example.com/r"}}, {"title": None, "contentHandler": None}], "paging": {"nextPage": "/page2"}}]):
            resources = _related_resources("course", {"title": "Ghost Dog Final", "parentId": "folder"}, "cookie")
        self.assertEqual([r["kind"] for r in resources], ["instructions", "rubric"])


class TestLeanBrowser(unittest.TestCase):
    def test_existing_attempt_url_is_replaced_with_preview_and_scope_forwarded(self):
        page = MagicMock()
        with patch("scrapers.quiz._scrape_assessment_playwright", new=AsyncMock(return_value={"title": "Example"})) as browser:
            result = asyncio.run(scrape_assessment_attempt_async(
                "https://blackboard.umbc.edu/ultra/courses/_1_1/assessment/_2_1/attempt/_3_1", page=page, force_browser=True, include_work=False))
        self.assertEqual(result["title"], "Example")
        self.assertIn("/ultra/course/assessment/_2_1/open?courseId=_1_1", browser.call_args.args[0])
        self.assertFalse(browser.call_args.kwargs["include_work"])
        self.assertFalse(browser.call_args.kwargs["allow_start"])

    def test_browser_evaluation_disables_question_body_extraction_and_start(self):
        page = MagicMock()
        page.goto = AsyncMock()
        page.locator.return_value.first.is_visible = AsyncMock(return_value=True)
        page.locator.return_value.first.click = AsyncMock()
        page.evaluate = AsyncMock(return_value={"title": "Example", "questions": [], "question_count": 0})
        with patch("scrapers.quiz.asyncio.sleep", new=AsyncMock()), patch("scrapers.quiz.load_courses", return_value={}):
            asyncio.run(_scrape_assessment_playwright("https://example.com/preview", page, include_work=False))
        script, options = page.evaluate.call_args.args
        self.assertEqual(options, {"includeWork": False})
        self.assertIn("const questionNodes = includeWork ?", script)
        self.assertIn('|| (includeWork ? document.body.innerText : "")', script)
        page.locator.return_value.first.click.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
