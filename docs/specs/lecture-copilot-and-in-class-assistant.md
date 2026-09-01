# Spec: Lecture Copilot & Real-Time In-Class Academic Assistant

## Problem Statement

When university students attend live lectures—such as **IS 410 Introduction to Database Design** on Tuesday afternoons (4:30 PM – 7:00 PM) or **ECON 122 Principles of Accounting II**—they operate under severe time constraints, cognitive fragmentation, and real-time academic pressure:

1. **High Navigational Latency in Live Environments:**
   Professors project presentation decks (e.g., `Week 2 - Conceptual Data Modeling & ERD.pdf`), refer to specific slide numbers, and expect students to follow along. To view the material, a student must manually open a browser, pass UMBC Duo multi-factor authentication, navigate to Blackboard Ultra Courses, select the specific course card, expand multi-tiered outline folders (`Course Materials / Lecture Notes / Week 02`), download the PDF, open it in an external reader, and scrub through dozens of slides. By the time this manual sequence finishes, 3 to 5 minutes of critical lecture delivery have elapsed.

2. **Sudden In-Class Discussion Prompts and Cold-Calls:**
   Instructors routinely present impromptu discussion questions, case scenarios, or architectural dilemmas on slides (e.g., *"Look at the university registrar schema on Slide 14: What composite primary key would prevent duplicate enrollments?"* or *"Analyze this patient admission table for 2NF violations"*). Students called on cold or asked to discuss in pairs have little time to transcribe the question text, recall relevant definitions, and structure a concise, articulate response.

3. **High-Stress In-Class Exercises & Lab Activities:**
   Many courses allocate 10 to 20 minutes for graded or participation-based in-class exercises embedded directly within lecture slide decks (e.g., *"In-Class Exercise 1: Construct an ER diagram with Crow's Foot notation for a hospital clinic"*). Students must extract the requirements from projected slides, interpret constraints under a ticking clock, and formulate solutions before the class review commences.

4. **Impromptu Logistics and Hidden Deliverables:**
   Professors frequently announce project milestones, homework assignments, or team requirements exclusively on lecture slides before posting them to the official Blackboard Gradebook or Calendar (e.g., *"Slide 4: Team project proposals due next Tuesday, Sept 8, at 4:30 PM; late submissions penalized 20%"*). If a student misses or forgets to transcribe these slide-only notices, they face unannounced submission penalties.

5. **Lack of Real-Time Temporal Awareness in Existing Tools:**
   While the `blackboard-scraper` suite provides calendar scraping (`bb --calendar`), outline crawling (`bb --outline`), and deadline tracking (`bb --due`), it requires manual flags (`-c IS410`) and human command invocation. It has no temporal awareness of the student's live weekly timetable. At 5:01 PM on a Tuesday, the system should already know the student is sitting in IS 410, determine the corresponding week of the semester, pinpoint today's lecture deck, extract the active slide content, and stage all in-class exercises and discussion answers for instant access.

---

## Solution

**Lecture Copilot** (`bb --in-class`, `-ic`, and the accompanying agent skill `lecture-copilot`) is a real-time academic intelligence system designed for live classroom environments. The copilot operates as an automated, proactive assistant that synchronizes with the student's live academic timetable.

The solution is architected around five interconnected components:

1. **Live Schedule Resolver (`LiveScheduleMatcher`):**
   Monitors local system time and matches it against the student's enrolled course meeting times (extracted from course metadata, syllabi schedules, or an explicit course timetable configuration). During an active lecture—or within a 20-minute pre-class staging window—the system automatically binds to the target course without requiring manual `-c` flags. When no lecture is active, it reports the next upcoming lecture and allows explicit manual selection.

2. **Slide Deck & Material Discoverer (`SlideDeckExtractor`):**
   Traverses the active course's content outline using the cached HTTP REST Fast-Path (<150ms). It identifies the most recent lecture deck, slide presentation, or weekly handout corresponding to the current date, week number, or lecture sequence (e.g., matching `Week 2`, `Lecture 02`, `09-01-2026`, or `Sept 1`). If the file is not yet cached locally, it downloads the PDF attachment directly to `.session/lectures/<course_id>/`.

