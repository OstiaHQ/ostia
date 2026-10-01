---
number: 15
title: Code comments
status: Accepted
authors: [ShAlireza]
components: [build]
created: 2026-09-30
updated: 2026-10-01
supersedes: []
superseded_by: []
discussion:
---

# ADR-0015: Code comments

## Context

[ADR-0013](0013-code-style.md) settles formatting and naming, but says nothing about comments. Much of Ostia's code is written with coding agents, and they tend to over-comment: they narrate each step ("Now we…", "Step 1:"), repeat the next line in words, leave commented-out code and dividers, and write notes about the edit ("Added the retry as requested") that belong in the commit. Such comments bury the few that matter and go stale as the code changes.

The code already has a house style: short comments that say why, state an invariant or cite an RFC section (see `tools/ci/`).

## Decision

A comment says what the code cannot: why it is this way, what must stay true, or what can go wrong.

**Write a comment for:**

- a reason that isn't obvious: a constraint, a rejected alternative, a workaround (with its issue or upstream link);
- invariants, preconditions, units, ownership and lifetime;
- hazards: thread safety, CUDA stream ordering, ABI stability, performance traps;
- references: `RFC-0001 §3.3`, `ADR-0014 rule 2`, a paper or a spec section;
- public API: docstrings, and comments on the C ABI headers.

**Don't write:**

- comments that restate the code, or narrate the steps it takes;
- commented-out code: delete it, git keeps the history;
- change logs or notes to the reviewer ("added", "fixed the", "as requested", "(new)"): they go in the commit message or pull request;
- banners and dividers: split the file or use functions instead;
- a `TODO` without an issue: write `TODO(#123)`;
- comments that no longer match the code: fix them in the same change.

Match the comment density of the code around you, and prefer a better name to a comment.

## Consequences

- `tools/ci/check_comments.py` flags the mechanical cases with heuristics: commented-out code, banners, narration, change-log notes, comments that restate the next line, TODOs without an issue, and over-commented edits. `comment-ok` on a line silences a false positive. *(update: RFC-0005 Rollout PR B: it moved to `tools/ostia-dev/src/ostia_dev/ci/check_comments.py`, run as `ostia-dev check comments`, and lint is `pixi run ostia-dev lint`.)*
- `pixi run lint` runs it over every tracked file as the `comments` check, so the pre-commit hook and the CI `lint` job enforce it for everyone. The judgement calls it can't make, such as whether a comment says something that matters, are left to review.
- `.claude/settings.json` also runs it as a Claude Code `PostToolUse` hook on every Edit and Write. It checks only the text the edit added and returns the findings to the agent, which revises the comments before lint ever sees them. The edit itself is kept.
- The hook is stricter than lint in two places: named section banners and the density rule. Banners already in long files stay until someone edits them; new ones are flagged. Density only makes sense for a single edit.
- A heuristic false positive now fails CI. Mark the line `comment-ok`, and if a rule misfires often, tune it in the checker.
