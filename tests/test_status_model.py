"""Gradable Item status model, and grades / due / briefing built on it. All HTTP is mocked."""
import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core import gradebook as gb
from core.time import format_local, local_zone, parse_datetime, to_iso
from scrapers import briefing, due_dates, grades

SCHEMA = {"results": [{"id": "s1", "scaleType": "Tabular", "symbols": [
    {"text": "A", "lowerBound": 90.0}, {"text": "B", "lowerBound": 80.0}, {"text": "C", "lowerBound": 70.0},
    {"text": "F", "lowerBound": 0.0}]}]}
# This tenant only exposes each letter's midpoint (`absoluteValue`).
MIDPOINT_SCHEMA = [{"id": "s", "scaleType": "Tabular", "symbols": [
    {"text": "A+", "absoluteValue": 98.5}, {"text": "A", "absoluteValue": 95.0}, {"text": "A-", "absoluteValue": 91.5},
    {"text": "B+", "absoluteValue": 88.5}, {"text": "F", "absoluteValue": 40.0}]}]


def col(cid, name, handler="resource/x-bb-assignment", possible=10, due=None, **extra):
    c = {"id": cid, "name": name, "scoreProviderHandle": handler, "score": {"possible": possible},
         "grading": {"type": "Attempts", **({"due": due} if due else {})}}
    c.update(extra)
    return c


def calc(cid, name="Overall Grade", possible=100, **extra):
    return {"id": cid, "name": name, "score": {"possible": possible}, "grading": {"type": "Calculated"}, **extra}


def rec(cid, status="GRADED", score=None, **extra):
    r = {"columnId": cid, "status": status, **extra}
    if score is not None:
        r["displayGrade"] = {"scaleType": "Score", "score": score}
    return r


class FakeApi:
    """Routes Blackboard REST paths to canned payloads and records every call."""

    def __init__(self, columns, records, schemas=SCHEMA, status=None, discussions=None, messages=None):
        self.columns, self.records, self.schemas = columns, records, schemas
        self.status, self.discussions, self.messages = status or {}, discussions, messages or {}
        self.calls = []

    def __call__(self, path, cookie, timeout=8.0):
        self.calls.append(path)
        for needle, code in self.status.items():
            if needle in path:
                return {"_http_status": code}
        if path.endswith("/users/me"):
            return {"id": "_me_1"}
        for course, columns in self.columns.items():
            if f"/courses/{course}/gradebook/columns/" in path and "/grades" in path:
                column_id = path.split("/columns/")[1].split("/")[0]
                record = next((r for r in self.records[course] if r.get("columnId") == column_id), None)
                if not record:
                    return {"results": []}
                if "/attempts" in path:
                    return {"results": [{"userId": "_me_1", "courseId": course, "gradeId": "grade-" + column_id,
                                         "status": record.get("status")}]}
                return {"results": [{"id": "grade-" + column_id, "userId": "_me_1", "courseId": course, "columnId": column_id}]}
            if f"/courses/{course}/gradebook/columns" in path:
                return {"results": columns}
            if f"/courses/{course}/gradebook/users/" in path:
                return {"results": self.records[course]}
            if f"/courses/{course}/gradebook/schemas" in path:
                return self.schemas
            if f"/courses/{course}/discussions/" in path and "/messages" in path:
                return {"results": self.messages.get(path.split("/discussions/")[1].split("/")[0], [])}
            if f"/courses/{course}/discussions" in path:
                return {"results": self.discussions or []}
        return {"_http_status": 404}


