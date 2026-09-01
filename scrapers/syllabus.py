"""
Syllabus Auto-Discovery, Extractor & Local Sync Engine
Traverses course outlines via REST Fast-Path to locate syllabi across native Ultra documents,
binary PDF attachments, and external links (e.g. Google Docs).
Supports automated local mirroring to `courses/<CourseName>/syllabus/` with YAML metadata.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import BLACKBOARD_BASE, load_courses
from core.async_engine import AdaptiveDOM, AsyncSessionManager, EngineConfig
from scrapers.outline import get_cookie_header, _api_get, _crawl_api_tree

logger = logging.getLogger("blackboard.scrapers.syllabus")

GOOGLE_DOC_RE = re.compile(r"docs\.google\.com/document/d/([a-zA-Z0-9_-]+)")


def is_syllabus_item(content_type: str, title: str, desc: str = "") -> bool:
    """Classifies whether an outline node represents a course syllabus."""
    if content_type == "syllabus":
        return True
    t_lower = (title or "").lower()
    d_lower = (desc or "").lower()
    if "syllabus" in t_lower or "syllabus" in d_lower:
        return True
    return False


def rewrite_google_doc_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Transforms a Google Docs URL into direct PDF and plain-text export URLs.
    Returns (pdf_export_url, txt_export_url).
    """
    match = GOOGLE_DOC_RE.search(url)
    if not match:
        return None, None
    doc_id = match.group(1)
    pdf_url = f"https://docs.google.com/document/d/{doc_id}/export?format=pdf"
    txt_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    return pdf_url, txt_url


def _sanitize_folder_name(name: str) -> str:
    """Cleans course name for safe filesystem directory creation."""
    cleaned = re.sub(r"\s*\([^)]*\).*$", "", name).strip()
    cleaned = re.sub(r"[^\w\s-]", "", cleaned).strip()
    return re.sub(r"[-\s]+", "_", cleaned)


def _compute_sha256(data: bytes) -> str:
    """Computes SHA-256 hash for content comparison."""
    return hashlib.sha256(data).hexdigest()


def _find_target_course_dir(root: Path, course_name: str) -> Path:
    courses_root = None
    for candidate in (root, root.parent, root.parent.parent):
        cand = candidate / "courses"
        if cand.exists():
            courses_root = cand
            break
    if not courses_root:
        courses_root = root / "courses"

    m = re.search(r"([A-Z]{3,5})\s*(\d{3})", course_name, re.IGNORECASE)
    if m:
        code_compact = f"{m.group(1).upper()}{m.group(2)}"
        code_spaced = f"{m.group(1).upper()} {m.group(2)}"
        for candidate in (code_compact, code_spaced):
            cand_path = courses_root / candidate
            if cand_path.exists():
                return cand_path / "syllabus"

    safe_folder = _sanitize_folder_name(course_name)
    return courses_root / safe_folder / "syllabus"


