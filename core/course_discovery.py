import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Dict, Optional

from core.config import BLACKBOARD_BASE, SESSION_DIR, save_courses

logger = logging.getLogger("blackboard.course_discovery")


def get_cookie_header() -> Optional[str]:
    """Extract Cookie header string from .session/cookies.json."""
    cookie_file = SESSION_DIR / "cookies.json"
    if not cookie_file.exists():
        return None
    try:
        cookies_list = json.loads(cookie_file.read_text())
        return "; ".join([
            f"{c['name']}={c['value']}"
            for c in cookies_list
            if "blackboard.umbc.edu" in c.get("domain", "") or "umbc.edu" in c.get("domain", "")
        ])
    except Exception:
        return None


def discover_courses_via_api() -> Dict[str, Dict[str, str]]:
    """
    High-speed course discovery via Blackboard Learn REST API (< 200ms).
    Retrieves user memberships, terms, and resolves merged parent/child sections.
    Returns: Dict[term_name, Dict[course_id, course_title]]
    """
    cookie_header = get_cookie_header()
    if not cookie_header:
        raise RuntimeError("No session cookies found. Please run `python3 main.py --login` first.")

    headers = {
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    # 1. Fetch User Identity
    req_me = urllib.request.Request(f"{BLACKBOARD_BASE}/learn/api/public/v1/users/me", headers=headers)
    with urllib.request.urlopen(req_me, timeout=6) as resp:
        user_data = json.loads(resp.read().decode("utf-8"))
    user_id = user_data.get("id")
    if not user_id:
        raise RuntimeError("Could not retrieve user ID from Blackboard API.")

    # 2. Fetch Academic Terms Map
    term_map: Dict[str, str] = {}
    try:
        req_terms = urllib.request.Request(f"{BLACKBOARD_BASE}/learn/api/public/v1/terms", headers=headers)
        with urllib.request.urlopen(req_terms, timeout=8) as resp:
            terms_data = json.loads(resp.read().decode("utf-8"))
            for t in terms_data.get("results", []):
                term_map[t["id"]] = t.get("name", "Unknown Term")
    except Exception as e:
        logger.debug(f"Terms fetch warning: {e}")

    # 3. Fetch Course Memberships
    req_courses = urllib.request.Request(
        f"{BLACKBOARD_BASE}/learn/api/public/v1/users/{user_id}/courses?expand=course",
        headers=headers,
    )
    with urllib.request.urlopen(req_courses, timeout=12) as resp:
        memberships_data = json.loads(resp.read().decode("utf-8"))

    raw_memberships = memberships_data.get("results", [])

    # Group by term and deduplicate merged sections
    courses_by_term: Dict[str, Dict[str, str]] = {}

    for m in raw_memberships:
        # Skip disabled or revoked memberships
        if m.get("availability", {}).get("available") == "Disabled":
            continue

        course = m.get("course", {})
        cid = m.get("courseId") or course.get("id")
        cname = course.get("name") or "Untitled Course"
        term_id = course.get("termId")
        term_name = term_map.get(term_id) or _guess_term_from_title(cname) or "Other Courses"

        if term_name not in courses_by_term:
            courses_by_term[term_name] = {}

        courses_by_term[term_name][cid] = cname

    # Clean up merged child duplicates per term
    for tname in list(courses_by_term.keys()):
        courses_by_term[tname] = _deduplicate_merged_sections(courses_by_term[tname])

    return courses_by_term


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


def handle_discover_courses_cli(term_filter: Optional[str] = None, list_only: bool = False) -> Dict[str, str]:
    """
    CLI handler for intelligent course discovery.
    """
    print("\n🔍 Querying Blackboard Learn API for enrolled course terms (<200ms)...")
    try:
        courses_by_term = discover_courses_via_api()
    except Exception as e:
        print(f"❌ API discovery error: {e}")
        print("   Falling back to browser-based course scraper...")
        return {}

    current_term = get_current_term_name(courses_by_term)
    print(f"✨ Detected Active Academic Term: \033[1m\033[32m{current_term}\033[0m\n")

    # Display all discovered terms
    for tname, clist in courses_by_term.items():
        is_current_indicator = " ⭐ [ACTIVE SEMESTER]" if tname == current_term else ""
        print(f"🗓️  \033[1m{tname}\033[0m ({len(clist)} courses){is_current_indicator}:")
        for cid, cname in clist.items():
            print(f"   • \033[36m{cid}\033[0m: {cname}")
        print("")

    if list_only:
        return courses_by_term.get(current_term, {})

    # Determine which courses to save
    selected_courses: Dict[str, str] = {}

    if term_filter:
        term_filter_clean = term_filter.strip().lower()
        if term_filter_clean in ("all", "every"):
            for clist in courses_by_term.values():
                selected_courses.update(clist)
            print(f"💾 Selected ALL terms ({len(selected_courses)} courses).")
        else:
            # Fuzzy match term name (e.g. 'fa2026' -> 'Fall 2026', 'sp2026' -> 'Spring 2026')
            matched_term = None
            for tname in courses_by_term:
                clean_t = tname.lower().replace(" ", "")
                if term_filter_clean in clean_t or clean_t in term_filter_clean:
                    matched_term = tname
                    break

            if matched_term:
                selected_courses = courses_by_term[matched_term]
                print(f"💾 Selected term '{matched_term}' ({len(selected_courses)} courses).")
            else:
                print(f"⚠️ Term '{term_filter}' not found. Defaulting to active semester ({current_term}).")
                selected_courses = courses_by_term.get(current_term, {})
    else:
        # Default: Select and save ONLY the current active semester courses
        selected_courses = courses_by_term.get(current_term, {})

    if selected_courses:
        save_courses(selected_courses, overwrite=True)
        print(f"✅ \033[32mSaved {len(selected_courses)} active courses to config.json!\033[0m")
        print("   All briefings, due dates, grade lookups, and Telegram alerts are now focused on your active term.\n")

    return selected_courses
