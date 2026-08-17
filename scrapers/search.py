import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from playwright.async_api import Page

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir
from scrapers.outline import scrape_course_outline_async

logger = logging.getLogger("blackboard.scrapers.search")


async def find_items_async(
    query: str,
    courses: Dict[str, str],
    page: Page,
    type_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Searches across courses for content items, assignments, or documents matching `query`.
    """
    print(f"🔍 Searching for '{query}' across {len(courses)} courses...")
    query_lower = query.lower()
    matches: List[Dict[str, Any]] = []

    for cid, cname in courses.items():
        outline = await scrape_course_outline_async(cid, page, max_depth=3)
        for item in outline:
            title = item.get("title", "")
            desc = item.get("description", "")
            ctype = item.get("content_type", "")

            if type_filter and type_filter.lower() not in ctype.lower():
                continue

            if query_lower in title.lower() or query_lower in desc.lower():
                matches.append({
                    "course_id": cid,
                    "course_name": cname,
                    "title": title,
                    "content_type": ctype,
                    "due_date": item.get("due_date", ""),
                    "description": desc,
                    "links": item.get("links", []),
                    "content_id": item.get("content_id", ""),
                })

    print(f"   ✅ Found {len(matches)} matching items.")
    return matches


async def grab_item_async(
    target_id_or_title: str,
    course_id: str,
    page: Page,
    download_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Pulls complete content for a specific item (instructions, attachments, document text).
    """
    print(f"📦 Grabbing item '{target_id_or_title}' in course {course_id}...")
    outline = await scrape_course_outline_async(course_id, page, max_depth=4)

    target_item = None
    target_lower = target_id_or_title.lower()
    for item in outline:
        if item.get("content_id") == target_id_or_title or target_lower in item.get("title", "").lower():
            target_item = item
            break

    if not target_item:
        print(f"   ❌ Item '{target_id_or_title}' not found in {course_id}.")
        return {}

    print(f"   Found item: {target_item['title']} [{target_item['content_type']}]")
    return target_item
