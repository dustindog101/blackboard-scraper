import unittest
from datetime import datetime, timezone

from scrapers.announcements import filter_announcements, announcement_label


class AnnouncementFilterTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 30, tzinfo=timezone.utc)
        self.rows = [
            {'title': 'Old', 'created': '2026-03-01T00:00:00Z', 'unread': True},
            {'title': 'Recent read', 'created': '2026-09-29T00:00:00Z', 'unread': False},
            {'title': 'Middle', 'created': '2026-09-20T00:00:00Z', 'unread': True},
        ]

    def test_interleaved_dates_sort_and_limit(self):
        self.assertEqual([r['title'] for r in filter_announcements(self.rows, limit=2)], ['Recent read', 'Middle'])

    def test_unread_and_relative_since(self):
        for since in ('14d', '2w', '2026-09-01'):
            self.assertEqual([r['title'] for r in filter_announcements(self.rows, unread=True, since=since,
                                                                      now=self.now)], ['Middle'])

    def test_older_term_and_age(self):
        label = announcement_label(self.rows[0], self.now, datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertIn('(older term)', label)
        self.assertIn('ago', label)
        self.assertTrue(label.startswith('🆕'))

    def test_invalid_filter_is_error(self):
        with self.assertRaises(ValueError):
            filter_announcements(self.rows, since='garbage')
        with self.assertRaises(ValueError):
            filter_announcements(self.rows, limit=-1)


class AnnouncementReadStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_unread_checks_browser_and_preserves_rest_dates(self):
        from unittest.mock import patch, AsyncMock
        from scrapers.announcements import scrape_announcements_async
        rest = [{'title': 'Synthetic', 'created': '2026-09-29T00:00:00Z', 'unread': None}]
        with patch('scrapers.announcements.scrape_announcements_api', return_value=rest), \
                patch('scrapers.announcements.scrape_announcements_playwright_async',
                      AsyncMock(return_value=[{'title': 'Synthetic', 'unread': True}])) as browser:
            rows = await scrape_announcements_async('synthetic', object(), verify_unread=True)
        browser.assert_awaited_once()
        self.assertTrue(rows[0]['unread'])
        self.assertEqual(rows[0]['created'], rest[0]['created'])

    async def test_unknown_unread_is_not_guessed_from_age(self):
        from unittest.mock import patch, AsyncMock
        from scrapers.announcements import scrape_announcements_async
        rest = [{'title': 'Synthetic', 'created': '2026-09-29T00:00:00Z', 'unread': None}]
        with patch('scrapers.announcements.scrape_announcements_api', return_value=rest), \
                patch('scrapers.announcements.scrape_announcements_playwright_async', AsyncMock(return_value=[])):
            rows = await scrape_announcements_async('synthetic', object(), verify_unread=True)
        self.assertEqual(filter_announcements(rows, unread=True), [])
