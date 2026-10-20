"""Submission claims must agree with attempt metadata, not aggregate gradebook labels."""
import unittest

from core import gradebook as gb
from scrapers import due_dates


COLUMN = {"id": "column", "name": "Library Research Module", "scoreProviderHandle": "resource/x-bb-assessment",
          "score": {"possible": 10}, "grading": {"type": "Attempts", "due": "2026-01-01T12:00:00Z"}}
RECORD = {"columnId": "column", "status": "NeedsGrading", "exempt": False}


class AttemptApi:
    def __init__(self, attempts, error=None, aggregate="NeedsGrading"):
        self.attempts, self.error, self.aggregate = attempts, error, aggregate
        self.paths = []

    def __call__(self, path, cookie):
        self.paths.append(path)
        if path.endswith("/users/me"):
            return {"id": "student"}
        if path.endswith("/gradebook/columns"):
            return {"results": [COLUMN]}
        if "/gradebook/users/" in path:
            return {"results": [{**RECORD, "status": self.aggregate}]}
        if path.endswith("/gradebook/schemas"):
            return {"results": []}
        if "/grades?userId=" in path:
            return {"results": [{"id": "grade", "userId": "student", "columnId": "column", "courseId": "course"}]}
        if path.endswith("/grades/grade/attempts"):
            if self.error:
                return {"_http_status": self.error}
            return {"results": [{"userId": "student", "courseId": "course", "gradeId": "grade", **a}
                                for a in self.attempts]}
        return {"_http_status": 404}


class TestAttemptEvidence(unittest.TestCase):
    def setUp(self):
        gb._user_cache.clear()

    def book(self, attempts, **kwargs):
        api = AttemptApi(attempts, **kwargs)
        book = gb.fetch_gradebooks({"course": "ENGL 100"}, "cookie", api)["course"]
        return book, api

    def test_live_shape_needs_grading_can_mean_in_progress(self):
        book, api = self.book([{"status": "IN_PROGRESS", "completedDate": "2026-01-01T10:00:00Z"}])
        self.assertEqual(book["items"][0]["status"], "in_progress")
        self.assertNotIn(book["items"][0]["status"], gb.COMPLETED)
        self.assertTrue(any(p.endswith("/attempts") for p in api.paths))
        self.assertFalse(any("toolAttemptDetail" in p for p in api.paths))
        entries = due_dates.reconcile({"course": book}, [], {"course": "ENGL 100"})
        self.assertEqual(len(due_dates.select(entries, "overdue", True)), 1)

    def test_unverified_aggregate_status_is_unknown(self):
        for raw in ("NeedsGrading", "ATTEMPTED", "COMPLETED", "SUBMITTED", "GRADED"):
            with self.subTest(raw=raw):
                self.assertEqual(gb.item_status(COLUMN, {**RECORD, "status": raw}), "unknown")

    def test_verification_failure_never_marks_done(self):
        book, _ = self.book([], error=403)
        self.assertEqual(book["items"][0]["status"], "unknown")

    def test_final_attempt_is_awaiting_grade(self):
        book, _ = self.book([{"status": "NEEDS_GRADING", "attemptReceipt": {"submissionDate": "2026-01-01T10:00:00Z"}}])
        self.assertEqual(book["items"][0]["status"], "needs_grading")

    def test_multiple_attempts_do_not_hide_an_active_draft(self):
        final, draft = {"status": "NEEDS_GRADING"}, {"status": "IN_PROGRESS"}
        for attempts in ([final, draft], [draft, final]):
            book, _ = self.book(attempts)
            self.assertEqual(book["items"][0]["status"], "in_progress")

    def test_final_attempt_does_not_hide_an_unsubmitted_attempt(self):
        for open_state in ("UNSUBMITTED", "NOT_STARTED", "NOT_ATTEMPTED"):
            for attempts in ([{"status": "NEEDS_GRADING"}, {"status": open_state}],
                             [{"status": open_state}, {"status": "NEEDS_GRADING"}]):
                book, _ = self.book(attempts)
                self.assertEqual(book["items"][0]["status"], "not_submitted")

    def test_posted_grade_does_not_hide_an_active_retake_or_lose_points(self):
        record = {**RECORD, "status": "Graded", "score": 8}
        self.assertEqual(gb.item_status(COLUMN, record, attempts=[{"status": "GRADED"}, {"status": "IN_PROGRESS"}]), "in_progress")
        book = gb.build_gradebook("course", "X", [COLUMN], [record], [], attempts={"column": [{"status": "IN_PROGRESS"}]})
        self.assertEqual(book["items"][0]["status"], "in_progress")
        self.assertEqual((book["overall"]["score"], book["overall"]["possible"]), (8, 10))
        self.assertEqual(book["overall"]["graded"], 1)

    def test_failed_native_attempt_check_keeps_grade_points_but_not_completion(self):
        record = {**RECORD, "status": "Graded", "score": 8}
        book = gb.build_gradebook("course", "X", [COLUMN], [record], [], attempts={"column": None})
        self.assertEqual(book["items"][0]["status"], "unknown")
        self.assertEqual(book["overall"]["score"], 8)

    def test_posted_offline_grade_is_not_called_missing_for_lack_of_online_attempts(self):
        record = {**RECORD, "status": "Graded", "score": 8}
        self.assertEqual(gb.item_status(COLUMN, record, attempts=[], attempts_checked=True), "graded")

    def test_attempt_from_another_user_is_not_evidence(self):
        book, _ = self.book([{"status": "NEEDS_GRADING", "userId": "someone-else"}])
        self.assertEqual(book["items"][0]["status"], "unknown")

    def test_zero_is_a_real_grade(self):
        self.assertEqual(gb.item_status(COLUMN, {**RECORD, "status": "Graded", "score": 0}), "graded")

    def test_expired_attempt_read_propagates_auth_failure(self):
        book, _ = self.book([], error=401)
        self.assertEqual(book, {"error": gb.EXPIRED})

    def test_empty_or_unknown_attempt_state_is_not_submitted(self):
        for attempts, expected in (([], "not_submitted"), ([{"status": "ATTEMPTED"}], "unknown"),
                                   ([{"status": None}], "unknown")):
            book, _ = self.book(attempts)
            self.assertEqual(book["items"][0]["status"], expected)

    def test_unverified_status_stays_open_when_verification_disabled(self):
        api = AttemptApi([{"status": "NEEDS_GRADING"}])
        book = gb.fetch_gradebooks({"course": "ENGL 100"}, "cookie", api, verify=False)["course"]
        self.assertEqual(book["items"][0]["status"], "unknown")

    def test_percent_only_total_does_not_invent_points(self):
        total = {"id": "total", "name": "Overall Grade", "grading": {"type": "Calculated"}, "score": {"possible": 100}}
        record = {"columnId": "total", "displayGrade": {"scaleType": "Percent", "score": 94.30555, "possible": 100}}
        overall = gb.build_gradebook("course", "X", [total], [record], [])["overall"]
        self.assertIsNone(overall["score"])
        self.assertIsNone(overall["possible"])
        self.assertEqual(overall["percent"], 94.3)

    def test_draft_text_and_files_are_not_presented_as_a_submission(self):
        from scrapers.assignment_view import format_assignment_view
        data = {"title": "Work", "status": "IN_PROGRESS", "view": "full", "submission": {
            "text": "Saved draft", "files": [{"name": "draft.txt"}], "submitted_at": None}}
        text = format_assignment_view(data)
        self.assertIn("Your draft (not submitted)", text)
        self.assertNotIn("Your submission:", text)


if __name__ == "__main__":
    unittest.main()
