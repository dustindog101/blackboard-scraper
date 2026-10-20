import re
import sys
from pathlib import Path

from core.config import OUTPUT_BASE

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def ensure_output_dir(section: str) -> Path:
    """Create and return output directory for a section."""
    out_dir = OUTPUT_BASE / section
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def status(message: object = "", *, end: str = "\n") -> None:
    """Print progress, warnings and fallback notices to stderr.

    stdout is reserved for the command's result (text or one JSON document), so
    scrapers report progress through here instead of a bare ``print``. Colour
    codes are stripped when stderr is not a terminal.
    """
    text = str(message)
    if not sys.stderr.isatty():
        text = _ANSI_RE.sub("", text)
    sys.stderr.write(text + end)
    sys.stderr.flush()
