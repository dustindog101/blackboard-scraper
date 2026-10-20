import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from core.display import align, clip, short_course
from core.output import status
from scrapers.discussions import scrape_discussions_http
from scrapers.outline import (
    download_content_item_files,
    scrape_course_outline_async,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger("blackboard.scrapers.search")


def _snippet(text: str, query_lower: str, width: int = 60) -> str:
    """Returns the text around the first match so results show why they matched."""
    flat = " ".join(text.split())
    idx = flat.lower().find(query_lower)
    if idx < 0:
        return flat[: width * 2]
    start = max(0, idx - width)
    end = min(len(flat), idx + len(query_lower) + width)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def _safe_discussions(cid: str) -> Optional[List[Dict[str, Any]]]:
    # Discussion prompts are a bonus source; never let them break outline search.
    try:
        return scrape_discussions_http(cid)
    except Exception as exc:
        logger.debug(f"Discussion prompt search skipped for {cid}: {exc}")
        return None


def _match_discussions(
    discussions: List[Dict[str, Any]],
    query_lower: str,
    cid: str,
    cname: str,
    skip_titles: set,
) -> List[Dict[str, Any]]:
    """Matches discussion prompts and prompt links, which the outline does not include."""
    matches = []
    for disc in discussions:
        title = disc.get("title", "")
        if title.strip().lower() in skip_titles:
            continue
        links = disc.get("links") or []
        link_hits = [link for link in links if query_lower in f"{link['text']} {link['url']}".lower()]
        prompt = disc.get("prompt", "")
        if query_lower not in f"{title} {prompt}".lower() and not link_hits:
            continue
        matches.append({
            "title": title,
            "content_id": disc.get("id"),
            "content_type": "discussion",
            "description": _snippet(prompt, query_lower) if query_lower in prompt.lower() else "",
            "due_date": disc.get("due_date", ""),
            "external_url": link_hits[0]["url"] if link_hits else "",
            "links": links,
            "parent_path": [],
            "course_id": cid,
            "course_name": cname,
        })
    return matches


async def find_items_async(
    query: str,
    courses: Dict[str, str],
    page: Optional['Page'] = None,
    type_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Searches across courses for content items, assignments, documents, or files matching `query`.
    Searches item title, parent folder hierarchy, descriptions, and attachment filenames.
    """
    status(f"Searching {len(courses)} course{'s' if len(courses) != 1 else ''} for '{query}'...")
    query_lower = query.lower()
    matches: List[Dict[str, Any]] = []

    async def _search_course(cid: str, cname: str):
        outline, discussions = await asyncio.gather(
            scrape_course_outline_async(cid, page=page, max_depth=6),
            asyncio.to_thread(_safe_discussions, cid),
        )
        c_matches = []
        for item in outline:
            title = item.get("title", "")
            desc = item.get("description", "")
            ctype = item.get("content_type", "")
            parent_path_str = " / ".join(item.get("parent_path", []))

            if type_filter and type_filter.lower() not in ctype.lower():
                continue

            searchable_text = f"{title} {desc} {parent_path_str}".lower()
            if query_lower in searchable_text or query == item.get("content_id"):
                match_record = dict(item)
                match_record["course_id"] = cid
                match_record["course_name"] = cname
                c_matches.append(match_record)

        if discussions and (not type_filter or type_filter.lower() in "discussion"):
            already = {m.get("title", "").strip().lower() for m in c_matches}
            c_matches.extend(_match_discussions(discussions, query_lower, cid, cname, already))
        return c_matches

    results = await asyncio.gather(*[_search_course(cid, cname) for cid, cname in courses.items()])
    for c_matches in results:
        matches.extend(c_matches)

    return matches


def format_search_results(matches: List[Dict[str, Any]], query: str) -> str:
    """Matches grouped by course (short label): type, title and where it lives; downloads marked."""
    if not matches:
        return f"No items matching '{query}'."
    by_course: Dict[str, List[Dict[str, Any]]] = {}
    for m in matches:
        by_course.setdefault(short_course(m.get("course_name")), []).append(m)
    lines = [f"Search '{query}': {len(matches)} match{'es' if len(matches) != 1 else ''} in {len(by_course)} course{'s' if len(by_course) != 1 else ''}"]
    for label in sorted(by_course):
        lines.append("")
        lines.append(f"{label}  ({len(by_course[label])})")
        rows = []
        for m in by_course[label]:
            where = " > ".join(m.get("parent_path") or [])
            flags = ["file"] if m.get("is_downloadable") else []
            if m.get("due_date"):
                flags.append(f"due {m['due_date']}")
            rows.append((
                m.get("content_type", "item"),
                clip(m.get("title", ""), 52),
                clip(where, 44),
                ", ".join(flags),
            ))
        for m, line in zip(by_course[label], align(rows)):
            lines.append(line)
            desc = m.get("description") or ""
            if desc and query.lower() in desc.lower() and query.lower() not in m.get("title", "").lower():
                lines.append(f"      {_snippet(desc, query.lower(), 50)}")
    return "\n".join(lines)


_CONTAINER_TYPES = ("folder", "learning_module")
# Item types that can carry a file. Everything else (assignment, test, link, ...) is not downloadable.
_FILE_BEARING_TYPES = ("file", "document", "syllabus", "item")


def _is_container(item: Dict[str, Any]) -> bool:
    return item.get("content_type") in _CONTAINER_TYPES or bool(item.get("has_children"))


def files_within(outline: List[Dict[str, Any]], folder_id: str) -> List[Dict[str, Any]]:
    """Downloadable files anywhere under a folder (outline items are a flat list linked by parent_id)."""
    children: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for item in outline:
        children.setdefault(item.get("parent_id"), []).append(item)
    found: List[Dict[str, Any]] = []
    stack = list(reversed(children.get(folder_id, [])))
    while stack:
        item = stack.pop()
        if item.get("is_downloadable"):
            found.append({"title": item["title"], "content_id": item["content_id"], "path": item.get("parent_path", [])})
        stack.extend(reversed(children.get(item.get("content_id"), [])))
    return found


def choose_download_target(matches: List[Dict[str, Any]], query: str) -> Dict[str, Any]:
    """Pick what to do with the matches. Returns {'item': ...} or {'status': ..., details}."""
    exact_id = [m for m in matches if m.get("content_id") == query]
    if len(exact_id) == 1:
        return {"item": exact_id[0]}
    leaves = [m for m in matches if not _is_container(m)]
    pool = leaves or matches
    exact_title = [m for m in pool if m.get("title", "").lower() == query.lower()]
    if len(exact_title) == 1:
        return {"item": exact_title[0]}
    if len(pool) == 1:
        return {"item": pool[0]}
    return {"status": "multiple_matches", "matches": pool}


async def grab_item_async(
    target_id_or_title: str,
    courses: Dict[str, str],
    target_cids: Optional[List[str]] = None,
    page: Optional['Page'] = None,
    download_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Find an item by ID or title and download its file(s).

    A name that matches a folder never hits the attachment endpoint: when no file matches, the
    result is ``status: folder`` listing the files inside. Nothing is created on disk unless a
    file is actually written. Progress goes to stderr; the caller prints the result.
    """
    search_courses = {cid: courses[cid] for cid in target_cids if cid in courses} if target_cids else courses
    status(f"Looking for '{target_id_or_title}' in {len(search_courses)} course{'s' if len(search_courses) != 1 else ''}...")

    outlines: Dict[str, List[Dict[str, Any]]] = {}
    target_lower = target_id_or_title.lower()

    async def _scan_course(cid: str, cname: str):
        outline = await scrape_course_outline_async(cid, page=page, max_depth=6)
        outlines[cid] = outline
        return [
            {**item, "course_id": cid, "course_name": cname}
            for item in outline
            if item.get("content_id") == target_id_or_title or target_lower in item.get("title", "").lower()
        ]

    results = await asyncio.gather(*[_scan_course(cid, cname) for cid, cname in search_courses.items()])
    all_matches = [m for found in results for m in found]
    if not all_matches:
        return {"status": "not_found", "query": target_id_or_title}

    choice = choose_download_target(all_matches, target_id_or_title)
    if choice.get("status") == "multiple_matches":
        return {
            "status": "multiple_matches",
            "query": target_id_or_title,
            "matches": [
                {"course_id": m["course_id"], "course_name": m["course_name"], "title": m["title"],
                 "content_id": m["content_id"], "content_type": m.get("content_type", "item")}
                for m in choice["matches"]
            ],
        }

    target_item = choice["item"]
    cid, cname = target_item["course_id"], target_item["course_name"]
    base = {
        "course_id": cid, "course_name": cname, "content_id": target_item["content_id"],
        "title": target_item["title"], "content_type": target_item.get("content_type", "item"),
    }

    if _is_container(target_item):
        files = files_within(outlines[cid], target_item["content_id"])
        return {**base, "status": "folder", "files": files}
    if target_item.get("content_type") not in _FILE_BEARING_TYPES:
        return {**base, "status": "not_a_file", "external_url": target_item.get("external_url")}

    downloaded: List[Dict[str, Any]] = []
    if download_dir:
        safe_course = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in short_course(cname)).strip("_")
        status(f"Downloading '{target_item['title']}' from {short_course(cname)}...")
        downloaded = await asyncio.to_thread(
            download_content_item_files,
            course_id=cid,
            content_id=target_item["content_id"],
            destination_dir=Path(download_dir) / safe_course,
            default_filename=target_item["title"],
            allow_default_endpoint=target_item.get("content_type") == "file",
        )
    return {**base, "status": "success" if downloaded else "found_no_attachments", "downloaded_files": downloaded}


def format_download_result(result: Dict[str, Any]) -> str:
    """One short human summary of a grab_item_async result."""
    st = result.get("status")
    if st == "not_found":
        return f"No item named '{result.get('query')}' found. Try: bb search \"{result.get('query')}\""
    if st == "multiple_matches":
        lines = [f"'{result['query']}' matches {len(result['matches'])} items; be more specific or pass the ID:"]
        rows = [(short_course(m["course_name"]), m.get("content_type", "item"), clip(m["title"], 52), m["content_id"]) for m in result["matches"]]
        return "\n".join(lines + align(rows))
    label = f"{short_course(result.get('course_name'))}: {result.get('title')}"
    if st == "folder":
        files = result["files"]
        if not files:
            return f"{label} is a folder with no downloadable files."
        lines = [f"{label} is a folder, not a file. Files inside ({len(files)}); download one by name or ID:"]
        lines += align([(clip(f["title"], 56), f["content_id"]) for f in files])
        return "\n".join(lines)
    if st == "not_a_file":
        extra = f" ({result['external_url']})" if result.get("external_url") else ""
        return f"{label} is a {result.get('content_type')}, not a downloadable file{extra}."
    if st == "found_no_attachments":
        return f"{label}: no downloadable attachments found."
    lines = [f"{label}: saved {len(result['downloaded_files'])} file{'s' if len(result['downloaded_files']) != 1 else ''}"]
    lines += [f"  {f['saved_to']}  ({f['size_bytes']:,} bytes)" for f in result["downloaded_files"]]
    return "\n".join(lines)
