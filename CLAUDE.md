# CLAUDE.md

Guidance for Claude Code when working in this repo.

## Mistake log

This repo keeps a `MISTAKES.md` — a log of real mistakes made while
working here (bugs shipped and caught, wrong assumptions, test/lint
setup missteps), each with root cause, fix, and the general lesson.

- **Before starting non-trivial work** (a new feature, a refactor across
  several files, anything touching an area with history), skim
  `MISTAKES.md` for entries relevant to the files or subsystem you're
  about to touch.
- **When you catch a real mistake** — something a test, a lint pass, a
  second read, or the user caught, not a typo fixed before running
  anything — add an entry to `MISTAKES.md` using the format at the top
  of that file, before moving on.
- If a past entry is directly relevant to a decision you're making,
  reference it explicitly in your response (e.g. "stubbing the full
  field set here per the 2026-08-13 Jinja Undefined entry") so the
  connection is visible, not silent.

## Project overview

See `README.md` for architecture, local setup, deployment, and scope.
