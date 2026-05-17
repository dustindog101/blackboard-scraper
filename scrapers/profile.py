from datetime import datetime
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from core.config import BLACKBOARD_BASE, load_courses
from core.output import ensure_output_dir
from scrapers.base import _navigate_and_check_page

def scrape_profile(page: Page) -> dict:
    """Scrape the user profile page."""
    url = f"{BLACKBOARD_BASE}/ultra/profile"
    print("👤 Scraping profile...")

    if not _navigate_and_check_page(page, url):
        return {}

    try:
        page.wait_for_selector("#main-heading, .data-row", state="attached", timeout=15_000)
    except PlaywrightTimeout:
        print("   ⚠️  Timed out waiting for profile to load.")
        return {}

    data = page.evaluate("""() => {
        const getVal = (selector) => {
            const el = document.querySelector(selector);
            return el ? el.innerText.trim() : null;
        };
        const getInputs = () => {
             const results = {};
             document.querySelectorAll('.data-row').forEach(row => {
                 const titleEl = row.querySelector('.data-title');
                 const valEl = row.querySelector('.data-value');
                 if(titleEl && valEl) {
                     results[titleEl.innerText.trim()] = valEl.innerText.trim().replace(/\\n/g, ' ').replace(' Edit', '');
                 }
             });
             return results;
        };

        const inputs = getInputs();

        // The user's name is usually under 'Full Name' or in h1#main-heading
        let name = getVal('.username bdi') || getVal('h1#main-heading');
        if (inputs['Full Name']) name = inputs['Full Name'];

        return {
            name: name,
            pronouns: inputs['Pronouns'] || getVal('.pronouns'),
            student_id: inputs['Student ID'] || getVal('[id*="studentId"]'),
            email: inputs['Email Address'] || inputs['Email'] || getVal('[id*="email"]'),
            system_role: inputs['System Role'],
            privacy: inputs['Privacy Settings'] || (document.querySelector('.icon-lock') ? 'Restricted' : 'Public')
        };
    }""")

    if data:
        print(f"   ✅ Found profile for: {data.get('name')}")
    else:
        print("   ❌ Failed to extract profile data.")
        
    return data or {}

def save_profile(data: dict):
    if not data: return
    out_dir = ensure_output_dir("profile")
    filepath = out_dir / "profile.md"
    
    lines = [
        f"# Blackboard Profile: {data.get('name', 'Unknown')}",
        f"_Scraped: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
        f"- **Email:** {data.get('email', 'N/A')}",
        f"- **Student ID:** {data.get('student_id', 'N/A')}",
        f"- **Pronouns:** {data.get('pronouns', 'N/A')}",
        f"- **System Role:** {data.get('system_role', 'N/A')}",
        f"- **Privacy:** {data.get('privacy', 'N/A')}",
    ]
    
    filepath.write_text("\n".join(lines))
    print(f"   💾 Saved to: {filepath.relative_to(Path.cwd()) if filepath.is_absolute() else filepath}")
