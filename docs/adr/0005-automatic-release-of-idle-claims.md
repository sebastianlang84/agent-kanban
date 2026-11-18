# ADR 0005: Automatically release idle claims

Status: accepted. Partially supersedes the no-expiry decisions in
[ADR 0003](0003-lifecycle-and-recovery.md) and
[ADR 0004](0004-merge-earlier-implementations.md).

## Context

Advisory reminders can leave tickets held indefinitely when sessions stop updating the board.
A session may still be live while no longer working on a particular ticket, so ticket inactivity
is a more direct expiry signal than session liveness. Releasing immediately on session end is
also unsafe because a session may resume with the same identity.

## Decision

Every start hook, including guest starts ingested on the host, releases `doing` tickets whose
`last_activity` is older than `KB_RELEASE_DAYS` days (default 7), and `blocked` tickets older
than `KB_RELEASE_BLOCKED_DAYS` days (default 30), to `todo`. Host config/environment settings
apply board-wide, with the environment taking precedence; 0 disables the corresponding rule.
Invalid settings release nothing and are logged without failing the hook. Recovery and
curator-held tickets are exempt. Session hooks never refresh ticket activity.

Release uses the ordinary unaddressed release fields: clear holder and claim time, clear a task's
addressee but retain a request's addressee, and preserve host, branch and worktree. A guarded
UPDATE requires the selected holder and activity time still to match. An automatic `release`
note names the previous holder, its last-seen age and branch/worktree; its author is the holder
and it does not refresh ticket activity. Only a live start releases: a guest's replayed start
would judge claims made after it occurred. The flag, the claim line and the hint end with the
next claim or release of the ticket. No schema change is needed.

## Consequences

After release, plain `kb claim <id>` suffices where an explicit `--take` was needed before
(requests still require their addressee). A holder working for seven days without any note or
touch loses its claim even if the session is live. Concurrent work remains possible.

Mitigations make that risk visible: the idle reminder gives the release date and says a note
or `kb touch <id>` keeps the claim; the board marks `auto-released`; claiming prints the previous
holder's label, last-seen age and saved branch/worktree; the previous holder receives a hint
once per release note, only while the ticket is still todo without an assignee.

After at least 14 days of operation, measure using the
[collection protocol](../idle-claim-measurement.md):

- The number of idle `doing` tickets.
- The share of released tickets reclaimed by the same holder within 24 hours; above 20% means
  the threshold is too short.
- Where released tickets ended up: closed, reclaimed, parked, or untouched in `todo`.
- Any documented case of double work. One such case reverses this decision.
