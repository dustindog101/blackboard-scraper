import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir, status as _status
from scrapers.quiz import (
    _api_get as _quiz_api_get,
    _clean_html_text,
    _extract_links,
    _format_local_datetime,
    _get_cookie_header,
)


if TYPE_CHECKING:
    from playwright.sync_api import Page

# Courses are fetched concurrently and each fetches topics/replies in small pools; this caps the
# number of requests in flight overall so --all stays polite to Blackboard.
_HTTP_SLOTS = threading.BoundedSemaphore(10)


def _api_get(path: str, cookie_header: str):
    with _HTTP_SLOTS:
        return _quiz_api_get(path, cookie_header)


def _api_pages(path: str, cookie_header: str, max_pages: int = 10) -> Optional[list[dict]]:
    """Collects `results` across Blackboard's paging.nextPage links. None if the first page fails."""
    results: list[dict] = []
    next_path: Optional[str] = path
    for _ in range(max_pages):
        if not next_path:
            break
        data = _api_get(next_path, cookie_header)
        if not data or "_http_status" in data or "results" not in data:
            return None if not results else results
        results.extend(data["results"])
        next_path = (data.get("paging") or {}).get("nextPage")
    return results


def _post_record(message: dict, is_reply: bool) -> dict:
    author = f"{message.get('givenName') or ''} {message.get('familyName') or ''}".strip()
    return {
        "id": message.get("id"),
        "author": author or message.get("userId") or "Unknown",
        "date": _format_local_datetime(message.get("postDate") or message.get("createdDate")),
        "text": _clean_html_text(message.get("body")),
        "parent_id": message.get("parentId"),
        "is_reply": is_reply,
    }


def _participants_from_posts(posts: list[dict]) -> list[dict]:
    counts: dict[str, list[int]] = {}
    for post in posts:
        tally = counts.setdefault(post["author"], [0, 0])
        tally[1 if post["is_reply"] else 0] += 1
    return [
        {"name": name, "stats": f"{responses} responses, {replies} replies"}
        for name, (responses, replies) in counts.items()
    ]


def _fetch_thread_posts(course_id: str, discussion_id: str, cookie_header: str) -> Optional[list[dict]]:
    """Top-level responses plus their replies. None if the messages endpoint fails."""
    base = f"/learn/api/public/v1/courses/{course_id}/discussions/{discussion_id}/messages"
    responses = _api_pages(f"{base}?limit=100", cookie_header)
    if responses is None:
        return None

    def replies_for(message: dict) -> list[dict]:
        return _api_pages(f"{base}/{message['id']}/replies?limit=100", cookie_header) or []

    # Replies live behind one request per response; fetch them in parallel.
    with ThreadPoolExecutor(max_workers=4) as pool:
        reply_lists = list(pool.map(replies_for, responses))

    posts: list[dict] = []
    for response, replies in zip(responses, reply_lists):
        posts.append(_post_record(response, is_reply=False))
        posts.extend(_post_record(reply, is_reply=True) for reply in replies)
    return posts


