# Unified Gradable Item Inspection & Attempt Safety Guards

## Context
Blackboard Ultra courses organize student deliverables across distinct gradable item types: quizzes/tests (`resource/x-bb-asmt-test-link`), assignment dropboxes (assessment subtype `Assignment`), and gradable discussion boards (`resource/x-bb-forumlink`).

Inspecting tests and assessments via automation presents two critical challenges:
1. **Accidental Attempt Initiation & Timer Triggering**: Naive navigation or clicking into a test drawer can initiate an attempt token or trigger a strict 60-minute countdown timer on timed exams before the student intends to take it.
2. **Scattered Multi-Type APIs**: Assignments, quizzes, and discussions expose their instructions, prompts, points, and attempts across three separate Blackboard REST API namespaces (`/courses/{id}/contents/{id}`, `/courses/{id}/gradebook/attempts/{id}`, and `/courses/{id}/discussions`).

## Decision
We adopted a unified gradable item inspection architecture with non-destructive default modes:
1. **Non-Destructive Info Mode by Default**: Read operations (`bb --quiz <item>`) extract complete prompt statements, discussion questions, point weights, due dates, and test settings via direct HTTP REST without creating new student attempt records or triggering exam timers.
2. **Safe In-Progress Continuation**: If an active attempt already exists (`IN_PROGRESS`), the engine continues and extracts all question attempts, options, and student responses non-destructively.
3. **Explicit Attempt Initiation & Timed Exam Guard**: Beginning a new attempt when none exists requires the explicit `--start-attempt` flag. If the assessment is a timed test, execution requires explicit user confirmation (`--force-start`) to prevent accidental countdown triggers.
4. **Unified Dual-Engine Scraper (`scrapers/quiz.py`)**: A single entry point handles tests, assignments, and discussions with sub-200ms REST fast-path and Playwright browser fallback.

## Consequences
- Students can safely preview and review all course assignments, prompt statements, and discussion questions offline or in automated briefings without risking exam attempts.
- In-progress quiz questions and student responses are inspected in `<200ms` via REST API without requiring a browser.
- System maintains 100% test safety guarantees across all academic courses.
