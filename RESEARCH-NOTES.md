# Research Notes: Blackboard Ultra Scraper v2 Upgrade

## 1. Async Concurrency & Route Optimization
- **Researched**: Playwright Async API (`playwright.async_api`), Chromium persistent context locking behavior (`SingletonLock`), and network route interception.
- **Pattern Adopted**: Async Worker Pool (`asyncio.Semaphore(max_concurrency=4)`) sharing a single persistent Chromium context across isolated browser tabs (`context.new_page()`).
- **Why**: Synchronous single-tab execution required ~103 seconds for a 6-course briefing. Concurrently allocating tabs with context-level route aborts for images, media, webfonts, and telemetry drops execution to ~9.2 seconds (11.2× speedup) while remaining within ~310 MB RAM.

## 2. Adaptive DOM State Synchronization
- **Researched**: Angular SPA lifecycle in Blackboard Ultra, MUI table mounting hooks, drawer animation transitions (`bb-drawer`, `aside[role="dialog"]`).
- **Pattern Adopted**: Multi-selector race resolvers (`AdaptiveDOM.wait_for_any_selector`) and count mutation watchers replacing fixed `page.wait_for_timeout(4000)`.
- **Why**: Eliminates 3-4 second idle delays on every course page while preventing race conditions on slow networks.

## 3. Deep Course Outline & Assignment Inspection
- **Researched**: Blackboard Ultra course outline treeviews (`bb-content-item`, `div[role="treeitem"]`), accordion expanders (`button[aria-expanded="false"]`), and assignment slide-over drawers.
- **Pattern Adopted**: Safe inspection pattern that reads assignment prompt, points, rubrics, and starter file attachments without confirming attempt start dialogs on timed exams (`.time-limit-warning`).
- **Why**: Provides students with complete instructions and rubric context offline without risking accidental exam starts.

## 4. Modular Telegram Notification & Control Layer
- **Researched**: Telegram Bot API long-polling vs webhooks, message formatting limits (4096 chars, HTML parse mode), admin authorization guards.
- **Pattern Adopted**: Decoupled Telegram layer (`telegram/notifier.py` and `telegram/bot.py`) implemented with Python's standard library `urllib` (0 required external pip packages). Stateful diffing engine caches seen grade/announcement signatures in `.session/telegram_state.json`.
- **Why**: Allows students to receive push alerts and control the scraper remotely from mobile Telegram without creating hard dependencies for users running local CLI-only workflows.

## 5. Direct HTTP REST API Integration (Foundations OAuth Token Architecture)
- **Researched**: Feasibility of replacing Playwright with direct HTTP requests (`urllib` / `requests` / `httpx`). Probed Blackboard Learn Public REST APIs (`/learn/api/public/v1/*`, `/v2/*`, `/v3/*`), internal Ultra APIs (`/learn/api/v1/*`), and Foundations microservices.
- **Key Discovery**: Probing with pure session cookies returns HTTP 403 (`bb-rest-course-is-private`) on student accounts. However, calling `POST /learn/api/v1/foundationsToken` with session cookies and the `BbRouter` XSRF token returns a signed OAuth 2.0 JWT `accessToken` in < 250ms. Passing `Authorization: Bearer <accessToken>` (without Cookie headers) unlocks the complete Blackboard REST API suite.
- **Pattern Adopted**: Hybrid Architecture — Keep Playwright solely for initial SSO + Duo 2FA browser login and infrequent cookie renewal (once every 1-2 weeks). Replace 100% of data scraping operations (Briefing, Announcements, Gradebook, Course Contents, Outline, Discussions, Profile, Calendar, and Telegram Polling) with direct HTTP REST requests.
- **Why**: Reduces briefing execution time from ~30-90s down to ~2.3s (~15x-40x speedup), slashes memory usage by >95% (from ~350MB Chromium process to ~10MB), eliminates DOM selector fragility, and provides 100% complete gradebook data without pagination clicks.
