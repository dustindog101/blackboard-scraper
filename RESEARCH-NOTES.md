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

## 6. Deep Gradable Item Inspection & Attempt Token Lifecycle
- **Researched**: Internal Blackboard Learn REST endpoints for assessments (`resource/x-bb-asmt-test-link`), discussion boards (`resource/x-bb-forumlink`), and standard assignment dropboxes.
- **Key Discoveries**:
  1. Full assignment instructions, statements, prompts, and APA citation requirements are exposed in `< 100ms` via `/learn/api/v1/courses/{course_id}/contents/{content_id}?expand=gradebookCategory` under `contentDetail["resource/x-bb-asmt-test-link"].test.assessment.instructions` without initiating student attempts.
  2. Gradable discussion topics with all prompt questions and grading rules are exposed via `/learn/api/public/v1/courses/{course_id}/discussions` with matching `gradebookColumnId`.
  3. Interactive test questions, choice options, and student responses for active/submitted attempts are queryable via `/learn/api/v1/courses/{course_id}/gradebook/attempts/{attempt_id}?expand=toolAttemptDetail,alignedGoals` in `< 150ms`.
- **Pattern Adopted**: Non-Destructive Inspection by Default — Read-only examination of test parameters, points, due dates, and assignment instructions never creates premature attempt tokens or triggers timed test countdowns. If an attempt is in progress, the engine continues and inspects existing question attempts non-destructively. When explicit attempt initiation is requested (`--start-attempt`), Playwright handles the attempt creation with explicit timed exam guards (`--force-start`).

## 7. Ultra Document Asset Extraction & Multi-Folder Subtree Expansion
- **Researched**: Structure of Ultra Documents (`resource/x-bb-document`) vs classic course files. In Ultra, instructors frequently embed PDFs, syllabi, and reading notes as inline rich-text attachments (`data-bbfile` JSON objects referencing `/bbcswebdav/` repository URIs) rather than standard `/contents/{id}/attachments` endpoints.
- **Pattern Adopted**:
  1. **Dual-Path Downloader**: Enhanced `fetch_item_attachments` in `scrapers/outline.py` to first check standard attachments, then traverse Ultra document bodies and child elements to extract embedded `data-bbfile` resource URLs, enabling seamless downloading of rich-text documents and syllabi.
  2. **Multi-Folder Selective Expansion**: `filter_outline_by_folder` accepts natural folder titles (e.g. `-f "Start Here"` or `-f "Syllabus & Course Information"`). If multiple containers match a query (e.g. `-f "Module"`), it simultaneously expands and displays subtrees for all matching modules.



## 8. REST Attempt Creation (researched 2026-09-09, NOT implemented)
- **Researched**: Blackboard's public docs map "Create an Attempt" to `POST /learn/api/public/v2/courses/{courseId}/gradebook/columns/{columnId}/attempts` (SOAP `GradebookWS.saveAttempts` successor), with `PATCH .../attempts/{attemptId}` for updates. In principle `--start-attempt` could create attempts over pure REST with no browser.
- **Why not wired**: (1) a public gradebook-row POST is not proven to initialize Ultra tool-question attempts the way the UI's internal `/learn/api/v1` flow does — risk of orphan gradebook rows; (2) any write path needs OAuth/XSRF handling the cookie-based client doesn't have; (3) the whole point of ADR 0003 is that REST is the provably-safe read-only lane. One accidental POST during a "just looking" run would violate the safety guarantee.
- **Pattern Adopted**: `--start-attempt` with no existing attempt now falls through to the Playwright starter (fixed in `fix/quiz-fidelity-and-start`; previously it silently returned metadata-only). Timed exams still require `--force-start`. REST stays GET-only.
- **Verified live**: `GET .../gradebook/attempts/_36086680_1?expand=toolAttemptDetail,alignedGoals` returns all 11 Module 3 questions, including `question.prompts[]` (Matching terms: Health/Wealth/Life span) and per-option boolean `givenAnswer` arrays on Multiple Answer items — so already-started attempts never need Playwright to be *read*.