class TestItemStatus(unittest.TestCase):
    def test_every_status(self):
        c = col("c1", "HW")
        self.assertEqual(gb.item_status(c, rec("c1", "Graded", 9)), "graded")
        self.assertEqual(gb.item_status(c, rec("c1", "NeedsGrading"), attempts=[{"status": "NEEDS_GRADING"}]), "needs_grading")
        self.assertEqual(gb.item_status(c, rec("c1", "COMPLETED"), attempts=[{"status": "COMPLETED"}]), "submitted")
        self.assertEqual(gb.item_status(c, rec("c1", "IN_PROGRESS")), "in_progress")
        self.assertEqual(gb.item_status(c, None), "not_submitted")
        self.assertEqual(gb.item_status(c, rec("c1", "GRADED", 9, exempt=True)), "exempt")

    def test_never_invents_a_status(self):
        c = col("c1", "HW")
        for raw in ("GRADED", "WEIRD", "", "NEEDS_GRADING", None):
            self.assertIn(gb.item_status(c, rec("c1", raw)), gb.STATUSES)
        self.assertEqual(gb.item_status(c, rec("c1", "WEIRD")), "unknown")

    def test_manual_column_without_record_is_unknown(self):
        self.assertEqual(gb.item_status(col("m", "Participation", handler=""), None), "unknown")

    def test_discussion_needs_grading_is_not_proof_of_submission(self):
        d = col("d", "Discussion 1", handler="resource/x-bb-forumlink")
        r = rec("d", "NEEDS_GRADING")
        self.assertEqual(gb.item_status(d, r), "unknown")
        self.assertEqual(gb.item_status(d, r, posts=0), "not_submitted")
        self.assertEqual(gb.item_status(d, r, posts=2), "needs_grading")


class TestBuildGradebook(unittest.TestCase):
    def test_official_total_uses_calculated_column_and_graded_points(self):
        columns = [col("c1", "HW 1", due="2026-10-01T03:59:00Z"), calc("t", possible=440, externalGrade=True)]
        records = [rec("c1", "GRADED", 9),
                   {"columnId": "t", "displayGrade": {"scaleType": "Score", "score": 94.0, "possible": 100.0}}]
        book = gb.build_gradebook("course", "IS 410 DB", columns, records, SCHEMA["results"])
        o = book["overall"]
        self.assertEqual((o["source"], o["label"], o["score"], o["possible"], o["percent"], o["letter"]),
                         ("official", "Overall Grade", 94.0, 100.0, 94.0, None))
        self.assertEqual([i["name"] for i in book["items"]], ["HW 1"])  # calculated columns are never items

    def test_calculated_total_when_no_official_column(self):
        columns = [col("a", "A", possible=10), col("b", "B", possible=20), col("c", "C", possible=10),
                   col("x", "Extra", possible=0), col("e", "Exempt", possible=10)]
        records = [rec("a", "GRADED", 9), rec("b", "GRADED", 14), rec("c", "NEEDS_GRADING"),
                   rec("e", "GRADED", 10, exempt=True)]
        o = gb.build_gradebook("course", "X", columns, records, SCHEMA["results"])["overall"]
        self.assertEqual((o["source"], o["score"], o["possible"], o["percent"], o["letter"]), ("calculated", 23, 30, 76.7, None))
        self.assertEqual((o["graded"], o["total"]), (2, 3))

    def test_excluded_items_with_reasons(self):
        columns = [col("x", "Extra", possible=0), col("e", "Exempt"), col("n", "Skipped", includeInCalculations=False)]
        book = gb.build_gradebook("c", "X", columns, [rec("e", "GRADED", 10, exempt=True)], [])
        self.assertEqual({e["name"]: e["reason"] for e in book["excluded"]},
                         {"Extra": "no points possible", "Exempt": "exempt", "Skipped": "not included in the course total"})

    def test_no_grades_yet(self):
        o = gb.build_gradebook("c", "X", [col("a", "A")], [], SCHEMA["results"])["overall"]
        self.assertEqual((o["source"], o["percent"]), ("none", None))

    def test_never_infers_a_letter_from_grade_schemas(self):
        for schema in (MIDPOINT_SCHEMA, SCHEMA["results"]):
            book = gb.build_gradebook("c", "X", [col("a", "HW"), calc("t")],
                                      [rec("a", "GRADED", 9), rec("t", "GRADED", 95)], schema)
            self.assertIsNone(book["overall"]["letter"])
            calculated = gb.build_gradebook("c", "X", [col("a", "HW")], [rec("a", "GRADED", 9)], schema)
            self.assertIsNone(calculated["overall"]["letter"])

    def test_sorted_by_due_date_and_documented_json_shape(self):
        columns = [col("c", "Late", due="2026-11-01T03:59:00Z"), col("n", "Undated"), col("a", "Early", due="2026-09-01T03:59:00Z")]
        book = gb.build_gradebook("c1", "IS 410 DB", columns, [], [])
        self.assertEqual([i["name"] for i in book["items"]], ["Early", "Late", "Undated"])
        view = gb.public_view(book)
        self.assertEqual(set(view), {"course_id", "course_name", "overall", "items", "excluded"})
        self.assertEqual(set(view["items"][0]), {"name", "category", "due_at", "status", "score", "possible", "percent", "feedback"})
        self.assertEqual(set(view["overall"]), {"source", "label", "score", "possible", "percent", "letter", "graded", "total"})
        self.assertTrue(view["items"][0]["due_at"].endswith("Z"))

    def test_unreadable_gradebook_makes_everything_unknown(self):
        self.assertEqual(gb.build_gradebook("c", "X", [col("a", "A")], None, [])["items"][0]["status"], "unknown")

    def test_due_date_precedence(self):
        c = col("a", "A", due="2026-10-01T03:59:00Z", dueDate="2026-10-02T03:59:00Z")
        self.assertEqual(gb.due_iso(c), "2026-10-02T03:59:00Z")
        self.assertEqual(gb.due_iso(col("b", "B", due="2026-10-01T03:59:00Z")), "2026-10-01T03:59:00Z")

    def test_labels(self):
        self.assertEqual(gb.course_label("IS 410 Intro To Database Design (03.1520/IS610.2303) FA2026"), "IS 410")
        self.assertEqual(gb.course_label("ECON122_2788_F"), "ECON 122")
        self.assertEqual(gb.course_title("IS 410 Intro To Database Design (03.1520) FA2026"), "Intro To Database Design")


