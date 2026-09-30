import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from scrapers.due_dates import aggregate_due_dates_async


class DueStatusTests(unittest.IsolatedAsyncioTestCase):
    async def aggregate(self, window='all', exclude=False, include=False):
        now = datetime.now(timezone.utc)
        past = (now - timedelta(days=2)).isoformat()
        future = (now + timedelta(days=2)).isoformat()
        records = {
            'a': [{'name': 'Homework', 'column_id': 'col-a', 'raw_due': past, 'status': 'Graded',
                   'completed': True, 'submission_status': 'GRADED'},
                  {'name': 'Publisher', 'completed': False},
                  {'name': 'Future', 'raw_due': future, 'completed': False}],
            'b': [{'name': 'Homework', 'column_id': 'col-b', 'raw_due': past, 'status': 'Not attempted',
                   'completed': False, 'submission_status': 'NOT_ATTEMPTED'}],
        }
        with patch('scrapers.due_dates.scrape_calendar_async', AsyncMock(return_value=[
            {'title': 'Homework', 'course': 'Course A', 'course_id': 'a', 'raw_due': past}
        ])), patch('scrapers.due_dates.scrape_grades_async', AsyncMock(side_effect=lambda cid: records[cid])):
            return await aggregate_due_dates_async(courses={'a': 'Course A', 'b': 'Course B'},
                                                   window_filter=window, exclude_completed=exclude,
                                                   include_completed=include)

    async def test_overdue_excludes_completed_and_undated(self):
        rows = await self.aggregate('overdue')
        self.assertEqual([(r['course_id'], r['title']) for r in rows], [('b', 'Homework')])

    async def test_include_completed_restores_past_due(self):
        self.assertEqual(len(await self.aggregate('overdue', include=True)), 2)

    async def test_same_title_stays_in_two_courses_and_calendar_inherits_status(self):
        rows = await self.aggregate()
        homework = [r for r in rows if r['title'] == 'Homework']
        self.assertEqual(len(homework), 2)
        self.assertTrue(next(r for r in homework if r['course_id'] == 'a')['completed'])
        self.assertFalse(next(r for r in rows if r['title'] == 'Publisher')['tracked'])

    async def test_exclude_completed_all_and_future_window(self):
        self.assertFalse(any(r['completed'] for r in await self.aggregate(exclude=True)))
        rows = await self.aggregate('7d', exclude=True)
        self.assertEqual([r['title'] for r in rows], ['Future'])

    def test_window_boundaries(self):
        from scrapers.due_dates import merge_due_items
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [{'name': name, 'raw_due': (now + timedelta(days=offset)).isoformat()}
                for name, offset in [('past', -0.01), ('now', 0), ('edge', 7), ('outside', 7.01)]]
        result = merge_due_items([], {'a': rows}, {'a': 'A'}, '7d', now=now)
        self.assertEqual([r['title'] for r in result], ['now', 'edge'])