def sync_syllabus_file(record: Dict[str, Any], base_dir: Optional[str] = None) -> Tuple[str, bool]:
    """
    Saves a syllabus record into `courses/<CourseName>/syllabus/` idempotently.
    Handles text/markdown files and binary attachments.
    Returns (saved_path, was_modified).
    """
    root = Path(base_dir) if base_dir else Path.cwd()
    course_name = record.get("course_name", record.get("course_id", "Course"))

    syllabus_dir = _find_target_course_dir(root, course_name)
    syllabus_dir.mkdir(parents=True, exist_ok=True)

    source_type = record.get("source_type", "unknown")
    title = record.get("title", "Syllabus")
    safe_title = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_") or "Syllabus"

    now_iso = datetime.now(timezone.utc).isoformat()
    cid = record.get("content_id", "")
    course_id = record.get("course_id", "")
    source_url = record.get("source_url", "")

    # 1. Text / Markdown Syllabus
    body_text = record.get("body_text", "")
    if body_text:
        target_file = syllabus_dir / f"{safe_title}.md"
        frontmatter = (
            "---\n"
            f"title: '{title}'\n"
            f"course_id: '{course_id}'\n"
            f"course_name: '{course_name}'\n"
            f"content_id: '{cid}'\n"
            f"source_type: '{source_type}'\n"
            f"source_url: '{source_url}'\n"
            f"last_synced: '{now_iso}'\n"
            "---\n\n"
        )
        full_content = frontmatter + body_text
        content_bytes = full_content.encode("utf-8")
        new_hash = _compute_sha256(body_text.encode("utf-8"))

        if target_file.exists():
            try:
                existing_content = target_file.read_text(encoding="utf-8")
                parts = existing_content.split("---\n\n", 1)
                existing_body = parts[1] if len(parts) > 1 else existing_content
                if _compute_sha256(existing_body.encode("utf-8")) == new_hash:
                    return str(target_file), False
            except Exception:
                pass

        target_file.write_bytes(content_bytes)
        return str(target_file), True

    # 2. Binary attachment / download URL
    download_url = record.get("export_url") or record.get("source_url")
    if download_url and (source_type == "file" or "attachments" in download_url or "export?format=pdf" in download_url):
        target_file = syllabus_dir / f"{safe_title}.pdf"
        cookie_header = get_cookie_header()
        headers = {"User-Agent": "Mozilla/5.0"}
        if cookie_header and "blackboard.umbc.edu" in download_url:
            headers["Cookie"] = cookie_header

        try:
            req = urllib.request.Request(download_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
                new_hash = _compute_sha256(data)
                if target_file.exists():
                    try:
                        existing_data = target_file.read_bytes()
                        if _compute_sha256(existing_data) == new_hash:
                            return str(target_file), False
                    except Exception:
                        pass

                target_file.write_bytes(data)

                # Write metadata sidecar
                sidecar = syllabus_dir / f"{safe_title}.meta.json"
                meta = {
                    "title": title,
                    "course_id": course_id,
                    "course_name": course_name,
                    "content_id": cid,
                    "source_type": source_type,
                    "source_url": source_url,
                    "content_hash": new_hash,
                    "last_synced": now_iso,
                }
                sidecar.write_text(json.dumps(meta, indent=2))
                return str(target_file), True
        except Exception as e:
            logger.debug(f"Binary syllabus download error for {download_url}: {e}")

    return str(syllabus_dir / f"{safe_title}.md"), False


def discover_course_syllabi_api(
    course_id: str,
    sync: bool = False,
    courses: Optional[Dict[str, str]] = None,
    base_sync_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Scans an individual course outline for syllabus items."""
    cookie_header = get_cookie_header()
    if not cookie_header:
        return {"course_id": course_id, "course_name": course_id, "syllabus_found": False, "items": []}

    if courses is None:
        courses = load_courses()

    course_name = courses.get(course_id, course_id)
    tree = _crawl_api_tree(course_id, cookie_header, max_depth=5)
    if not tree:
        return {"course_id": course_id, "course_name": course_name, "syllabus_found": False, "items": []}

    def find_nodes(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        nodes: List[Dict[str, Any]] = []
        for item in items:
            content_type = item.get("content_type", "")
            title = item.get("title", "")
            description = item.get("description", "")
            if is_syllabus_item(content_type, title, description):
                nodes.append(item)
            if item.get("children"):
                nodes.extend(find_nodes(item["children"]))
        return nodes

    candidates = find_nodes(tree)
    items_list: List[Dict[str, Any]] = []

    for cand in candidates:
        cand_id = cand.get("content_id", "")
        title = cand.get("title", "Syllabus")
        ext_url = cand.get("external_url") or ""
        source_type = "blackboard_item"
        source_url = ext_url
        export_url = None
        body_text = cand.get("description", "")

        if ext_url and "docs.google.com/document" in ext_url:
            source_type = "google_doc"
            pdf_url, txt_url = rewrite_google_doc_url(ext_url)
            export_url = pdf_url
            if txt_url:
                try:
                    req = urllib.request.Request(txt_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        body_text = resp.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
        elif cand.get("attachments"):
            source_type = "file"
            att = cand["attachments"][0]
            source_url = att.get("download_url") or ""
            export_url = source_url

        rec = {
            "content_id": cand_id,
            "title": title,
            "source_type": source_type,
            "source_url": source_url,
            "export_url": export_url,
            "body_text": body_text,
            "synced": False,
            "local_path": None,
            "content_hash": _compute_sha256(body_text.encode("utf-8")) if body_text else None,
            "last_synced": None,
        }

        if sync and (body_text or export_url or source_url):
            saved_path, _ = sync_syllabus_file(
                {**rec, "course_id": course_id, "course_name": course_name},
                base_dir=base_sync_dir,
            )
            rec["synced"] = True
            rec["local_path"] = saved_path
            rec["last_synced"] = datetime.now(timezone.utc).isoformat()

        items_list.append(rec)

    return {
        "course_id": course_id,
        "course_name": course_name,
        "syllabus_found": len(items_list) > 0,
        "items": items_list,
    }


def discover_syllabi_api(
    course_id: Optional[str] = None,
    all_courses: bool = False,
    sync: bool = False,
    courses: Optional[Dict[str, str]] = None,
    base_sync_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Discovers syllabi across one or all courses via REST Fast-Path.
    Executes multiple course scans concurrently when all_courses=True.
    Returns a standardized list of course syllabus bundles matching spec schema.
    """
    if courses is None:
        courses = load_courses()

    target_cids = list(courses.keys()) if (all_courses or not course_id) else [course_id]

    if len(target_cids) > 1:
        # Concurrent execution across courses
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(target_cids), 6)) as executor:
            futures = [
                executor.submit(discover_course_syllabi_api, cid, sync, courses, base_sync_dir)
                for cid in target_cids
            ]
            return [f.result() for f in futures]
    elif target_cids:
        return [discover_course_syllabi_api(target_cids[0], sync, courses, base_sync_dir)]

    return []


