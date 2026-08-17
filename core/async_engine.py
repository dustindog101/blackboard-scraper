"""
Async Concurrency Engine for Blackboard Ultra Scraper.

Key Features:
- Playwright Async API (`playwright.async_api.async_playwright`)
- Async Worker Pool with bounded concurrency via `asyncio.Semaphore`
- Context-level Route Optimization (aborts images, webfonts, media, telemetry)
- Adaptive DOM State Synchronization (zero rigid sleep timeouts)
- Course Worker Isolation (per-task error containment)
"""

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Coroutine, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    Route,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from core.config import BLACKBOARD_BASE, LOGIN_INDICATORS, SESSION_DIR, load_config

logger = logging.getLogger("blackboard.async_engine")


# ============================================================================
# 1. Route Optimization & Request Abort Configuration
# ============================================================================

class RouteOptimizer:
    """
    Manages high-performance network route interception.
    Aborts unneeded assets (images, fonts, video, audio, trackers) to save bandwidth
    and accelerate page load and DOM parsing.
    """

    BLOCKED_EXTENSIONS: Set[str] = {
        # Heavy Images & Icons
        "png", "jpg", "jpeg", "gif", "webp", "avif", "bmp", "tiff", "ico", "svg",
        # Video & Audio Media
        "mp4", "webm", "m4v", "mov", "avi", "wmv", "mp3", "wav", "ogg", "aac",
        # Custom Webfonts
        "woff", "woff2", "ttf", "eot", "otf",
    }

    BLOCKED_DOMAINS: Set[str] = {
        "google-analytics.com",
        "googletagmanager.com",
        "doubleclick.net",
        "telemetry.blackboard.com",
        "mixpanel.com",
        "segment.io",
        "hotjar.com",
        "sentry.io",
        "newrelic.com",
        "pendo.io",
        "datadoghq.com",
    }

    WHITELIST_PATTERNS: List[re.Pattern] = [
        re.compile(r"/learn/api/public/"),
        re.compile(r"/learn/api/v1/"),
        re.compile(r"/ultra/"),
        re.compile(r"/webapps/"),
        re.compile(r"vendor\.js"),
        re.compile(r"main\.js"),
        re.compile(r"runtime\.js"),
        re.compile(r"styles\.css"),
    ]

    @classmethod
    async def route_handler(cls, route: Route) -> None:
        """Route handler attached to BrowserContext."""
        req = route.request
        url = req.url
        resource_type = req.resource_type

        # Always allow essential scripts, APIs, and stylesheets
        for pattern in cls.WHITELIST_PATTERNS:
            if pattern.search(url):
                await route.continue_()
                return

        # Block by resource type
        if resource_type in ("image", "media", "font"):
            await route.abort()
            return

        # Block by domain
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if any(blocked in netloc for blocked in cls.BLOCKED_DOMAINS):
            await route.abort()
            return

        # Block by file extension
        path = parsed.path.lower()
        ext = path.split(".")[-1] if "." in path else ""
        if ext in cls.BLOCKED_EXTENSIONS:
            await route.abort()
            return

        # Pass everything else
        await route.continue_()


# ============================================================================
# 2. Adaptive DOM State Synchronization
# ============================================================================

class AdaptiveDOM:
    """
    Utilities for resilient DOM interaction without rigid sleep timers.
    Uses race resolvers and dynamic mutation observers.
    """

    @staticmethod
    async def wait_for_any_selector(
        page: Page,
        selectors: List[str],
        state: str = "attached",
        timeout: int = 15_000,
    ) -> Tuple[Optional[str], Optional[Any]]:
        """
        Wait for ANY of the specified selectors to reach the desired state.
        Returns (matched_selector, locator) or (None, None) if timeout.
        """
        deadline = time.time() + (timeout / 1000.0)
        while time.time() < deadline:
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    count = await loc.count()
                    if count > 0:
                        if state == "visible":
                            if await loc.is_visible():
                                return sel, loc
                        else:
                            return sel, loc
                except Exception:
                    continue
            await asyncio.sleep(0.08)
        return None, None

    @staticmethod
    async def safe_click(
        page: Page,
        selector: str,
        timeout: int = 5_000,
        wait_after: float = 0.2,
    ) -> bool:
        """Attempt to click an element with fallback error catching."""
        try:
            loc = page.locator(selector).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click(timeout=timeout)
            if wait_after > 0:
                await asyncio.sleep(wait_after)
            return True
        except Exception:
            return False

    @staticmethod
    async def adaptive_infinite_scroll(
        page: Page,
        item_selector: str,
        max_scrolls: int = 15,
        idle_wait_ms: int = 400,
    ) -> int:
        """Scroll dynamically until no new items are appended."""
        prev_count = await page.locator(item_selector).count()
        for _ in range(max_scrolls):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            # Poll until item count increases or idle timeout expires
            start = time.time()
            new_items_found = False
            while (time.time() - start) * 1000 < idle_wait_ms:
                curr_count = await page.locator(item_selector).count()
                if curr_count > prev_count:
                    prev_count = curr_count
                    new_items_found = True
                    break
                await asyncio.sleep(0.05)
            if not new_items_found:
                break
        return prev_count


