# Blackboard Academic Intelligence

A tool for autonomous academic workflows, scraping, aggregating, and interacting with UMBC Blackboard Ultra course materials, deadlines, grades, and outlines.

## Language

**Outline**:
The hierarchical tree structure representing all learning materials, folders, modules, assignments, and files within a Blackboard course.
_Avoid_: Syllabus tree, course content dump

**Folder**:
A container item within a Blackboard course outline that groups related items and subfolders.
_Avoid_: Directory, category

**Learning Module**:
A sequential container for course content items in Blackboard Ultra that guides students through structured learning steps.
_Avoid_: Chapter, unit

**Shallow Outline**:
A depth-limited view of a course outline displaying top-level items directly and container items collapsed with summary item counts.
_Avoid_: Folder list, brief outline

**Selective Expansion**:
The focused rendering of descendant content items under a specific target folder or content ID.
_Avoid_: Drill down, folder opening

**Content Item**:
An individual node within a course outline, such as an assignment, document, file, syllabus, web link, or test.
_Avoid_: Resource, file object

**REST Fast-Path**:
Direct HTTP API queries against Blackboard's public and foundations REST endpoints using cached session cookies, executing in <150ms without launching a browser.
_Avoid_: Direct DB query, headless scrape

**Playwright Fallback**:
Automated headless browser DOM crawler invoked automatically when an HTTP REST API request is restricted or blocked, notifying the user before falling back.
_Avoid_: Browser crash handler, secondary scraper

**Window Filter**:
A relative date range filter (e.g. `7d`, `14d`, `30d`, `overdue`, `all`) that filters aggregated deadlines relative to current execution time.
_Avoid_: Date cutoff, range parameter

**Cross-Source Aggregator**:
A unified deadline aggregation engine combining global calendar events (`/calendars/items`) with per-course gradebook columns into a single deduplicated schedule.
_Avoid_: Multi-scraper, calendar merger