3. **Slide Structure & Text Extractor (`SlideDeckExtractor`):**
   Parses the downloaded PDF using zero-overhead stream extraction (`pypdf`), segmenting the document into distinct, structured slide pages. It captures slide numbers, extracted titles, bullet lists, body paragraphs, and speaker notes/captions.

4. **Heuristic Exercise & Question Detector (`ExerciseDetector`):**
   Applies a deterministic, multi-layered regex pattern matrix across extracted slides to detect:
   - **In-Class Exercises & Practice Problems:** Tagged with `[EXERCISE]`, extracting problem text, constraints, and target deliverables.
   - **Cold-Call Discussion Questions:** Tagged with `[DISCUSSION_QUESTION]`, isolating question sentences and case scenarios.
   - **Impromptu Deliverables & Logistics:** Tagged with `[DELIVERABLE_ALERT]`, identifying due dates, submission criteria, and team guidelines announced on slides.
   - **Class Agenda & Roadmap:** Tagged with `[AGENDA]`, detailing what the instructor plans to cover during the session.

5. **Live In-Class Briefing Engine (`InClassBriefingEngine`):**
   Compiles extracted intelligence into a high-density, actionable briefing:
   - **Class Header:** Course name, meeting room/location, instructor, scheduled window, and elapsed class time progress bar.
   - **Active Slide Deck:** File name, total slide count, download location, and detected topic.
   - **Today's Agenda:** Chronological roadmap of topics covered in the deck.
   - **Upcoming In-Class Exercises:** Full prompt text with structural breakdowns and recommended solution strategies.
   - **Discussion Prep:** Direct talking points and pre-formulated answers for identified discussion prompts, enabling confident participation.
   - **Slide Logistics & Due Dates:** Deadlines announced on slides cross-referenced against Blackboard Gradebook entries.
   - **Interactive Slide Inspection:** Rapid lookup commands (`bb --in-class --slide <N>`) to retrieve the exact text of any slide projected on the screen.

---

## User Stories

