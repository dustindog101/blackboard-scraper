import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.config import save_courses
from core.display import parse_dt
from core.gradebook import course_label
from core.output import status
from core.rest import RestError, get_json, get_pages

logger = logging.getLogger("blackboard.course_discovery")


def _discover(now: Optional[datetime] = None) -> Tuple[Dict[str, Dict[str, str]], Optional[str]]:
    """Fetch enrolled courses grouped by term. Returns (courses_by_term, active_term_name).

    Raises RestError (auth/forbidden/unknown). The active term is the one whose date range contains
    ``now``; None when the terms endpoint gives no usable dates.
    """
    now = now or datetime.now(timezone.utc)
    user_id = get_json("/learn/api/public/v1/users/me", timeout=6).get("id")
    if not user_id:
        raise RestError("unknown", None, "no user id in /users/me")

    term_map: Dict[str, str] = {}
    windows: Dict[str, Tuple[Optional[datetime], Optional[datetime]]] = {}
    try:
        for t in get_pages("/learn/api/public/v1/terms"):
            name = t.get("name", "Unknown Term")
            term_map[t["id"]] = name
            duration = (t.get("availability") or {}).get("duration") or {}
            windows[name] = (parse_dt(duration.get("start")), parse_dt(duration.get("end")))
    except RestError as exc:
        logger.debug(f"Terms fetch warning: {exc}")

    memberships = get_pages(f"/learn/api/public/v1/users/{user_id}/courses?expand=course", timeout=12)
    courses_by_term: Dict[str, Dict[str, str]] = {}
    for m in memberships:
        if m.get("availability", {}).get("available") == "Disabled":
            continue
        course = m.get("course", {})
        cid = m.get("courseId") or course.get("id")
        cname = course.get("name") or "Untitled Course"
        term_name = term_map.get(course.get("termId")) or _guess_term_from_title(cname) or "Other Courses"
        courses_by_term.setdefault(term_name, {})[cid] = cname

    for tname in list(courses_by_term):
        courses_by_term[tname] = _deduplicate_merged_sections(courses_by_term[tname])

    active = next(
        (n for n in courses_by_term if windows.get(n, (None, None))[0] and windows[n][0] <= now
         and (windows[n][1] is None or now <= windows[n][1])),
        None,
    )
    return courses_by_term, active


def discover_courses_via_api() -> Dict[str, Dict[str, str]]:
    """Course discovery via the REST API: Dict[term_name, Dict[course_id, course_title]]."""
    return _discover()[0]


def _guess_term_from_title(title: str) -> Optional[str]:
    """Extract academic term from course name if termId is missing (e.g. FA2026 -> Fall 2026)."""
    match = re.search(r"\b(FA|SP|SU|WI)(\d{4})\b", title, re.IGNORECASE)
    if match:
        season_code, year = match.groups()
        season_map = {"FA": "Fall", "SP": "Spring", "SU": "Summer", "WI": "Winter"}
        return f"{season_map.get(season_code.upper(), season_code)} {year}"
    return None


def _deduplicate_merged_sections(course_dict: Dict[str, str]) -> Dict[str, str]:
    """
    Resolve merged course sections:
    If a parent section (e.g. 'IS 410 Intro... (03.1520/IS610.2303)') and child section exist,
    favor the parent master section ID or the cleaner course title.
    """
    deduped: Dict[str, str] = {}
    seen_prefixes: Dict[str, str] = {}  # prefix (e.g. 'IS 410', 'AGNG 100') -> cid

    for cid, title in course_dict.items():
        # Match Course Code prefix (e.g. 'IS 410', 'ECON 121', 'MATH 215')
        prefix_match = re.match(r"^([A-Z]{2,4}\s*\d{3}[A-Z]?)", title)
        if prefix_match:
            prefix = prefix_match.group(1).replace(" ", "").upper()
            if prefix in seen_prefixes:
                existing_cid = seen_prefixes[prefix]
                existing_title = deduped[existing_cid]
                # If current has a slash (merged parent identifier) or longer title, replace
                if ("/" in title and "/" not in existing_title) or (len(title) > len(existing_title) and "/" not in existing_title):
                    del deduped[existing_cid]
                    deduped[cid] = title
                    seen_prefixes[prefix] = cid
                continue
            else:
                seen_prefixes[prefix] = cid

        deduped[cid] = title

    return deduped


