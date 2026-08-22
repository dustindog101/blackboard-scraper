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
