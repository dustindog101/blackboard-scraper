"""Handlers for the REST-first read commands: announcements, activity, profile, terms, discover.

Each handler prints exactly one result to stdout (text, or one JSON document through ``emit``)
and sends progress and warnings to stderr.
"""
import asyncio
import sys
from typing import Any, Callable, Dict, List

from core.output import status
from core.rest import RestError

Emit = Callable[[Any], None]

REST_MESSAGES = {
    "auth": "session expired; run `bb login`",
    "forbidden": "not visible to students",
    "not_found": "not found",
    "unknown": "network or server error; try again",
}


def machine_mode(args: Any) -> bool:
    return bool(getattr(args, "json", False) or getattr(args, "out", None) or getattr(args, "raw", False))


def fail(args: Any, emit: Emit, exc: RestError) -> None:
    """Report a REST failure (JSON error document in machine mode) and exit 1."""
    message = REST_MESSAGES.get(exc.kind, str(exc))
    if machine_mode(args):
        emit({"error": exc.kind, "message": message})
    print(f"bb: {message}", file=sys.stderr)
    raise SystemExit(1)


async def run_announcements(args: Any, courses: Dict[str, str], cids: List[str], emit: Emit) -> None:
    from scrapers.announcements import (
        fetch_course_announcements, fetch_term_starts, filter_announcements, format_announcements,
        mark_older_term, parse_since, save_announcements, scrape_announcements_async, verify_read_state,
    )

    since = parse_since(args.since) if args.since else None
    fetched, term_starts = await asyncio.gather(
        asyncio.gather(*(asyncio.to_thread(fetch_course_announcements, c) for c in cids)),
        asyncio.to_thread(fetch_term_starts, cids),
    )
    rows: Dict[str, List[dict]] = {}
    errors: Dict[str, str] = {}
    for cid, (items, error) in zip(cids, fetched):
        if error == "auth":
            fail(args, emit, RestError("auth", 401))
        if error == "unknown":
            items = await scrape_announcements_async(cid)  # REST unreachable: browser fallback
        elif error:
            errors[cid] = error
        rows[cid] = items
        mark_older_term(items, term_starts.get(cid))

    candidates = {c: filter_announcements(r, since=since) for c, r in rows.items()}
    if args.unread:
        to_check = {c: r for c, r in candidates.items() if r}
        if to_check:
            status("Checking read state in the browser (read-only)...")
            unknown = await verify_read_state(to_check, headless=not getattr(args, "visible", False), cdp_url=getattr(args, "cdp", None))
            if unknown:
                status(f"{unknown} announcement(s) have unverifiable read state; they are excluded from --unread.")
    groups = []
    for c in cids:
        matching = filter_announcements(candidates[c], unread=args.unread)
        shown = matching[: args.limit] if args.limit is not None else matching
        groups.append({
            "course_id": c,
            "course_name": courses.get(c, c),
            "total": len(matching),
            "announcements": shown,
            **({"error": errors[c]} if c in errors else {}),
        })
        if args.md:
            save_announcements(shown, c)
    if machine_mode(args):
        emit([{k: v for k, v in g.items() if k != "total"} for g in groups])
    else:
        print(format_announcements(groups))


async def run_activity(args: Any, emit: Emit) -> None:
    from scrapers.activity import DEFAULT_LIMIT, format_activity, save_activity, scrape_activity_async

    limit = args.limit if args.limit is not None else DEFAULT_LIMIT
    try:
        everything = await scrape_activity_async(full=args.full)
    except RestError as exc:
        fail(args, emit, exc)
    items = everything[:limit] if limit else everything
    if args.md:
        save_activity(items)
    if machine_mode(args):
        emit(items)
    else:
        source = "REST" if any("at" in i for i in items) else "browser"
        print(format_activity(items, len(everything), source))


async def run_profile(args: Any, emit: Emit) -> None:
    from scrapers.profile import save_profile, scrape_profile_async

    try:
        data = await scrape_profile_async()
    except RestError as exc:
        fail(args, emit, exc)
    if args.md:
        save_profile(data)
    if machine_mode(args):
        emit(data)
        return
    print(data.get("name", "Unknown"))
    for label, key in (("role", "system_role"), ("pronouns", "pronouns")):
        if data.get(key):
            print(f"  {label:<9}{data[key]}")


async def run_terms(args: Any, emit: Emit) -> None:
    """Read-only: lists every enrolled term. Never touches config.json."""
    from core.course_discovery import format_terms, load_terms, terms_view

    try:
        by_term, active = await asyncio.to_thread(load_terms)
    except RestError as exc:
        fail(args, emit, exc)
    terms = terms_view(by_term, active)
    if machine_mode(args):
        emit(terms)
    else:
        print(format_terms(terms))


async def run_discover(args: Any, emit: Emit) -> bool:
    """Saves the chosen term's courses to config.json (and says so). Returns False if REST failed."""
    from core.course_discovery import load_terms, save_selected, select_courses

    try:
        by_term, active = await asyncio.to_thread(load_terms)
        label, selected = select_courses(by_term, active, getattr(args, "term", None))
    except ValueError as exc:
        print(f"bb: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except RestError as exc:
        if exc.kind == "auth":
            fail(args, emit, exc)
        status(f"Course API unavailable ({exc.kind}); trying the browser.")
        return False
    if not selected:
        print(f"bb: no courses found for {label}; config.json left unchanged.", file=sys.stderr)
        raise SystemExit(1)
    save_selected(label, selected)
    if machine_mode(args):
        emit({"term": label, "saved": True, "courses": [{"id": c, "name": n} for c, n in selected.items()]})
    else:
        print(f"Saved {len(selected)} courses for {label} to config.json:")
        for cid, name in selected.items():
            print(f"  {cid}  {name}")
    return True
