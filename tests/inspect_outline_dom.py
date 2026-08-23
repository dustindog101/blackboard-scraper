import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

BLACKBOARD_BASE = "https://blackboard.umbc.edu"
SESSION_DIR = Path(".session").resolve()
OUTPUT_DIR = Path("output/dom_inspection").resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

async def inspect_courses():
    print(f"🚀 Launching persistent context from {SESSION_DIR}...")
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=SESSION_DIR,
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1440, "height": 900}
        )

        cookie_file = SESSION_DIR / "cookies.json"
        if cookie_file.exists():
            try:
                cookies = json.loads(cookie_file.read_text())
                if cookies:
                    await context.add_cookies(cookies)
                    print(f" Loaded {len(cookies)} cookies from {cookie_file.name}")
            except Exception as e:
                print(f"⚠️ Error loading cookies: {e}")

        page = context.pages[0] if context.pages else await context.new_page()

        # 1. Navigate to courses page
        print(f" Navigating to courses list: {BLACKBOARD_BASE}/ultra/course...")
        await page.goto(f"{BLACKBOARD_BASE}/ultra/course", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)

        print(f" Current URL: {page.url}")
        if "login" in page.url.lower() or "auth" in page.url.lower():
            print("❌ Not logged in! Session expired or redirected to login.")
            await context.close()
            return

        # Save courses page HTML
        courses_html = await page.content()
        (OUTPUT_DIR / "courses_page.html").write_text(courses_html)
        print(f"💾 Saved courses list HTML ({len(courses_html)} bytes)")

        # Inspect courses list from DOM and API responses
        # Let's extract all course cards / links / terms
        courses_data = await page.evaluate("""() => {
            const results = [];

            // Try selector for course cards/rows
            const rows = document.querySelectorAll('bb-course-card, div[data-course-id], [class*="course-card"], div.course-element-card, a[href*="/ultra/courses/"]');

            const seen = new Set();
            rows.forEach(el => {
                let href = el.getAttribute('href') || el.querySelector('a[href*="/ultra/courses/"]')?.getAttribute('href') || '';
                let title = el.querySelector('h3, h4, span.name, [class*="course-title"], [class*="title"]')?.innerText?.trim() || el.innerText?.trim();
                let status = el.querySelector('[class*="status"], [class*="badge"], [class*="label"], [class*="term"]')?.innerText?.trim() || '';

                let courseIdMatch = href.match(/\/ultra\/courses\/([^/]+)/);
                let courseId = courseIdMatch ? courseIdMatch[1] : (el.getAttribute('data-course-id') || '');

                if (courseId && !seen.has(courseId)) {
                    seen.add(courseId);
                    results.push({
                        courseId,
                        title,
                        status,
                        href,
                        htmlSnippet: el.outerHTML.slice(0, 300)
                    });
                }
            });

            // Also look for term filters or dropdowns
            const termButtons = Array.from(document.querySelectorAll('button, select option, a')).map(b => ({
                text: b.innerText?.trim(),
                role: b.getAttribute('role'),
                ariaSelected: b.getAttribute('aria-selected'),
                id: b.id
            })).filter(b => b.text && (b.text.includes('202') || b.text.includes('Term') || b.text.includes('Semester') || b.text.includes('Current') || b.text.includes('Past')));

            return { courses: results, terms: termButtons };
        }""")

        print(f" Found {len(courses_data.get('courses', []))} courses on current view.")
        (OUTPUT_DIR / "courses_list.json").write_text(json.dumps(courses_data, indent=2))

        # Check known courses from config.json
        config_path = Path("config.json")
        known_courses = {}
        if config_path.exists():
            known_courses = json.loads(config_path.read_text()).get("courses", {})

        print(f"\n Checking known courses from config ({len(known_courses)} courses) + any found...")
        all_to_check = dict(known_courses)
        for c in courses_data.get('courses', []):
            if c['courseId'] not in all_to_check:
                all_to_check[c['courseId']] = c['title'] or c['courseId']

        # Let's test each course outline directly
        inspection_results = []
        for course_id, course_title in all_to_check.items():
            print("\n==================================================")
            print(f"🔍 Inspecting Course: {course_title} ({course_id})")
            outline_url = f"{BLACKBOARD_BASE}/ultra/courses/{course_id}/outline"

            try:
                await page.goto(outline_url, wait_until="networkidle", timeout=25000)
            except Exception as e:
                print(f"   ⚠️ Navigation error: {e}")
                try:
                    await page.goto(outline_url, wait_until="domcontentloaded", timeout=15000)
                except Exception as e2:
                    print(f"   ⚠️ Second navigation attempt failed: {e2}")

            await asyncio.sleep(2)

            # Check availability / access denied
            content = await page.content()
            (OUTPUT_DIR / f"outline_{course_id}.html").write_text(content)

            # DOM Evaluation on the course page
            dom_info = await page.evaluate("""() => {
                const bodyText = document.body.innerText;
                const isAccessDenied = bodyText.includes("You can't access this course right now") ||
                                     bodyText.includes("Course is unavailable") ||
                                     bodyText.includes("Private");

                const hasCourseOutline = !!document.querySelector('.course-outline-tree, bb-course-outline, div[role="tree"], [data-analytics-id="course-outline"], .course-outline');

                // Inspect all elements that look like outline items
                const outlineNodes = document.querySelectorAll(
                    'bb-content-item, ' +
                    'div[role="treeitem"], ' +
                    'div.course-outline-item, ' +
                    'div.element-details, ' +
                    'li.content-item, ' +
                    '[data-analytics-id*="outline"], ' +
                    '[class*="content-item"], ' +
                    '[class*="outline-row"], ' +
                    '[class*="outline-node"]'
                );

                const items = [];
                outlineNodes.forEach((el, idx) => {
                    const titleEl = el.querySelector('h3, h4, span.title, a.element-details-link, [class*="itemName"], .js-title, a');
                    const text = titleEl ? titleEl.innerText.trim() : el.innerText.split('\\n')[0].trim();
                    const tag = el.tagName.toLowerCase();
                    const role = el.getAttribute('role');
                    const ariaExpanded = el.getAttribute('aria-expanded');
                    const btnExpanded = el.querySelector('button[aria-expanded]')?.getAttribute('aria-expanded');
                    const dataAnalyticsId = el.getAttribute('data-analytics-id');
                    const dataContentId = el.getAttribute('data-content-id');
                    const classes = el.className;
                    const links = Array.from(el.querySelectorAll('a[href]')).map(a => ({ text: a.innerText.trim(), href: a.href }));

                    items.push({
                        idx,
                        tag,
                        role,
                        ariaExpanded: ariaExpanded || btnExpanded,
                        dataAnalyticsId,
                        dataContentId,
                        classes,
                        text,
                        links,
                        htmlSnippet: el.outerHTML.slice(0, 250)
                    });
                });

                // Check expand buttons
                const expandBtns = Array.from(document.querySelectorAll('button[aria-expanded]')).map(b => ({
                    ariaExpanded: b.getAttribute('aria-expanded'),
                    ariaLabel: b.getAttribute('aria-label'),
                    text: b.innerText.trim(),
                    classes: b.className
                }));

                // Check tabs / navigation (Content, Calendar, Discussions, Gradebook, Messages)
                const navTabs = Array.from(document.querySelectorAll('nav a, [role="tablist"] a, [role="tab"], bb-course-navigation a')).map(t => ({
                    text: t.innerText.trim(),
                    href: t.getAttribute('href'),
                    role: t.getAttribute('role'),
                    ariaSelected: t.getAttribute('aria-selected')
                }));

                return {
                    url: window.location.href,
                    isAccessDenied,
                    hasCourseOutline,
                    outlineNodesCount: outlineNodes.length,
                    expandButtonsCount: expandBtns.length,
                    expandButtons: expandBtns.slice(0, 10),
                    items: items.slice(0, 20),
                    navTabs: navTabs,
                    bodySnippet: bodyText.slice(0, 400).replace(/\\n+/g, ' ')
                };
            }""")

            print(f"   Status: {'⛔ ACCESS DENIED' if dom_info.get('isAccessDenied') else '✅ ACCESSIBLE'}")
            print(f"   Has outline element: {dom_info.get('hasCourseOutline')}")
            print(f"   Outline nodes count: {dom_info.get('outlineNodesCount')}")
            print(f"   Expand buttons: {dom_info.get('expandButtonsCount')}")
            print(f"   Nav tabs found: {[t['text'] for t in dom_info.get('navTabs', []) if t.get('text')]}")
            print(f"   Body snippet: {dom_info.get('bodySnippet')[:150]}...")

            inspection_results.append({
                "course_id": course_id,
                "course_title": course_title,
                "dom_info": dom_info
            })

        (OUTPUT_DIR / "inspection_results.json").write_text(json.dumps(inspection_results, indent=2))
        print(f"\n Full inspection summary written to {OUTPUT_DIR / 'inspection_results.json'}")

        await context.close()

if __name__ == "__main__":
    asyncio.run(inspect_courses())
