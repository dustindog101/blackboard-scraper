import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

BLACKBOARD_BASE = "https://blackboard.umbc.edu"
SESSION_DIR = Path(".session").resolve()
OUTPUT_DIR = Path("output/dom_inspection").resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

async def check_terms_and_orgs():
    print(f"🚀 Launching browser with session from {SESSION_DIR}...")
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
            except Exception:
                pass

        page = context.pages[0] if context.pages else await context.new_page()

        # Navigate to /ultra/course
        print("🌐 Navigating to /ultra/course...")
        await page.goto(f"{BLACKBOARD_BASE}/ultra/course", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)

        # 1. Click on Terms dropdown to see all available terms
        print("\n--- Checking Terms Dropdown ---")
        terms_selector = "#courses-overview-filter-terms, div[role='combobox']:has-text('202'), div[role='combobox']"
        try:
            term_dd = page.locator(terms_selector).first
            if await term_dd.is_visible():
                await term_dd.click()
                await asyncio.sleep(1)

                # Get listbox items
                term_options = await page.evaluate("""() => {
                    const options = document.querySelectorAll('li[role="option"], ul[role="listbox"] li, div.MuiMenuItem-root');
                    return Array.from(options).map(o => ({
                        text: o.innerText.trim(),
                        selected: o.getAttribute('aria-selected') === 'true' || o.classList.contains('Mui-selected'),
                        value: o.getAttribute('data-value') || o.id
                    }));
                }""")
                print(f" Found {len(term_options)} term options in dropdown:")
                for opt in term_options:
                    print(f"   - {opt['text']} (selected={opt['selected']})")

                (OUTPUT_DIR / "term_options.json").write_text(json.dumps(term_options, indent=2))

                # Press Escape or click away to close dropdown
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
        except Exception as e:
            print(f"⚠️ Error checking terms dropdown: {e}")

        # 2. Check if removing term filter or selecting each term reveals courses
        # Let's see all terms that were found
        term_options_file = OUTPUT_DIR / "term_options.json"
        all_terms = []
        if term_options_file.exists():
            all_terms = json.loads(term_options_file.read_text())

        all_discovered_courses = []

        # Iterate through each term in the dropdown to scrape all courses across all terms
        for term_opt in all_terms:
            term_name = term_opt['text']
            print(f"\n Selecting Term: {term_name}...")
            try:
                term_dd = page.locator(terms_selector).first
                await term_dd.click()
                await asyncio.sleep(0.8)

                # Click the option
                opt_locator = page.locator(f"li[role='option']:has-text('{term_name}'), div.MuiMenuItem-root:has-text('{term_name}')").first
                if await opt_locator.is_visible():
                    await opt_locator.click()
                    await asyncio.sleep(2)

                    # Extract courses visible under this term
                    term_courses = await page.evaluate("""(currentTerm) => {
                        const cards = document.querySelectorAll('bb-base-course-card, div.default-group');
                        const results = [];
                        cards.forEach(card => {
                            const titleEl = card.querySelector('h4, a.course-title, [class*="course-title"]');
                            const title = titleEl ? titleEl.innerText.trim() : '';
                            const idEl = card.querySelector('[id^="course-id-"], [id^="course-link-"]');
                            const courseIdMatch = (idEl ? idEl.id : '').match(/course-(?:id|link)-(_\d+_\d+)/);
                            const courseId = courseIdMatch ? courseIdMatch[1] : '';
                            const statusEl = card.querySelector('.course-status, [class*="status"]');
                            const status = statusEl ? statusEl.innerText.trim() : 'Open';
                            const isClosed = card.innerText.includes('Closed') || card.innerText.includes('Private');

                            if (title) {
                                results.push({
                                    term: currentTerm,
                                    courseId: courseId,
                                    title: title,
                                    status: isClosed ? 'Closed' : 'Open',
                                    rawStatus: status
                                });
                            }
                        });
                        return results;
                    }""", term_name)

                    print(f"   Found {len(term_courses)} courses under {term_name}:")
                    for tc in term_courses:
                        print(f"     * [{tc['status']}] {tc['title']} ({tc['courseId']})")
                        all_discovered_courses.append(tc)
            except Exception as e:
                print(f"⚠️ Error selecting term {term_name}: {e}")

        # 3. Check /ultra/organization
        print("\n--- Checking /ultra/organization ---")
        try:
            await page.goto(f"{BLACKBOARD_BASE}/ultra/organization", wait_until="networkidle", timeout=25000)
            await asyncio.sleep(3)
            org_content = await page.content()
            (OUTPUT_DIR / "organizations_page.html").write_text(org_content)

            orgs = await page.evaluate("""() => {
                const cards = document.querySelectorAll('bb-base-course-card, div.default-group, a[id^="course-link-"]');
                const results = [];
                cards.forEach(card => {
                    const titleEl = card.querySelector('h4, a.course-title, [class*="course-title"]') || card;
                    const title = titleEl ? titleEl.innerText.trim() : '';
                    const idEl = card.querySelector('[id^="course-id-"], [id^="course-link-"]') || card;
                    const courseIdMatch = (idEl ? idEl.id : '').match(/course-(?:id|link)-(_\d+_\d+)/);
                    const courseId = courseIdMatch ? courseIdMatch[1] : '';
                    const isClosed = card.innerText.includes('Closed') || card.innerText.includes('Private');
                    if (title) {
                        results.push({
                            type: 'organization',
                            courseId: courseId,
                            title: title,
                            status: isClosed ? 'Closed' : 'Open'
                        });
                    }
                });
                return results;
            }""")
            print(f" Found {len(orgs)} organizations:")
            for o in orgs:
                print(f"   * [{o['status']}] {o['title']} ({o['courseId']})")
                all_discovered_courses.append(o)
        except Exception as e:
            print(f"⚠️ Error checking organizations: {e}")

        # Save all discovered
        (OUTPUT_DIR / "all_discovered_courses.json").write_text(json.dumps(all_discovered_courses, indent=2))

        # 4. Now for ANY open / accessible course or org, let's navigate to outline and inspect DOM!
        open_courses = [c for c in all_discovered_courses if c.get('status') == 'Open' and c.get('courseId')]
        print("\n==================================================")
        print(f" Found {len(open_courses)} OPEN/ACCESSIBLE courses to inspect in detail!")

        for oc in open_courses:
            cid = oc['courseId']
            ctitle = oc['title']
            print(f"\n🔍 Detailed DOM Inspection for OPEN COURSE: {ctitle} ({cid})")

            outline_url = f"{BLACKBOARD_BASE}/ultra/courses/{cid}/outline"
            await page.goto(outline_url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(4)

            # Dump HTML
            html_dump = await page.content()
            (OUTPUT_DIR / f"open_outline_{cid}.html").write_text(html_dump)

            # Detailed DOM Analysis
            analysis = await page.evaluate("""() => {
                const res = {};

                // 1. Check Course Navigation Bar
                res.navTabs = Array.from(document.querySelectorAll('bb-course-navigation a, nav[aria-label="course"] a')).map(a => ({
                    title: a.innerText.trim(),
                    href: a.href,
                    analyticsId: a.getAttribute('data-analytics-id'),
                    isActive: a.classList.contains('active')
                }));

                // 2. Check Expand Accordions / Collapsibles
                const expandButtons = Array.from(document.querySelectorAll('button[aria-expanded]')).map(b => ({
                    ariaExpanded: b.getAttribute('aria-expanded'),
                    ariaLabel: b.getAttribute('aria-label'),
                    text: b.innerText.trim(),
                    classes: b.className,
                    analyticsId: b.getAttribute('data-analytics-id')
                }));
                res.expandButtons = expandButtons;

                // 3. Tree container selectors
                res.treeSelectorsFound = {
                    'bb-course-outline': !!document.querySelector('bb-course-outline'),
                    'div.course-outline-tree': !!document.querySelector('div.course-outline-tree'),
                    'div[role="tree"]': !!document.querySelector('div[role="tree"]'),
                    '[data-analytics-id="course-outline"]': !!document.querySelector('[data-analytics-id="course-outline"]'),
                    'bb-content-item': document.querySelectorAll('bb-content-item').length,
                    'div[role="treeitem"]': document.querySelectorAll('div[role="treeitem"]').length,
                    'div.course-outline-item': document.querySelectorAll('div.course-outline-item').length,
                    'div.element-details': document.querySelectorAll('div.element-details').length
                };

                // 4. Extract all content items in the outline
                const items = [];
                const allNodes = document.querySelectorAll('bb-content-item, div[role="treeitem"], div.course-outline-item, div.element-details');
                allNodes.forEach((el, idx) => {
                    const titleEl = el.querySelector('h3, h4, span.title, a.element-details-link, [class*="itemName"], .js-title');
                    const title = titleEl ? titleEl.innerText.trim() : (el.getAttribute('aria-label') || '').trim();
                    const links = Array.from(el.querySelectorAll('a[href]')).map(a => ({ text: a.innerText.trim(), href: a.href }));
                    const isExpanded = el.querySelector('button[aria-expanded]')?.getAttribute('aria-expanded');

                    items.push({
                        idx,
                        tag: el.tagName.toLowerCase(),
                        title,
                        isExpanded,
                        links,
                        htmlSnippet: el.outerHTML.slice(0, 300)
                    });
                });
                res.items = items;

                return res;
            }""")

            (OUTPUT_DIR / f"open_analysis_{cid}.json").write_text(json.dumps(analysis, indent=2))
            print(f"   Nav tabs: {[t['title'] for t in analysis.get('navTabs', [])]}")
            print(f"   Expand buttons count: {len(analysis.get('expandButtons', []))}")
            print(f"   Tree selectors match: {analysis.get('treeSelectorsFound')}")
            print(f"   Extracted outline items count: {len(analysis.get('items', []))}")

        await context.close()
        print("\n🎉 Done check_terms_and_orgs!")

if __name__ == "__main__":
    asyncio.run(check_terms_and_orgs())
