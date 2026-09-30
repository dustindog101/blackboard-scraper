import argparse
import contextlib
import io
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import main


class FakeSession:
    def __init__(self, *args, **kwargs):
        pass

    async def initialize(self):
        pass

    async def close(self):
        pass

    @contextlib.asynccontextmanager
    async def acquire_page(self):
        yield object()


class FakePool:
    def __init__(self, *args, **kwargs):
        pass

    async def execute_task_per_course(self, courses, worker):
        return {cid: await worker(cid, name, object()) for cid, name in courses.items()}


class MachineOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_json_handler_is_machine_clean(self):
        parser = main._build_parser()
        commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
        required = {'search': ['query'], 'find': ['query'], 'download': ['file'], 'grab': ['file'],
                    'get': ['file'], 'assignment': ['item'], 'quiz': ['item'], 'asmt': ['item'],
                    'assessment': ['item']}
        async def noisy(*args, **kwargs):
            from core.output import status
            status('\033[32mProgress\033[0m')
            return []
        with contextlib.ExitStack() as stack:
            for name in ('scrape_grades_async', 'scrape_announcements_async', 'scrape_calendar_async',
                         'scrape_activity_async', 'scrape_course_assignments_async', 'scrape_course_outline_async',
                         'find_items_async'):
                if hasattr(main, name):
                    stack.enter_context(patch.object(main, name, side_effect=noisy))
            stack.enter_context(patch.object(main, 'scrape_profile_async', AsyncMock(return_value={'name': 'Synthetic'})))
            stack.enter_context(patch.object(main, 'run_briefing_async', AsyncMock(return_value={'courses': {}, 'calendar': [], 'activity': []})))
            stack.enter_context(patch.object(main, 'aggregate_due_dates_async', AsyncMock(return_value=[])))
            stack.enter_context(patch.object(main, 'scrape_assessment_attempt_async', AsyncMock(return_value={'title': 'Synthetic'})))
            stack.enter_context(patch.object(main, 'grab_item_async', AsyncMock(return_value={'title': 'Synthetic'})))
            stack.enter_context(patch.object(main, '_run_discussions_sync', return_value=[]))
            stack.enter_context(patch.object(main, 'load_courses', return_value={'synthetic': 'SYN101'}))
            stack.enter_context(patch.object(main, '_require_session', return_value=True))
            stack.enter_context(patch.object(main, '_require_session_async', AsyncMock(return_value=True)))
            stack.enter_context(patch.object(main, 'AsyncSessionManager', FakeSession))
            stack.enter_context(patch.object(main, 'AsyncCourseWorkerPool', FakePool))
            stack.enter_context(patch('core.course_discovery.discover_courses_via_api', return_value={'Fall 2026': {'synthetic': 'SYN101'}}))
            stack.enter_context(patch('core.course_discovery.save_courses'))
            seen = set()
            for name, command in commands.items():
                if id(command) in seen or not any('--json' in a.option_strings for a in command._actions):
                    continue
                seen.add(id(command))
                with self.subTest(command=name):
                    args = parser.parse_args([name, *required.get(name, []), '--json', '--all']
                                             if name in ('assignments', 'outline', 'discussions') else
                                             [name, *required.get(name, []), '--json'])
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        await main.main_async(args)
                    json.loads(out.getvalue())
                    self.assertNotIn('\033', out.getvalue() + err.getvalue())

    async def test_terms_out_writes_array(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('core.course_discovery.discover_courses_via_api', return_value={'Fall 2026': {'synthetic': 'SYN101'}}):
            path = directory + '/terms.json'
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                await main.main_async(main._build_parser().parse_args(['terms', '--out', path]))
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(set(data[0]), {'term', 'active', 'courses'})

    async def test_empty_profile_is_still_json(self):
        with patch.object(main, 'scrape_profile_async', AsyncMock(return_value={})), \
                patch.object(main, '_require_session_async', AsyncMock(return_value=True)), \
                patch.object(main, 'load_courses', return_value={'synthetic': 'SYN101'}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                await main.main_async(main._build_parser().parse_args(['profile', '--json']))
            self.assertEqual(json.loads(out.getvalue()), {})
