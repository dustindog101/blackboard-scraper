"""Duo passcode sources, raced: the first fresh, valid 6-8 digit code wins.

A source runs in its own thread and calls `emit(raw, received_at)` when it
gets something that looks like a code. It returns a short reason when it ends
without one ("no new SMS in 25s"). The race rejects:

- anything that is not 6-8 digits,
- a code that arrived before the SMS was requested (`received_at < sent_at`),
- a code Duo already rejected (`tried`).

Sources never see each other's results; the race cancels the rest on a win.
"""

import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

CLOCK_SKEW = 2.0  # seconds a code may predate the request and still count (clock rounding)


def normalize_passcode(value) -> Optional[str]:
    text = str(value or "").strip()
    return text if re.fullmatch(r"[0-9]{6,8}", text) else None


@dataclass
class Source:
    name: str
    label: str
    run: Optional[Callable[[Callable, threading.Event], Optional[str]]] = None
    unavailable: Optional[str] = None  # why it cannot run at all (shown in the final summary)


@dataclass
class RaceResult:
    code: Optional[str] = None
    source: Optional[str] = None
    notes: dict = field(default_factory=dict)  # label -> why it produced nothing

    def summary(self) -> str:
        return "; ".join(f"{label}: {why}" for label, why in self.notes.items()) or "no source was available"


def collect_passcode(sources, *, tried=frozenset(), sent_at: float, timeout: float,
                     cancel: Optional[threading.Event] = None, say=print) -> RaceResult:
    """Start every available source at once and return the first acceptable code."""
    results: queue.Queue = queue.Queue()
    stop = threading.Event()
    result = RaceResult()
    active = set()
    workers = []
    if cancel is not None and cancel.is_set():
        return result

    def work(source):
        reason = None
        try:
            reason = source.run(lambda raw, at=None: results.put(("code", source, raw, at)), stop)
        except Exception as error:  # a broken source must not break the login
            reason = f"failed ({type(error).__name__})"
        results.put(("done", source, reason or "no code", None))

    for source in sources:
        if source.run is None:
            result.notes[source.label] = source.unavailable or "unavailable"
        else:
            active.add(source.name)
            worker = threading.Thread(target=work, args=(source,), daemon=True)
            workers.append(worker)
            worker.start()

    deadline = time.monotonic() + timeout
    try:
        while active and time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                return result
            try:
                kind, source, value, at = results.get(timeout=0.1)
            except queue.Empty:
                continue
            if kind == "done":
                active.discard(source.name)
                result.notes.setdefault(source.label, value)
                continue
            code = normalize_passcode(value)
            if code is None:
                say(f"   ⚠️ Ignored input from {source.label}: passcodes contain 6–8 digits.")
            elif at is not None and at < sent_at - CLOCK_SKEW:
                say(f"   ⚠️ Ignored an old code from {source.label} (received before this SMS was requested).")
            elif code in tried:
                say(f"   ⚠️ Ignored a code from {source.label} that Duo already rejected.")
            else:
                result.code, result.source = code, source.name
                result.notes.pop(source.label, None)
                return result
        for source in sources:
            if source.run is not None and source.name in active:
                result.notes.setdefault(source.label, f"nothing within {timeout:.0f}s")
        return result
    finally:
        stop.set()
        # Let cooperative file/terminal readers release their input before the
        # next attempt starts. Network sources may still be finishing one I/O;
        # those must check stop before consuming or publishing its result.
        cleanup_deadline = time.monotonic() + 0.5
        for worker in workers:
            worker.join(max(0, cleanup_deadline - time.monotonic()))


# ------------------------------------------------------------------ sources

def immediate_source(code: str) -> Source:
    """A code given up front (--passcode): submitted as-is on the first attempt."""
    def run(emit, stop):
        emit(code, None)
        return "submitted"
    return Source("argument", "--passcode", run)


def file_source(path, poll: float = 0.5) -> Source:
    """--code-file: an agent writes the code into the file after WAITING_FOR_CODE is printed.

    The file is deleted when read. A file last written before the SMS was
    requested is stale: it is deleted unread.
    """
    target = Path(path)

    def run(emit, stop):
        while not stop.wait(poll):
            try:
                written = target.stat().st_mtime
                raw = target.read_text(encoding="utf-8-sig", errors="ignore").strip()
            except OSError:
                continue
            if not raw:
                continue
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            emit(raw, written)
        return "nothing written to the file"
    return Source("file", "code file", run)


class TerminalSource:
    """One stdin reader per login, reused by every attempt (never two readers on one tty)."""

    def __init__(self, reader, stop_event, automatic_capture=False):
        self.lines: queue.Queue = queue.Queue()
        self.closed = False
        threading.Thread(target=reader, args=(self.lines, stop_event, automatic_capture), daemon=True).start()

    def source(self) -> Source:
        if self.closed:
            return Source("cli", "terminal", unavailable="input closed")

        def run(emit, stop):
            while not stop.is_set():
                try:
                    kind, text = self.lines.get(timeout=0.1)[:2]
                except queue.Empty:
                    continue
                if kind == "terminal-closed":
                    self.closed = True
                    return "input closed"
                emit(text, time.time())
            return "nothing typed"
        return Source("cli", "terminal", run)
