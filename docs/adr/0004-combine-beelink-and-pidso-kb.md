# ADR 0004: Combine the beelink kb.py and the pidso kb into kb 0.3

Status: accepted, 2026-10-14, by Sebastian ("ich will wirklich alles was positiv ist übernehmen
und einbauen"). Designed with Codex `gpt-6.1-sol` and an Opus planner; reviewed by Codex.

## Context

Two implementations of the same board existed. The beelink `kb.py` 1.x (now `legacy/kb_a.py`)
ran the live board on beelink: about 1000 lines, 18 tests, push notifications (`notify.d`,
`kb-watch`), backlog labels, takeover of stale claims, a tty curator and a board other machines
used over ssh with tokens. The pidso `kb` 0.2 had a versioned schema, a richer lifecycle
(blocked, abandoned, reopen), installation-scoped identity, guest mode with a spool, prompt
hints with close and idle reminders, the guard hook, `kb report`, restore, setup and 205 tests.

On 2026-10-13 a Claude session restarted with `--resume`. `kb.py`'s end hook marked it ended and
the next `kb` call released its claims automatically; the human then thought it dead and another
session did the same work twice. Many answered requests also stayed open for weeks, because only
the opener could close them.

## Decision

The pidso `kb` is the base. Ported from `kb.py`, each with a test in `tests/test_ported.py`:

- No automatic release (pidso D3 already); `kb claim --take` takes over a ticket only when the
  board flags its holder `session-ended` (without a later start or stop) or `idle>`.
- Identity: nearest harness via `/proc` for nested harnesses, `CODEX_THREAD_ID`,
  `KB_SESSION=pi:<id>` from the pi adapter, `KB_HARNESS`, the tty curator, `kb start` always a new
  token session (32 hex, so saved tokens stay valid), `kb name`, `kb end`, a global `--session`.
- `kb add --claim/--todo` (a harness session claims at once, a token session files todo),
  `--note`, a first note on every request, word-aware similarity.
- Close with `--where/--changed/--open`, `--result`, `--withdraw`; a request closes by its
  opener, assignee, the curator or a later session with the opener's name; the reply needs the
  claim, except the opener of a request to the curator records the curator's answer.
- `kb label`, hidden `backlog`, `kb board --closed/--json`, `--to` including `any`.
- Hooks: start context, JSON output for Claude Code and codex, `KB=0` /
  `BRAIN_SESSION_CONTEXT=0`, `hook-debug.log`; push notifications through `notify.d`.
- `KB_DB` from the config; a database in a git-ignored path; backup rotation `--dir/--keep`.
- Schema 6 adds the note kind `event`; a beelink board is imported in place on first open.

## Consequences

- Tasks added by scripts with a token stay todo, as before; agents get their tasks claimed.
- `kb start` inside a harness no longer renames the harness session; `kb name` does.
- The helper scripts moved into the repository (`tools/`) and read the new schema.
- `legacy/` keeps the old implementation for the import tests and a rollback onto a backup.
- Revisit the tty curator if more than one person uses a host.