def scrape_discussions_http(
    course_id: str,
    include_posts: bool = False,
    posts_only: bool = False,
    participants_only: bool = False,
) -> Optional[list[dict]]:
    """
    REST Fast-Path for discussions: titles, due dates, full prompt text and prompt links,
    plus thread posts and participant counts when include_posts is set.

    Returns None when the session or API is unavailable so callers can fall back
    to the browser scraper. A thread whose posts fail to load keeps its prompt and
    gets a `posts_error` flag instead of failing the whole course.
    """
    cookie_header = _get_cookie_header()
    if not cookie_header:
        return None

    topics = _api_pages(f"/learn/api/public/v1/courses/{course_id}/discussions", cookie_header)
    if topics is None:
        return None

    due_by_column: dict[str, str] = {}
    columns = _api_pages(f"/learn/api/public/v2/courses/{course_id}/gradebook/columns", cookie_header) or []
    for col in columns:
        # Ultra stores discussion due dates under grading.due; dueDate is often null.
        due = col.get("dueDate") or (col.get("grading") or {}).get("due")
        if col.get("id") and due:
            due_by_column[col["id"]] = _format_local_datetime(due)

    results = []
    for topic in topics:
        body = (topic.get("topic") or {}).get("body") or ""
        prompt = _clean_html_text(body)
        results.append({
            "id": topic.get("id"),
            "title": topic.get("title") or "",
            "url": f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/outline",
            "due_date": due_by_column.get(topic.get("gradebookColumnId"), ""),
            "preview_text": prompt[:200],
            "prompt": prompt,
            "links": _extract_links(body),
            "group_discussion": bool(topic.get("groupDiscussion")),
            "posts": [],
            "participants": [],
        })

    if include_posts:
        with ThreadPoolExecutor(max_workers=3) as pool:
            thread_posts = list(pool.map(
                lambda disc: _fetch_thread_posts(course_id, disc["id"], cookie_header) if disc["id"] else None,
                results,
            ))
        for disc, posts in zip(results, thread_posts):
            if posts is None:
                disc["posts_error"] = "Could not load posts for this thread."
                continue
            if not participants_only:
                disc["posts"] = posts
            if not posts_only:
                disc["participants"] = _participants_from_posts(posts)
    return results


