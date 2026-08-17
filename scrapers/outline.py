import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir
from core.async_engine import AdaptiveDOM

logger = logging.getLogger("blackboard.scrapers.outline")


async def scrape_course_outline_async(
    course_id: str,
    page: Page,
    max_depth: int = 4,
) -> List[Dict[str, Any]]:
    """
    Scrapes full course outline across Blackboard Ultra and Classic layouts.
    Traverses learning modules, folders, documents, syllabi, attachments, and external links.
    """
    courses = load_courses()
    course_name = courses.get(course_id, course_id)

    # Step 1: Navigate to Ultra Outline URL
    url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/outline"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
    except Exception as e:
        logger.debug(f"Navigation error for {course_name}: {e}")
        return []

    # Step 2: Check for course availability or error banners
    matched_sel, _ = await AdaptiveDOM.wait_for_any_selector(
        page,
        [
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

    # Step 3: Ultra layout handling - Expand collapsible folders and learning modules
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

    # Step 4: Extract items for Ultra AND Classic Blackboard layouts
    extracted_items = await page.evaluate("""() => {
        const items = [];
        const seenIds = new Set();

        // 1. Ultra Elements
        const ultraNodes = document.querySelectorAll(
            'bb-content-item, ' +
            'div[role="treeitem"], ' +
            'div.course-outline-item, ' +
            'div.element-details, ' +
            'li.content-item'
        );

        ultraNodes.forEach((el, index) => {
            const titleEl = el.querySelector('h3, h4, span.title, a.element-details-link, [class*="itemName"], .js-title');
            const title = titleEl ? titleEl.innerText.trim() : (el.getAttribute('aria-label') || '').trim();
            if (!title) return;

            const analyticsId = el.getAttribute('data-analytics-id') || el.getAttribute('data-content-id') || '';
            const contentId = analyticsId || `outline_node_${index}`;
            if (seenIds.has(contentId)) return;
            seenIds.add(contentId);

            // Determine content type
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
            else if (html.includes('file') || html.includes('attachment') || html.includes('.pdf')) contentType = 'file';

            // Due Date
            let dueDate = '';
            const dueEl = el.querySelector('[class*="dueDate"], [class*="due-date"], [class*="gradingDetail"]');
            if (dueEl) {
                dueDate = dueEl.innerText.replace(/due\\s*date[:\\s]*/i, '').trim();
            }

            // Description / Snippet
            let description = '';
            const descEl = el.querySelector('.element-details-summary, [class*="description"], .js-description, p');
            if (descEl && descEl !== titleEl) {
                description = descEl.innerText.trim();
            }

            // Links / Attachments
            const links = [];
            el.querySelectorAll('a[href]').forEach(a => {
                const href = a.href;
                const linkText = a.innerText.trim();
                if (href && !href.startsWith('javascript:')) {
                    links.push({ text: linkText || title, url: href });
                }
            });

            // Calculate tree depth from nesting parents
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
                title: title,
                content_type: contentType,
                due_date: dueDate,
                description: description,
                depth: depth,
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
                    title: title,
                    content_type: contentType,
                    due_date: '',
                    description: el.querySelector('.details, .vtbegenerated')?.innerText.trim() || '',
                    depth: 0,
                    links: links
                });
            });
        }

        return items;
    }""")

    return extracted_items


def format_outline_tree(data: List[Dict[str, Any]], course_name: str, course_id: str = "") -> str:
    """Formats outline into a readable hierarchical CLI string."""
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
        "item": "📌",
    }

    lines = [
        f"📚 Course Outline: {course_name} ({course_id})" if course_id else f"📚 Course Outline: {course_name}",
        "━" * 50,
    ]

    if not data:
        lines.append("  (Course is currently closed or has no content items)")
        return "\n".join(lines)

    for item in data:
        depth = item.get("depth", 0)
        indent = "  " * depth
        icon = type_icons.get(item.get("content_type", "item"), "📌")
        title = item.get("title", "Untitled")
        due = f" — (Due: {item['due_date']})" if item.get("due_date") else ""
        ctype = item.get("content_type", "item")

        lines.append(f"{indent}{icon} {title} [{ctype}]{due}")
        if item.get("description"):
            desc = item["description"].replace("\n", " ").strip()
            if len(desc) > 120:
                desc = desc[:117] + "..."
            lines.append(f"{indent}   > {desc}")

        for l in item.get("links", []):
            if l.get("url") and not l["url"].endswith("#"):
                lines.append(f"{indent}   └ 🔗 {l.get('text', 'Link')}: {l['url']}")

    return "\n".join(lines)


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
            "item": "📌",
        }

        for item in data:
            depth = item.get("depth", 0)
            indent = "  " * depth
            icon = type_icons.get(item.get("content_type", "item"), "📌")
            title = item.get("title", "Untitled")
            due = f" — _(Due: {item['due_date']})_" if item.get("due_date") else ""

            lines.append(f"{indent}- {icon} **{title}** [{item.get('content_type', 'item')}]{due}")
            if item.get("description"):
                desc_snippet = item['description'].replace("\n", " ").strip()
                if len(desc_snippet) > 160:
                    desc_snippet = desc_snippet[:157] + "..."
                lines.append(f"{indent}  > _{desc_snippet}_")

            for l in item.get("links", []):
                if l.get("url") and not l["url"].endswith("#"):
                    lines.append(f"{indent}  └ 🔗 [{l.get('text', 'Open Link')}]({l['url']})")

    filepath.write_text("\n".join(lines))
    return filepath
