# ADR 0001: Build an own `kb` instead of adopting beads

Status: accepted, 2026-09-23, by the curator. Source: `PLAN.md` D1 (option B), 2.6.

## Context

Agent sessions on svrllm and the Windows workstation need a shared board: who works on what, since
when, what a finished session left behind, and requests between sessions (`PLAN.md` G1–G6). G6,
"as lightweight as possible", is an explicit curator constraint.

beads v1.3.0 (`bd`) was tested in a `python:3.11` container (`PLAN.md` 2.6). It met G1–G5, including
an atomic 10-way claim race and 20 concurrent comments. Its costs on svrllm:

- The release binary needs glibc ≥ 2.34; Debian 11 on svrllm has glibc 2.31. Running it needs a
  source build (untested), a `docker run` wrapper, or an OS upgrade.
- The binary is 154 MB.
- `bd init` writes `AGENTS.md`, `CLAUDE.md`, `.claude/settings.json`, `.codex/config.toml`,
  `.codex/hooks.json`, `.cursor/hooks.json` and a git repo. In a real repository this conflicts with
  the rule that `AGENTS.md` has one owner.
- Schema churn is fast: v1.3.0 alone applies 13 migrations (v53 → v66).
- The surface is large (gates, molecules, federation, memory) and overlaps with the brains.

The alternative, option B, is one Python 3.9 stdlib file on SQLite, both already installed on svrllm.

## Decision

Build an own `kb` (option B). The planned P0 spike (source build of `bd` on Debian 11, `docker run`
measurement) is dropped.

## Consequences

- Concurrency correctness is our own code. P1 carries it: every transition is one
  `BEGIN IMMEDIATE` transaction with a guarded `UPDATE` checked by `rowcount`, and a 10-process claim
  race test that also turns red against a read-then-write claim.
- Maintenance, schema migrations and export are ours; the schema is small and versioned by
  `PRAGMA user_version`.
- Nothing is written into harness configuration or repositories, and there is no telemetry.
- The beads test setup was removed from svrllm on 2026-09-23.

Revisit and take beads instead if any of these holds:

- dependency graphs between tasks become necessary;
- more than one curator uses the board;
- building `bd` from source on Debian 11 succeeds within a time box and a pinned version is
  acceptable;
- a measurement shows the `docker run` wrapper costs less than building and maintaining `kb`.