class TestFetch(unittest.TestCase):
    def setUp(self):
        gb._user_cache.clear()

    def test_bulk_records_plus_targeted_checks_only_for_ambiguous_items(self):
        columns = {"c1": [col("a", "A"), col("b", "B")], "c2": [col("x", "X")]}
        records = {"c1": [rec("a", "GRADED", 5), rec("b", "NEEDS_GRADING")], "c2": [rec("x", "GRADED", 10)]}
        api = FakeApi(columns, records)
        result = gb.fetch_gradebooks({"c1": "IS 410", "c2": "ECON 122"}, "cookie", api)
        self.assertEqual(result["c1"]["items"][0]["status"], "graded")
        self.assertEqual(sum("/users/me" in p for p in api.calls), 1)
        self.assertEqual(sum("/gradebook/users/_me_1" in p for p in api.calls), 2)
        self.assertEqual(set(p for p in api.calls if "/grades?" in p),
                         {f"/learn/api/v1/courses/{course}/gradebook/columns/{column}/grades?userId=_me_1"
                          for course, column in (("c1", "a"), ("c1", "b"), ("c2", "x"))})
        self.assertFalse(any("/schemas" in p for p in api.calls))

    def test_401_is_expired_not_empty(self):
        api = FakeApi({"c1": []}, {"c1": []}, status={"/gradebook/columns": 401})
        self.assertEqual(gb.fetch_gradebooks({"c1": "X"}, "cookie", api)["c1"], {"error": "expired"})

    def test_403_and_network_error_are_distinct(self):
        api = FakeApi({"c1": []}, {"c1": []}, status={"/gradebook/columns": 403})
        self.assertEqual(gb.fetch_gradebooks({"c1": "X"}, "cookie", api)["c1"]["error"], "forbidden")
        self.assertEqual(gb.fetch_gradebooks({"c1": "X"}, "cookie", lambda p, c: None)["c1"]["error"], "unavailable")

    def test_discussion_verified_against_own_posts(self):
        columns = {"c1": [col("d", "Disc", handler="resource/x-bb-forumlink")]}
        api = FakeApi(columns, {"c1": [rec("d", "NEEDS_GRADING")]}, discussions=[{"id": "t1", "gradebookColumnId": "d"}],
                      messages={"t1": [{"userId": "someone", "status": "PUBLISHED"}]})
        self.assertEqual(gb.fetch_gradebooks({"c1": "X"}, "cookie", api)["c1"]["items"][0]["status"], "not_submitted")
        api.messages["t1"].append({"userId": "_me_1", "status": "PUBLISHED"})
        self.assertEqual(gb.fetch_gradebooks({"c1": "X"}, "cookie", api)["c1"]["items"][0]["status"], "needs_grading")

    def test_expired_session_raises_with_login_hint(self):
        api = FakeApi({"c1": []}, {"c1": []}, status={"/users/me": 401})
        real = gb.fetch_gradebooks
        with patch("scrapers.grades._cookie", return_value="cookie"), \
             patch("scrapers.grades.fetch_gradebooks", lambda c, k: real(c, k, api)):
            with self.assertRaises(grades.SessionExpired) as ctx:
                asyncio.run(grades.fetch_gradebooks_async({"c1": "X"}))
        self.assertEqual(str(ctx.exception), "Session expired — run `bb login`")