1. As a student sitting in IS 410 on Tuesday at 4:45 PM, I want to type `bb --in-class` with no extra arguments, so that the tool automatically detects that IS 410 is my active class based on the current time.
2. As a student arriving 15 minutes early to lecture, I want the system to detect an upcoming class within a 20-minute buffer window, so that I can review today's slide deck before the professor starts speaking.
3. As a student whose professor just projected "Week 2 - Conceptual Data Modeling.pdf", I want `bb --in-class` to locate, download, and extract that specific deck automatically from Blackboard, so that I don't have to manually browse folders.
4. As a student in a lecture hall with spotty Wi-Fi, I want previously downloaded slide decks to be cached locally on my laptop, so that `bb --in-class` works offline without network delays.
5. As a student looking at an in-class exercise on the lecture screen, I want the copilot to extract the exact exercise prompt into clean terminal text, so that I don't have to squint or re-type the prompt into my notes.
6. As a student worried about cold-call questioning, I want the copilot to identify discussion questions in the slide deck and generate concise, structured talking points, so that I can participate confidently when called on.
7. As a student participating in an impromptu group breakout, I want the copilot to provide a step-by-step solution outline for the assigned in-class exercise, so that my team can complete our deliverable efficiently.
8. As a student tracking assignments, I want the copilot to detect project deadlines or homework dates written on the slides, so that I don't miss requirements that were omitted from official Blackboard announcement posts.
9. As a student sitting in a 2.5-hour evening class, I want to see an elapsed/remaining time progress bar, so that I know exactly how much time remains in the lecture.
10. As a student whose professor references "Slide 18", I want to run `bb --in-class --slide 18`, so that I can immediately read the full content of that specific slide in my terminal.
11. As a student studying outside of class hours, I want `bb --in-class` to inform me that no lecture is currently active and display my next scheduled class, so that I understand the system's schedule status.
12. As a student preparing for a class ahead of time, I want to pass an explicit course flag (e.g., `bb --in-class -c ECON122`), so that I can inspect lecture materials for any course regardless of the current time.
13. As an autonomous AI agent, I want `bb --in-class --json` to emit a standardized JSON payload, so that I can feed the lecture context into conversational downstream tools.
14. As a student using Telegram on my phone during class, I want to send `/lecture` to `@blackboardscrapbot`, so that I receive today's lecture briefing and exercise prompts directly on my mobile device.
15. As a Telegram user reviewing an in-class exercise, I want an inline button labeled "Solve Exercise 1", so that the bot immediately delivers a detailed solution approach without leaving the chat.
16. As a student in a course that uploads PowerPoint (`.pptx`) or PDF slide files, I want the extractor to resolve converted PDF assets or extract text from slides seamlessly, so that file format variations do not break extraction.
17. As a student in a course where slides are split into multiple parts (e.g., `Part1.pdf` and `Part2.pdf`), I want the copilot to prioritize the part matching today's date or provide an enumerated selection, so that I examine the correct document.
18. As a student whose professor uploads updated slides mid-lecture, I want a `--refresh` flag that re-checks Blackboard outline items, downloads newer versions, and re-parses content in under 2 seconds.
19. As a terminal power user, I want `bb --in-class --watch` mode to periodically check for new slide uploads or announcement drops while I take notes, so that I am notified the moment new resources appear.
20. As a student reviewing an assignment prompt on a slide, I want the system to cross-reference the assignment name against Blackboard Gradebook columns, so that I can verify if a submission portal has already been created.
21. As a student with a custom schedule, I want to define or override course meeting times in `config.json` (e.g., `"schedule": {"IS410": {"days": ["Tue"], "start": "16:30", "end": "19:00"}}`), so that non-standard course timetables are matched accurately.
22. As a student taking back-to-back classes, I want the schedule matcher to handle 10-minute passing periods cleanly without false overlaps, so that the correct course is selected.
23. As a student reviewing past lectures, I want to pass `--week 1` to `bb --in-class -c IS410 --week 1`, so that I can inspect lecture decks from earlier in the term.
24. As a student with vision sensitivity, I want terminal output formatted with clean UTF-8 box characters, clear headings, and syntax-highlighted badges (`[EXERCISE]`, `[DISCUSSION]`, `[DELIVERABLE]`), so that I can scan information rapidly during class.
25. As a macOS user, I want the native Menubar app (`bb --menubar`) to display a lecture status indicator (e.g., `🎓 IS 410 (45m left)`) during active lecture windows, so that I can monitor lecture timing at a glance.
26. As a student whose professor uploads lecture notes with complex code snippets or SQL queries, I want the extractor to preserve whitespace and indentation in code blocks, so that SQL or Python examples remain readable.
27. As a student whose professor embeds YouTube or web video links on slides, I want the detector to extract and list those URLs, so that I can open multimedia resources with a single click.
28. As a student whose slide deck contains an unnumbered title page, I want the slide indexer to treat the title page as Slide 1 and synchronize numbered references with what the professor projects, so that slide counts match the in-room display.
29. As a privacy-conscious student, I want all slide parsing, regex extraction, and schedule matching to execute entirely on my local machine without sending document text to third-party cloud servers by default, so that academic integrity and privacy are maintained.
30. As a student preparing to submit an in-class lab, I want `bb --in-class --open`, so that the copilot opens the lecture slide deck or associated submission portal directly in my default desktop browser via the authenticated Nautilus wrapper.

---

## Implementation Decisions

### 1. Architectural Seams and Module Decomposition

Following Matt Pocock's `codebase-design` principles, Lecture Copilot is designed with deep module boundaries, minimal surface area, and clean separation between schedule temporal matching, document extraction, heuristic detection, and presentation formatting.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                  Consumer Surface                                      │
│      (CLI: bb --in-class, Telegram: /lecture, Agent Skill: lecture-copilot, Menubar)   │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                                  [Lecture Copilot Seam]
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                               InClassBriefingEngine                                    │
│  Orchestrates schedule matching, slide discovery, extraction, detection & synthesis   │
└───────┬───────────────────────────────┬──────────────────────────────┬─────────────────┘
        │                               │                              │
        ▼                               ▼                              ▼
