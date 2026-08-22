# Shallow Course Outline Default with Selective Expansion

## Context
Blackboard courses often contain dozens or hundreds of items organized across modules, review materials, and weekly folders. Previously, `--outline` unconditionally printed the entire recursive tree down to all leaves. This caused severe terminal overflow, burying the specific active folders (e.g. current homework or project) that students actually needed.

## Decision
We default `--outline` to a shallow view that displays root items directly and renders top-level container folders and learning modules in a collapsed state with rich item count breakdowns (e.g., `📁 Homework [folder] [ID: _105740_1] (12 items: 8 assignments, 4 files)`). We provide `--folder <name|ID>` (alias `-f`) for selective subtree expansion, `--expand-all` / `--deep` for full recursive tree dumps, and `-i` / `--interactive` for an interactive terminal folder menu.

## Consequences
Terminal output remains concise, immediately readable, and fast to scan. Users can target specific folder subtrees without terminal noise. Automated workflows and agents needing the full tree can pass `--expand-all` or use `--json`.
