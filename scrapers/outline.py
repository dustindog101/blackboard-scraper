import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from core.config import BLACKBOARD_BASE, SESSION_DIR, load_courses
from core.output import ensure_output_dir
from core.async_engine import AdaptiveDOM

logger = logging.getLogger("blackboard.scrapers.outline")


# ============================================================================
# 1. Session & REST API Helpers
# ============================================================================

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


def _api_get(url: str, cookie_header: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    """Execute authenticated HTTP GET against Blackboard REST API."""
    headers = {
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            return {"_http_status": e.code, "error": str(e.reason)}
        logger.debug(f"HTTP Error {e.code} for {url}: {e}")
        return None
    except Exception as e:
        logger.debug(f"Network error for {url}: {e}")
        return None


def normalize_content_type(handler_id: str, title: str, html_desc: str = "") -> str:
    """Classifies Blackboard content handler IDs into clean canonical types."""
    handler = (handler_id or "").lower()
    t_lower = (title or "").lower()
    d_lower = (html_desc or "").lower()

    if "syllabus" in t_lower or "syllabus" in d_lower:
        return "syllabus"
    if "folder" in handler:
        return "folder"
    if "lesson" in handler or "learning-module" in handler or "learningmodule" in handler:
        return "learning_module"
    if "file" in handler:
        return "file"
    if "externallink" in handler or "weblink" in handler:
        return "link"
    if "assignment" in handler or "asmt" in handler:
        return "assignment"
    if "test" in handler or "quiz" in handler or "exam" in handler:
        return "test"
    if "discussion" in handler:
        return "discussion"
    if "document" in handler or "doc" in handler:
        return "document"
    if "blti" in handler or "lti" in handler:
        return "lti_tool"
    return "item"


def _clean_title(raw_title: str, parent_path: List[str]) -> str:
    """Cleans up internal artifact titles (like 'ultraDocumentBody')."""
    if not raw_title or raw_title.strip() == "ultraDocumentBody":
        if parent_path:
            return f"{parent_path[-1]} Document"
        return "Course Document"
    return raw_title.strip()


# ============================================================================
# 2. High-Speed REST API Tree Crawler (Primary Engine)
# ============================================================================

def _crawl_api_tree(
    course_id: str,
    cookie_header: str,
    parent_id: Optional[str] = None,
    parent_path: Optional[List[str]] = None,
    depth: int = 0,
    max_depth: int = 6,
) -> List[Dict[str, Any]]:
    """
    Recursively extracts the full course outline tree via Blackboard REST API.
    Handles folders, learning modules, files, direct attachments, external links, and descriptions.
    """
    if depth > max_depth:
        return []

    parent_path = parent_path or []
    if parent_id:
        url = f"{BLACKBOARD_BASE}/learn/api/public/v1/courses/{course_id}/contents/{parent_id}/children"
    else:
        url = f"{BLACKBOARD_BASE}/learn/api/public/v1/courses/{course_id}/contents"

    data = _api_get(url, cookie_header)
    if not data:
        return []

    if "_http_status" in data:
        if data["_http_status"] in (401, 403):
            logger.debug(f"Course {course_id} is closed/unavailable (HTTP {data['_http_status']}).")
        return []

    results = data.get("results", [])
    extracted: List[Dict[str, Any]] = []

    for item in results:
        avail = item.get("availability", {}).get("available", "Yes")
        if avail.lower() == "no":
            continue

        cid = item.get("id", "")
        raw_title = item.get("title", "Untitled Item")
        title = _clean_title(raw_title, parent_path)
        desc = item.get("description", "") or ""
        handler_id = item.get("contentHandler", {}).get("id", "")
        content_type = normalize_content_type(handler_id, title, desc)
        has_children = bool(item.get("hasChildren")) or content_type in ("folder", "learning_module")

        # Resolve attachments / direct download URLs
        attachments: List[Dict[str, Any]] = []
        download_url: Optional[str] = None
        is_downloadable = False

        if content_type == "file":
            is_downloadable = True
            file_meta = item.get("contentHandler", {}).get("file", {})
            file_name = file_meta.get("fileName") or title
            mime_type = file_meta.get("mimeType") or "application/octet-stream"
            download_url = f"{BLACKBOARD_BASE}/learn/api/public/v1/courses/{course_id}/contents/{cid}/attachments/default/download"
            attachments.append({
                "id": cid,
                "file_name": file_name,
                "mime_type": mime_type,
                "download_url": download_url,
            })

        # Resolve external link URL
        external_url: Optional[str] = None
        if content_type == "link":
            external_url = item.get("contentHandler", {}).get("url")

        # Format links list for backward compatibility
        links: List[Dict[str, str]] = []
        if external_url:
            links.append({"text": title, "url": external_url})
        for att in attachments:
            if att.get("download_url"):
                links.append({"text": att.get("file_name", "Download"), "url": att["download_url"]})

        node = {
            "content_id": cid,
            "parent_id": parent_id,
            "parent_path": list(parent_path),
            "title": title,
            "content_type": content_type,
            "description": desc.strip(),
            "depth": depth,
            "has_children": has_children,
            "is_downloadable": is_downloadable,
            "download_url": download_url,
            "attachments": attachments,
            "external_url": external_url,
            "links": links,
            "due_date": "",
            "created": item.get("created"),
            "modified": item.get("modified"),
        }
        extracted.append(node)

        # Recursively traverse children if item is a folder or module
        if has_children:
            child_items = _crawl_api_tree(
                course_id=course_id,
                cookie_header=cookie_header,
                parent_id=cid,
                parent_path=parent_path + [title],
                depth=depth + 1,
                max_depth=max_depth,
            )
            extracted.extend(child_items)

    return extracted


def scrape_course_outline_api(course_id: str, max_depth: int = 6) -> List[Dict[str, Any]]:
    """Primary REST API course outline scraper."""
    cookie_header = get_cookie_header()
    if not cookie_header:
        logger.debug("No session cookies available for API outline scraper.")
        return []
    return _crawl_api_tree(course_id, cookie_header, max_depth=max_depth)


# ============================================================================
# 3. Modern Playwright DOM Scraper (Fallback Engine)
# ============================================================================

async def scrape_course_outline_playwright_async(
    course_id: str,
    page: Page,
    max_depth: int = 4,
) -> List[Dict[str, Any]]:
    """
    Fallback browser DOM scraper for Blackboard Ultra & Classic layouts.
    Traverses learning modules, folders, documents, syllabi, attachments, and external links.
    """
    courses = load_courses()
    course_name = courses.get(course_id, course_id)

    url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/outline"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
    except Exception as e:
        logger.debug(f"Navigation error for {course_name}: {e}")
        return []

    # Check for course availability or error banners with modern selectors
    matched_sel, _ = await AdaptiveDOM.wait_for_any_selector(
        page,
        [
            ".course-content-container",
            "section.course-outline-content",
            "[bb-cache-compilation='course-outline']",
            "div.course-outline-tree",
            "bb-course-outline",
            "div[role='tree']",
            "[data-analytics-id='course-outline']",
            "#courseMenuPalette_contents",
            "#content_listContainer",
            "#notification-modal-api-error",
            "div:has-text(\"You can't access this course right now\")",
            "div:has-text(\"Course is not currently available\")",
            "div.empty-state",
            "p:has-text(\"No content\")",
        ],
        timeout=10_000,
    )

    if not matched_sel or "notification-modal" in matched_sel or "You can't access" in matched_sel or "not currently available" in matched_sel:
        return []

    # Expand collapsible folders and learning modules
    for depth in range(max_depth):
        expand_buttons = page.locator("button[aria-expanded='false'], button.accordion-toggle[aria-expanded='false']")
        count = await expand_buttons.count()
        if count == 0:
            break

        clicked_any = False
        for idx in range(min(count, 15)):
            try:
                btn = expand_buttons.nth(idx)
                if await btn.is_visible():
                    await btn.click(timeout=2000)
                    clicked_any = True
                    await asyncio.sleep(0.15)
            except Exception:
                continue

        if not clicked_any:
            break
        await asyncio.sleep(0.3)

    # Extract items for Ultra AND Classic Blackboard layouts
    extracted_items = await page.evaluate("""() => {
        const items = [];
        const seenIds = new Set();

        // 1. Ultra Elements
        const ultraNodes = document.querySelectorAll(
            'bb-content-item, ' +
            'div[role="treeitem"], ' +
            'div.course-outline-item, ' +
            'div.element-details, ' +
            'li.content-item, ' +
            '[data-analytics-id*="outline"], ' +
            'div.element-card'
        );

        ultraNodes.forEach((el, index) => {
            const titleEl = el.querySelector('h3, h4, span.title, a.element-details-link, [class*="itemName"], .js-title');
            const title = titleEl ? titleEl.innerText.trim() : (el.getAttribute('aria-label') || '').trim();
            if (!title || title === 'Course Content') return;

            const analyticsId = el.getAttribute('data-analytics-id') || el.getAttribute('data-content-id') || '';
            const contentId = analyticsId || `outline_node_${index}`;
            if (seenIds.has(contentId)) return;
            seenIds.add(contentId);

            const html = el.outerHTML.toLowerCase();
            let contentType = 'item';
            if (html.includes('syllabus') || title.toLowerCase().includes('syllabus')) contentType = 'syllabus';
            else if (html.includes('folder') || el.querySelector('button[aria-expanded]')) contentType = 'folder';
            else if (html.includes('learning-module') || html.includes('learningmodule')) contentType = 'learning_module';
            else if (html.includes('document') || html.includes('doc')) contentType = 'document';
            else if (html.includes('assignment') || html.includes('assessment')) contentType = 'assignment';
            else if (html.includes('test') || html.includes('quiz') || html.includes('exam')) contentType = 'test';
            else if (html.includes('discussion')) contentType = 'discussion';
            else if (html.includes('weblink') || html.includes('external-link')) contentType = 'link';
            else if (html.includes('file') || html.includes('attachment') || html.includes('.pdf') || html.includes('.ipynb')) contentType = 'file';

            let dueDate = '';
            const dueEl = el.querySelector('[class*="dueDate"], [class*="due-date"], [class*="gradingDetail"]');
            if (dueEl) {
                dueDate = dueEl.innerText.replace(/due\\s*date[:\\s]*/i, '').trim();
            }

            let description = '';
            const descEl = el.querySelector('.element-details-summary, [class*="description"], .js-description, p');
            if (descEl && descEl !== titleEl) {
                description = descEl.innerText.trim();
            }

            const links = [];
            el.querySelectorAll('a[href]').forEach(a => {
                const href = a.href;
                const linkText = a.innerText.trim();
                if (href && !href.startsWith('javascript:')) {
                    links.push({ text: linkText || title, url: href });
                }
            });

            let depth = 0;
            let parent = el.parentElement;
            while (parent && depth < 10) {
                if (parent.getAttribute('role') === 'group' || parent.classList.contains('nested-content')) {
                    depth += 1;
                }
                parent = parent.parentElement;
            }

            items.push({
                content_id: contentId,
                parent_id: null,
                parent_path: [],
                title: title,
                content_type: contentType,
                due_date: dueDate,
                description: description,
                depth: depth,
                has_children: contentType === 'folder' || contentType === 'learning_module',
                is_downloadable: contentType === 'file',
                download_url: links.length > 0 ? links[0].url : null,
                attachments: [],
                external_url: contentType === 'link' && links.length > 0 ? links[0].url : null,
                links: links
            });
        });

        // 2. Classic Layout Elements (if Ultra nodes are empty)
        if (items.length === 0) {
            document.querySelectorAll('#content_listContainer li.clearfix, #content_listContainer .item, .contentList li').forEach((el, index) => {
                const titleEl = el.querySelector('h3 a, .item h3, a');
                const title = titleEl ? titleEl.innerText.trim() : el.innerText.trim().split('\\n')[0];
                if (!title) return;

                const contentId = el.id || `classic_item_${index}`;
                if (seenIds.has(contentId)) return;
                seenIds.add(contentId);

                let contentType = 'item';
                const lower = el.innerText.toLowerCase();
                if (lower.includes('syllabus')) contentType = 'syllabus';
                else if (lower.includes('folder')) contentType = 'folder';
                else if (lower.includes('assignment')) contentType = 'assignment';
                else if (lower.includes('document')) contentType = 'document';

                const links = [];
                el.querySelectorAll('a[href]').forEach(a => {
                    if (a.href && !a.href.startsWith('javascript:')) {
                        links.push({ text: a.innerText.trim() || title, url: a.href });
                    }
                });

                items.push({
                    content_id: contentId,
                    parent_id: null,
                    parent_path: [],
                    title: title,
                    content_type: contentType,
                    due_date: '',
                    description: el.querySelector('.details, .vtbegenerated')?.innerText.trim() || '',
                    depth: 0,
                    has_children: contentType === 'folder',
                    is_downloadable: contentType === 'file',
                    download_url: links.length > 0 ? links[0].url : null,
                    attachments: [],
                    external_url: contentType === 'link' && links.length > 0 ? links[0].url : null,
                    links: links
                });
            });
        }

        return items;
    }""")

    return extracted_items


# ============================================================================
# 4. Async Dispatcher (REST API Fast Path with DOM Fallback)
# ============================================================================

async def scrape_course_outline_async(
    course_id: str,
    page: Optional[Page] = None,
    max_depth: int = 6,
) -> List[Dict[str, Any]]:
    """
    Unified entrypoint: Runs high-speed REST API crawler first (<300ms).
    Falls back gracefully to Playwright DOM extraction if needed.
    """
    # Fast path: REST API extraction
    try:
        api_results = await asyncio.to_thread(scrape_course_outline_api, course_id, max_depth)
        if api_results:
            return api_results
    except Exception as e:
        logger.debug(f"API outline fetch exception for {course_id}: {e}")

    # Fallback path: Playwright browser DOM scraper
    if page:
        try:
            return await scrape_course_outline_playwright_async(course_id, page, max_depth=max_depth)
        except Exception as e:
            logger.debug(f"Playwright outline error for {course_id}: {e}")

    return []


# ============================================================================
# 5. File & Attachment Downloader Helpers
# ============================================================================

def fetch_item_attachments(
    course_id: str,
    content_id: str,
    cookie_header: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Fetches real attachment IDs, filenames, and download URLs for a specific content item.
    """
    cookie_header = cookie_header or get_cookie_header()
    if not cookie_header:
        return []

    url = f"{BLACKBOARD_BASE}/learn/api/public/v1/courses/{course_id}/contents/{content_id}/attachments"
    data = _api_get(url, cookie_header, timeout=8.0)
    if not data or "results" not in data:
        return []

    attachments = []
    for att in data.get("results", []):
        att_id = att.get("id")
        file_name = att.get("fileName", "attachment")
        mime_type = att.get("mimeType", "application/octet-stream")
        dl_url = f"{BLACKBOARD_BASE}/learn/api/public/v1/courses/{course_id}/contents/{content_id}/attachments/{att_id}/download"
        attachments.append({
            "id": att_id,
            "file_name": file_name,
            "mime_type": mime_type,
            "download_url": dl_url,
        })
    return attachments


def download_course_file(
    download_url: str,
    destination_path: Path,
    cookie_header: Optional[str] = None,
) -> bool:
    """
    Downloads an authenticated course file or attachment to local disk.
    Supports both absolute and relative Blackboard URLs.
    Creates parent directories automatically.
    """
    cookie_header = cookie_header or get_cookie_header()
    if not cookie_header:
        logger.error("Cannot download file: Missing session cookies.")
        return False

    if download_url.startswith("/"):
        download_url = f"{BLACKBOARD_BASE}{download_url}"

    headers = {
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        req = urllib.request.Request(download_url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp, open(destination_path, "wb") as f:
            f.write(resp.read())
        return True
    except Exception as e:
        logger.error(f"Failed to download file from {download_url} to {destination_path}: {e}")
        return False


def download_content_item_files(
    course_id: str,
    content_id: str,
    destination_dir: Path,
    default_filename: str = "downloaded_file",
    cookie_header: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Resolves true attachment IDs for an item and downloads all associated files.
    Returns list of downloaded file info dicts.
    """
    destination_dir.mkdir(parents=True, exist_ok=True)
    attachments = fetch_item_attachments(course_id, content_id, cookie_header)
    downloaded = []

    if attachments:
        for att in attachments:
            fname = att.get("file_name") or default_filename
            dest = destination_dir / fname
            success = download_course_file(att["download_url"], dest, cookie_header)
            if success:
                downloaded.append({
                    "file_name": fname,
                    "saved_to": str(dest),
                    "size_bytes": dest.stat().st_size if dest.exists() else 0,
                })
    else:
        # Fallback to direct content attachment download endpoint
        fallback_url = f"{BLACKBOARD_BASE}/learn/api/public/v1/courses/{course_id}/contents/{content_id}/attachments/default/download"
        dest = destination_dir / default_filename
        success = download_course_file(fallback_url, dest, cookie_header)
        if success:
            downloaded.append({
                "file_name": default_filename,
                "saved_to": str(dest),
                "size_bytes": dest.stat().st_size if dest.exists() else 0,
            })

    return downloaded


# ============================================================================
# 6. Folder Statistics & Selective Expansion Helpers
# ============================================================================

def compute_folder_stats(data: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Computes recursive item statistics for every folder/learning module in outline data.
    Returns mapping: content_id -> {
        "total_descendants": int,
        "item_count": int,
        "subfolder_count": int,
        "type_counts": Dict[str, int],
        "summary_str": str,
    }
    """
    children_map: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for item in data:
        pid = item.get("parent_id")
        children_map.setdefault(pid, []).append(item)

    stats: Dict[str, Dict[str, Any]] = {}

    def get_descendants(parent_id: str) -> List[Dict[str, Any]]:
        desc = []
        for child in children_map.get(parent_id, []):
            desc.append(child)
            cid = child.get("content_id")
            if cid:
                desc.extend(get_descendants(cid))
        return desc

    for item in data:
        cid = item.get("content_id")
        ctype = item.get("content_type", "")
        if not cid or (ctype not in ("folder", "learning_module") and not item.get("has_children")):
            continue

        descendants = get_descendants(cid)
        subfolders = [d for d in descendants if d.get("content_type") in ("folder", "learning_module") or d.get("has_children")]
        leaves = [d for d in descendants if d.get("content_type") not in ("folder", "learning_module") and not d.get("has_children")]

        type_counts: Dict[str, int] = {}
        for d in leaves:
            t = d.get("content_type", "item")
            type_counts[t] = type_counts.get(t, 0) + 1

        total_desc = len(descendants)
        type_summary_parts = []
        order = ["assignment", "file", "document", "syllabus", "test", "quiz", "discussion", "link", "item"]
        for t in order:
            if t in type_counts:
                cnt = type_counts[t]
                plural = f"{cnt} {t}s" if cnt > 1 and not t.endswith("s") else f"{cnt} {t}"
                type_summary_parts.append(plural)
        for t, cnt in sorted(type_counts.items()):
            if t not in order:
                plural = f"{cnt} {t}s" if cnt > 1 else f"{cnt} {t}"
                type_summary_parts.append(plural)

        if total_desc == 0:
            summary_str = "(0 items)"
        else:
            breakdown = f": {', '.join(type_summary_parts)}" if type_summary_parts else ""
            sub_info = f" in {len(subfolders)} subfolders" if len(subfolders) > 0 else ""
            item_word = "item" if total_desc == 1 else "items"
            summary_str = f"({total_desc} {item_word}{sub_info}{breakdown})"

        stats[cid] = {
            "total_descendants": total_desc,
            "item_count": len(leaves),
            "subfolder_count": len(subfolders),
            "type_counts": type_counts,
            "summary_str": summary_str,
        }

    return stats


def filter_outline_by_folder(data: List[Dict[str, Any]], folder_query: str) -> List[Dict[str, Any]]:
    """
    Finds matching folder/module by exact content_id or case-insensitive title match,
    and returns that folder and all its descendant items.
    """
    if not folder_query or not data:
        return []

    q_lower = folder_query.strip().lower()

    # 1. Exact content_id match
    matching_roots = [it for it in data if it.get("content_id", "").lower() == q_lower]

    # 2. Exact title match
    if not matching_roots:
        matching_roots = [it for it in data if it.get("title", "").strip().lower() == q_lower]

    # 3. Substring match in title among folders/containers
    if not matching_roots:
        matching_roots = [
            it for it in data
            if q_lower in it.get("title", "").lower()
            and (it.get("content_type") in ("folder", "learning_module") or it.get("has_children"))
        ]

    # 4. Fallback substring match across any item
    if not matching_roots:
        matching_roots = [it for it in data if q_lower in it.get("title", "").lower()]

    if not matching_roots:
        return []

    # Map children
    children_map: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for item in data:
        pid = item.get("parent_id")
        children_map.setdefault(pid, []).append(item)

    def collect_subtree(root_node: Dict[str, Any]) -> List[Dict[str, Any]]:
        nodes = [root_node]
        cid = root_node.get("content_id")
        if cid:
            for child in children_map.get(cid, []):
                nodes.extend(collect_subtree(child))
        return nodes

    result: List[Dict[str, Any]] = []
    seen_ids = set()
    for root in matching_roots:
        for node in collect_subtree(root):
            nid = node.get("content_id")
            if nid not in seen_ids:
                seen_ids.add(nid)
                result.append(node)

    return result


# ============================================================================
# 7. Formatting & Markdown Exporters
# ============================================================================

def format_outline_tree(
    data: List[Dict[str, Any]],
    course_name: str,
    course_id: str = "",
    target_folder: Optional[str] = None,
    expand_all: bool = False,
    depth: Optional[int] = None,
) -> str:
    """
    Formats outline items into a clear, beautiful hierarchical tree view.
    
    Modes:
    - Default (Shallow Summary): Renders root items directly, and collapses top-level folders with item count breakdowns.
    - target_folder: Renders the subtree for a specific folder matched by name or ID.
    - expand_all=True: Recursively expands all folders down to leaves (legacy behavior).
    - depth=N: Limits expansion up to N depth levels.
    """
    type_icons = {
        "syllabus": "📜",
        "folder": "📁",
        "learning_module": "📦",
        "document": "📄",
        "assignment": "📝",
        "test": "🧪",
        "quiz": "🧪",
        "discussion": "💬",
        "link": "🔗",
        "file": "📎",
        "lti_tool": "🛠️",
        "item": "📌",
    }

    if not data:
        header = f"📚 Course Outline: {course_name} ({course_id})" if course_id else f"📚 Course Outline: {course_name}"
        return f"{header}\n{'━' * len(header)}\n  (Course is currently closed or has no content items)"

    folder_stats = compute_folder_stats(data)

    # 1. Targeted Folder Expansion
    if target_folder:
        filtered = filter_outline_by_folder(data, target_folder)
        if not filtered:
            available_folders = [
                f"  • {it.get('title')} [ID: {it.get('content_id')}]"
                for it in data
                if it.get("content_type") in ("folder", "learning_module") or it.get("has_children")
            ]
            folders_str = "\n".join(available_folders) if available_folders else "  (No folders found)"
            return (
                f"❌ No folder or module matching '{target_folder}' found in {course_name}.\n\n"
                f"📁 Available folders in this course:\n{folders_str}\n"
            )

        root_folder = filtered[0]
        header = (
            f"📚 Course Outline: {course_name} ({course_id}) ➔ 📁 {root_folder.get('title', 'Folder')}"
            if course_id
            else f"📚 Course Outline: {course_name} ➔ 📁 {root_folder.get('title', 'Folder')}"
        )
        lines = [header, "━" * len(header)]

        # Build subtree map
        tree: Dict[Optional[str], List[Dict[str, Any]]] = {}
        for it in filtered:
            pid = it.get("parent_id")
            tree.setdefault(pid, []).append(it)

        # Base parent is the parent_id of the matching root(s)
        base_parent_ids = {it.get("parent_id") for it in filtered if it.get("content_id") == root_folder.get("content_id")}

        def render_targeted_nodes(parent_id: Optional[str], prefix: str = "", current_depth: int = 0):
            children = tree.get(parent_id, [])
            total = len(children)
            for i, node in enumerate(children):
                is_last = (i == total - 1)
                connector = "└── " if is_last else "├── "
                child_prefix = "    " if is_last else "│   "

                icon = type_icons.get(node.get("content_type", "item"), "📌")
                title = node.get("title", "Untitled")
                ctype = node.get("content_type", "item")
                cid = node.get("content_id", "")
                due = f" (Due: {node['due_date']})" if node.get("due_date") else ""
                id_tag = f" [ID: {cid}]" if node.get("is_downloadable") or ctype in ("file", "document", "syllabus", "folder", "learning_module") else ""

                is_container = ctype in ("folder", "learning_module") or node.get("has_children")
                stat = folder_stats.get(cid, {})
                stat_str = f" {stat.get('summary_str', '')}" if is_container and cid != root_folder.get("content_id") else ""

                lines.append(f"{prefix}{connector}{icon} {title} [{ctype}]{id_tag}{due}{stat_str}")

                if node.get("description"):
                    desc = node["description"].replace("\n", " ").strip()
                    if len(desc) > 95:
                        desc = desc[:92] + "..."
                    lines.append(f"{prefix}{child_prefix}   💬 {desc}")

                if node.get("external_url"):
                    lines.append(f"{prefix}{child_prefix}   🔗 {node['external_url']}")

                if (is_container or cid in tree) and (depth is None or current_depth < depth):
                    render_targeted_nodes(cid, prefix + child_prefix, current_depth + 1)

        for bpid in base_parent_ids:
            render_targeted_nodes(bpid, "", 0)

        return "\n".join(lines)

    # 2. General Rendering (Shallow vs Expand-All vs Depth-Limited)
    header = f"📚 Course Outline: {course_name} ({course_id})" if course_id else f"📚 Course Outline: {course_name}"
    lines = [header, "━" * len(header)]

    # Build parent -> children map
    tree: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for it in data:
        pid = it.get("parent_id")
        tree.setdefault(pid, []).append(it)

    is_shallow = not expand_all and depth is None

    def render_nodes(parent_id: Optional[str], prefix: str = "", current_depth: int = 0):
        children = tree.get(parent_id, [])
        total = len(children)
        for i, node in enumerate(children):
            is_last = (i == total - 1)
            connector = "└── " if is_last else "├── "
            child_prefix = "    " if is_last else "│   "

            icon = type_icons.get(node.get("content_type", "item"), "📌")
            title = node.get("title", "Untitled")
            ctype = node.get("content_type", "item")
            cid = node.get("content_id", "")
            due = f" (Due: {node['due_date']})" if node.get("due_date") else ""

            is_container = ctype in ("folder", "learning_module") or node.get("has_children")
            stat = folder_stats.get(cid, {})

            # Show ID tag for easy referencing / downloads
            id_tag = f" [ID: {cid}]" if is_container or node.get("is_downloadable") or ctype in ("file", "document", "syllabus") else ""

            # In shallow mode, containers show count breakdown and do NOT expand children
            if is_shallow and is_container:
                count_str = f" {stat.get('summary_str', '')}"
                lines.append(f"{prefix}{connector}{icon} {title} [{ctype}]{id_tag}{due}{count_str}")
                if node.get("description"):
                    desc = node["description"].replace("\n", " ").strip()
                    if len(desc) > 95:
                        desc = desc[:92] + "..."
                    lines.append(f"{prefix}{child_prefix}   💬 {desc}")
                continue

            # In depth-limited mode, if reached max depth, display summary without expanding
            if depth is not None and current_depth >= depth and is_container:
                count_str = f" {stat.get('summary_str', '')}"
                lines.append(f"{prefix}{connector}{icon} {title} [{ctype}]{id_tag}{due}{count_str}")
                continue

            lines.append(f"{prefix}{connector}{icon} {title} [{ctype}]{id_tag}{due}")

            if node.get("description"):
                desc = node["description"].replace("\n", " ").strip()
                if len(desc) > 95:
                    desc = desc[:92] + "..."
                lines.append(f"{prefix}{child_prefix}   💬 {desc}")

            if node.get("external_url"):
                lines.append(f"{prefix}{child_prefix}   🔗 {node['external_url']}")

            # Recursively render children
            if node.get("has_children") or cid in tree:
                render_nodes(cid, prefix + child_prefix, current_depth + 1)

    # Render starting from root (parent_id is None)
    render_nodes(None, "", 0)

    # If in shallow mode and containers were present, add helpful navigation hint
    has_containers = any(it.get("content_type") in ("folder", "learning_module") or it.get("has_children") for it in data)
    if is_shallow and has_containers:
        lines.append("")
        lines.append("💡 Tip: Use '--folder <name|ID>' to expand a folder, or '--expand-all' / '--deep' for full outline tree.")

    return "\n".join(lines)


def interactive_folder_picker(data: List[Dict[str, Any]], course_name: str, course_id: str = "") -> None:
    """Interactive CLI menu to browse and expand course folders."""
    stats = compute_folder_stats(data)
    containers = [it for it in data if it.get("content_type") in ("folder", "learning_module") or it.get("has_children")]

    if not containers:
        print(f"ℹ️ No collapsible folders or modules found in {course_name}.")
        print(format_outline_tree(data, course_name, course_id, expand_all=True))
        return

    while True:
        header = f"📚 Course Folders: {course_name} ({course_id})" if course_id else f"📚 Course Folders: {course_name}"
        print("\n" + header)
        print("━" * len(header))

        for idx, folder in enumerate(containers, 1):
            cid = folder.get("content_id", "")
            title = folder.get("title", "Untitled")
            ctype = folder.get("content_type", "folder")
            stat = stats.get(cid, {})
            summary = stat.get("summary_str", "")
            icon = "📁" if ctype == "folder" else "📦"
            path_str = f" ({' / '.join(folder.get('parent_path', []))})" if folder.get("parent_path") else ""
            print(f" [{idx}] {icon} {title} [ID: {cid}]{path_str} {summary}")

        print("━" * len(header))
        print("Commands: Enter number(s) (e.g. '1' or '1,3'), 'all' for full tree, 's' for root summary, 'q' to quit.")
        try:
            choice = input("\n👉 Select folder to expand: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting folder explorer.")
            break

        if not choice or choice.lower() in ("q", "quit", "exit"):
            break

        if choice.lower() in ("all", "*"):
            print("\n" + format_outline_tree(data, course_name, course_id, expand_all=True))
            continue

        if choice.lower() in ("s", "summary"):
            print("\n" + format_outline_tree(data, course_name, course_id))
            continue

        try:
            selected_indices = [int(p.strip()) for p in choice.split(",") if p.strip().isdigit()]
        except ValueError:
            print("❌ Invalid input. Please enter folder numbers like '1' or '2,3'.")
            continue

        if not selected_indices:
            print("❌ Invalid choice. Try again.")
            continue

        for idx in selected_indices:
            if 1 <= idx <= len(containers):
                target_f = containers[idx - 1]
                t_cid = target_f.get("content_id")
                print("\n" + format_outline_tree(data, course_name, course_id, target_folder=t_cid))
            else:
                print(f"⚠️ Index {idx} out of range.")



def clean_outline_json(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Prunes bloated null/empty fields from outline items for clean, compact JSON output."""
    cleaned = []
    for item in data:
        node: Dict[str, Any] = {
            "id": item.get("content_id"),
            "title": item.get("title"),
            "type": item.get("content_type", "item"),
        }
        if item.get("parent_path"):
            node["path"] = " / ".join(item["parent_path"])
        if item.get("description"):
            node["description"] = item["description"].strip()
        if item.get("is_downloadable"):
            node["is_downloadable"] = True
        if item.get("download_url"):
            node["download_url"] = item["download_url"]
        if item.get("external_url"):
            node["external_url"] = item["external_url"]
        if item.get("due_date"):
            node["due_date"] = item["due_date"]
        cleaned.append(node)
    return cleaned


def save_outline(data: List[Dict[str, Any]], course_id: str) -> Path:
    """Saves human-readable course outline Markdown to output/outlines/<course_id>.md."""
    out_dir = ensure_output_dir("outlines")
    filepath = out_dir / f"{course_id}.md"

    courses = load_courses()
    course_name = courses.get(course_id, course_id)

    lines = [
        f"# Course Outline: {course_name}",
        f"_Course ID: {course_id}_",
        f"_Scraped: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "", "---", ""
    ]

    if not data:
        lines.append("_No course content found or course is unavailable._")
    else:
        type_icons = {
            "syllabus": "📜",
            "folder": "📁",
            "learning_module": "📦",
            "document": "📄",
            "assignment": "📝",
            "test": "🧪",
            "quiz": "🧪",
            "discussion": "💬",
            "link": "🔗",
            "file": "📎",
            "lti_tool": "🛠️",
            "item": "📌",
        }

        for item in data:
            depth = item.get("depth", 0)
            indent = "  " * depth
            icon = type_icons.get(item.get("content_type", "item"), "📌")
            title = item.get("title", "Untitled")
            due = f" — _(Due: {item['due_date']})_" if item.get("due_date") else ""
            dl_str = f" `[ID: {item.get('content_id')}]`" if item.get("is_downloadable") else ""

            lines.append(f"{indent}- {icon} **{title}** [{item.get('content_type', 'item')}]{dl_str}{due}")
            if item.get("description"):
                desc_snippet = item['description'].replace("\n", " ").strip()
                if len(desc_snippet) > 160:
                    desc_snippet = desc_snippet[:157] + "..."
                lines.append(f"{indent}  > _{desc_snippet}_")

            if item.get("external_url"):
                lines.append(f"{indent}  └ 🔗 [{item.get('title', 'Link')}]({item['external_url']})")

    filepath.write_text("\n".join(lines))
    return filepath