┌───────────────────────┐   ┌───────────────────────────┐   ┌────────────────────────────┐
│  LiveScheduleMatcher  │   │    SlideDeckExtractor     │   │      ExerciseDetector      │
│ ┌───────────────────┐ │   │ ┌───────────────────────┐ │   │ ┌────────────────────────┐ │
│ │ Timetable Matrix  │ │   │ │ Outline Fast-Path     │ │   │ │ Exercise Pattern Scan  │ │
│ ├───────────────────┤ │   │ ├───────────────────────┤ │   │ ├────────────────────────┤ │
│ │ Active Window Calc│ │   │ │ PDF Stream Parser     │ │   │ │ Discussion Prompt Scan │ │
│ ├───────────────────┤ │   │ ├───────────────────────┤ │   │ ├────────────────────────┤ │
│ │ Buffer & Overlaps │ │   │ │ Local Deck Cache      │ │   │ │ Deliverable & Date Scan│ │
│ └───────────────────┘ │   │ └───────────────────────┘ │   │ └────────────────────────┘ │
└───────────────────────┘   └───────────────────────────┘   └────────────────────────────┘
        │                               │                              │
        ▼                               ▼                              ▼
┌───────────────────────┐   ┌───────────────────────────┐   ┌────────────────────────────┐
│ Course Configuration  │   │  Blackboard REST API &    │   │ Deterministic Regex Engine │
│ (config.json/syllabi) │   │  Local Storage Subsystem  │   │ (Zero ML, <15ms execution) │
└───────────────────────┘   └───────────────────────────┘   └────────────────────────────┘
```

#### Seam 1: The Unified In-Class Briefing Seam
- **Location:** Positioned between the consumer layer (CLI argument router in `main.py`, Telegram dispatcher in `telegram/bot.py`, and Menubar daemon) and the copilot domain logic.
- **Contract:** Callers supply execution options (optional course override, slide number, JSON output flag); the engine resolves schedules, locates documents, parses pages, runs detection, and returns a unified `InClassLectureContext` domain object.

#### Seam 2: Document Extraction Seam
- **Location:** Positioned between the Blackboard REST Outline client / filesystem and the slide analysis layer.
- **Contract:** Accepts a target course ID, temporal reference, and cache directory; handles REST discovery of candidate items, verifies local cache freshness, downloads missing files, parses PDF binary streams, and outputs a structured `ParsedSlideDeck` object.

---

### 2. Deep Module Interfaces and Contracts

#### Module 1: `LiveScheduleMatcher`

Responsible for mapping wall-clock time to enrolled courses and resolving active or upcoming lectures.

##### Interface Contract
```python
def resolve_current_lecture(
    current_time: Optional[datetime] = None,
    target_course_id: Optional[str] = None,
    pre_class_buffer_minutes: int = 20,
    post_class_buffer_minutes: int = 10,
    courses_override: Optional[Dict[str, Any]] = None,
) -> ScheduleMatchResult:
    """
    Evaluates current system time against enrolled course schedules.
    Returns matched active lecture, upcoming lecture within buffer, or next scheduled class.
    Accepts explicit course override to bypass temporal resolution.
    """
```

##### Prototype Type Shapes
*(Derived from UMBC Fall 2026 course schedule research)*

```python
from dataclasses import dataclass
from datetime import datetime, time
from typing import List, Optional, Dict, Literal, Any
from pathlib import Path

DayOfWeek = Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

@dataclass(frozen=True)
class CourseMeetingTime:
    course_id: str
    course_code: str
    course_name: str
    days: List[DayOfWeek]
    start_time: time
    end_time: time
    location: str
    instructor: str

@dataclass(frozen=True)
class ScheduleMatchResult:
    status: Literal["active", "pre_class", "post_class", "idle", "manual_override"]
    meeting: Optional[CourseMeetingTime]
    time_remaining_minutes: Optional[int]
    time_elapsed_minutes: Optional[int]
    next_meeting: Optional[CourseMeetingTime]
    next_meeting_delta_hours: Optional[float]
    message: str
```

---

#### Module 2: `SlideDeckExtractor`

Locates, downloads, caches, and parses lecture slide decks for a specified course and lecture date/week.

##### Interface Contract
```python
def extract_active_slide_deck(
    course_id: str,
    reference_date: Optional[datetime] = None,
    explicit_week: Optional[int] = None,
    force_download: bool = False,
    cache_dir: Optional[Path] = None,
) -> SlideDeckExtractionResult:
    """
    Discovers the latest lecture deck for a course via REST outline metadata.
    Retrieves or downloads the PDF asset, extracts slide text page by page,
    and returns an ordered collection of slide records.
    """
