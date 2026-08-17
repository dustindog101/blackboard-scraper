import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def _to_unix_timestamp(raw: Optional[str]) -> Optional[int]:
    """Best-effort datetime parsing for common Blackboard date strings."""
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None

    text = re.sub(r"\s+", " ", text)
    text = text.replace(" at ", " ")

    formats = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %I:%M %p",
        "%b %d, %Y %I:%M %p",
        "%B %d, %Y %I:%M %p",
        "%m/%d/%Y %I:%M %p",
        "%m/%d %I:%M %p",
        "%b %d, %Y",
        "%B %d, %Y",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%m/%d %I:%M %p":
                dt = dt.replace(year=datetime.now().year)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def build_item(
    *,
    kind: str,
    title: str,
    course_id: Optional[str] = None,
    course_name: Optional[str] = None,
    notes: Optional[str] = None,
    due_text: Optional[str] = None,
    source_ref: Optional[str] = None,
    url: Optional[str] = None,
    group_name: Optional[str] = "School",
    priority: Optional[int] = None,
    is_starred: Optional[bool] = None,
    status: Optional[str] = None,
    tags: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Constructs a normalized individual school item dict."""
    item: Dict[str, Any] = {
        "kind": kind,
        "course_id": course_id,
        "course_name": course_name,
        "title": (title or "").strip(),
        "notes": (notes or "").strip() or None,
        "due_at": _to_unix_timestamp(due_text),
        "source_ref": source_ref,
        "url": url,
        "group_name": group_name,
        "priority": priority,
        "is_starred": is_starred,
        "status": status,
        "tags": tags or [],
        "metadata": metadata or {},
    }
    if due_text:
        item["metadata"]["due_text"] = due_text
    return item


def build_export_doc(
    items: List[Dict[str, Any]],
    *,
    source: str = "blackboard-scraper",
    pretty: bool = True,
) -> str:
    """Serialize items into the standard v2 export envelope and return a JSON string."""
    doc = {
        "version": "2.0",
        "source": source,
        "generated_at": int(datetime.now().timestamp()),
        "generated_at_human": datetime.now().isoformat(),
        "total_items": len(items),
        "items": items,
    }
    if pretty:
        return json.dumps(doc, indent=2, ensure_ascii=False)
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


def build_composite_schema(
    bundle: Dict[str, Any],
    *,
    user_info: Optional[Dict[str, Any]] = None,
    source: str = "blackboard-scraper",
    pretty: bool = True,
) -> str:
    """
    Builds the standardized v2 composite school intelligence JSON document.
    Includes courses, outlines, syllabi, assignments, rubrics, grades, announcements, and global streams.
    """
    courses_data = bundle.get("courses", {})
    calendar_data = bundle.get("calendar", [])
    activity_data = bundle.get("activity", [])

    courses_list = []
    total_assignments = 0
    total_announcements = 0
    unread_announcements = 0

    for cid, cdata in courses_data.items():
        if not isinstance(cdata, dict):
            continue
        cname = cdata.get("course_name", cid)
        anns = cdata.get("announcements", [])
        grades = cdata.get("grades", [])
        outline = cdata.get("outline", [])
        assigns = cdata.get("assignments", [])

        total_announcements += len(anns)
        unread_announcements += len([a for a in anns if a.get("unread")])
        total_assignments += len(assigns)

        # Detect syllabus item in outline
        syllabus = None
        for item in outline:
            if item.get("content_type") == "syllabus" or "syllabus" in item.get("title", "").lower():
                syllabus = item
                break

        courses_list.append({
            "course_id": cid,
            "course_name": cname,
            "syllabus": syllabus,
            "outline": outline,
            "assignments": assigns,
            "grades": grades,
            "announcements": anns,
        })

    doc = {
        "version": "2.0",
        "source": source,
        "generated_at": int(datetime.now().timestamp()),
        "generated_at_human": datetime.now().isoformat(),
        "summary": {
            "total_courses": len(courses_list),
            "upcoming_deadlines_count": len(calendar_data),
            "total_announcements_count": total_announcements,
            "unread_announcements_count": unread_announcements,
        },
        "user": user_info or {},
        "courses": courses_list,
        "global": {
            "activity_stream": activity_data,
            "calendar_due_dates": calendar_data,
        },
    }

    if pretty:
        return json.dumps(doc, indent=2, ensure_ascii=False)
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


def write_export(
    items: Any,
    *,
    output_path: str,
    source: str = "blackboard-scraper",
    pretty: bool = True,
) -> Path:
    """Write the export envelope to *output_path* and return the Path."""
    export_path = Path(output_path)
    export_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(items, dict) and "courses" in items:
        payload = build_composite_schema(items, source=source, pretty=pretty)
    elif isinstance(items, list):
        payload = build_export_doc(items, source=source, pretty=pretty)
    else:
        payload = json.dumps(items, indent=2 if pretty else None, ensure_ascii=False)
    export_path.write_text(payload)
    return export_path
