import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

BLACKBOARD_BASE = "https://blackboard.umbc.edu"
SESSION_DIR = Path(".session").resolve()
OUTPUT_DIR = Path("output/dom_inspection").resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

async def inspect_all():
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=SESSION_DIR,
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1440, "height": 900}
        )

        page = context.pages[0] if context.pages else await context.new_page()

        api_responses = []

        async def on_response(response):
            url = response.url
            if "/learn/api/" in url or "/ultra/" in url:
                if response.status == 200 and "json" in (response.headers.get("content-type") or ""):
                    try:
                        data = await response.json()
                        api_responses.append({"url": url, "data": data})
                    except Exception:
                        pass

        page.on("response", on_response)

        print("🌐 Navigating to /ultra/course...")
        await page.goto(f"{BLACKBOARD_BASE}/ultra/course", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(4)

        # Let's inspect the page content and see if there are term filters, dropdowns, etc.
        page_info = await page.evaluate("""() => {
            const buttons = Array.from(document.querySelectorAll('button, a, select, [role="button"], [role="tab"]')).map(el => ({
                tag: el.tagName,
                text: el.innerText?.trim(),
                ariaLabel: el.getAttribute('aria-label'),
                role: el.getAttribute('role'),
                id: el.id,
                classes: el.className
            }));
            
            return {
                title: document.title,
                url: window.location.href,
                bodyText: document.body.innerText.slice(0, 2000),
                buttons: buttons.filter(b => b.text || b.ariaLabel)
            };
        }""")

        print(f"Page title: {page_info.get('title')}")
        print(f"Body snippet:\n{page_info.get('bodyText')[:500]}\n")

        # Let's also fetch user's memberships / courses directly via Blackboard's internal Learn API using page.evaluate fetch!
        api_courses = await page.evaluate("""async () => {
            const endpoints = [
                '/learn/api/public/v1/users/me',
                '/learn/api/public/v1/users/me/courses',
                '/learn/api/public/v1/courses',
                '/learn/api/v1/courses',
                '/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_1_1',
                '/learn/api/public/v1/terms',
                '/learn/api/public/v1/courses?limit=100'
            ];
            
            const results = {};
            for (const ep of endpoints) {
                try {
                    const res = await fetch(ep, { headers: { 'Accept': 'application/json' } });
                    const status = res.status;
                    let body = null;
                    try {
                        body = await res.json();
                    } catch (e) {
                        body = await res.text();
                    }
                    results[ep] = { status, body };
                } catch (err) {
                    results[ep] = { error: err.toString() };
                }
            }
            return results;
        }""")

        (OUTPUT_DIR / "api_courses_endpoints.json").write_text(json.dumps(api_courses, indent=2))
        (OUTPUT_DIR / "intercepted_api.json").write_text(json.dumps(api_responses, indent=2))

        print("Endpoints probe results:")
        for ep, res in api_courses.items():
            status = res.get('status')
            body = res.get('body')
            print(f"  {ep} -> status: {status}")
            if isinstance(body, dict):
                print(f"     keys: {list(body.keys())}")
                if "results" in body:
                    print(f"     results count: {len(body['results'])}")
                    for item in body['results'][:5]:
                        print(f"       - {item.get('id') or item.get('courseId')} : {item.get('name') or item.get('title') or item.get('courseName')}")

        await context.close()

if __name__ == "__main__":
    asyncio.run(inspect_all())