```

##### Prototype Type Shapes

```python
@dataclass(frozen=True)
class ExtractedSlidePage:
    slide_number: int
    title: str
    raw_text: str
    bullet_points: List[str]
    detected_code_blocks: List[str]
    embedded_urls: List[str]
    char_count: int

@dataclass(frozen=True)
class ParsedSlideDeck:
    course_id: str
    content_id: str
    title: str
    filename: str
    local_path: str
    file_size_bytes: int
    total_slides: int
    week_number: Optional[int]
    lecture_date_str: Optional[str]
    slides: List[ExtractedSlidePage]

@dataclass(frozen=True)
class SlideDeckExtractionResult:
    status: Literal["success", "no_deck_found", "download_error", "parse_error"]
    deck: Optional[ParsedSlideDeck]
    message: str
```

---

#### Module 3: `ExerciseDetector`

Performs high-speed deterministic regex analysis across parsed slide text to categorize classroom interactions and requirements.

##### Interface Contract
```python
def detect_in_class_items(
    deck: ParsedSlideDeck,
    min_confidence: float = 0.60,
) -> DetectionSummary:
    """
    Scans all slides within a parsed deck to isolate exercises, discussion
    prompts, upcoming deliverables, and agenda topics.
    """
```

##### Prototype Type Shapes

```python
DetectionCategory = Literal[
    "EXERCISE",
    "DISCUSSION_QUESTION",
    "DELIVERABLE_ALERT",
    "AGENDA_TOPIC"
]

@dataclass(frozen=True)
class DetectedItem:
    category: DetectionCategory
    slide_number: int
    slide_title: str
    headline: str
    prompt_text: str
    structured_solution_starter: Optional[str]
    due_date_str: Optional[str]
    confidence_score: float
    matched_keywords: List[str]

@dataclass(frozen=True)
class DetectionSummary:
    agenda_topics: List[str]
    exercises: List[DetectedItem]
    discussion_questions: List[DetectedItem]
    deliverable_alerts: List[DetectedItem]
    total_detected: int
```

---

#### Module 4: `InClassBriefingEngine`

Synthesizes outputs into final presentations for CLI, JSON export, and Telegram broadcasts.

##### Prototype Type Shapes

```python
@dataclass(frozen=True)
class InClassLectureContext:
    timestamp: str
    schedule_status: str
    course_id: str
    course_name: str
    location: str
    instructor: str
    session_progress_pct: float
    elapsed_str: str
    remaining_str: str
    active_deck_name: str
    deck_local_path: str
    total_slides: int
    agenda: List[str]
    exercises: List[DetectedItem]
    discussion_questions: List[DetectedItem]
    deliverables: List[DetectedItem]
    raw_deck: Optional[ParsedSlideDeck]
