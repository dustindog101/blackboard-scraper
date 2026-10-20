"""Text-layout helpers (relative age, clipping, aligned columns). Dates use core.time, labels core.gradebook."""
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional, Sequence

from core.time import format_local, parse_datetime

def parse_dt(value: Any) -> Optional[datetime]:
    """Epoch milliseconds or any string core.time understands -> aware datetime, else None."""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return parse_datetime(value) if isinstance(value, str) else None


def fmt_when(dt: Optional[datetime], now: Optional[datetime] = None) -> str:
    """core.time.format_local, but blank (not 'TBD') when there is no time."""
    return format_local(dt) if dt else ""


def ago(dt: Optional[datetime], now: Optional[datetime] = None) -> str:
    """Compact relative age: 'just now', '5m ago', '3h ago', '2d ago', '5w ago', '4mo ago'."""
    if dt is None:
        return "date unknown"
    seconds = max(0, int(((now or datetime.now(timezone.utc)) - dt).total_seconds()))
    if seconds < 90:
        return "just now"
    for limit, unit, size in ((3600, "m", 60), (86400, "h", 3600), (14 * 86400, "d", 86400), (60 * 86400, "w", 7 * 86400)):
        if seconds < limit:
            return f"{seconds // size}{unit} ago"
    return f"{seconds // (30 * 86400)}mo ago"


def clip(text: Optional[str], width: int) -> str:
    """Collapse whitespace and cut at a word boundary with an ellipsis, never mid-word."""
    flat = " ".join((text or "").split())
    if len(flat) <= width:
        return flat
    cut = flat[: width - 1].rsplit(" ", 1)[0] if " " in flat[: width - 1] else flat[: width - 1]
    return cut.rstrip(" ,;:-") + "…"


def align(rows: Iterable[Sequence[str]], gap: int = 2, indent: str = "  ") -> List[str]:
    """Pad every column but the last to the widest cell so rows line up."""
    rows = [list(map(str, r)) for r in rows]
    if not rows:
        return []
    ncols = max(len(r) for r in rows)
    widths = [max((len(r[i]) for r in rows if i < len(r)), default=0) for i in range(ncols - 1)]
    out = []
    for row in rows:
        cells = [row[i].ljust(widths[i]) for i in range(len(row) - 1)] + [row[-1]]
        out.append((indent + (" " * gap).join(cells)).rstrip())
    return out
