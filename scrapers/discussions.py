from datetime import datetime
from pathlib import Path
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir
from scrapers.base import _navigate_and_check_page

def scrape_discussions(course_id: str, page: Page, max_post_clicks: int = None, max_participant_clicks: int = None,
                       posts_only: bool = False, participants_only: bool = False,
                       titles_only: bool = False) -> list[dict]:
    """
    Scrape discussions from a Blackboard Ultra course engagement page.
    Finds discussions, clicks into each, and grabs top posts + participation.
    """
    outline_url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/outline"
    courses = load_courses()
    name = courses.get(course_id, course_id)
    print(f"💬 Scraping discussions for {name}...")

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
        print("   ⚠️  Timed out finding discussion tool link.")
        return []

    # Wait for either discussions or the 'No discussions' message to appear
    # The empty state typically has an h1 containing "Participate in a discussion"
    try:
        page.wait_for_selector(
            "h3 a[href*='/engagement/discussion/'], h1:has-text('Participate in a discussion')",
            timeout=15_000
        )
    except PlaywrightTimeout:
        print("   ⚠️  Timed out waiting for discussions list.")
        return []

    if page.locator("h1:has-text('Participate in a discussion')").count() > 0:
        print("   No discussions found for course (Empty State detected).")
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
        print("   No discussions found for course.")
        return []

    print(f"   Found {len(discussion_links)} discussion topics. Inspecting threads...")

    if titles_only:
        return discussion_links

    # 2. Iterate and scrape individual discussions
    for disc in discussion_links:
        print(f"   ↳ Thread: {disc['title'][:40]}...")

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
            print(f"   ⚠️  Could not click link for {disc['title'][:20]}")
            continue

        try:
            # Wait for the main thread container or the empty state to render
            page.wait_for_selector(".thread-post, bb-usercard, .empty-state, h3", timeout=20_000)
            # Let animations and async fetches settle
            page.wait_for_timeout(3000)
        except PlaywrightTimeout:
            print(f"   ⚠️  Timed out waiting for thread {disc['title'][:20]}")
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

    print(f"   ✅ Extracted {len(results)} fully scraped discussions")
    return results

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
            if "preview_text" in disc and disc["preview_text"] and titles_only:
                lines.append(f"> {disc['preview_text']}")
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
    print(f"   💾 Saved to: {filepath.relative_to(Path.cwd()) if filepath.is_absolute() else filepath}")