class TestTime(unittest.TestCase):
    def test_iso_and_local_format(self):
        with patch.dict("os.environ", {"BB_TZ": "America/New_York"}):
            self.assertEqual(to_iso("2026-10-22T03:59:00Z"), "2026-10-22T03:59:00Z")
            self.assertEqual(format_local(parse_datetime("2026-10-22T03:59:00Z")), "Wed Oct 21 11:59 PM EDT")
            self.assertEqual(to_iso("8/30/26, 11:59 PM (EDT)"), "2026-08-31T03:59:00Z")
            self.assertIsNone(to_iso("not a date"))
            self.assertEqual(format_local(None), "TBD")


def iso(delta_hours):
    return (datetime.now(timezone.utc) + timedelta(hours=delta_hours)).isoformat().replace("+00:00", "Z")


class TestDue(unittest.TestCase):
    COURSES = {"c1": "IS 410 DB (x) FA2026", "c2": "ECON 122 Accounting (y) FA2026"}

    def books(self):
        columns = {
            "c1": [col("a", "Homework 1", due=iso(-30)), col("b", "Quiz", due=iso(30)), col("n", "Undated")],
            "c2": [col("x", "Homework 1", due=iso(-5)), col("y", "Project", due=iso(24 * 20))],
        }
        gb._user_cache.clear()
        return gb.fetch_gradebooks(self.COURSES, "cookie", FakeApi(columns, {"c1": [rec("a", "GRADED", 10)], "c2": []}))

    def test_dedupe_key_is_course_and_title(self):
        entries = due_dates.reconcile(self.books(), [], self.COURSES)
        homework = {e["course"]: e["status"] for e in entries if e["title"] == "Homework 1"}
        self.assertEqual(homework, {"IS 410": "graded", "ECON 122": "not_submitted"})

    def test_calendar_merges_by_course_and_ignores_other_terms(self):
        cal = [{"title": "Quiz", "course_id": "c1", "due_at": iso(30)},
               {"title": "Quiz", "course_id": "c2", "due_at": iso(30)},
               {"title": "Old", "course_id": "_999_1", "due_at": iso(-900)}]
        entries = due_dates.reconcile(self.books(), cal, self.COURSES)
        self.assertEqual(sum(e["title"] == "Quiz" for e in entries), 2)
        self.assertFalse(any(e["title"] == "Old" for e in entries))
        self.assertEqual(next(e for e in entries if e["title"] == "Quiz" and e["course"] == "IS 410")["source"], "both")

    def test_overdue_respects_real_completion(self):
        entries = due_dates.reconcile(self.books(), [], self.COURSES)
        self.assertEqual([(e["course"], e["title"]) for e in due_dates.select(entries, "overdue", False)],
                         [("ECON 122", "Homework 1")])

    def test_windows_and_exclude_completed(self):
        entries = due_dates.reconcile(self.books(), [], self.COURSES)

        def titles(window, exclude=False):
            return [(e["course"], e["title"]) for e in due_dates.select(entries, window, exclude)]

        self.assertEqual(titles("7d"), [("IS 410", "Quiz")])
        self.assertEqual(titles("30d"), [("IS 410", "Quiz"), ("ECON 122", "Project")])
        self.assertIn(("IS 410", "Homework 1"), titles("all"))
        self.assertNotIn(("IS 410", "Homework 1"), titles("all", exclude=True))

    def test_undated_only_in_all_and_in_own_group(self):
        entries = due_dates.reconcile(self.books(), [], self.COURSES)
        self.assertFalse(any(e["_dt"] is None for e in due_dates.select(entries, "30d", False)))
        everything = due_dates.select(entries, "all", False)
        self.assertEqual(everything[-1]["title"], "Undated")
        text = due_dates.format_due_dates_table(everything, "all")
        for expected in ("No due date (1)", "Overdue (1)", "Already done (1)"):
            self.assertIn(expected, text)

    def test_bad_window(self):
        with self.assertRaises(ValueError):
            due_dates.parse_window("soon")

    def test_text_shows_zone_course_label_and_status(self):
        entries = due_dates.reconcile(self.books(), [], self.COURSES)
        text = due_dates.format_due_dates_table(due_dates.select(entries, "7d", False), "7d", overdue=1)
        self.assertIn("Due in the next 7 days · 1 open", text)
        self.assertIn(local_zone().tzname(datetime.now()), text)
        for expected in ("IS 410", "not submitted", "1 overdue and not done: bb due overdue"):
            self.assertIn(expected, text)
        self.assertNotIn("_100", text)
        self.assertIn("Overdue · none", due_dates.format_due_dates_table([], "overdue"))


