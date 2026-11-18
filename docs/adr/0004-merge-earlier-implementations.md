# ADR 0004: Merge two earlier implementations

Status: accepted. [ADR 0005](0005-automatic-release-of-idle-claims.md) supersedes the original
no-expiry rule for idle claims.

## Context

Two earlier implementations served the same purpose. The unversioned `kb.py` CLI, retained as
`legacy/kb_a.py`, provided notifications, backlog labels, explicit takeover, token sessions, and
a terminal curator. The versioned `kb` CLI provided a richer ticket lifecycle, installation-scoped
identity, guest mode with a spool, prompt hints, guard hooks, reporting, restore, and setup.

A resumed agent can retain its harness session ID after an end hook. Releasing its work solely
because of that end event risks duplicate work. Restricting answered-request closure to the
original opener can also leave completed requests open indefinitely.

## Decision

Use the versioned implementation as the base and merge the complementary features:

- Explicit takeover of holders flagged `session-ended` or idle; session-end hooks retain claims.
- Harness discovery, token sessions, `kb name`, `kb end`, and the global `--session` option.
- Explicit `--claim` and `--todo`, initial request notes, and word-aware duplicate detection.
- Task closing lines, request results and withdrawal, and closure by permitted later sessions.
- Labels, hidden backlog, JSON board output, and addressed requests including `any`.
- Start context, harness-specific hook output, hook disabling, debug logs, and `notify.d`.
- Configured database paths, Git-ignored storage paths, and rotating backups.
- Schema migration for `event` notes and automatic import of the earlier unversioned tables.

## Consequences

- Script-created token tasks stay unclaimed unless `--claim` is used; harness-created tasks claim
  by default. `kb start` creates a token session; `kb name` renames a harness session.
- Helper scripts live in `tools/` and operate on the current schema.
- `legacy/kb_a.py` remains a migration fixture, exercised by `tests/test_legacy.py`.
  Feature regressions are covered by `tests/test_ported.py`.
- Terminal curator identity remains suitable only for a trusted host and operator.