def scrape_discussions(course_id: str, page: 'Page', max_post_clicks: int = None, max_participant_clicks: int = None,
                       posts_only: bool = False, participants_only: bool = False,
                       titles_only: bool = False) -> list[dict]:
    """
    Scrape discussions from a Blackboard Ultra course engagement page.
    Finds discussions, clicks into each, and grabs top posts + participation.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from scrapers.base import _navigate_and_check_page

    outline_url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/outline"
    courses = load_courses()
    name = courses.get(course_id, course_id)
    _status(f"Reading discussions for {name} in the browser...")

    results = []

    # Outline routing approach required:
    # We must route through the outline to trigger Angular routing for the discussion component.
    # A direct load of the engagement URL leaves us with an empty ui-view due to the new SPA architecture.
    if not _navigate_and_check_page(page, outline_url):
        return []

    # Click the Discussions navigation tool
    try:
        page.wait_for_selector(".js-course-discussion-tool", timeout=15_000)
        page.locator(".js-course-discussion-tool").first.click()
    except PlaywrightTimeout:
        raise TimeoutError("timed out finding the Discussions tool")

    # Wait for either discussions or the 'No discussions' message to appear
    # The empty state typically has an h1 containing "Participate in a discussion"
    try:
        page.wait_for_selector(
            "h3 a[href*='/engagement/discussion/'], h1:has-text('Participate in a discussion')",
            timeout=15_000
        )
    except PlaywrightTimeout:
        raise TimeoutError("timed out waiting for the discussions list")

    if page.locator("h1:has-text('Participate in a discussion')").count() > 0:
        _status("   No discussions found for course (Empty State detected).")
        return []

    # 1. Grab all discussion topic titles and metadata
    discussion_links = page.evaluate("""() => {
        const links = [];
        // The container holding each discussion item
        document.querySelectorAll('.content-list-item, div[data-content-id]').forEach(container => {
            const linkEl = container.querySelector('h3 a[href*="/engagement/discussion/"]');
            if (linkEl) {
                const title = linkEl.innerText.trim();
                const url = linkEl.href;

                let dueDate = "";
                const dueEl = container.querySelector('[class*="gradeDetail"]');
                if (dueEl) {
                    dueDate = dueEl.innerText.trim();
                }

                let previewText = "";
                const descEl = container.querySelector('.js-description, [class*="contentItemDescriptionContainer"]');
                if (descEl) {
                    previewText = descEl.innerText.trim();
                }

                links.push({
                    title: title,
                    url: url,
                    due_date: dueDate,
                    preview_text: previewText
                });
            }
        });
        return links;
    }""")

    if not discussion_links:
        _status("   No discussions found for course.")
        return []

    _status(f"   Found {len(discussion_links)} discussion topics. Inspecting threads...")

    if titles_only:
        return discussion_links

    # 2. Iterate and scrape individual discussions
    for disc in discussion_links:
        _status(f"   ↳ Thread: {disc['title'][:40]}...")

        # Navigate back to outline and click engagement again, because direct nav fails
        page.goto(outline_url, wait_until="domcontentloaded")
        page.wait_for_selector(".js-course-discussion-tool", timeout=15_000)
        page.locator(".js-course-discussion-tool").first.click()
        page.wait_for_timeout(3000)

        # Click the link instead of hard-navigating to the URL, which prevents redirects
        try:
            escaped_title = disc['title'].replace('"', '\\"')
            page.wait_for_selector(f'h3 a:has-text("{escaped_title}")', timeout=10_000)
            page.locator("h3 a").filter(has_text=disc['title']).first.click()
        except PlaywrightTimeout:
            _status(f"   ⚠️  Could not click link for {disc['title'][:20]}")
            continue

        try:
            # Wait for the main thread container or the empty state to render
            page.wait_for_selector(".thread-post, bb-usercard, .empty-state, h3", timeout=20_000)
            # Let animations and async fetches settle
            page.wait_for_timeout(3000)
        except PlaywrightTimeout:
            _status(f"   ⚠️  Timed out waiting for thread {disc['title'][:20]}")
            pass

        # Wait briefly for thread cards to become fully interactive
        page.wait_for_timeout(3000)

        # Blackboard pagination might require scrolling inside `.content-item-container` or `document.documentElement`
        if not participants_only:
            post_clicks = 0
            while True:
                if max_post_clicks is not None and post_clicks >= max_post_clicks:
                    break
                try:
                    if page.locator("button.load-more-items").is_visible(timeout=500):
                        page.locator("button.load-more-items").first.click()
                        page.wait_for_timeout(1500)
                        post_clicks += 1
                    else:
                        break
                except Exception:
                    break

        # Click "+ X more..." for participants repeatedly until it disappears
        if not posts_only:
            part_clicks = 0
            while True:
                if max_participant_clicks is not None and part_clicks >= max_participant_clicks:
                    break
                try:
                    # Look for button that contains "+ " or " more..." inside participant panel
                    more_btn = page.locator("button.button.text.expanded")
                    if more_btn.is_visible(timeout=500):
                        more_btn.first.click()
                        page.wait_for_timeout(1000)
                        part_clicks += 1
                    else:
                        break
                except Exception:
                    break

        page.wait_for_timeout(2000)
        thread_data = page.evaluate(r"""([getPosts, getParticipants]) => {
            const data = { posts: [], participants: [] };

            if (getPosts) {
                const processedPosts = new Set();
                const containers = document.querySelectorAll('.comment-entry-container');
                containers.forEach(container => {
                    let author = "Unknown";
                    const userCard = container.querySelector('bb-usercard');
                    if (userCard) {
                        const authorRaw = userCard.innerText.split('\n')[0];
                        if (authorRaw) author = authorRaw.trim();
                    }

                    let text = "";
                    const contentEl = container.querySelector('.ql-editor, .vtbegenerated, .format-text > p');
                    if (contentEl) {
                        text = contentEl.innerText.trim();
                    } else {
                        const pTags = container.querySelectorAll('.format-text p');
                        if (pTags.length > 0) {
                            text = Array.from(pTags).map(p => p.innerText.trim()).join('\n');
                        } else {
                            text = container.innerText.trim();
                        }
                    }

                    if (!text) return;

                    let date = "Unknown Date";
                    const dateEl = container.querySelector('.date, .timestamp, [class*="time"], .moment');
                    if (dateEl) date = dateEl.innerText.trim();

                    const uniqueKey = author + '|' + date + '|' + text.substring(0, 50);
                    if (!processedPosts.has(uniqueKey)) {
                        processedPosts.add(uniqueKey);
                        data.posts.push({ author: author, date: date, text: text });
                    }
                });
            }

            if (getParticipants) {
                document.querySelectorAll('.participant-card-wrap').forEach(card => {
                    const nameEl = card.querySelector('bb-username bdi');
                    const name = nameEl ? nameEl.innerText.trim() : card.innerText.split('\n')[0];

                    const stats = [];
                    card.querySelectorAll('p').forEach(p => {
                        if(p.innerText.trim().length > 0) stats.push(p.innerText.trim());
                    });

                    if (name) {
                        data.participants.push({
                            name: name.trim(),
                            stats: stats.join(' ')
                        });
                    }
                });
            }

            return data;
        }""", [not posts_only, not participants_only])

        disc["posts"] = thread_data.get("posts", [])
        disc["participants"] = thread_data.get("participants", [])
        results.append(disc)

    _status(f"   ✅ Extracted {len(results)} fully scraped discussions")
    return results

def format_discussions(courses: list[dict]) -> str:
    """Per-course text: topics in course order, with due dates, post counts and prompt links."""
    from core.display import clip, short_course

    lines: list[str] = []
    for course in sorted(courses, key=lambda c: short_course(c["course_name"])):
        topics = course["discussions"]
        lines.append(f"{short_course(course['course_name'])}  ({len(topics)} topic{'s' if len(topics) != 1 else ''})")
        if course.get("error"):
            lines.append(f"  could not read discussions: {course['error']}")
        for topic in topics:
            posts = f"  [{len(topic['posts'])} posts]" if topic.get("posts") else ""
            due = f"  due {topic['due_date']}" if topic.get("due_date") else ""
            lines.append(f"  {clip(topic.get('title', ''), 60)}{due}{posts}")
            for link in topic.get("links") or []:
                lines.append(f"      {clip(link['text'], 40)}: {link['url']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def save_discussions(discussions: list[dict], course_id: str, titles_only: bool = False):
    """Save discussion threads as markdown."""
    out_dir = ensure_output_dir("discussions")
    filename = f"{course_id}_titles.md" if titles_only else f"{course_id}.md"
    filepath = out_dir / filename

    courses = load_courses()
    course_name = courses.get(course_id, course_id)

    lines = [
        f"# Blackboard Discussions: {course_name}",
        f"_Scraped: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "", "---", ""
    ]

    if not discussions:
        lines.append("_No discussions found._")
    else:
        for disc in discussions:
            lines.append(f"## Thread: {disc['title']}")
            lines.append(f"*(Source: {disc['url']})*\\n")

            if "due_date" in disc and disc["due_date"]:
                lines.append(f"**Due:** {disc['due_date']}")
            if disc.get("prompt"):
                lines.append("### Prompt")
                lines.append("> " + disc["prompt"].replace("\n", "\n> "))
                lines.append("")
            elif "preview_text" in disc and disc["preview_text"] and titles_only:
                lines.append(f"> {disc['preview_text']}")
                lines.append("")
            if disc.get("links"):
                lines.append("### Links")
                for link in disc["links"]:
                    lines.append(f"- [{link['text']}]({link['url']})")
                lines.append("")

            if disc.get("participants"):
                lines.append("### Participation Stats")
                for p in disc["participants"]:
                    lines.append(f"- **{p['name']}**: {p['stats'] or 'No stats'}")
                lines.append("")

            if disc.get("posts"):
                lines.append("### Latest Posts")
                for post in disc["posts"]:
                    author = post.get('author', 'Unknown')
                    date = post.get('date', 'Unknown Date')
                    text = post.get('text', '')
                    lines.append(f"**{author}** ({date})")
                    formatted_text = text.replace('\n', '\n> ')
                    lines.append(f"> {formatted_text}")
                    lines.append("")
            elif not titles_only:
                lines.append("_No text posts extracted._")
                lines.append("")

            lines.append("---")
            lines.append("")

    filepath.write_text("\n".join(lines))
    _status(f"   💾 Saved to: {filepath.relative_to(Path.cwd()) if filepath.is_absolute() else filepath}")