```

---

### 3. Implementation Details & Algorithmic Heuristics

#### A. Schedule Matching Heuristics & Timetable Configuration
Course meeting times are maintained in `config.json` under an extensible `"schedules"` dictionary, with automatic fallback inference from course names and syllabus metadata:

1. **Config Schema Extension:**
   ```json
   "schedules": {
     "_105737_1": {
       "code": "IS 410",
       "days": ["Tue"],
       "start": "16:30",
       "end": "19:00",
       "location": "Sherman Hall 014",
       "instructor": "Dr. Sankar"
     },
     "_107884_1": {
       "code": "ECON 122",
       "days": ["Mon", "Wed"],
       "start": "13:00",
       "end": "14:15",
       "location": "Academic IV 208",
       "instructor": "Prof. Kelly"
     }
   }
   ```
2. **Resolution Pipeline:**
   - Evaluates current weekday (`now.strftime("%a")` $\rightarrow$ `"Tue"`).
   - Filters candidate courses active on the current day.
   - Computes time delta in minutes: $\Delta t = t_{\text{current}} - t_{\text{start}}$.
   - **Active Window:** $0 \le \Delta t \le \text{duration}$ (Status: `active`).
   - **Pre-Class Staging Window:** $-\text{buffer} \le \Delta t < 0$ (Status: `pre_class`).
   - **Post-Class Buffer:** $\text{duration} < \Delta t \le \text{duration} + \text{post\_buffer}$ (Status: `post_class`).
   - If no course matches the immediate window, selects the next chronologically occurring lecture.

#### B. Slide Deck Discovery & Temporal Correlation
To identify which deck corresponds to "today's" lecture:
1. **Week Calculation:**
   Using semester start date (e.g., UMBC Fall 2026 start: August 26, 2026), calculate the active academic week index:
   $$\text{Week Index} = \left\lfloor \frac{\text{Date} - \text{SemesterStart}}{7} \right\rfloor + 1$$
   *(Example: Sept 1, 2026 $\rightarrow$ Day 6 of semester $\rightarrow$ Week 2).*
2. **Outline Matching Priority:**
   - High Priority: Outline items matching `Week <N>` or `W<N>` in title or parent folder.
   - Medium Priority: Outline items with filenames containing the current month/day (e.g., `09-01`, `Sept1`, `Lecture 2`).
   - Fallback: The most recently updated file item with a `.pdf` or `.pptx` extension under a folder named "Lectures", "Slides", "Class Notes", or "Course Materials".

#### C. Deterministic Regex Matrix for In-Class Item Detection
All item categorization operates without heavy neural networks or cloud dependencies using a multi-pattern regex matrix:

1. **In-Class Exercises & Problems (`EXERCISE`):**
   - Matches: `(?i)\b(?:in[- ]?class\s+(?:exercise|activity|assignment|lab|work))\b`
   - Matches: `(?i)\b(?:practice\s+problem|hands[- ]?on\s+task|try\s+it\s+yourself)\b`
   - Matches: `(?i)\b(?:exercise\s+[0-9]+|activity\s+[0-9]+|problem\s+[0-9]+:)\b`
   - Matches: `(?i)\b(?:group\s+activity|team\s+breakout|paired\s+exercise)\b`

2. **Discussion Questions & Cold-Call Prompts (`DISCUSSION_QUESTION`):**
   - Matches: `(?i)\b(?:discussion\s+question|class\s+discussion|think[- ]?pair[- ]?share)\b`
   - Matches: `(?i)\b(?:what\s+do\s+you\s+think|why\s+would\s+we|how\s+should\s+we)\b`
   - Matches: `(?i)\b(?:case\s+scenario|case\s+study|consider\s+this\s+scenario)\b`
   - Matches: `(?i)\b(?:question\s+for\s+the\s+class|brainstorming\s+prompt)\b`

3. **Slide Deliverables & Announcements (`DELIVERABLE_ALERT`):**
   - Matches: `(?i)\b(?:due\s+(?:next\s+week|tomorrow|tonight|by\s+[a-z]+\s+[0-9]{1,2}))\b`
   - Matches: `(?i)\b(?:project\s+proposal\s+due|homework\s+[0-9]+\s+assigned)\b`
   - Matches: `(?i)\b(?:submit\s+(?:to|on)\s+blackboard\s+by)\b`
   - Matches: `(?i)\b(?:deliverable|milestone\s+[0-9]+|submission\s+deadline)\b`

4. **Agenda Detection (`AGENDA_TOPIC`):**
   - Matches slides titled "Agenda", "Today's Topics", "Roadmap", "Outline", or "Schedule".
   - Extracts individual bullet items into ordered agenda milestones.

#### D. Discussion Talking Point Generation
To provide immediate academic value during live discussion:
- For identified discussion questions, the engine extracts the core entity (e.g., "composite primary key", "third normal form", "referential integrity").
- Synthesizes a three-part answer starter framework:
  1. **Direct Thesis:** Clear 1-sentence answer addressing the prompt.
  2. **Technical Rationale:** Key academic concepts and justification.
  3. **Concrete Example:** Illustrative database schema or accounting context.

#### E. Performance Guarantee (<400ms Local Execution)
- **Local PDF Parsing:** Extracted via lightweight `pypdf` stream readers with page-level text bounding; 40-slide decks parse in under 120ms.
- **Outline Traversal:** Reuses the cached session outline tree; avoids browser launches.
- **Regex Detection:** Deterministic string matching over 40 pages executes in under 20ms.
- Total processing time from CLI invocation to terminal rendering is under 400ms.

---

## Testing Decisions

### 1. Test Principles

- **Seam-Only Behavioral Verification:** Tests execute exclusively through public interfaces (`resolve_current_lecture`, `extract_active_slide_deck`, `detect_in_class_items`, `InClassBriefingEngine.build_briefing`). No assertions depend on private regex groupings or internal loop variables.
- **Deterministic Time Mocking:** Timetable resolution tests must freeze system time via standardized mocking across all boundary conditions (active lecture, 5 minutes pre-class, 30 minutes post-class, weekend gap).
- **Frozen PDF Fixtures:** Parsing and detection tests run against real, recorded PDF slide fixtures (representing actual IS 410 database modeling and ECON 122 accounting slides) checked into the test suite.
- **Zero Network Ingestion:** Scraper REST and outline calls are intercepted via existing session mock fixtures to ensure the test suite runs in under 300ms without network connectivity.

### 2. Tested Behaviors & Test Scenarios

- **Schedule Matcher (`LiveScheduleMatcher`):**
  - *Active Lecture Window:* Given Tuesday 5:01 PM and IS 410 scheduled for Tuesday 4:30 PM - 7:00 PM $\rightarrow$ assert status `active`, `course_code == "IS 410"`, `time_elapsed == 31`, `time_remaining == 119`.
  - *Pre-Class Buffer:* Given Tuesday 4:15 PM (15 minutes prior) $\rightarrow$ assert status `pre_class`, `course_code == "IS 410"`.
  - *Post-Class Buffer:* Given Tuesday 7:05 PM (5 minutes post-lecture) $\rightarrow$ assert status `post_class`, `course_code == "IS 410"`.
  - *Idle / Inter-Class Period:* Given Wednesday 10:00 AM $\rightarrow$ assert status `idle`, assert next meeting correctly points to ECON 122 (Wednesday 1:00 PM).
  - *Manual Override:* Given target course `ECON122` provided via argument $\rightarrow$ assert status `manual_override`, bypassing time check.

- **Slide Deck Extractor (`SlideDeckExtractor`):**
  - *Discovery by Week:* Given Week 2 date $\rightarrow$ assert candidate deck matching `Week 2` is selected.
  - *Local Cache Hit:* When PDF is already cached on disk, verify download is bypassed and cached file is parsed directly.
  - *Page Segmentation:* Verify slide count matches source PDF and page text boundaries are preserved without truncated paragraphs.
  - *Code Block Preservation:* Verify SQL blocks within slide text preserve newlines and indentation.

- **Exercise & Question Detector (`ExerciseDetector`):**
  - *Exercise Classification:* Given slide text *"In-Class Exercise 1: Construct an ER diagram for a veterinary clinic"* $\rightarrow$ assert detected item category is `EXERCISE` with confidence $\ge 0.90$.
  - *Discussion Prompt Classification:* Given slide text *"Discussion Question: Why would an enterprise avoid 3NF normalization?"* $\rightarrow$ assert detected item category is `DISCUSSION_QUESTION`.
  - *Deliverable Extraction:* Given slide text *"Project Proposal Due Next Tuesday Sept 8 at 4:30 PM"* $\rightarrow$ assert detected item category is `DELIVERABLE_ALERT` and due date string is extracted.
  - *Agenda Extraction:* Given slide titled *"Today's Agenda"* with four bullet points $\rightarrow$ assert agenda topic list contains all four items in order.

- **In-Class Briefing Engine & CLI Router:**
  - *Terminal Output Layout:* Assert CLI text contains expected box headers, progress indicators, exercise prompts, and discussion cards.
  - *JSON Output Schema:* Assert `bb --in-class --json` adheres strictly to the defined schema without missing required keys.
  - *Slide Drilldown:* Given `--slide 14` $\rightarrow$ assert output returns only Slide 14 text and metadata.

### 3. Prior Art in Codebase

- `tests/test_academic_intelligence.py`: Pattern for mocking Blackboard REST API calls and testing assessment inspection.
- `tests/test_outline_explorer.py`: Outline tree traversal and selective expansion tests.
- `tests/test_v2_features.py`: Standardized JSON schema export verification and time window filtering tests.

---

## Out of Scope

- **Real-Time Audio Recording & Speech-to-Text:** Capturing in-room professor audio via microphone or transcribing spoken lecture audio is explicitly out of scope.
- **Computer Vision / Screen OCR:** Capturing video streams of the physical projector or using optical character recognition on live webcam footage is out of scope.
- **Automated Quiz Submission:** Submitting in-class Blackboard assessments or answering graded quizzes automatically is strictly forbidden.
- **Heavy Cloud Neural Model Dependencies:** Relying on external OpenAI API keys or local multi-gigabyte GPU models for baseline extraction is out of scope; the core engine must run locally and deterministically.
- **Modifying Course Materials:** Uploading files, altering grades, or posting content to Blackboard course folders is out of scope.

---

## Further Notes

### 1. Command-Line Interface Syntax

The feature introduces the following CLI commands and flags:

```bash
# Core real-time lecture copilot (auto-resolves active lecture by current time)
bb --in-class
bb -ic

