import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import OUTPUT_BASE


def _to_unix_timestamp(raw: str | None) -> int | None:
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
    course_id: str | None = None,
    course_name: str | None = None,
    notes: str | None = None,
    due_text: str | None = None,
    source_ref: str | None = None,
    url: str | None = None,
    group_name: str | None = "School",
    priority: int | None = None,
    is_starred: bool | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
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
    items: list[dict[str, Any]],
    *,
    source: str = "blackboard-scraper",
    pretty: bool = True,
) -> str:
    """Serialize items into the export envelope and return a JSON string."""
    doc = {
        "source": source,
        "generated_at": int(datetime.now().timestamp()),
        "items": items,
    }
    if pretty:
        return json.dumps(doc, indent=2, ensure_ascii=False)
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


def write_export(
    items: list[dict[str, Any]],
    *,
    output_path: str,
    source: str = "blackboard-scraper",
    pretty: bool = True,
) -> Path:
    """Write the export envelope to *output_path* and return the Path."""
    export_path = Path(output_path)
    export_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_export_doc(items, source=source, pretty=pretty)
    export_path.write_text(payload)
    return export_path
