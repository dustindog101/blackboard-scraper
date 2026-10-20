"""Real managed-browser transfer on each CI OS, with a local synthetic service."""

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from core import async_engine, session, session_transfer


class AuthFixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        authenticated = cookies.get("session") and cookies["session"].value == "fixture-active"
        if not authenticated:
            self.send_response(401)
            self.end_headers()
            return
        if self.path == "/learn/api/public/v1/users/me":
            body = b'{"id":"fixture-user","userName":"fixture-account"}'
            content_type = "application/json"
        else:
            body = b'<html><base-side-menu>Authenticated fixture</base-side-menu></html>'
            content_type = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestRealBrowserTransfer(unittest.TestCase):
    def test_transfer_restart_and_rejected_import_preservation(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), AuthFixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with tempfile.TemporaryDirectory(prefix="bb browser ") as temp:
                root = Path(temp)
                profile = root / "profile with spaces"
                profile.mkdir()
                saved = profile / "cookies.json"
                active = [{"name": "session", "value": "fixture-active", "domain": "127.0.0.1",
                           "path": "/", "expires": -1, "httpOnly": True, "secure": False, "sameSite": "Lax"}]
                old = [dict(active[0], value="fixture-old"), dict(active[0], name="old_extra", value="stale")]
                saved.write_text(json.dumps(active), encoding="utf-8")
                exported = root / "session ü with spaces.json"
                with patch.dict(os.environ, {"BLACKBOARD_BROWSER": "chromium", "BLACKBOARD_BROWSER_PATH": ""}), \
                     patch.object(session_transfer, "SESSION_DIR", profile), \
                     patch.object(session_transfer, "BLACKBOARD_BASE", base), \
                     patch.object(session, "SESSION_DIR", profile), \
                     patch.object(async_engine, "SESSION_DIR", profile):
                    self.assertEqual(session_transfer.export_session(str(exported)), 1)
                    saved.write_text(json.dumps(old), encoding="utf-8")
                    with session.sync_playwright() as playwright:
                        context, _ = session._launch_context(playwright, headless=True)
                        context.close()
                    count, user = session_transfer.import_session(str(exported))
                    self.assertEqual(count, 1)
                    self.assertEqual(user["id"], "fixture-user")
                    self.assertEqual(json.loads(saved.read_text()), active)

                    # Existing synchronous launcher uses the imported session after restart.
                    with session.sync_playwright() as playwright:
                        context, page = session._launch_context(playwright, headless=True)
                        try:
                            self.assertFalse(any(c["name"] == "old_extra" for c in context.cookies()))
                            page.goto(base + "/ultra/course", wait_until="domcontentloaded")
                            self.assertTrue(page.locator("base-side-menu").is_visible())
                        finally:
                            context.close()

                    # The existing asynchronous fallback uses the same imported cookies.
                    async def async_browser():
                        manager = async_engine.AsyncSessionManager(async_engine.EngineConfig(max_concurrency=1))
                        await manager.initialize()
                        try:
                            async with manager.acquire_page() as page:
                                await page.goto(base + "/ultra/course", wait_until="domcontentloaded")
                                self.assertTrue(await page.locator("base-side-menu").is_visible())
                        finally:
                            await manager.close()

                    asyncio.run(async_browser())
                    before = hashlib.sha256(saved.read_bytes()).digest()
                    rejected = root / "rejected.json"
                    rejected.write_text(json.dumps(old), encoding="utf-8")
                    with self.assertRaises(session_transfer.SessionTransferError):
                        session_transfer.import_session(str(rejected))
                    self.assertEqual(hashlib.sha256(saved.read_bytes()).digest(), before)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