def get_current_term_name(courses_by_term: Dict[str, Dict[str, str]]) -> str:
    """
    Intelligently determine the active academic term based on current date & available terms.
    """
    now = datetime.now()
    month = now.month
    year = now.year

    # Map month to typical academic semester
    if 1 <= month <= 5:
        expected_seasons = [f"Spring {year}", f"Winter {year}", f"Fall {year-1}"]
    elif 6 <= month <= 7:
        expected_seasons = [f"Summer {year}", f"Spring {year}", f"Fall {year}"]
    else:  # August to December
        expected_seasons = [f"Fall {year}", f"Summer {year}", f"Spring {year}"]

    for expected in expected_seasons:
        if expected in courses_by_term and courses_by_term[expected]:
            return expected

    # Fallback to the latest year/term found in dictionary
    sorted_terms = sorted(
        courses_by_term.keys(),
        key=lambda t: (
            int(re.search(r"\d{4}", t).group(0)) if re.search(r"\d{4}", t) else 0,
            1 if "Fall" in t else (2 if "Summer" in t else (3 if "Spring" in t else 0))
        ),
        reverse=True
    )
    return sorted_terms[0] if sorted_terms else "Current Term"


def terms_view(courses_by_term: Dict[str, Dict[str, str]], active: str) -> List[Dict[str, Any]]:
    """JSON-friendly term list: [{term, active, courses: [{id, name}]}], active term first, then newest."""
    def order(name: str) -> tuple:
        year = re.search(r"\d{4}", name)
        return (name != active, -(int(year.group(0)) if year else 0), name)

    return [
        {"term": t, "active": t == active, "courses": [{"id": cid, "name": n} for cid, n in courses_by_term[t].items()]}
        for t in sorted(courses_by_term, key=order)
    ]


def select_courses(courses_by_term: Dict[str, Dict[str, str]], active: str, term_filter: Optional[str]) -> Tuple[str, Dict[str, str]]:
    """Courses `discover` would save: (label, courses). Raises ValueError for an unknown term."""
    if not term_filter:
        return active, courses_by_term.get(active, {})
    wanted = term_filter.strip().lower().replace(" ", "")
    wanted = {"fa": "fall", "sp": "spring", "su": "summer", "wi": "winter"}.get(wanted[:2], "") + wanted[2:] \
        if re.fullmatch(r"(fa|sp|su|wi)\d{4}", wanted) else wanted
    if wanted in ("all", "every"):
        merged: Dict[str, str] = {}
        for clist in courses_by_term.values():
            merged.update(clist)
        return "all terms", merged
    for tname, clist in courses_by_term.items():
        clean = tname.lower().replace(" ", "")
        if wanted in clean or clean in wanted:
            return tname, clist
    raise ValueError(f"No enrolled term matches '{term_filter}'. Run `bb terms` to see them.")


def load_terms() -> Tuple[Dict[str, Dict[str, str]], str]:
    """(courses_by_term, active_term). Raises RestError."""
    courses_by_term, active = _discover()
    return courses_by_term, active or get_current_term_name(courses_by_term)


def format_terms(terms: List[Dict[str, Any]]) -> str:
    lines = []
    for t in terms:
        n = len(t["courses"])
        lines.append(f"{t['term']}  ({n} course{'s' if n != 1 else ''}){'  <- active' if t['active'] else ''}")
        lines.extend(f"  {course_label(c['name']):<10}{c['id']}" for c in t["courses"])
        lines.append("")
    return "\n".join(lines).rstrip()


def save_selected(label: str, selected: Dict[str, str]) -> None:
    """Write the selected courses to config.json (the one place `discover` changes anything)."""
    save_courses(selected, overwrite=True)
    status(f"Saved {len(selected)} course{'s' if len(selected) != 1 else ''} ({label}) to config.json, replacing the previous list.")


def handle_discover_courses_cli(term_filter: Optional[str] = None, list_only: bool = False) -> Dict[str, str]:
    """Programmatic discovery used by the menubar app: saves the selected term unless list_only."""
    try:
        courses_by_term, active = load_terms()
        label, selected = select_courses(courses_by_term, active, term_filter)
    except (RestError, ValueError) as exc:
        status(f"Course discovery failed: {exc}")
        return {}
    if selected and not list_only:
        save_selected(label, selected)
    return selected
