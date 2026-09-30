import contextlib
import io
import json
import re
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

import main
from core.export_json import build_composite_schema
from core.time import add_due_fields
from scrapers.outline import clean_outline_json
from scrapers.due_dates import merge_due_items
from scrapers.search import find_items_async


def keys_shape(value):
    if isinstance(value, dict):
        return {key: keys_shape(child) for key, child in value.items()}
    if isinstance(value, list):
        return [keys_shape(value[0])] if value else []
    return None


class DocumentedSchemaTests(unittest.TestCase):
    def test_guide_schema_examples_match_fixture_outputs(self):
        guide = main.HELP_GUIDES['schema']
        snippets = re.findall(r'```json\n(.*?)\n```', guide, re.S)
        self.assertEqual(len(snippets), 3, 'Document each schema example as valid fenced JSON')
        actual = [
            json.loads(build_composite_schema({'courses': {}, 'calendar': [], 'activity': []})),
            add_due_fields(merge_due_items([], {'synthetic-course': [
                {'name': 'Synthetic', 'raw_due': '2026-01-02T12:00:00Z',
                 'dueDate': '1/2/26, 7:00 AM EST', 'status': 'Not attempted',
                 'submission_status': 'NOT_ATTEMPTED', 'completed': False}]},
                {'synthetic-course': 'Synthetic Course'}, now=datetime(2026, 1, 1, tzinfo=timezone.utc))),
            [{'course_id': 'synthetic-course', 'course_name': 'Synthetic Course', 'items': clean_outline_json([
                {'content_id': 'synthetic-node', 'title': 'Synthetic Folder', 'content_type': 'folder'}])}],
        ]
        for documented, output in zip(snippets, actual):
            self.assertEqual(keys_shape(json.loads(documented)), keys_shape(output))
        from pathlib import Path
        readme = Path('README.md').read_text()
        self.assertIn(snippets[0], readme)

    def test_auth_guidance_uses_session_checks(self):
        self.assertNotIn('weeks to months', main.HELP_GUIDES['auth'])
        self.assertIn('bb check', main.HELP_GUIDES['auth'])


class SearchSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_emits_canonical_type(self):
        with patch('scrapers.search.scrape_course_outline_async', AsyncMock(return_value=[
            {'content_id': 'synthetic-node', 'title': 'Synthetic', 'content_type': 'folder'}])), \
                contextlib.redirect_stdout(io.StringIO()):
            rows = await find_items_async('Synthetic', {'synthetic-course': 'Synthetic'})
        self.assertEqual(rows[0]['type'], 'folder')
