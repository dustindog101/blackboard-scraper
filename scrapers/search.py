import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from playwright.async_api import Page

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir
from scrapers.outline import download_course_file, scrape_course_outline_async

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

    for cid, cname in courses.items():
        outline = await scrape_course_outline_async(cid, page=page, max_depth=6)
        for item in outline:
            title = item.get("title", "")
            desc = item.get("description", "")
            ctype = item.get("content_type", "")
            parent_path_str = " / ".join(item.get("parent_path", []))
            attachment_names = " ".join([a.get("file_name", "") for a in item.get("attachments", [])])

            if type_filter and type_filter.lower() not in ctype.lower():
                continue

            searchable_text = f"{title} {desc} {parent_path_str} {attachment_names}".lower()
            if query_lower in searchable_text:
                match_record = dict(item)
                match_record["course_id"] = cid
                match_record["course_name"] = cname
                matches.append(match_record)

    print(f"   ✅ Found {len(matches)} matching items.")
    return matches


async def grab_item_async(
    target_id_or_title: str,
    course_id: str,
    page: Optional[Page] = None,
    download_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Pulls complete content for a specific item (instructions, attachments, download links).
    Optionally downloads file attachments to disk if `download_dir` is provided.
    """
    courses = load_courses()
    course_name = courses.get(course_id, course_id)
    print(f"📦 Grabbing item '{target_id_or_title}' in {course_name} ({course_id})...")
    outline = await scrape_course_outline_async(course_id, page=page, max_depth=6)

    target_item = None
    target_lower = target_id_or_title.lower()
    for item in outline:
        if item.get("content_id") == target_id_or_title or target_lower in item.get("title", "").lower():
            target_item = item
            break

    if not target_item:
        print(f"   ❌ Item '{target_id_or_title}' not found in {course_name}.")
        return {}

    print(f"   ✅ Found: {target_item['title']} [{target_item['content_type']}]")

    # Download attachments if download_dir is specified
    if download_dir and target_item.get("is_downloadable"):
        download_dir = Path(download_dir)
        attachments = target_item.get("attachments") or []

        # Fallback for DOM-scraped items with direct link or content ID
        if not attachments:
            if target_item.get("download_url"):
                attachments = [{
                    "file_name": target_item.get("title", "downloaded_file"),
                    "download_url": target_item["download_url"],
                }]
            elif target_item.get("content_id"):
                cid = target_item["content_id"]
                dl_url = f"{BLACKBOARD_BASE}/learn/api/public/v1/courses/{course_id}/contents/{cid}/attachments/default/download"
                attachments = [{
                    "file_name": target_item.get("title", "downloaded_file"),
                    "download_url": dl_url,
                }]

        for att in attachments:
            dl_url = att.get("download_url")
            fname = att.get("file_name", "downloaded_file")
            if dl_url:
                dest = download_dir / fname
                print(f"   📥 Downloading '{fname}' to {dest}...")
                success = download_course_file(dl_url, dest)
                if success:
                    print(f"   ✨ Saved: {dest}")
                else:
                    print(f"   ⚠️ Download failed for {fname}")

    return target_item

