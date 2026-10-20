import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from core.config import BLACKBOARD_BASE
from core.output import ensure_output_dir, status
from core.rest import RestError, get_json

logger = logging.getLogger("blackboard.scrapers.profile")


def scrape_profile_api() -> Dict[str, Any]:
    """REST profile from /users/me. Student ID, username and email (which embeds it) are dropped. Raises RestError."""
    raw = get_json("/learn/api/public/v1/users/me")
    name = raw.get("name") or {}
    full_name = f"{name.get('given', '')} {name.get('family', '')}".strip() or "Student"
    roles = raw.get("systemRoleIds")
    return {
        "name": full_name,
        "system_role": roles[0] if isinstance(roles, list) and roles else "Student",
        "pronouns": raw.get("pronouns") or "",
    }


def scrape_profile(page: Any) -> dict:
    """Scrape the user profile page via Playwright."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from scrapers.base import _navigate_and_check_page

    url = f"{BLACKBOARD_BASE}/ultra/profile"

    if not _navigate_and_check_page(page, url):
        return {}

    try:
        page.wait_for_selector("#main-heading, .data-row", state="attached", timeout=15_000)
    except PlaywrightTimeout:
        status("Timed out waiting for the profile page.")
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
            email: inputs['Email Address'] || inputs['Email'] || getVal('[id*="email"]'),
            system_role: inputs['System Role'],
            privacy: inputs['Privacy Settings'] || (document.querySelector('.icon-lock') ? 'Restricted' : 'Public')
        };
    }""")

    return data or {}


async def scrape_profile_async(page: Optional[Any] = None) -> Dict[str, Any]:
    """Profile via REST; the browser page is used only when REST is unavailable (not on auth errors)."""
    try:
        return await asyncio.to_thread(scrape_profile_api)
    except RestError as exc:
        if exc.kind == "auth" or page is None:
            raise
        status(f"Profile REST unavailable ({exc.kind}); reading the profile page in the browser.")
        return await asyncio.to_thread(scrape_profile, page)


def save_profile(data: dict):
    if not data:
        return
    out_dir = ensure_output_dir("profile")
    filepath = out_dir / "profile.md"

    lines = [
        f"# Blackboard Profile: {data.get('name', 'Unknown')}",
        f"_Scraped: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
        f"- **Name:** {data.get('name', 'N/A')}",
        f"- **Pronouns:** {data.get('pronouns', 'N/A')}",
        f"- **System Role:** {data.get('system_role', 'N/A')}",
    ]

    filepath.write_text("\n".join(lines))
    status(f"Saved profile to {filepath}")