class TestGradesOutput(unittest.TestCase):
    def test_text_has_header_overall_and_aligned_items(self):
        columns = [col("a", "Homework 1", due="2026-10-01T03:59:00Z"), col("b", "Homework 2", due="2026-10-08T03:59:00Z"),
                   calc("t", externalGrade=True)]
        records = [rec("a", "GRADED", 9), rec("t", "GRADED", 94.3, displayGrade={"scaleType": "Percent", "score": 94.3})]
        book = gb.build_gradebook("c1", "IS 410 Intro To DB (x) FA2026", columns, records, SCHEMA["results"])
        with patch.dict("os.environ", {"BB_TZ": "America/New_York"}):
            text = grades.format_grades_text([book])
        lines = text.splitlines()
        self.assertEqual(lines[0], "IS 410  Intro To DB")
        for expected in ("94.3 / 100", "official: Overall Grade", "1 of 2 graded"):
            self.assertIn(expected, lines[1])
        for expected in ("Wed Sep 30 11:59 PM EDT", "9 / 10", "not submitted"):
            self.assertIn(expected, text)
        self.assertNotIn("%", text)
        self.assertNotIn("✓", text)
        self.assertNotIn("Unopened", text)
        json.dumps(grades.grades_json([book]))


class TestStudentWording(unittest.TestCase):
    def test_status_text(self):
        self.assertEqual(gb.status_text("needs_grading"), "submitted (awaiting grade)")
        self.assertEqual(gb.status_text("graded"), "graded")
        self.assertEqual(gb.status_text("graded", {"possible": 0}), "graded (no points)")
        self.assertEqual(gb.status_text("in_progress"), "in progress (not submitted)")
        self.assertEqual(gb.status_text("unknown"), "status unknown")
        self.assertEqual(gb.status_text("not_submitted"), "not submitted")

    def test_due_lists_open_work_before_submitted_work_within_a_day(self):
        courses = {"c1": "IS 410 DB (x) FA2026"}
        due = iso(30)
        columns = {"c1": [col("a", "Submitted early", due=due), col("b", "Still open", due=due)]}
        gb._user_cache.clear()
        books = {"c1": gb.build_gradebook("c1", courses["c1"], columns["c1"], [rec("a", "COMPLETED")], [],
                                          attempts={"a": [{"status": "COMPLETED"}]})}
        text = due_dates.format_due_dates_table(due_dates.select(due_dates.reconcile(books, [], courses), "7d", False), "7d")
        self.assertLess(text.index("Still open"), text.index("Submitted early"))
        self.assertIn("1 open, 1 done", text)
        self.assertIn("submitted", text)
        self.assertNotIn("✓", text)

    def test_grades_summary_shared_widths_and_no_points(self):
        a = gb.build_gradebook("c1", "IS 410 DB", [col("x", "Short", possible=0), col("y", "Quiz", possible=5)],
                               [rec("x", "GRADED", 0), rec("y", "GRADED", 5)], SCHEMA["results"])
        b = gb.build_gradebook("c2", "MATH 215 Finite", [col("z", "A much longer item name here", possible=100)],
                               [rec("z", "GRADED", 50)], SCHEMA["results"])
        summary = grades.format_grades_text([a, b], summary_only=True)
        self.assertTrue(summary.startswith("GRADES"))
        self.assertIn("IS 410", summary)
        self.assertNotIn("Short", summary)
        text = grades.format_grades_text([a, b], summary=True)
        self.assertIn("graded (no points)", text)
        quiz = next(line for line in text.splitlines() if "Quiz" in line)
        longer = next(line for line in text.splitlines() if "much longer" in line)
        self.assertEqual(quiz.index("graded"), longer.index("graded"))