# Override course context manually
bb --in-class -c IS410
bb --in-class -c ECON122

# Inspect a specific slide projected on screen
bb --in-class --slide 14
bb --in-class -c IS410 --slide 22

# Extract and display solution starter for specific exercise
bb --in-class --exercise 1

# Continuous watch mode during class (polls outline for new uploads every 90s)
bb --in-class --watch

# Target a past or future semester week
bb --in-class -c IS410 --week 3

# Force fresh re-download of slide deck bypassing cache
bb --in-class --refresh

# Programmatic JSON output for agent skill consumption
bb --in-class --json
bb --in-class -c IS410 --json
```

### 2. Telegram Bot Integration (`/lecture`)

Subscribers to `@blackboardscrapbot` can access live in-class intelligence directly from mobile devices:

- **Command:** `/lecture` or `/inclass`
- **Output Format:**
  ```html
  🎓 <b>IS 410 Lecture Copilot</b> (Sherman 014)
  ⏱️ <i>Progress: 5:01 PM [██████░░░░░░░░] 31m elapsed / 119m remaining</i>
  📑 <b>Active Deck:</b> <code>Week 2 - Conceptual Modeling.pdf</code> (38 slides)

  📋 <b>Today's Agenda:</b>
  1. Entities, Attributes & Keys
  2. Crow's Foot Notation & Multiplicities
  3. In-Class ER Modeling Lab

  💡 <b>Discussion Prompt (Slide 12):</b>
  <i>"Why should a student ID be surrogate rather than natural?"</i>
  👉 <b>Quick Answer:</b> Prevents PII leaks (SSN) and insulates foreign key cascades from administrative changes.

  🚨 <b>Deliverable Alert (Slide 4):</b>
  • <b>Team Project Proposal:</b> Due next Tuesday, Sept 8, 4:30 PM
  ```
- **Inline Keyboards:**
  - `[🔍 View Slide 12]`
  - `[✏️ In-Class Exercise 1]`
  - `[🔄 Refresh Deck]`

### 3. Agent Skill Specification: `lecture-copilot`

The agent skill (`.agents/skills/lecture-copilot/SKILL.md`) equips autonomous assistants to answer student questions during live classes. When a student types *"What's the answer to the slide discussion question?"* or *"Help me solve exercise 2"*, the agent calls `bb --in-class --json` to load full slide context, extracts the exercise parameters, and produces an academically grounded response within seconds.

### 4. Timetable Discovery Heuristic

If `config.json` does not contain an explicit `"schedules"` entry, the system automatically runs a discovery fallback:
1. Crawls syllabus files synced locally via `bb --syllabus --sync`.
2. Scans for meeting patterns matching `(Mon|Tue|Wed|Thu|Fri)[a-z]*\s*(?:&|,|\band\b)?\s*(Mon|Tue|Wed|Thu|Fri)?[a-z]*\s*([0-9]{1,2}:[0-9]{2}\s*(?:AM|PM))\s*(?:-|to)\s*([0-9]{1,2}:[0-9]{2}\s*(?:AM|PM))`.
3. Auto-populates the `"schedules"` table in `config.json` with user confirmation.

### 5. ADR Alignment

- **ADR 0001 (Shallow Outline Traversal):** Leverages depth-limited REST crawling to locate weekly slide decks without loading unneeded outline folders.
- **ADR 0002 (HTTP REST Fast-Path with Playwright Fallback):** Executes all metadata lookups and file downloads through direct HTTP endpoints in under 200ms without spinning up headless browser processes.
