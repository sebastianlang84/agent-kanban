# ADR 0003: Explicit ownership, lifecycle recording, and recovery

Status: accepted. [ADR 0005](0005-automatic-release-of-idle-claims.md) supersedes the original
no-expiry rule for idle claims.

## Context

A shared board must prevent concurrent sessions from silently taking the same work. A display
name is not a stable session identity, a session ending does not prove its work is abandoned,
and recovery must account for claims restored from a backup.

## Decision

- Use per-session tokens and installation-scoped harness identities. Names remain routing labels.
- Show the repository's full board before a session starts work; `--mine` alone hides conflicts.
- Enforce permissions for claim, release, block, resume, close, abandon, and reopen. A task's
  holder closes it; the human curator can force a transition.
- Record start, stop, and end through harness hooks. Session-end flags advise explicit takeover
  rather than immediately releasing claims. Idle expiry is defined separately in ADR 0005.
- Use atomic SQLite transactions with guarded updates and check affected-row counts. Synchronize
  concurrent claim tests so they exercise a real race.
- Distinguish tasks from addressed requests. Accept note text on stdin for portable quoting.
- Import old communication records idempotently and preserve their identifiers and history.
- Back up the database and put restored claims into recovery until reconciled.
- Keep data outside tracked working-tree paths. Remote access uses authenticated SSH; hooks have
  bounded deadlines and spool lifecycle events when the host is unreachable.
- Measure board use against recorded session starts, excluding short child runs without board use.

## Consequences

Lifecycle and recovery rules are enforced by the CLI, rather than relying on agents to infer
ownership from names or Git activity. Installation integrates supported harnesses through setup,
while the same CLI remains usable by scripts and plain terminal sessions.