class TestBriefing(unittest.TestCase):
    def test_rest_only_briefing_without_a_browser(self):
        courses = {"c1": "IS 410 DB (x) FA2026"}
        columns = {"c1": [col("a", "Done HW", due=iso(-30)), col("o", "Late HW", due=iso(-3)), col("s", "Soon HW", due=iso(10)),
                          col("w", "Week HW", due=iso(24 * 4)), calc("t", externalGrade=True)]}
        columns["c1"].append(col("low", "Bad Quiz", due=iso(-24 * 5)))
        records = {"c1": [rec("a", "GRADED", 10), rec("low", "GRADED", 2),
                          rec("t", "GRADED", 91, displayGrade={"scaleType": "Percent", "score": 91})]}
        api = FakeApi(columns, records)
        gb._user_cache.clear()
        real = gb.fetch_gradebooks

        async def fake_calendar(page=None, course_id=None):
            return []

        announcements = [{"title": "Midterm moved", "created": iso(-5), "meta": "", "body": "x", "unread": True},
                         {"title": "Old news", "created": iso(-24 * 30), "meta": "", "body": "x"}]
        with patch("scrapers.briefing.load_courses", return_value=courses), \
             patch("scrapers.grades._cookie", return_value="cookie"), \
             patch("scrapers.grades.fetch_gradebooks", lambda c, k: real(c, k, api)), \
             patch("scrapers.briefing.scrape_calendar_async", fake_calendar), \
             patch("scrapers.briefing.scrape_announcements_api", return_value=announcements), \
             patch("core.async_engine.AsyncSessionManager", side_effect=AssertionError("browser launched")):
            bundle = asyncio.run(briefing.run_briefing_async())
        self.assertEqual([i["title"] for i in bundle["attention"]], ["Bad Quiz", "Soon HW"])
        self.assertEqual([i["title"] for i in bundle["overdue"]], ["Late HW"])
        self.assertEqual([i["title"] for i in bundle["urgent"]], ["Soon HW"])
        self.assertEqual([i["title"] for i in bundle["upcoming"]], ["Week HW"])
        self.assertEqual(bundle["courses"]["c1"]["overall"]["percent"], 91)
        self.assertEqual([a["title"] for a in bundle["courses"]["c1"]["announcements"]], ["Midterm moved"])
        text = briefing.format_briefing_cli(bundle)
        for expected in ("NEEDS ATTENTION (2)", "OVERDUE (1)", "DUE TODAY / TOMORROW (1)", "LATER THIS WEEK (1)", "GRADES", "91 / 100", "official",
                         "Midterm moved", "not submitted"):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()


