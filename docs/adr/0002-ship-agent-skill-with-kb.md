# ADR 0002: Ship agent guidance with the CLI

Status: accepted.

## Context

A CLI alone does not tell an agent when to check the board, how to avoid duplicate work, or how
to hand off an unfinished task. Instructions kept only in one installation can drift from the
behavior they describe and are unavailable to new users.

## Decision

Ship `skills/using-kb/SKILL.md` with the repository and update it alongside public behavior.
The skill covers identity, task workflow, requests, approvals, flags, and hooks.

`kb setup` links it into supported skill directories and adds a short board trigger to existing
global harness instruction files. Installation-specific details, such as the operator's identity,
SSH destination, and notification preferences, stay in local configuration.

## Consequences

- Shared rules live in the skill; local rules stay in installation guidance.
- The trigger reminds agents to check the board even before they load the skill.
- A shell-accessible CLI is sufficient for the supported harnesses. An MCP wrapper would only be
  useful for clients that cannot invoke shell commands.
