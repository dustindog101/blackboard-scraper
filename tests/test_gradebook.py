import unittest
from unittest.mock import patch

from scrapers.grades import scrape_grades_api


class GradebookTests(unittest.TestCase):
    def fetch(self, records, columns=None):
        columns = columns or [{'id': k, 'name': k, 'score': {'possible': 20}} for k in records]
        def api(path, cookies):
            if path.endswith('users/me'):
                return {'id': 'synthetic-user'}
            if path.endswith('/columns'):
                return {'results': columns}
            if '/grades?' in path:
                return {'results': records[path.split('/columns/')[1].split('/')[0]]}
            return {'results': []}
        with patch('scrapers.grades.get_cookie_header', return_value='synthetic-cookie'), \
                patch('scrapers.grades._api_get', side_effect=api):
            return scrape_grades_api('synthetic-course')

    def test_posted_unposted_and_missing(self):
        rows = self.fetch({
            'posted': [{'status': 'GRADED', 'effectiveScore': 17, 'manualStatus': 'IS_POSTED',
                        'instructorFeedback': '<p>Good reasoning</p>', 'displayGrade': {'displayValue': 'B'}}],
            'hidden': [{'status': 'GRADED', 'effectiveScore': 19}],
            'submitted': [{'status': 'NEEDS_GRADING'}],
            'empty': [],
        })
        by = {r['name']: r for r in rows}
        self.assertEqual(by['posted']['score'], 17)
        self.assertIn('17 / 20', by['posted']['grade'])
        self.assertEqual(by['posted']['feedback'], 'Good reasoning')
        self.assertEqual(by['hidden']['status'], 'Graded (not yet posted)')
        self.assertIsNone(by['hidden']['score'])
        self.assertEqual(by['submitted']['status'], 'Submitted')
        self.assertEqual(by['empty']['status'], 'Not attempted')

    def test_totals_are_skipped(self):
        self.assertEqual(self.fetch({}, [{'id': 'total', 'name': 'Total Points Earned'}]), [])

    def test_discussion_without_posts_is_not_completed(self):
        rows = self.fetch({'board': [{'status': 'NEEDS_GRADING'}]}, [
            {'id': 'board', 'name': 'Board', 'scoreProviderHandle': 'resource/x-bb-forumlink'}])
        self.assertFalse(rows[0]['completed'])
        self.assertNotEqual(rows[0]['status'], 'Submitted')

    def test_closed_course_returns_empty(self):
        with patch('scrapers.grades.get_cookie_header', return_value='cookie'), \
                patch('scrapers.grades._api_get', return_value={'_http_status': 403}):
            self.assertEqual(scrape_grades_api('closed'), [])
