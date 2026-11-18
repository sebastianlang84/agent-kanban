# ADR 0001: Use a small Python and SQLite board

Status: accepted.

## Context

Coding-agent sessions need a shared record of work, ownership, handoffs, and requests across
machines. Installation should require little infrastructure and work on hosts with older system
libraries. Existing task trackers can provide richer workflows, but introduce binary dependencies,
additional configuration, and concepts beyond this board's scope.

## Decision

Implement the board as one Python 3.9 standard-library CLI over a local SQLite database.
Use SSH for remote access rather than running an application server. Keep board data outside
repositories, except in explicitly ignored paths.

## Consequences

- Concurrency, migrations, backup, and recovery are maintained in this project.
- State transitions use transactions and guarded updates. Claim-race tests check that only one
  session wins, including a mutation check against a deliberately unsafe implementation.
- The schema is versioned with `PRAGMA user_version`.
- Setup can install harness hooks and agent guidance; ordinary board commands do not edit
  repository instructions. There is no telemetry service.
- Reconsider a larger tracker if dependency graphs, multiple human curators, or maintenance costs
  exceed the value of a small standalone CLI.
