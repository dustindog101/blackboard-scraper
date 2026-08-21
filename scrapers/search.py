import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from playwright.async_api import Page

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir
from scrapers.outline import (
    download_content_item_files,
    download_course_file,
    fetch_item_attachments,
    scrape_course_outline_async,
)

logger = logging.getLogger("blackboard.scrapers.search")


async def find_items_async(
    query: str,
    courses: Dict[str, str],
    page: Optional[Page] = None,
    type_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Searches across courses for content items, assignments, documents, or files matching `query`.
    Searches item title, parent folder hierarchy, descriptions, and attachment filenames.
    """
    print(f"🔍 Searching for '{query}' across {len(courses)} courses...")
    query_lower = query.lower()
    matches: List[Dict[str, Any]] = []

    async def _search_course(cid: str, cname: str):
        outline = await scrape_course_outline_async(cid, page=page, max_depth=6)
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
        return c_matches

    results = await asyncio.gather(*[_search_course(cid, cname) for cid, cname in courses.items()])
    for c_matches in results:
        matches.extend(c_matches)

    print(f"   ✅ Found {len(matches)} matching items.")
    return matches


async def grab_item_async(
    target_id_or_title: str,
    courses: Dict[str, str],
    target_cids: Optional[List[str]] = None,
    page: Optional[Page] = None,
    download_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Finds and downloads an item by ID or title across specified courses or all active courses.
    Handles auto-discovery across courses if no course is specified.
    """
    search_courses = {cid: courses[cid] for cid in target_cids if cid in courses} if target_cids else courses

    if len(search_courses) == 1:
        cid, cname = list(search_courses.items())[0]
        print(f"📦 Grabbing item '{target_id_or_title}' in {cname} ({cid})...")
    else:
        print(f"📦 Searching for '{target_id_or_title}' across {len(search_courses)} courses to download...")

    # Crawl target courses
    all_matches: List[Dict[str, Any]] = []

    async def _scan_course(cid: str, cname: str):
        outline = await scrape_course_outline_async(cid, page=page, max_depth=6)
        found = []
        target_lower = target_id_or_title.lower()
        for item in outline:
            cid_match = (item.get("content_id") == target_id_or_title)
            title_match = (target_lower in item.get("title", "").lower())
            if cid_match or title_match:
                rec = dict(item)
                rec["course_id"] = cid
                rec["course_name"] = cname
                found.append(rec)
        return found

    results = await asyncio.gather(*[_scan_course(cid, cname) for cid, cname in search_courses.items()])
    for c_found in results:
        all_matches.extend(c_found)

    if not all_matches:
        print(f"   ❌ Item '{target_id_or_title}' not found in any searched course.")
        return {"status": "not_found", "query": target_id_or_title}

    # If multiple matches found in different courses, ask user to disambiguate unless one is an exact ID match
    exact_id_matches = [m for m in all_matches if m.get("content_id") == target_id_or_title]
    exact_title_matches = [m for m in all_matches if m.get("title", "").lower() == target_id_or_title.lower()]

    if len(exact_id_matches) == 1:
        target_item = exact_id_matches[0]
    elif len(exact_title_matches) == 1:
        target_item = exact_title_matches[0]
    elif len(all_matches) == 1:
        target_item = all_matches[0]
    else:
        # Multiple matches
        print(f"   ⚠️ Found {len(all_matches)} matching items across courses for '{target_id_or_title}':")
        for i, m in enumerate(all_matches, 1):
            print(f"      {i}. [{m['course_name']}] {m['title']} (ID: {m['content_id']})")
        print(f"   💡 Please specify course: bb --download \"{target_id_or_title}\" -c <CourseID>")
        return {
            "status": "multiple_matches",
            "query": target_id_or_title,
            "matches": [
                {"course_id": m["course_id"], "course_name": m["course_name"], "title": m["title"], "content_id": m["content_id"]}
                for m in all_matches
            ]
        }

    cid = target_item["course_id"]
    cname = target_item["course_name"]
    content_id = target_item["content_id"]
    item_title = target_item["title"]

    print(f"   ✅ Found: {item_title} [{target_item.get('content_type', 'item')}] in {cname}")

    downloaded_files = []
    if download_dir:
        # Sanitize folder name
        safe_course_folder = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in cname).strip("_")
        target_dir = Path(download_dir) / safe_course_folder
        print(f"   📥 Downloading files to {target_dir}...")
        downloaded_files = download_content_item_files(
            course_id=cid,
            content_id=content_id,
            destination_dir=target_dir,
            default_filename=item_title,
        )
        if downloaded_files:
            for df in downloaded_files:
                print(f"   ✨ Saved: {df['saved_to']} ({df['size_bytes']:,} bytes)")
        else:
            print(f"   ⚠️ No downloadable file attachments found for {item_title}.")

    return {
        "status": "success" if downloaded_files else "found_no_attachments",
        "course_id": cid,
        "course_name": cname,
        "content_id": content_id,
        "title": item_title,
        "content_type": target_item.get("content_type", "item"),
        "downloaded_files": downloaded_files,
    }

