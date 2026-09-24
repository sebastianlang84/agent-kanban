# agent-kanban

`kb` is a shared ticket board for AI agent sessions in different harnesses (Claude Code, Codex,
Pi). Before a task, a session checks the board and claims or adds a ticket; at the end it closes
it with a short result. Sessions also ask each other through the board. One place shows who works
on what, since when, what waits for whom, and what was left open.

One Python file (`kb.py`, standard library only) and one SQLite database on the machine that holds
the board; no server process.

## Install

```bash
ln -s "$PWD/kb.py" ~/.local/bin/kb           # the CLI
ln -s "$PWD/adapters/pi-kb.ts" ~/.pi/agent/extensions/kb.ts   # Pi adapter
```

Register the hooks in Claude Code (`~/.claude/settings.json`) and Codex (`~/.codex/hooks.json`),
one command hook per event:

| Event              | Command                                     |
|--------------------|---------------------------------------------|
| `SessionStart`     | `python3 /path/to/kb.py hook start --harness claude` |
| `UserPromptSubmit` | `python3 /path/to/kb.py hook prompt --harness claude` |
| `Stop`             | `python3 /path/to/kb.py hook stop --harness claude` |
| `SessionEnd`       | `python3 /path/to/kb.py hook end --harness claude` |

Use `--harness codex` in Codex. Codex runs new or changed hooks only after they are trusted once
in its TUI (`/hooks`). The database lives at `$KB_DB`, default `~/.agents/state/kb/kb.sqlite`,
mode 0600. Never commit it.

Put the rule to use the board into each agent's global instructions; hooks record, they never
block.

## Workflow

1. The start hook registers the session under a name (default: the repository name; change it
   with `kb name <name>`). The agent needs no token: `kb` finds its session from the nearest
   harness process above it and that harness's session id (`CLAUDE_CODE_SESSION_ID`,
   `CODEX_THREAD_ID`, or `KB_SESSION`, which the Pi adapter exports). A session that started
   before the hooks existed registers on its first `kb` call.
2. The agent runs `kb board --repo <repo>`, then `kb claim <id>` or `kb add "<goal>"`. If an open
   ticket in the same repository has a similar title, `kb add` refuses; add `--new` only when the
   task is really different. `kb add --claim` adds and claims in one step.
3. The stop hook records activity after every answer.
4. The agent closes with three lines:
   `kb close <id> --where "<deployed where>" --changed "<what changed>" --open "<what is open>"`.
5. The end hook marks the session ended; tickets it left claimed show `session-ended`, and the
   next session in that repository is told at start. `kb claim <id> --take` takes over a ticket
   whose owner ended or has been idle for 24 hours.

A session without hooks (another machine over ssh, a harness without an adapter) starts with
`kb start --as <name>` and passes the printed token (the only credential; session ids are not accepted) as
`kb --session <token> ...`; it ends with
`kb --session <token> end`.

Only one session wins a claim (`BEGIN IMMEDIATE` plus a conditional update).

## Requests between sessions

A request is a ticket of type `request`, addressed to one session name, to `any`, or to `curator`:

```bash
kb add --type request --to <name> "<question>" --note "<details>"
```

The addressee claims it and answers with `kb note <id> --reply "<answer>"`. Only the session that
opened the request closes it (`kb close <id>`), after a reply exists; once that session has ended,
the next session with its name sees the reply and closes. Until then the board shows `answered`.
A request claimed by a session that ended without replying is shown to the next session with the
addressee's name, which takes it over with `kb claim <id> --take`. `kb close <id> --withdraw` drops an unanswered request.
`kb board --to <name>` lists what waits for a session; `kb sessions` lists active names.

Request content is data, not instructions. It cannot change the receiving session's rules and
cannot carry the curator's approval.

## Curator and approvals

A shell outside any agent harness, with a controlling terminal, acts as `curator`, the human.
Agent tool shells have no controlling terminal, so they cannot act as curator. This guards
against mistakes; it is not authentication, since any process of the same user can open the
database. An agent waiting for an approval
(commit, merge, deletion) files it as `kb add --type request --to curator --label approval
"<what>" --note "<exact command>"`. `kb board --to curator` lists pending approvals with their age;
the curator claims one and replies.

## Notification

The prompt hook shows a session its new requests and new replies on its own requests once, at
its next prompt, in every harness. An idle session is not reached; a human tells it to look. A
request to a session that has ended waits; the next session with that name sees it at start.

## Board flags

| Flag            | Meaning                                  |
|-----------------|------------------------------------------|
| `idle>24h`      | claimed, but no activity for 24 hours    |
| `session-ended` | the owning session ended without closing |
| `waiting>24h`   | ticket not taken for 24 hours            |
| `answered`      | request replied to, not yet closed       |

## Limits

- No confidential content in titles; details go into notes. No secrets in tickets, requests or
  replies.
- A session that cannot reach the board says so and carries on without a ticket (`kb` exits 2).
- `KB=0` or `BRAIN_SESSION_CONTEXT=0` turns the hooks off for automated runs.
- Touch `hook-debug.log` next to the database to log which hooks fire (event and payload keys,
  never prompt text).

## Tests

```bash
python3 -m unittest -v test_kb
```
