"""Shared text-output helpers: course labels, local times with zone, relative ages, clipping."""
import re
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional, Sequence

_CODE_RE = re.compile(r"^\s*([A-Za-z]{2,6})\s*(\d{3,4}[A-Za-z]?)\b")
_SECTION_RE = re.compile(r"\s*\([^)]*\)\s*(?:SP|FA|SU|WI)\d{4}\s*\([^)]*\)\s*$|\s*\([0-9A-Za-z./]+\)\s*(?:SP|FA|SU|WI)\d{4}\s*$")


def short_course(name: Optional[str]) -> str:
    """'IS 410 Intro To Database Design (03.1520) FA2026' -> 'IS 410'. Falls back to the cleaned name."""
    if not name:
        return "General"
    match = _CODE_RE.match(name)
    if match:
        return f"{match.group(1).upper()} {match.group(2).upper()}"
    return _SECTION_RE.sub("", name).strip() or name


def course_title(name: Optional[str]) -> str:
    """Course name without the trailing section/term codes."""
    return _SECTION_RE.sub("", name or "").strip() or (name or "")


def parse_dt(value: Any) -> Optional[datetime]:
    """ISO-8601 string or epoch milliseconds -> timezone-aware datetime, else None."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        text = str(value).strip()
        if text.endswith(" UTC"):  # "2026-09-08 20:30 UTC" as the assignments scraper formats it
            return datetime.strptime(text[:-4], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.astimezone()
    except (ValueError, OverflowError, OSError):
        return None


def fmt_when(dt: Optional[datetime], now: Optional[datetime] = None) -> str:
    """Local time with zone, e.g. 'Tue Oct 21 11:59 PM EDT' (year added when not the current year)."""
    if dt is None:
        return ""
    local = dt.astimezone()
    now = (now or datetime.now().astimezone())
    year = "" if local.year == now.year else f" {local.year}"
    return f"{local:%a %b} {local.day}{year} {local:%I:%M %p %Z}".replace(" 0", " ")


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
