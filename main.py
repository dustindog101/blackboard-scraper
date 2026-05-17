import argparse
import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from core.config import BLACKBOARD_BASE, load_courses, save_courses
from core.export_json import build_export_doc, build_item, write_export
from core.session import _launch_context, _require_session, check_session, login, login_auto
from scrapers.activity import save_activity, scrape_activity
from scrapers.announcements import save_announcements, scrape_announcements
from scrapers.calendar import save_calendar, scrape_calendar
from scrapers.discussions import save_discussions, scrape_discussions
from scrapers.grades import save_grades, scrape_grades
from scrapers.profile import save_profile, scrape_profile


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _safe_relpath(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _emit_output(args: argparse.Namespace, items: list[dict]) -> None:
    """Print JSON to stdout or write to --out file."""
    pretty = not args.compact
    payload = build_export_doc(items, source=args.source, pretty=pretty)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload)
        print(f"💾 JSON written to: {_safe_relpath(out_path)}", file=sys.stderr)
    else:
        print(payload)


def _emit_raw(args: argparse.Namespace, data: list[dict]) -> None:
    """Print raw scraper dicts (pre-transform) to stdout or --out file."""
    pretty = not args.compact
    if pretty:
        payload = json.dumps(data, indent=2, ensure_ascii=False)
    else:
        payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload)
        print(f"💾 Raw data written to: {_safe_relpath(out_path)}", file=sys.stderr)
    else:
        print(payload)


# ---------------------------------------------------------------------------
# Console print helpers
# ---------------------------------------------------------------------------

def _print_profile(data: dict) -> None:
    if not data:
        print("No profile data found.")
        return
    print("\n👤 Blackboard Profile")
    print(f"  Name: {data.get('name', 'N/A')}")
    print(f"  Email: {data.get('email', 'N/A')}")
    print(f"  Student ID: {data.get('student_id', 'N/A')}")
    print(f"  Pronouns: {data.get('pronouns', 'N/A')}")
    print(f"  System Role: {data.get('system_role', 'N/A')}")
    print(f"  Privacy: {data.get('privacy', 'N/A')}\n")


# ---------------------------------------------------------------------------
# Course name cleaning
# ---------------------------------------------------------------------------

_COURSE_CODE_RE = re.compile(r"\s*\([^)]*\)\s*(?:SP|FA|SU|WI)\d{4}\s*\([^)]*\)\s*$")


def _short_course_name(raw: str | None) -> str | None:
    """Strip trailing section/semester codes from Blackboard course names.

    'IS 247 Computer Programming II (01.2288) SP2026 (IS247_2288_SP2026)'
    → 'IS 247 Computer Programming II'
    """
    if not raw:
        return raw
    cleaned = _COURSE_CODE_RE.sub("", raw).strip()
    return cleaned or raw


# ---------------------------------------------------------------------------
# build_item wrappers
# ---------------------------------------------------------------------------

def _build_activity_items(activity: list[dict], default_group: str) -> list[dict]:
    out: list[dict] = []
    for entry in activity:
        raw_preview = (entry.get("message") or "").strip()
        date_str = (entry.get("date") or "").strip()
        context = (entry.get("context") or "").strip()
        course_name = _short_course_name(context)

        # Use the message/preview as title if available; fall back to a short label.
        if raw_preview:
            title = raw_preview.split("\n")[0][:120]
        elif context:
            title = f"Activity: {course_name or context}"
        else:
            title = "Activity update"

        out.append(
            build_item(
                kind="activity",
                course_name=course_name,
                title=title,
                notes=None,
                due_text=entry.get("due_date") or None,
                source_ref=f"activity:{context}:{date_str}",
                group_name=default_group,
                metadata={"date": date_str, "raw_preview": raw_preview} if (date_str or raw_preview) else {},
            )
        )
    return out


