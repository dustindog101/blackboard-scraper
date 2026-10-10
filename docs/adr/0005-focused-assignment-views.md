# Focused assignment output with rubric discovery

## Context

Assignment inspection previously printed existing test answers and discussion posts by default while omitting native rubrics. A request for instructions could consume substantial agent context. Assignment titles also concealed actual types, and some instructors kept prompt/rubric documents beside an otherwise empty submission item.

## Decision

The CLI default returns instructions, brief metadata and availability hints. Mutually exclusive instructions, rubric and full views control retrieval and output, including JSON and Markdown. Lean views do not retrieve previous question/answer or discussion-post bodies. Existing Python inspector callers retain full-detail defaults; CLI consumers needing previous work add `--full`.

Native rubric previews strictly require `displayBefore: true`. Failures remain unknown. Only explicit prompt/rubric resources in matching assignment folders are followed, with supported provider/document types, bounded reads, provenance and visible truncation/failure notices. Cookies remain scoped to Blackboard. External learning tools are labelled and never launched. Existing submission file metadata is exposed only in full output.

## Consequences

The default is useful to humans and agents without unrelated previous-work payloads. Full output is an explicit request for accessible detail, not an attempt-start request or a course-wide crawl. This changes the CLI output default described in ADR-0003 while retaining all its attempt initiation and timed-exam guards. Starting an attempt still requires explicit flags and implies full output.
