import asyncio
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from core.config import BLACKBOARD_BASE, SESSION_DIR
from core.output import ensure_output_dir
from scrapers.base import _navigate_and_check_page

logger = logging.getLogger("blackboard.scrapers.profile")


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


def scrape_profile_api() -> Dict[str, Any]:
    """High-speed REST API student profile fetcher (<120ms)."""
    cookie_header = get_cookie_header()
    if not cookie_header:
        return {}

    headers = {
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    url = f"{BLACKBOARD_BASE}/learn/api/public/v1/users/me"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read().decode("utf-8"))

        name_dict = raw.get("name", {})
        given = name_dict.get("given", "")
        family = name_dict.get("family", "")
        full_name = f"{given} {family}".strip() or raw.get("userName", "Student")

        contact = raw.get("contact", {})
        email = contact.get("email") or f"{raw.get('userName', '').lower()}@umbc.edu"

        return {
            "name": full_name,
            "username": raw.get("userName", "N/A"),
            "student_id": raw.get("studentId") or raw.get("userName", "N/A"),
            "email": email,
            "system_role": raw.get("systemRoleIds", ["Student"])[0] if isinstance(raw.get("systemRoleIds"), list) else "Student",
            "pronouns": raw.get("pronouns", "N/A"),
            "privacy": "Restricted",
            "user_id": raw.get("id"),
        }
    except Exception as e:
        logger.debug(f"API profile fetch failed: {e}")
        return {}


def scrape_profile(page: Page) -> dict:
    """Scrape the user profile page via Playwright."""
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


async def scrape_profile_async(page: Optional[Any] = None) -> Dict[str, Any]:
    """Unified profile fetcher with REST API fast path."""
    api_data = await asyncio.to_thread(scrape_profile_api)
    if api_data:
        print(f"   ✅ Retrieved profile for: {api_data.get('name')} ({api_data.get('username')})")
        return api_data

    if page:
        return await asyncio.to_thread(scrape_profile, page)
    return {}


def save_profile(data: dict):
    if not data:
        return
    out_dir = ensure_output_dir("profile")
    filepath = out_dir / "profile.md"

    lines = [
        f"# Blackboard Profile: {data.get('name', 'Unknown')}",
        f"_Scraped: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
        f"- **Student Name:** {data.get('name', 'N/A')}",
        f"- **Username / ID:** {data.get('username') or data.get('student_id', 'N/A')}",
        f"- **Email:** {data.get('email', 'N/A')}",
        f"- **Pronouns:** {data.get('pronouns', 'N/A')}",
        f"- **System Role:** {data.get('system_role', 'N/A')}",
        f"- **Privacy:** {data.get('privacy', 'N/A')}",
    ]

    filepath.write_text("\n".join(lines))
    print(f"   💾 Saved to: {filepath.relative_to(Path.cwd()) if filepath.is_absolute() else filepath}")
