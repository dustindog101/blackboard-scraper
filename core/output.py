from pathlib import Path
from core.config import OUTPUT_BASE

def ensure_output_dir(section: str) -> Path:
    """Create and return output directory for a section."""
    out_dir = OUTPUT_BASE / section
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


# Context propagates through asyncio tasks and asyncio.to_thread.
import builtins
import re
import sys
from contextvars import ContextVar
from contextlib import contextmanager

_machine_output = ContextVar('machine_output', default=False)


@contextmanager
def output_mode(machine):
    token = _machine_output.set(machine)
    try:
        yield
    finally:
        _machine_output.reset(token)


def status(*values, **kwargs):
    """Progress goes to stderr without ANSI escapes in machine output modes."""
    if _machine_output.get():
        values = tuple(re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(value)) for value in values)
        kwargs['file'] = sys.stderr
    builtins.print(*values, **kwargs)
