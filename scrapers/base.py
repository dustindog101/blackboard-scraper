from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from playwright.sync_api import Page
else:
    Page = object
from core.session import _navigate

def _navigate_and_check_page(page: Page, url: str) -> bool:
    """Helper to navigate an existing page to a URL and verify session is valid."""
    if not _navigate(page, url):
        return False
    return True