class TestReviewRegressions(unittest.TestCase):
    def test_category_subtotal_is_not_the_official_course_grade(self):
        book = gb.build_gradebook('c', 'IS 410', [col('hw', 'Homework'), calc('quiz', 'Quiz average')],
                                  [rec('hw', score=5), rec('quiz', score=90)], [])
        self.assertEqual(book['overall']['source'], 'calculated')
        self.assertEqual(book['overall']['percent'], 50)

    def test_percent_overall_does_not_use_column_points_as_denominator(self):
        book = gb.build_gradebook('c', 'IS 410', [calc('total', possible=500)],
                                  [{'columnId': 'total', 'displayGrade': {'scaleType': 'Percent', 'score': 90}}], [])
        self.assertEqual(book['overall']['percent'], 90)

    def test_duplicate_assignment_titles_keep_both_deadlines(self):
        book = gb.build_gradebook('c', 'IS 410',
                                  [col('a', 'Homework', due='2026-10-01T12:00:00Z'),
                                   col('b', 'Homework', due='2026-10-02T12:00:00Z')],
                                  [rec('b', score=9)], [])
        rows = due_dates.reconcile({'c': book}, [], {'c': 'IS 410'})
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['status'] for r in rows}, {gb.GRADED, gb.NOT_SUBMITTED})

    def test_unreadable_student_records_are_explicitly_partial(self):
        gb._user_cache.clear()
        api = FakeApi({'c': [col('a', 'Homework')]}, {'c': []}, status={'/gradebook/users/': 403})
        book = gb.fetch_gradebooks({'c': 'IS 410'}, 'synthetic-cookie', api_get=api)['c']
        self.assertEqual(book['records_error'], gb.FORBIDDEN)
        self.assertEqual(gb.public_view(book)['records_error'], gb.FORBIDDEN)
        self.assertIn('grades unavailable', grades.format_grades_text([book]))

    def test_assignment_auth_failure_does_not_request_browser_fallback(self):
        from scrapers import assignments
        with patch('scrapers.assignments._get_cookie_header', return_value='synthetic-cookie'), \
             patch('scrapers.assignments._api_get', return_value={'_http_status': 401}):
            with self.assertRaises(gb.GradebookError) as caught:
                assignments.scrape_course_assignments_http('c')
        self.assertEqual(caught.exception.kind, gb.EXPIRED)

    def test_telegram_briefing_keeps_overdue_and_deduplicates_urgent(self):
        from telegram.formatter import format_daily_briefing
        overdue = {'title': 'Late homework', 'course': 'IS 410', 'due_date': 'Yesterday'}
        urgent = {'title': 'Soon homework', 'course': 'IS 410', 'due_date': 'Today'}
        text = '\n'.join(format_daily_briefing({'overdue': [overdue], 'urgent': [urgent],
                                              'calendar': [urgent], 'courses': {}}))
        self.assertIn('Late homework', text)
        self.assertEqual(text.count('Soon homework'), 1)


class TestPartialBriefingExport(unittest.TestCase):
    def test_partial_grade_error_and_attention_survive_json_export(self):
        from core.export_json import build_composite_schema
        bundle = {'courses': {'c': {'course_name': 'IS 410', 'records_error': 'forbidden', 'grades': []}},
                  'notes': ['IS 410: grades unavailable'], 'attention': [{'title': 'Review homework'}]}
        data = json.loads(build_composite_schema(bundle))
        self.assertEqual(data['courses'][0]['records_error'], 'forbidden')
        self.assertIn('grades unavailable', data['notes'][0])
        self.assertEqual(data['global']['attention'][0]['title'], 'Review homework')


class TestMixedPercentAndDeadlineIdentity(unittest.TestCase):
    def test_official_percent_uses_display_score_when_raw_points_also_exist(self):
        record = {'columnId': 't', 'score': 450, 'displayGrade': {'scaleType': 'Percent', 'score': 90}}
        book = gb.build_gradebook('c', 'IS 410', [calc('t', possible=500)], [record], [])
        self.assertEqual(book['overall']['percent'], 90)

    def test_telegram_keeps_distinct_items_with_same_course_title_and_due(self):
        from telegram.formatter import format_daily_briefing
        item = {'title': 'Homework', 'course': 'IS 410', 'due_date': 'Today'}
        text = '\n'.join(format_daily_briefing({'urgent': [{**item, 'item_id': 'a'}, {**item, 'item_id': 'b'}]}))
        self.assertEqual(text.count('Homework'), 2)
