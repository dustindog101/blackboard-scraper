"""`bb due`: deadlines from the gradebook (status model) reconciled with the calendar, per course."""
import asyncio
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.gradebook import COMPLETED, FORBIDDEN, NOT_FOUND, UNKNOWN, course_label, status_text
from core.output import ensure_output_dir
from core.time import format_clock, format_local, local_zone, parse_datetime
from scrapers.calendar import scrape_calendar_async
from scrapers.grades import _clip, _num, fetch_gradebooks_async

logger = logging.getLogger("blackboard.scrapers.due_dates")

_COURSE_ID_RE = re.compile(r"^_\d+_\d+$")


def _normalize_title(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", "", title.lower()).split())


def parse_window(window: Optional[str]) -> Tuple[str, Optional[float]]:
    """`7d` -> ('days', 7); `overdue` / `all` -> (name, None). Raises ValueError for anything else."""
    text = str(window or "7d").lower().strip()
    if text in ("overdue", "all"):
        return text, None
    m = re.fullmatch(r"(\d+)\s*d?", text)
    if not m:
        raise ValueError(f"Unknown window '{window}'. Use 7d, 14d, 30d, overdue or all.")
    return "days", float(m.group(1))


def _entry(cid: Optional[str], course: str, title: str, due_at: Optional[str], status: str, source: str, **extra: Any) -> Dict[str, Any]:
    dt = parse_datetime(due_at)
    return {
        "title": title, "course_id": cid, "course": course, "due_at": due_at, "status": status,
        "completed": status in COMPLETED, "score": None, "possible": None, "percent": None,
        "category": None, "source": source, "_dt": dt, **extra,
    }


def reconcile(books: Dict[str, Dict[str, Any]], calendar_items: List[Dict[str, Any]], courses: Dict[str, str]) -> List[Dict[str, Any]]:
    """Keep distinct gradebook identities; join calendar titles only when unique within a course."""
    combined: Dict[Tuple[Optional[str], str], Dict[str, Any]] = {}
    by_title: Dict[tuple, list] = {}
    by_source_id: Dict[Tuple[Optional[str], str], Tuple[Optional[str], str]] = {}

    for cid, book in books.items():
        if book.get("error"):
            continue
        label = course_label(courses.get(cid) or book.get("course_name"))
        for index, item in enumerate(book["items"]):
            key = (cid, item.get("_column_id") or item.get("_content_id") or f"row:{index}")
            by_title.setdefault((cid, _normalize_title(item["name"])), []).append(key)
            combined[key] = _entry(
                cid, label, item["name"], item["due_at"], item["status"], "gradebook",
                score=item["score"], possible=item["possible"], percent=item["percent"], category=item["category"],
                item_id=item.get("_column_id") or item.get("_content_id") or key[1],
            )
            for ident in (item.get("_column_id"), item.get("_content_id")):
                if ident:
                    by_source_id[(cid, ident)] = key

    for c in calendar_items:
        title = (c.get("title") or "").strip()
        cal_id = c.get("course_id")
        if not title:
            continue
        if cal_id in courses:
            cid: Optional[str] = cal_id
        elif courses and cal_id and _COURSE_ID_RE.match(cal_id):
            continue  # a course that is not one of the configured (current) courses
        else:
            cid = None
        matches = by_title.get((cid, _normalize_title(title)), [])
        key = by_source_id.get((cid, c.get("source_id") or ""))
        if key is None and len(matches) == 1:
            key = matches[0]
        if key is None:
            key = (cid, "calendar:" + str(c.get("source_id") or (title, c.get("due_at") or c.get("raw_due"))))
        due_at = c.get("due_at") or c.get("raw_due")
        if key in combined:
            existing = combined[key]
            existing["source"] = "both"
            if existing["_dt"] is None and parse_datetime(due_at):
                existing.update(due_at=due_at, _dt=parse_datetime(due_at))
            continue
        label = course_label(courses.get(cid) if cid else c.get("course"))
        combined[key] = _entry(cid, label, title, due_at if parse_datetime(due_at) else None, UNKNOWN, "calendar", item_id=key[1])

    return list(combined.values())


def select(entries: List[Dict[str, Any]], window: str, exclude_completed: bool, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Apply the window. Overdue = dated, past and not completed; undated items only appear under `all`."""
    kind, days = parse_window(window)
    now = now or datetime.now(local_zone())
    picked = []
    for e in entries:
        dt = e["_dt"]
        if exclude_completed and e["completed"]:
            continue
        if dt is None:
            if kind == "all" and not e["completed"] and e["source"] != "calendar":
                picked.append(e)
            continue
        past = dt < now
        if kind == "overdue":
            if past and not e["completed"] and (e["status"] != UNKNOWN or e["source"] != "calendar"):
                picked.append(e)
        elif kind == "days":
            if not past and dt <= now + timedelta(days=days):
                picked.append(e)
        else:
            picked.append(e)
    picked.sort(key=lambda e: (e["_dt"] is None, e["_dt"].timestamp() if e["_dt"] else 0, e["course"], e["title"].lower()))
    return picked


async def due_report_async(
    page: Optional[Any] = None,
    courses: Optional[Dict[str, str]] = None,
    window_filter: str = "7d",
    exclude_completed: bool = False,
) -> Dict[str, Any]:
    """`{"items": [...], "notes": [...]}`. Raises scrapers.grades.SessionExpired on a 401."""
    parse_window(window_filter)  # fail fast on a bad window
    courses = courses or {}
    calendar, books = await asyncio.gather(
        scrape_calendar_async(page),
        fetch_gradebooks_async(courses, page),
        return_exceptions=True,
    )
    if isinstance(books, BaseException):
        raise books
    calendar_items = calendar if isinstance(calendar, list) else []
    notes = []
    failed = [course_label(courses[cid]) for cid, b in books.items() if b.get("error") in (FORBIDDEN, NOT_FOUND)]
    if failed:
        notes.append(f"{', '.join(failed)}: gradebook not visible to students, skipped")
    for cid, book in books.items():
        if book.get("records_error"):
            notes.append(f"{course_label(courses[cid])}: grades unavailable; item statuses unknown")
    entries = reconcile(books, calendar_items, courses)
    items = select(entries, window_filter, exclude_completed)
    overdue_hidden = 0
    if parse_window(window_filter)[0] != "overdue":
        overdue_hidden = len(select(entries, "overdue", exclude_completed))
    return {"items": items, "notes": notes, "overdue": overdue_hidden}


async def aggregate_due_dates_async(page=None, courses=None, window_filter="7d", exclude_completed=False) -> List[Dict[str, Any]]:
    """Items only (Telegram, menubar): same fields as `public_item`, plus legacy `due_date` text."""
    report = await due_report_async(page, courses, window_filter, exclude_completed)
    return [public_item(e, legacy=True) for e in report["items"]]


def public_item(e: Dict[str, Any], legacy: bool = False) -> Dict[str, Any]:
    out = {k: v for k, v in e.items() if not k.startswith("_")}
    if legacy:
        out["due_date"] = format_local(e["_dt"]) if e["_dt"] else "TBD"
        out["status"] = status_text(e["status"], e)
    return out


# ---------------------------------------------------------------------------
# Text output
# ---------------------------------------------------------------------------

def _day_header(dt: datetime, today) -> str:
    local = dt.astimezone(local_zone())
    delta = (local.date() - today).days
    base = f"{local:%a %b} {local.day}"
    return {0: f"Today, {base}", 1: f"Tomorrow, {base}"}.get(delta, base)


def _dt(e: Dict[str, Any]) -> Optional[datetime]:
    return e["_dt"] if "_dt" in e else parse_datetime(e.get("due_at"))


def rows(items: List[Dict[str, Any]], full_date: bool) -> List[str]:
    """Aligned `when  course  title  points  status` rows, for internal entries or public dicts."""
    cells = []
    for e in items:
        dt = _dt(e)
        when = ("TBD" if dt is None else format_local(dt) if full_date else format_clock(dt) + f" {dt.astimezone(local_zone()):%Z}")
        pts = (f"{_num(e['score'])} / {_num(e['possible'])}" if e["score"] is not None and e["possible"]
               else f"{_num(e['possible'])} pts" if e["possible"] else "")
        cells.append((when, e["course"], _clip(e["title"], 44), pts, status_text(e["status"], e)))
    widths = [max(len(c[i]) for c in cells) for i in range(5)]
    return [f"  {w:<{widths[0]}}  {c:<{widths[1]}}  {t:<{widths[2]}}  {p:>{widths[3]}}  {s}".rstrip() for w, c, t, p, s in cells]


def format_due_dates_table(items: List[Dict[str, Any]], window_filter: str = "7d", notes: Optional[List[str]] = None, overdue: int = 0) -> str:
    """Grouped text view of `select()` output (internal entries, not public dicts)."""
    kind, days = parse_window(window_filter)
    title = {"overdue": "Overdue", "all": "All deadlines"}.get(kind) or f"Due in the next {int(days)} days"
    done = sum(1 for e in items if e["completed"])
    count = f"{len(items) - done} open" + (f", {done} done" if done else "")
    lines = [f"{title} · {count if items else 'none'}"]
    now = datetime.now(local_zone())
    today = now.date()
    dated = [e for e in items if e["_dt"] is not None]
    undated = [e for e in items if e["_dt"] is None]

    if not items:
        lines += ["", {"overdue": "Nothing overdue.", "all": "Nothing found."}.get(kind, "Nothing due in this window.")]
    elif kind == "overdue":
        lines += [""] + rows(dated, full_date=True)
    else:
        past_incomplete = [e for e in dated if e["_dt"] < now and not e["completed"]]
        past_done = [e for e in dated if e["_dt"] < now and e["completed"]]
        upcoming = [e for e in dated if e["_dt"] >= now]
        if past_incomplete:
            lines += ["", f"Overdue ({len(past_incomplete)})"] + rows(past_incomplete, full_date=True)
        current = None
        group: List[Dict[str, Any]] = []
        groups = []
        for e in sorted(upcoming, key=lambda e: (e["_dt"].astimezone(local_zone()).date(), e["completed"], e["_dt"])):
            header = _day_header(e["_dt"], today)
            if header != current:
                group = []
                groups.append((header, group))
                current = header
            group.append(e)
        for header, g in groups:
            lines += ["", header] + rows(g, full_date=False)
        if undated:
            lines += ["", f"No due date ({len(undated)})"] + rows(undated, full_date=True)
        if past_done:
            lines += ["", f"Already done ({len(past_done)})"] + rows(past_done, full_date=True)
    if overdue and kind != "overdue":
        lines += ["", f"{overdue} overdue and not done: bb due overdue"]
    lines += [f"  {n}" for n in (notes or [])]
    return "\n".join(lines)


def save_due_dates(items: List[Dict[str, Any]], window_filter: str = "7d") -> Path:
    """Saves the report to output/calendar/due_dates.md."""
    out_dir = ensure_output_dir("calendar")
    filepath = out_dir / "due_dates.md"
    lines = [f"# Due ({window_filter})", f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_", "", "| Course | Item | Due | Status |", "|---|---|---|---|"]
    for e in items:
        due = format_local(e["_dt"]) if e["_dt"] else "TBD"
        lines.append(f"| {e['course']} | {e['title'].replace('|', '-')} | {due} | {status_text(e['status'], e)} |")
    filepath.write_text("\n".join(lines))
    print(f"   💾 Saved to: {filepath.name}", file=sys.stderr)
    return filepath


def _when(c: Dict[str, Any]) -> Optional[datetime]:
    return parse_datetime(c.get("due_at") or c.get("raw_due") or c.get("due"))


def format_calendar_list(items: List[Dict[str, Any]]) -> str:
    """`bb calendar`: raw calendar entries sorted by time, in local time."""
    if not items:
        return "Nothing on the calendar."
    dated = sorted(items, key=lambda c: (_when(c) is None, c.get("due_at") or ""))
    cells = [(format_local(_when(c)), course_label(c.get("course")), _clip(c.get("title", ""), 50)) for c in dated]
    widths = [max(len(c[i]) for c in cells) for i in range(2)]
    return f"Calendar · {len(items)} items\n\n" + "\n".join(f"  {w:<{widths[0]}}  {c:<{widths[1]}}  {t}" for w, c, t in cells)
