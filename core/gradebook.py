"""Shared, GET-only Gradable Item status reconciliation."""
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from scrapers.quiz import _api_get, _clean_html_text

_user_lock = Lock()
_user_cache = {}
TOTAL_NAMES = {'overall grade', 'total', 'weighted total', 'total points earned'}


def current_user(cookie_header, api_get=_api_get):
    # One identity request per command, shared by concurrent course workers.
    key = (cookie_header, id(api_get))
    with _user_lock:
        if key not in _user_cache:
            me = api_get('/learn/api/v1/users/me', cookie_header) or {}
            _user_cache[key] = me.get('id')
        return _user_cache[key]


def fetch_user_grades(course_id, columns, cookie_header, api_get=_api_get):
    user_id = current_user(cookie_header, api_get)
    discussions = None
    if any('forum' in c.get('scoreProviderHandle', '') or 'discussion' in c.get('scoreProviderHandle', '')
           for c in columns):
        discussions = api_get(f'/learn/api/public/v1/courses/{course_id}/discussions', cookie_header)

    def fetch(column):
        cid = column.get('id')
        response = api_get(
            f'/learn/api/v1/courses/{course_id}/gradebook/columns/{cid}/grades?userId={user_id}',
            cookie_header,
        ) if user_id and cid else None
        records = (response or {}).get('results', [])
        record = records[0] if records else {}
        raw = record.get('status') or 'NOT_ATTEMPTED'
        completed = raw in ('GRADED', 'NEEDS_GRADING', 'COMPLETED')
        status = {'GRADED': 'Graded', 'NEEDS_GRADING': 'Submitted', 'IN_PROGRESS': 'In progress',
                  'COMPLETED': 'Completed'}.get(raw, 'Not attempted')
        posts = None
        handler = column.get('scoreProviderHandle', '')
        if ('forum' in handler or 'discussion' in handler) and completed:
            discussion = next((d for d in (discussions or {}).get('results', [])
                               if d.get('gradebookColumnId') == cid), None)
            if discussion and user_id:
                messages = api_get(
                    f"/learn/api/public/v1/courses/{course_id}/discussions/{discussion['id']}/messages?limit=100",
                    cookie_header,
                )
                if messages and 'results' in messages and not messages.get('paging', {}).get('nextPage'):
                    posts = sum(m.get('userId') == user_id and m.get('status') != 'DRAFT'
                                for m in messages['results'])
            if not posts:
                completed = False
                status = 'Not attempted (0 posts)' if posts == 0 else 'Submission unverified'
        display = record.get('displayGrade')
        posted = record.get('manualStatus') == 'IS_POSTED' or bool(display)
        score = record.get('effectiveScore', record.get('manualScore')) if posted else None
        possible = record.get('pointsPossible', column.get('score', {}).get('possible'))
        if raw == 'GRADED' and not posted and completed:
            status = 'Graded (not yet posted)'
        display_text = (display.get('displayValue') or display.get('text') or display.get('value')) \
            if isinstance(display, dict) else display
        grade = f'{score:g} / {possible:g}' if isinstance(score, (int, float)) and isinstance(possible, (int, float)) \
            else str(score) if score is not None else status
        if posted and display_text:
            grade += f' ({display_text})'
        feedback = record.get('instructorFeedback') if posted else None
        if isinstance(feedback, dict):
            feedback = feedback.get('displayText') or feedback.get('rawText') or ''
        return cid, {
            'submission_status': raw, 'status': status, 'completed': completed, 'score': score,
            'display_grade': display if posted else None, 'feedback': _clean_html_text(feedback) if feedback else None,
            'posted': posted, 'grade': grade, 'points_possible': possible, 'column_id': cid,
            'content_id': column.get('contentId'), 'posts_from_you': posts,
            'has_been_viewed': record.get('hasBeenViewedByStudent'),
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(fetch, columns))


def newly_graded(item):
    """A posted grade unseen by the student is actionable as new."""
    return item.get('posted') and item.get('score') is not None and item.get('has_been_viewed') is False
