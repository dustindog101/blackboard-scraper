# PR: fix(quiz) — fidelity + honest --start-attempt (for agent review)

> Pushed from the quiz-dev worktree (`gh` authenticated as dustindog101).
> Open for agent review — accept or request changes per the checklist below.

- **Branch:** `fix/quiz-fidelity-and-start` (based on `dev` @ `2c750a0`)
- **Commit:** `6ed5bdd` (+ this handoff file)
- **Spec:** `docs/adr/0003-*` (unchanged principles) + new `RESEARCH-NOTES.md §8`
- **Base for merge:** `main` (origin has no `dev` branch, so the PR targets
  main and carries the full quiz-dev + fix diff for one review)

## Why (spec gap)

ADR 0003 promises: (a) faithful question/choice extraction, (b) `--start-attempt`
that actually starts when asked. Live run against AGNG 100 Module 3
(`_8836912_1` / attempt `_36086680_1`, 11 questions) showed three violations:

1. **Duplicate numbering** — presentation block + Q1 both `number: 1`
   (`visibleQuestionNumber` is shared with non-graded blocks).
2. **Matching terms dropped** — only `answers[]` (definitions) parsed;
   `prompts[]` (terms: Health/Wealth/Life span) ignored.
3. **`--start-attempt` silent no-op on REST** — metadata-only returned,
   flag swallowed. Plus `givenAnswer` shape drift (`givenAnswers` plural,
   per-option boolean arrays) crashed the whole REST parse once
   (`TypeError` on `"; ".join`, caught → silent browser fallback).

## What changed (`scrapers/quiz.py`, `scrapers/assignments.py`, `RESEARCH-NOTES.md`)

- Positional numbering (comment cites the live collision).
- `match_terms[]` parsed from `question.prompts[]`, rendered in CLI + Markdown.
- `givenAnswer` accepts dict / str / list; bool-only lists parallel to
  `choices` zip to selected texts (Module 3 Q9 verified); other lists sanitized.
- `allow_start` + no attempt → warn to stderr, fall through to Playwright
  starter (timed `--force-start` guard unchanged). REST stays GET-only.
- Lint: dropped unused `Tuple` / `SESSION_DIR` imports, placeholder-less f-string.
- `RESEARCH-NOTES.md §8`: `POST .../gradebook/columns/{id}/attempts` exists in
  public docs but is deliberately NOT wired (orphan-row risk, XSRF, safety).

## Verification (all live, read-only except the guarded starter)

- `ruff check main.py scrapers/quiz.py scrapers/assignments.py scrapers/outline.py` → **All checks passed**
- `--assignment <Module-3-URL> --json` → `engine: http_rest`, 11 questions,
  sequential 1–11, `match_terms: [Health span, Wealth span, Life span]`,
  Q9 `selected: None` (was `"False; False; False; False"` mid-fix).
- No attempts created/modified during verification (info-mode runs only).

## Reviewer checklist (accept / request changes)

- [ ] `match_terms` key addition acceptable on the JSON schema? (additive only)
- [ ] Positional numbering vs `visibleQuestionNumber` — agree?
- [ ] Fallthrough to browser on `--start-attempt`: desired, or prefer loud error?
- [ ] RESEARCH-NOTES §8 reasoning for not wiring POST — agree?
- [ ] Known leftover: browser-fallback question parsing is low quality
      (mislabels type, echoes prompt as answer — seen when REST 500s).
      Accept as follow-up, or fix here?

## To open the PR (agent with gh)

```bash
cd "/Users/king/Desktop/school files/tools/blackboard-scraper-quiz-dev"
git push -u origin fix/quiz-fidelity-and-start
gh pr create --base main --head fix/quiz-fidelity-and-start \
  --title "fix(quiz): sequential numbering, matching terms, givenAnswer shapes, REST --start-attempt fallthrough" \
  --body-file .scratch/quiz-fidelity/PR-BODY.md
```
