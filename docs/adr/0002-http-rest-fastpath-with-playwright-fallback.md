# HTTP REST API Fast-Path with Playwright Browser Fallback

## Context
Blackboard Ultra web applications are single-page applications (SPAs) that load heavy JavaScript client bundles and FullCalendar DOM widgets. Previously, scrapers for calendar due dates, course announcements, and student gradebooks relied strictly on Playwright browser navigation. Spawning browser pages for each course or feature introduced significant latency (3–10 seconds per command), consumed 250–350 MB of memory per worker, and was vulnerable to UI layout changes or Angular mount race conditions.

## Decision
We implemented a dual-engine architecture:
1. **HTTP REST API Fast-Path (Primary)**: All read-only scrapers (`calendar`, `due_dates`, `announcements`, `grades`, `outline`, `search`, `profile`, `check-session`) execute direct HTTP REST requests against Blackboard's public and foundations APIs (`/learn/api/public/v1/*` and `/learn/api/public/v2/*`) using cached session cookies in `<150ms`.
2. **Playwright Browser Fallback (Secondary)**: If an HTTP REST request returns a fatal status (e.g. non-standard routing or blocking), the scraper prints a clear user notification (`⚠️ HTTP <Feature> API unavailable; falling back to Playwright browser scraper...`) and seamlessly delegates to the Playwright browser DOM crawler.
3. **Playwright Exclusive (Auth)**: Playwright remains dedicated to initial automated SSO WebAuth logins, Duo 2FA SMS code interception, and interactive browser sessions.

## Consequences
- Routine operations (daily briefings, checking grades, viewing upcoming deadlines, searching syllabi) now execute in under 400ms across all enrolled courses with near-zero memory footprint.
- Browser automation is only triggered when explicitly necessary or when API access is restricted.
- System resilience is maximized through automatic fallback failover.
