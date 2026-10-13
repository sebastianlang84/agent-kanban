# ADR 0003: Revise the plan after the 2026-09-23 design review

Status: accepted, 2026-09-23, by the curator. Revises `PLAN.md` D3–D8 and P1–P3. Recorded as an
ADR on 2026-10-13; the raw reviews and their adjudication lived in `reviews/` until commit
f2ef896 and remain in the git history.

## Context

Two reviewers, `claude-fable-5-1` via Claude Code and `gpt-6-astra` via codex, read the first
`PLAN.md` (213 lines). Each finding was checked against the plan and, where it made a factual
claim, against svrllm.

## Decision

The plan takes the 15 confirmed findings:

| # | Finding | Plan change |
|---|---|---|
| C1 | `kb board --mine` hides other sessions' work, so the pre-start check cannot find duplicates. | The board shows all sessions (D5, D6). |
| C2 | Close by "assignee or opener" lets the opener close work another session is doing. | A task closes only by its assignee; the curator can force (D3). |
| C3 | Stale claims released by "the next session" is unreliable expiry; git heuristics fail both ways. | No automatic expiry; flags are hints, only owner or curator releases (D5). |
| C4 | Identity names a location, not a session. | Per-session token (D4). |
| C5 | Nothing records a session that ends without `kb close`. | Recording hooks for start, stop and end (D6). |
| C6 | The race test can pass without an atomic claim; SQLite 3.34 lacks `RETURNING`. | Start barrier and mutation check in P1. |
| C7 | The lifecycle leaves who may block, release, abandon or reopen undefined. | Transition table (D3). |
| C8 | P3 compliance has no independent denominator. | Start events as denominator (P3, `kb report`). |
| C9 | P0 spike and the D1 switch condition depend on each other. | P0 dropped; D1 decided for the own `kb` (ADR 0001). |
| C10 | The bus migration lacks carry-over, idempotent import and cutover. | D7 import tool and cutover. |
| C11 | No backup or recovery for the SQLite file. | Backup and `kb restore`, claims blocked until reconciled (D3). |
| C12 | Confidentiality unresolved; HTTP named without authentication. | D8: database outside any working tree; `kb` itself only over ssh (D2). A later revision (2026-09-24) added a read-only dashboard export of titles and metadata, so titles carry nothing confidential. |
| C13 | codex on svrllm reads no global instruction file. | `~/.codex/AGENTS.md` gets the Board line (D6, `kb setup`). |
| C14 | Notes from Windows need stdin from the start. | Note text is read from stdin. |
| C15 | `note.kind` alone does not say whether a ticket is claimable. | Ticket type `task` or `request` (D3). |

Kept as lower-priority notes, not as plan changes: explicit `BEGIN IMMEDIATE`, WAL and
`busy_timeout` (an implementation note, adopted in the code); approval of the bus migration by all
three bus sessions, since another session proposed it (handled in the D7 cutover); undefined
behaviour when svrllm or ssh is unreachable (later defined by D9: exit code 3, hook spool).

Rejected: section 7 read as injected instructions (it is the plan's next step); redacting hostnames
(the curator approved sending the plan); SQLite on a network mount (local ext4, measured); Claude
Code not reading the global AGENTS.md (`~/.claude/CLAUDE.md` links to it, measured); "sessions
agreed" as no criterion (the bus README required it, the curator decides in addition).

## Consequences

- Later feature reviews (approvals, bus import, D9, hooks, report, ticket rename; 2026-09-24) were
  folded into the code and `PLAN.md` directly and are kept only in the git history.
