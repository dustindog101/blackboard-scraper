from playwright.sync_api import Page
from core.session import _navigate

def _navigate_and_check_page(page: Page, url: str) -> bool:
    """Helper to navigate an existing page to a URL and verify session is valid."""
    if not _navigate(page, url):
        return False
    return True