# ============================================================================
# 3. Async Session Manager
# ============================================================================

@dataclass
class EngineConfig:
    headless: bool = True
    cdp_url: Optional[str] = None
    max_concurrency: int = 4
    page_timeout_ms: int = 25_000
    navigation_timeout_ms: int = 30_000
    block_assets: bool = True


class AsyncSessionManager:
    """
    Manages Playwright async lifecycle, persistent context, cookie state,
    and tab allocation bounded by concurrency limits.
    """

    def __init__(self, config: Optional[EngineConfig] = None):
        self.config = config or EngineConfig()
        self._pw: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Launch persistent context or connect via CDP."""
        async with self._lock:
            if self._initialized:
                return

            self._pw = await async_playwright().start()

            if self.config.cdp_url:
                logger.info(f"🔌 Connecting to existing CDP browser: {self.config.cdp_url}")
                browser = await self._pw.chromium.connect_over_cdp(self.config.cdp_url)
                self._context = browser.contexts[0] if browser.contexts else await browser.new_context()
            else:
                user_data_dir = str(SESSION_DIR)
                logger.debug(f"Launching persistent browser context from {user_data_dir}")
                self._context = await self._pw.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=self.config.headless,
                    args=["--disable-blink-features=AutomationControlled"],
                    viewport={"width": 1280, "height": 800},
                )

                # Add session cookies if present
                cookie_file = Path(SESSION_DIR) / "cookies.json"
                if cookie_file.exists():
                    import json
                    try:
                        cookies = json.loads(cookie_file.read_text())
                        if cookies:
                            await self._context.add_cookies(cookies)
                    except Exception:
                        pass

            # Apply route interception
            if self.config.block_assets and self._context:
                await self._context.route("**/*", RouteOptimizer.route_handler)

            self._initialized = True

    @asynccontextmanager
    async def acquire_page(self) -> AsyncGenerator[Page, None]:
        """
        Acquires an isolated page tab bounded by max_concurrency semaphore.
        Guarantees page closure on exit.
        """
        if not self._initialized:
            await self.initialize()

        async with self._semaphore:
            if not self._context:
                raise RuntimeError("Browser context is not initialized.")
            page = await self._context.new_page()
            page.set_default_timeout(self.config.page_timeout_ms)
            page.set_default_navigation_timeout(self.config.navigation_timeout_ms)
            try:
                yield page
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    async def close(self) -> None:
        """Gracefully shuts down the browser context and Playwright instance."""
        async with self._lock:
            if self._context:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None

            if self._pw:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
                self._pw = None

            self._initialized = False


# ============================================================================
# 4. Async Course Worker Pool
# ============================================================================

class AsyncCourseWorkerPool:
    """
    Executes scraping tasks across multiple courses concurrently.
    Provides complete error containment per course.
    """

    def __init__(self, session_manager: AsyncSessionManager):
        self.session_manager = session_manager

    async def execute_task_per_course(
        self,
        courses: Dict[str, str],
        task_fn: Callable[[str, str, Page], Coroutine[Any, Any, Any]],
    ) -> Dict[str, Any]:
        """
        Run `task_fn(course_id, course_name, page)` concurrently across all `courses`.
        Returns {course_id: result_data_or_dict}.
        """
        async def _worker(cid: str, cname: str) -> Tuple[str, Any]:
            try:
                async with self.session_manager.acquire_page() as page:
                    res = await task_fn(cid, cname, page)
                    return cid, res
            except Exception as e:
                logger.error(f"Error scraping course {cname} ({cid}): {e}")
                return cid, {"error": str(e), "course_name": cname, "course_id": cid}

        tasks = [_worker(cid, cname) for cid, cname in courses.items()]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return dict(results)
