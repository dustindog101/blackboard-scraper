# Blackboard skill audit — 2026-10-20

Two independent Blackboard skills exist: this repository's `.agents/skills/blackboard-assistant`
and a Claude-only `~/.claude/skills/blackboard-assistant` workaround. The Desktop repository
path resolves to the main school checkout, so it is an alias rather than a third skill.
The candidate's separate worktree initially had a byte-identical repository skill.
No Blackboard skill entrypoint was found under `~/.codex/skills` or `~/.agents/skills`.
The eleven school ops skills implement separate scheduled/explicit workflows.

## Candidate changes

- Keep the operational skill versioned with its CLI. Global `bb` still executes the
  original checkout; use the candidate's `main.py` and configured interpreter for review.
- Replace fixed semester/account examples, universal timing claims and always-forced
  SMS login instructions with current health, HTTPS refresh and offered-Duo-method behavior.
- Explain official versus calculated grades, unknown/partial records, discussion own-post
  verification, announcement read-state uncertainty and authoritative provider completion.
- Keep ordinary reads separate from configuration changes, tracker syncs and notifications.
- Replace the duplicated CLI catalog with a pointer to `docs/CLI_REFERENCE.md`; retain
  short conditional REST/login references without personal identifiers.

The candidate skill passes `skill-creator/scripts/quick_validate.py`. An independent
Sol high reader forward-tested four simulated requests (no external side effects):
no-save weekly deadlines, login with a network timeout, delayed passcode handoff into
one running process, and ambiguous discussion/submission plus hidden-rubric inspection.
Selected flags were present; relative links resolved. Its interpreter-path and mid-flow
input-channel feedback was incorporated. This is behavioral review, not live provider
completion or a statistical evaluation.

## Guidance checked

[OpenAI Build skills](https://learn.chatgpt.com/docs/build-skills),
[OpenAI's September skill guidance](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra),
and [Anthropic skill authoring guidance](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
were retrieved during the audit. They support concise routing descriptions, conditional
references and realistic behavior checks instead of duplicating manuals or adding rigid
instructions for every task.

## Promotion boundary

Installed/main and the independent Claude workaround are unchanged. After candidate
promotion, migrate the Claude workaround to a thin entrypoint pointing at the promoted
guide while preserving its explicit-only invocation preference. Do not install a third
same-name global copy. The separate school-bb-sync and school-due-alert routing/login
contradictions were noted but those jobs, policies and schedules were not changed here.
