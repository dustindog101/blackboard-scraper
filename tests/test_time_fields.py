import unittest
from unittest.mock import patch

from core.time import parse_datetime, due_at, format_local


class TimeFieldsTests(unittest.TestCase):
    @patch.dict('os.environ', {'BB_TZ': 'America/New_York'})
    def test_spring_dst_and_fall_dst(self):
        self.assertIn('EST', format_local('2026-03-08T06:59:00Z'))
        self.assertIn('EDT', format_local('2026-03-08T07:01:00Z'))
        self.assertIn('EDT', format_local('2026-11-01T05:59:00Z'))
        self.assertIn('EST', format_local('2026-11-01T06:01:00Z'))

    @patch.dict('os.environ', {'BB_TZ': 'America/New_York'})
    def test_midnight_rollover_and_offset(self):
        self.assertEqual(due_at('2026-09-29T23:59:00-04:00'), '2026-09-30T03:59:00Z')
        self.assertIn('11:59 PM EDT', format_local('2026-09-30T03:59:00Z'))
        self.assertEqual(due_at('2026-09-29 20:30 UTC'), '2026-09-29T20:30:00Z')

    def test_missing_and_invalid_date(self):
        self.assertIsNone(due_at('TBD'))
        self.assertIsNone(parse_datetime('unknown'))

    @patch.dict('os.environ', {'BB_TZ': 'UTC'})
    def test_timezone_override(self):
        self.assertIn('UTC', format_local('2026-09-30T03:59:00Z'))

    def test_assignments_uses_grading_due_fallback(self):
        from scrapers.assignments import scrape_course_assignments_http
        def api(path, cookie):
            if path.endswith('/columns'):
                return {'results': [{'id': 'synthetic-col', 'name': 'Synthetic',
                                     'grading': {'due': '2026-09-30T03:59:00Z'}}]}
            if path.endswith('users/me'):
                return {'id': 'synthetic-user'}
            return {'results': []}
        with patch('scrapers.assignments._get_cookie_header', return_value='synthetic-cookie'), \
                patch('scrapers.assignments._api_get', side_effect=api):
            rows = scrape_course_assignments_http('synthetic-course')
        self.assertEqual(rows[0]['due_at'], '2026-09-30T03:59:00Z')

    def test_export_boundary_adds_iso_to_legacy_fallback(self):
        from core.time import add_due_fields
        result = add_due_fields({'items': [{'due_date': '2026-09-29 20:30 UTC'}]})
        self.assertEqual(result['items'][0]['due_at'], '2026-09-29T20:30:00Z')