async def discover_syllabi_playwright_async(
    course_id: str,
    page: Any,
) -> Dict[str, Any]:
    """
    Playwright fallback for syllabus discovery if REST API is restricted.
    """
    url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/outline"
    courses = load_courses()
    course_name = courses.get(course_id, course_id)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except Exception as e:
        logger.debug(f"Playwright syllabus outline navigation failed: {e}")
        return {"course_id": course_id, "course_name": course_name, "syllabus_found": False, "items": []}

    await AdaptiveDOM.wait_for_any_selector(page, ["div.course-outline-tree", "bb-course-outline", "div[role='tree']"], timeout=8000)

    items = await page.evaluate("""() => {
        const results = [];
        document.querySelectorAll('bb-content-item, div[role="treeitem"]').forEach(el => {
            const title = el.innerText.trim();
            if (/syllabus/i.test(title)) {
                results.push({
                    title: title.split('\\n')[0],
                    content_id: el.getAttribute('data-content-id') || '',
                    source_type: 'browser_item',
                    source_url: window.location.href,
                    body_text: '',
                });
            }
        });
        return results;
    }""")

    return {
        "course_id": course_id,
        "course_name": course_name,
        "syllabus_found": len(items) > 0,
        "items": items,
    }


def format_syllabi_cli(bundles: List[Dict[str, Any]]) -> str:
    """Formats discovered syllabi into clean terminal view adhering to CONTEXT.md glossary."""
    any_found = any(b.get("syllabus_found") for b in bundles)
    if not any_found:
        return "\nℹ️  No syllabi found in the selected course(s).\n"

    lines = [
        "",
        "╔═══════════════════════════════════════════════════════════════════════════╗",
        "║  📜 COURSE SYLLABI SUMMARY",
        "╚═══════════════════════════════════════════════════════════════════════════╝",
    ]

    item_idx = 1
    for bundle in bundles:
        cname = bundle.get("course_name", bundle.get("course_id"))
        items = bundle.get("items", [])
        if not items:
            continue

        lines.append(f"\n▶ {cname}")
        for r in items:
            title = r.get("title", "Syllabus")
            stype = r.get("source_type", "link")
            url = r.get("source_url") or r.get("export_url") or "Blackboard Internal"
            local = r.get("local_path")

            badge = "📄 Google Doc" if stype == "google_doc" else "📎 File / Attachment" if stype == "file" else "📝 Blackboard Doc"

            lines.append(f"  [{item_idx}] {title} ({badge})")
            lines.append(f"      • Source Link: {url}")
            if local:
                lines.append(f"      • Local Mirror: 💾 {local}")

            body = r.get("body_text", "").strip()
            if body:
                preview = body[:250].replace("\n", " ").strip()
                lines.append(f"      • Preview: \"{preview}...\"")
            item_idx += 1

    lines.append("")
    return "\n".join(lines)
