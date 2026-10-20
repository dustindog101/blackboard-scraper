"""Opt-in real browser login fixture; no school credentials or live SSO."""

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from core import async_engine, session


class LoginFixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        authenticated = cookie.get("fixture") and cookie["fixture"].value == "logged-in"
        body = (b"<base-side-menu>Authenticated</base-side-menu>" if authenticated else
                b'<form method="post"><input name="username"><button>Log in</button></form>')
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(303)
        self.send_header("Set-Cookie", "fixture=logged-in; Path=/; HttpOnly; SameSite=Lax")
        self.send_header("Location", "/")
        self.end_headers()


@unittest.skipUnless(os.environ.get("BB_BROWSER_SMOKE"), "Set BB_BROWSER_SMOKE=1 to run the real browser fixture")
class TestBrowserSmoke(unittest.TestCase):
    def test_login_sync_async_and_visible_with_separate_profile(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), LoginFixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with tempfile.TemporaryDirectory(prefix="bb browser smoke ") as temp:
                profile = Path(temp) / "separate profile"
                with patch.object(session, "SESSION_DIR", profile), patch.object(async_engine, "SESSION_DIR", profile):
                    # A local form models the browser part of login: fill, click,
                    # follow redirect, capture cookies for the REST Fast-Path.
                    with session.sync_playwright() as pw:
                        context, page = session._launch_context(pw, headless=True)
                        try:
                            page.goto(base)
                            page.locator("input").fill("fixture-user")
                            page.get_by_role("button", name="Log in").click()
                            page.locator("base-side-menu").wait_for(state="visible")
                            cookies = context.cookies()
                            self.assertTrue(any(c["name"] == "fixture" for c in cookies))
                            (profile / "cookies.json").write_text(json.dumps(cookies), encoding="utf-8")
                        finally:
                            context.close()

                    async def check_async():
                        manager = async_engine.AsyncSessionManager(async_engine.EngineConfig(max_concurrency=1))
                        try:
                            await manager.initialize()
                            async with manager.acquire_page() as page:
                                await page.goto(base)
                                self.assertTrue(await page.locator("base-side-menu").is_visible())
                        finally:
                            await manager.close()
                    asyncio.run(check_async())

                    # macOS/Windows support the visible browser used by manual
                    # Duo login with the same executable and captured cookies.
                    if sys.platform in ("darwin", "win32"):
                        with session.sync_playwright() as pw:
                            context, page = session._launch_context(pw, headless=False)
                            try:
                                page.goto(base)
                                self.assertTrue(page.locator("base-side-menu").is_visible())
                            finally:
                                context.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
