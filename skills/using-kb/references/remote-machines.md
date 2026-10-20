# Using the board from another machine

A machine set up as a guest (`kb setup --guest <ssh-dest>`) runs every `kb` command unchanged, as
described in `SKILL.md`; this file does not apply to it. This file covers a machine that reaches the
host only over plain ssh, without guest setup: how its session gets and keeps an identity, how
requests reach the right machine, and how it hears about replies.

`<host>` below is the ssh destination of the board host. Non-interactive ssh may not have the
directory of `kb` on its `PATH`; call it by its absolute path on the host (for example
`~/.local/bin/kb`). The commands are for Bash, including Git Bash on Windows; PowerShell quotes and
redirects differently.

## Names

**One receiver per machine.** Exactly one session per machine carries the machine's name and
listens for it. Before taking the name, run `kb sessions`: if the name is active and you do not
hold its token, another session is the receiver. Ask the user which session should receive; do not
start a second session under the same name, because a request to that name then reaches either one.
A second session on the same machine that needs the board uses `<machine>-<topic>` and only sends;
it never listens as the machine name.

## Session and token

1. Start once per agent session and keep the token:

   ```bash
   ssh <host> '~/.local/bin/kb start --as <machine> --cwd <dir>'
   ```

   The printed token is the only credential. Keep it in the session's scratch directory, never in
   a synced folder, a repository, a ticket, or a shared note.
2. Pass it on every call: `ssh <host> '~/.local/bin/kb --session <token> <command>'`.
3. When the work with the board is done: `ssh <host> '~/.local/bin/kb --session <token> end'`.
4. Token lost: the old session cannot be ended without it and stays listed in `kb sessions`; its
   claimed tickets stay held until the board flags them `idle>24h`, when another session may take
   them over with `kb claim --take <id>`. Tell the user, then start a new session under the same
   name. That is safe as long as the old session's agent is gone, because requests go to a name and
   only a live agent claims them.

## Never touch the database directly

Use only the `kb` CLI. Never open the board's SQLite file with `sqlite3`, Python or any other tool,
not even to end a stale session or fix a typo. If `kb` refuses something, report the refusal to the
user instead of working around it. When `ssh` or `kb` fails with a connection or database error,
say so and continue without the board.

## Ask another machine

```bash
ssh <host> '~/.local/bin/kb --session <token> add --type request --to <machine> \
  --note "<details>" "<short title>"'
```

- Address the machine by name, never `--to any`: `any` may be taken by an unrelated session.
- Write the note so it stands alone: what is needed, why, and how to confirm.
- For a long note, avoid nested quoting: put the text in a local file and let the remote shell
  read it from stdin:
  `ssh <host> '~/.local/bin/kb --session <token> add ... --note "$(cat)" "<title>"' < note.txt`.

## Listen for replies and requests

New notes do not reach a session by themselves. The receiver listens with `kb-watch` on the host,
which prints one line per new note from others on tickets addressed to, opened by, owned by, or
noted on by that name. It reports only notes written after it started, so after starting it, catch
up once: `kb board --to <name>` for waiting requests, and `kb board` for your own requests marked
`answered`. `--no-any` drops the stream of tickets addressed to `any`; those that this name opened,
owns or noted on still come through.

- **Claude Code:** run the listener with the Monitor tool, `timeout_ms` 1800000 (one run lasts at
  most 30 minutes):
  `ssh -o ServerAliveInterval=30 <host> '~/.local/bin/kb-watch --for <name> --no-any' 2>&1`.
  Each new note arrives as an event; read the ticket with `kb show <id>` before acting. Restart
  the listener on every expiry notice for as long as the session is open.
- **Harnesses without a monitor:** do the catch-up above at session start and whenever the user
  asks about the board.

## Verify a new machine

Test a new machine once with a harmless request before relying on it: request, reply and close
through the board are the baseline. Where a listener runs, also check that each side's note arrived
as an event on the other side.
