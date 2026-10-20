"""The Duo passcode race: first fresh valid code wins, stale and repeated codes are rejected."""

import threading
import time
import unittest

from core.passcodes import Source, collect_passcode, file_source, immediate_source


def emitter(raw, delay=0.0, at="now", reason=None):
    def run(emit, stop):
        if stop.wait(delay):
            return "cancelled"
        emit(raw, time.time() if at == "now" else at)
        return reason
    return run


def silent(duration, reason="nothing"):
    def run(emit, stop):
        stop.wait(duration)
        return reason
    return run


def race(sources, tried=frozenset(), timeout=2, sent_at=None, say=None):
    return collect_passcode(sources, tried=tried, sent_at=time.time() - 1 if sent_at is None else sent_at,
                            timeout=timeout, say=say or (lambda *_: None))


class TestRace(unittest.TestCase):
    def test_fastest_valid_code_wins_and_losers_are_cancelled(self):
        cancelled = threading.Event()

        def slow(emit, stop):
            if stop.wait(5):
                cancelled.set()
            return "stopped"
        started = time.monotonic()
        result = race([Source("sms", "Messages", slow), Source("cli", "terminal", emitter("1234567", 0.2)),
                       Source("tg", "Telegram", emitter("7654321", 1.0))])
        self.assertEqual((result.code, result.source), ("1234567", "cli"))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(cancelled.wait(1))

    def test_stale_code_is_rejected_and_a_fresh_one_wins(self):
        said = []
        sent_at = time.time()
        sources = [Source("sms", "Messages", emitter("111111", 0.0, at=sent_at - 600)),
                   Source("cli", "terminal", emitter("222222", 0.3))]
        result = race(sources, sent_at=sent_at, say=said.append)
        self.assertEqual(result.code, "222222")
        self.assertTrue(any("old code" in line for line in said))

    def test_only_stale_codes_yields_none_with_reasons(self):
        sent_at = time.time()
        result = race([Source("file", "code file", emitter("111111", 0, at=sent_at - 600, reason="nothing written"))], sent_at=sent_at)
        self.assertIsNone(result.code)
        self.assertIn("code file", result.summary())

    def test_clock_skew_allows_a_code_stamped_just_before_the_request(self):
        sent_at = time.time()
        result = race([Source("sms", "Messages", emitter("123456", 0, at=sent_at - 1))], sent_at=sent_at)
        self.assertEqual(result.code, "123456")

    def test_rejected_codes_and_garbage_are_ignored(self):
        said = []
        result = race([Source("cli", "terminal", emitter("12ab", 0)), Source("tg", "Telegram", emitter("333333", 0.1)),
                       Source("sms", "Messages", emitter("444444", 0.4))], tried={"333333"}, say=said.append)
        self.assertEqual(result.code, "444444")
        self.assertEqual(len(said), 2)

    def test_argument_code_wins_immediately_without_a_staleness_check(self):
        result = race([immediate_source("987654"), Source("sms", "Messages", silent(5))], sent_at=time.time() + 100)
        self.assertEqual((result.code, result.source), ("987654", "argument"))

    def test_summary_lists_every_source_tried_when_nothing_arrives(self):
        result = race([Source("sms", "macOS Messages", unavailable="Full Disk Access is missing"),
                       Source("tg", "Telegram", silent(0.05, "no reply")),
                       Source("cli", "terminal", unavailable="not an interactive terminal")], timeout=1)
        self.assertIsNone(result.code)
        for text in ("macOS Messages: Full Disk Access is missing", "Telegram: no reply", "terminal: not an interactive terminal"):
            self.assertIn(text, result.summary())

    def test_timeout_reports_sources_still_waiting(self):
        result = race([Source("cli", "terminal", silent(5))], timeout=0.3)
        self.assertIn("nothing within", result.summary())

    def test_cancel_returns_promptly(self):
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        started = time.monotonic()
        result = collect_passcode([Source("cli", "terminal", silent(5))], sent_at=0, timeout=10, cancel=cancel, say=lambda *_: None)
        self.assertIsNone(result.code)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_crashing_source_does_not_stop_the_others(self):
        def broken(emit, stop):
            raise RuntimeError("boom")
        result = race([Source("sms", "Messages", broken), Source("cli", "terminal", emitter("555555", 0.2))])
        self.assertEqual(result.code, "555555")


class TestFileSource(unittest.TestCase):
    def test_fresh_file_is_read_and_deleted(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "code.txt"
            threading.Timer(0.3, lambda: path.write_text("246810\n")).start()
            result = race([file_source(path, poll=0.05)])
            self.assertEqual((result.code, result.source), ("246810", "file"))
            self.assertFalse(path.exists())

    def test_file_written_before_the_request_is_stale(self):
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "code.txt"
            path.write_text("246810")
            old = time.time() - 3600
            os.utime(path, (old, old))
            result = race([file_source(path, poll=0.05)], timeout=0.5, sent_at=time.time())
            self.assertIsNone(result.code)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