def _build_calendar_items(calendar: list[dict], course_id: str | None, default_group: str) -> list[dict]:
    out: list[dict] = []
    for entry in calendar:
        course_name = _short_course_name(entry.get("course"))
        out.append(
            build_item(
                kind="calendar_due",
                course_id=course_id,
                course_name=course_name,
                title=entry.get("title") or "Calendar item",
                notes=None,
                due_text=entry.get("due"),
                source_ref=f"calendar:{course_id or 'global'}:{entry.get('title','')}:{entry.get('due','')}",
                group_name=default_group,
            )
        )
    return out


def _build_announcement_items(
    announcements: list[dict],
    course_id: str,
    course_name: str,
    default_group: str,
) -> list[dict]:
    out: list[dict] = []
    for ann in announcements:
        body = (ann.get("body") or "").strip()
        meta = (ann.get("meta") or "").strip()
        out.append(
            build_item(
                kind="announcement",
                course_id=course_id,
                course_name=_short_course_name(course_name),
                title=ann.get("title") or "Announcement",
                notes=body or None,
                source_ref=f"announcement:{course_id}:{ann.get('title','')}",
                group_name=default_group,
                priority=3 if ann.get("unread") else 2,
                is_starred=bool(ann.get("unread")),
                metadata={
                    "posted": meta,
                    "unread": str(bool(ann.get("unread"))).lower(),
                },
            )
        )
    return out


def _build_grade_items(
    grades: list[dict],
    course_id: str,
    course_name: str,
    default_group: str,
) -> list[dict]:
    out: list[dict] = []
    for item in grades:
        name = item.get("name") or "Grade item"
        due = item.get("dueDate") or ""
        status = item.get("status") or ""
        grade = item.get("grade") or ""
        notes_parts = []
        if status:
            notes_parts.append(f"Status: {status}")
        if grade:
            notes_parts.append(f"Grade: {grade}")
        out.append(
            build_item(
                kind="grade",
                course_id=course_id,
                course_name=_short_course_name(course_name),
                title=name,
                notes="\n".join(notes_parts) or None,
                due_text=due or None,
                source_ref=f"grade:{course_id}:{name}:{due}",
                group_name=default_group,
                metadata={"status": status, "grade": grade},
            )
        )
    return out


