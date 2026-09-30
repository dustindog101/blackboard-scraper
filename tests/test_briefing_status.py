import unittest
from datetime import datetime, timedelta, timezone

from scrapers.briefing import build_briefing_sections, format_briefing_cli, briefing_icon
from telegram.formatter import format_daily_briefing


class BriefingStatusTests(unittest.TestCase):
    def test_stale_activity_and_completion_do_not_define_urgency(self):
        now = datetime.now(timezone.utc)
        def row(title, days, **fields):
            return {'name': title, 'raw_due': (now + timedelta(days=days)).isoformat(), **fields}
        bundle = {'activity': [{'title': 'Past due: Finished'}, {'title': 'Due: Far future'}], 'calendar': [],
                  'courses': {'synthetic': {'course_name': 'Synthetic', 'announcements': [
                      {'title': 'News', 'unread': True}], 'grades': [
                          row('Finished', -2, completed=True, submission_status='GRADED', status='Graded'),
                          row('Late', -1, completed=False), row('Soon', 1, completed=False),
                          row('Week', 4, completed=False), row('Far future', 11, completed=False),
                          row('Submitted', -3, completed=True, submission_status='NEEDS_GRADING'),
                          row('New grade', -2, completed=True, posted=True, score=17, grade='17 / 20',
                              has_been_viewed=False), {'name': 'Publisher'}]}}}
        sections = build_briefing_sections(bundle, now=now)
        self.assertEqual([i['title'] for i in sections['overdue']], ['Late'])
        self.assertEqual([i['title'] for i in sections['due_soon']], ['Soon'])
        self.assertEqual([i['title'] for i in sections['this_week']], ['Week'])
        self.assertEqual([i['title'] for i in sections['awaiting_grade']], ['Submitted'])
        self.assertEqual([i['title'] for i in sections['newly_graded']], ['New grade'])
        self.assertEqual([i['title'] for i in sections['untracked']], ['Publisher'])
        text = format_briefing_cli(bundle)
        telegram = '\n'.join(format_daily_briefing(bundle))
        for heading in ('Overdue', 'Due within 48 h', 'This week', 'Submitted, awaiting grade',
                        'Newly graded', 'Unread announcements', 'Not tracked by Blackboard'):
            self.assertIn(heading, text)
            self.assertIn(heading, telegram)
        self.assertNotIn('Past due: Finished', text)
        self.assertEqual(briefing_icon(bundle), '🔴')

    def test_empty_sections_omitted(self):
        self.assertNotIn('Overdue', format_briefing_cli({'courses': {}}))
        self.assertEqual(briefing_icon({'courses': {}}), '🟢')
