# ADR 0002: Ship the agent instructions as a skill in the kb repository

Status: accepted, 2026-09-24, by the curator (kb ticket #65). Revises `PLAN.md` D6.

## Context

D6 put the board rule into one global instruction file on the host (`~/.agents/AGENTS.md`). That
file is the curator's private configuration. A colleague who installs kb from the repository gets the
CLI and `docs/workflow.md`, which is written for humans, but no agent guidance. Instructions kept in
one private file also drift from the CLI they describe.

An MCP server was considered instead. All three harnesses have a shell and kb is a CLI, so an MCP
server would add no capability; it would still need the when-and-why rules, and it loads tool
schemas into every session.

## Decision

- The repository ships the agent skill `skills/using-kb/SKILL.md`: identity, task workflow,
  requests, approvals, flags, hooks. It is versioned with `kb` and changes in the same commit as the
  behaviour it describes.
- An installation links the skill into the global skill directory (`~/.agents/skills/using-kb`)
  and writes one trigger line into the global instruction file: run `kb board --repo <repo>` before
  starting a task and load the `using-kb` skill. Until the P5 install script exists, both are done
  by hand.
- The global instruction file keeps only what is specific to one installation: who the curator
  is, how sessions are notified, the machine-specific ssh line.

## Consequences

- A rule about kb that holds for every installation is written in the skill, not in an
  instruction file.
- The trigger line stays in the always-loaded file, so a session still checks the board when the
  skill's description alone would not trigger.
- Revisit MCP only as a thin wrapper around `kb` if clients without a shell must use the board.