def _build_discussion_items(
    discussions: list[dict],
    course_id: str,
    course_name: str,
    default_group: str,
) -> list[dict]:
    out: list[dict] = []
    for disc in discussions:
        preview = disc.get("preview_text") or ""
        posts = disc.get("posts") or []
        participants = disc.get("participants") or []
        notes_parts = []
        if preview:
            notes_parts.append(preview)
        if posts:
            first_post = posts[0]
            notes_parts.append(
                f"Latest post by {first_post.get('author', 'Unknown')} ({first_post.get('date', '')}):\n{first_post.get('text', '')}"
            )
        notes = "\n\n".join(part for part in notes_parts if part).strip()
        out.append(
            build_item(
                kind="discussion",
                course_id=course_id,
                course_name=_short_course_name(course_name),
                title=disc.get("title") or "Discussion thread",
                notes=notes or None,
                due_text=disc.get("due_date"),
                source_ref=disc.get("url") or f"discussion:{course_id}:{disc.get('title','')}",
                url=disc.get("url"),
                group_name=default_group,
                metadata={
                    "participants_count": str(len(participants)),
                    "posts_count": str(len(posts)),
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bb",
        description="UMBC Blackboard Ultra Scraper",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 main.py --briefing\n"
            "  python3 main.py --announcements --all\n"
            "  python3 main.py --grades --all --out grades.json\n"
            "  python3 main.py --activity --raw\n"
            "  python3 main.py --check-session --visible\n"
            "  python3 main.py --calendar --compact\n"
        ),
    )

    # --- authentication ---
    auth = parser.add_argument_group("authentication")
    auth.add_argument("--login", action="store_true", help="Login via SSO (skips if session is valid)")
    auth.add_argument("--auto", "-a", action="store_true", help="With --login: fully automated SSO + Duo SMS login")
    auth.add_argument("--force", action="store_true", help="With --login: force re-login even if session exists")
    auth.add_argument("--username", "-u", help="Username for automated login")
    auth.add_argument("--password", "-p", help="Password for automated login")
    auth.add_argument("--check-session", action="store_true", help="Test if the current session is valid")
    auth.add_argument("--session-info", action="store_true", help="Show session creation / last-used timestamps")
    auth.add_argument("--debug", action="store_true", help="Print detailed debug output (use with --check-session)")

    # --- discovery ---
    discovery = parser.add_argument_group("discovery")
    discovery.add_argument("--discover", action="store_true", help="Find and save enrolled courses")
    discovery.add_argument("--courses", action="store_true", help="List configured courses")

    # --- scrapers ---
    scrapers = parser.add_argument_group("scrapers")
    scrapers.add_argument("--briefing", action="store_true", help="Run all core scrapers (activity + calendar + announcements + grades)")
    scrapers.add_argument("--activity", action="store_true", help="Scrape homepage activity stream")
    scrapers.add_argument("--calendar", action="store_true", help="Scrape calendar due-dates")
    scrapers.add_argument("--announcements", action="store_true", help="Scrape course announcements")
    scrapers.add_argument("--grades", action="store_true", help="Scrape gradebook")
    scrapers.add_argument("--discussions", action="store_true", help="Scrape course discussions")
    scrapers.add_argument("--profile", action="store_true", help="Show your Blackboard profile")

    # --- discussion modifiers ---
    disc = parser.add_argument_group("discussion modifiers")
    disc.add_argument("--max-posts", type=int, default=None, metavar="N", help="Max 'Load more' post clicks")
    disc.add_argument("--max-parts", type=int, default=None, metavar="N", help="Max participant-expand clicks")
    disc.add_argument("--posts-only", action="store_true", help="Fetch posts only")
    disc.add_argument("--participants-only", action="store_true", help="Fetch participants only")
    disc.add_argument("--titles-only", action="store_true", help="Fetch thread titles only")

    # --- scope ---
    scope = parser.add_argument_group("scope")
    scope.add_argument("--course", "-c", help="Target course ID (e.g. _100001_1)")
    scope.add_argument("--all", action="store_true", help="Run against all configured courses")
    scope.add_argument("--visible", "-v", action="store_true", help="Show browser window (useful for debugging)")
    scope.add_argument("--cdp", help="Connect to an existing browser via CDP URL (e.g. http://localhost:9222)")

    # --- output ---
    output = parser.add_argument_group("output")
    output.add_argument("--out", metavar="FILE", help="Write JSON output to FILE instead of stdout")
    output.add_argument("--md", action="store_true", help="Also save markdown file(s) to the output/ directory")
    output.add_argument("--raw", action="store_true", help="Output raw scraper data (pre-transform) instead of the export envelope")
    output.add_argument("--compact", action="store_true", help="Emit minified JSON (no indentation)")
    output.add_argument("--source", default="blackboard-scraper", metavar="NAME", help="Value for the JSON 'source' field (default: blackboard-scraper)")
    output.add_argument("--group", default="School", metavar="NAME", help="Group name for exported items (default: School)")

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------

def _handle_discover_courses(headless: bool, cdp: str | None) -> None:
    if not _require_session(cdp):
        return
    print("🔍 Discovering enrolled courses...", file=sys.stderr)
    with sync_playwright() as p:
        ctx, page = _launch_context(p, headless, cdp)
        from scrapers.base import _navigate_and_check_page

        url = f"{BLACKBOARD_BASE}/ultra/course"
        if not _navigate_and_check_page(page, url):
            ctx.close()
            return

        page.wait_for_timeout(3000)
        page.wait_for_selector(".course-element-card", timeout=15_000)

        discovered = page.evaluate(
            """() => {
                const results = {};
                document.querySelectorAll('.course-element-card').forEach(el => {
                    const titleEl = el.querySelector('.js-course-title-element');
                    const courseId = el.getAttribute('data-course-id');
                    if (titleEl && courseId) {
                        results[courseId] = titleEl.innerText.trim();
                    }
                });
                return results;
            }"""
        )

        if discovered:
            print(f"   ✅ Found {len(discovered)} courses:", file=sys.stderr)
            for cid, cname in discovered.items():
                print(f"      {cid}: {cname}", file=sys.stderr)
            save_courses(discovered)
            print("   💾 Saved to config.json", file=sys.stderr)
        else:
            print("   ❌ No courses found.", file=sys.stderr)
        ctx.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()
    if len(sys.argv) == 1:
        print("Run with --help to see available commands.", file=sys.stderr)
        sys.exit(1)

    headless = not args.visible
    cdp = args.cdp
    courses = load_courses()
    os.environ["TMPDIR"] = "/tmp"

    # --- auth / utility commands ---

    if args.courses:
        print("\n📚 Configured Courses:", file=sys.stderr)
        for cid, name in courses.items():
            print(f"  {cid}: {name}", file=sys.stderr)
        print("", file=sys.stderr)
        return

    if args.login:
        if args.auto:
            login_auto(username=args.username, password=args.password, headless=headless, cdp_url=cdp)
        else:
            login(args.force, args.username, args.password, cdp)
        return

    if args.check_session:
        # --visible is the global flag that controls headless/visible browser.
        # When passed alongside --check-session it runs the check with a visible window.
        ok = check_session(debug=args.debug, headless=headless)
        if not ok and not args.visible:
            # Fallback: try visible check to distinguish detection issues from real failures.
            visible_ok = check_session(debug=args.debug, headless=False)
            if visible_ok:
                print(
                    "⚠️  Headless check failed but visible check passed.\n"
                    "   Likely a headless-detection/timing issue; session is probably valid.",
                    file=sys.stderr,
                )
        return

    if args.session_info:
        from core.config import SESSION_DIR
        import json as _json

        meta = SESSION_DIR / "session_metadata.json"
        print("\n🕒 Session Info:", file=sys.stderr)
        if meta.exists():
            data = _json.loads(meta.read_text())
            print(f"  Created:   {data.get('login_time_human', 'Unknown')}", file=sys.stderr)
            print(f"  Last Used: {data.get('last_used_time_human', 'Unknown')}", file=sys.stderr)
        else:
            print("  No session metadata found. Run --login first.", file=sys.stderr)
        print("", file=sys.stderr)
        return

    if args.discover:
        _handle_discover_courses(headless, cdp)
        return

    # --- scraper commands ---

    if args.profile:
        if not _require_session(cdp):
            return
        with sync_playwright() as p:
            ctx, page = _launch_context(p, headless, cdp)
            data = scrape_profile(page)
            if data:
                _print_profile(data)
                if args.md:
                    save_profile(data)
            ctx.close()
        return

    json_items: list[dict] = []

    if args.briefing:
        if not _require_session(cdp):
            return
        from scrapers.briefing import run_briefing

        bundle = run_briefing(headless=headless, cdp_url=cdp, write_markdown=args.md)
        if args.raw:
            _emit_raw(args, bundle)
            return
        json_items.extend(_build_activity_items(bundle.get("activity", []), args.group))
        json_items.extend(_build_calendar_items(bundle.get("calendar", []), None, args.group))
        for course_id, course_data in bundle.get("courses", {}).items():
            course_name = course_data.get("course_name", courses.get(course_id, course_id))
            json_items.extend(
                _build_announcement_items(course_data.get("announcements", []), course_id, course_name, args.group)
            )
            json_items.extend(_build_grade_items(course_data.get("grades", []), course_id, course_name, args.group))
        _emit_output(args, json_items)
        return

    if args.activity:
        if not _require_session(cdp):
            return
        with sync_playwright() as p:
            ctx, page = _launch_context(p, headless, cdp)
            activity = scrape_activity(page)
            if args.md:
                save_activity(activity)
            ctx.close()
        if args.raw:
            _emit_raw(args, activity)
            return
        json_items.extend(_build_activity_items(activity, args.group))
        _emit_output(args, json_items)
        return

    if args.calendar:
        if not _require_session(cdp):
            return
        with sync_playwright() as p:
            ctx, page = _launch_context(p, headless, cdp)
            calendar = scrape_calendar(page, args.course)
            if args.md:
                save_calendar(calendar, args.course)
            ctx.close()
        if args.raw:
            _emit_raw(args, calendar)
            return
        json_items.extend(_build_calendar_items(calendar, args.course, args.group))
        _emit_output(args, json_items)
        return

    if args.announcements:
        if not _require_session(cdp):
            return
        target_courses = list(courses.keys()) if args.all else ([args.course] if args.course else [])
        if not target_courses:
            print("❌ Specify --course <ID> or --all", file=sys.stderr)
            return
        raw_all: list[dict] = []
        with sync_playwright() as p:
            ctx, _ = _launch_context(p, headless, cdp)
            for course_id in target_courses:
                page = ctx.new_page()
                data = scrape_announcements(course_id, page)
                if args.md:
                    save_announcements(data, course_id)
                if args.raw:
                    raw_all.extend(data)
                else:
                    json_items.extend(
                        _build_announcement_items(data, course_id, courses.get(course_id, course_id), args.group)
                    )
                page.close()
            ctx.close()
        if args.raw:
            _emit_raw(args, raw_all)
            return
        _emit_output(args, json_items)
        return

    if args.grades:
        if not _require_session(cdp):
            return
        target_courses = list(courses.keys()) if args.all else ([args.course] if args.course else [])
        if not target_courses:
            print("❌ Specify --course <ID> or --all", file=sys.stderr)
            return
        raw_all: list[dict] = []
        with sync_playwright() as p:
            ctx, _ = _launch_context(p, headless, cdp)
            for course_id in target_courses:
                page = ctx.new_page()
                data = scrape_grades(course_id, page)
                if args.md:
                    save_grades(data, course_id)
                if args.raw:
                    raw_all.extend(data)
                else:
                    json_items.extend(_build_grade_items(data, course_id, courses.get(course_id, course_id), args.group))
                page.close()
            ctx.close()
        if args.raw:
            _emit_raw(args, raw_all)
            return
        _emit_output(args, json_items)
        return

    if args.discussions:
        if not _require_session(cdp):
            return
        kwargs = {
            "max_post_clicks": args.max_posts,
            "max_participant_clicks": args.max_parts,
            "posts_only": args.posts_only,
            "participants_only": args.participants_only,
            "titles_only": args.titles_only,
        }
        target_courses = list(courses.keys()) if args.all else ([args.course] if args.course else [])
        if not target_courses:
            print("❌ Specify --course <ID> or --all", file=sys.stderr)
            return
        raw_all: list[dict] = []
        with sync_playwright() as p:
            ctx, _ = _launch_context(p, headless, cdp)
            for course_id in target_courses:
                page = ctx.new_page()
                data = scrape_discussions(course_id, page, **kwargs)
                if args.md:
                    save_discussions(data, course_id, titles_only=args.titles_only)
                if args.raw:
                    raw_all.extend(data)
                else:
                    json_items.extend(
                        _build_discussion_items(data, course_id, courses.get(course_id, course_id), args.group)
                    )
                page.close()
            ctx.close()
        if args.raw:
            _emit_raw(args, raw_all)
            return
        _emit_output(args, json_items)
        return

    print("No scraper action selected. Run with --help.", file=sys.stderr)


if __name__ == "__main__":
    main()
