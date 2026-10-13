# agent-kanban

`kb` is a shared ticket board for AI coding agent sessions (Claude Code, codex, pi). Before a task,
a session checks the board and claims or adds a ticket; at the end it closes the ticket with three
lines: deployed where, what changed, what is still open. Sessions also ask each other through the
board, and an agent files every approval it waits for as a request to the curator, the person who
decides.

`kb` is one Python file over one SQLite database. There is no server process. One machine, the
host, holds the board. Other machines, the guests, use it over ssh (see "Host and guests").

## Requirements

- Python 3.9 or later, with its bundled `sqlite3` (SQLite 3.26 or later)
- git 2.31 or later (`kb` uses `git rev-parse --path-format`)
- OpenSSH on host and guests, for guest mode
- Linux for the board host (tested on Debian 11; other systems untested)
- A Windows guest needs Python 3.9 or later and OpenSSH.

## Install

Clone the repository and run the setup, as host or as guest:

```sh
git clone git@bitbucket.org:pidso/agent-kanban.git ~/agent-kanban
~/agent-kanban/install.sh --host                  # this machine holds the board
~/agent-kanban/install.sh --guest <ssh-dest>      # this machine uses the board on <ssh-dest>
```

On Windows, run `py -3 <repo>\kb setup --guest <ssh-dest>`. `install.sh` only runs `kb setup`.
Without `--host` or `--guest`, setup asks on a terminal. `--dry-run` prints the actions and
writes nothing. Where no installation id or guest key exists yet, the dry run prints
placeholders: the id, the key and the `kb authorize` line appear only on the real run. Setup is idempotent: a second run changes nothing and prints `ok:` for each step
already done. Before it replaces a file it keeps a copy, `<file>.bak-<UTC time>`. Where it will
not change something, such as an existing file in place of a link or a hook whose `kb` is
missing or not executable, it prints `refused:` and the reason.

Setup writes:

1. The config file `$XDG_CONFIG_HOME/agent-kanban/config` (default
   `~/.config/agent-kanban/config`) with `KB_INSTALLATION`, a random id of this machine. A guest
   also gets `KB_HOST` (the ssh destination), `KB_SSH_KEY` and a dedicated ed25519 key without
   passphrase (`~/.ssh/kb_guest_<installation>`).
2. `kb` on the `PATH`: the link `~/.local/bin/kb`, on Windows `%USERPROFILE%\.local\bin\kb.cmd`.
   Setup warns if that directory is not on `PATH`.
3. The agent skill `using-kb`, linked into `~/.agents/skills` and, if `~/.claude` exists, into
   `~/.claude/skills` (a directory junction on Windows).
4. One Board line in each existing global instruction file (Claude Code `~/.claude/CLAUDE.md`,
   codex `~/.codex/AGENTS.md`, pi `~/.pi/agent/AGENTS.md`). A `CLAUDE.md` with an `@<path>`
   import line of another of these files gets no line of its own.
5. The hooks (start, stop, end, prompt, and guard on the Bash tool), with the full path of
   `kb`: merged into `~/.claude/settings.json` and
   `~/.codex/hooks.json` (every other hook and key kept, the old file backed up), and the
   pi extension `~/.pi/agent/extensions/kb.ts` (from `hooks/pi/kb.ts`). A file that is not valid
   JSON is refused; setup prints the entries to add by hand.

Setup skips a harness whose directory does not exist. These steps stay manual:

- On a guest: make the host reachable by ssh without a prompt. Setup prints one `kb authorize`
  line; the curator runs it on the host. Both are described under "Host and guests".
- codex runs the new hooks only after you trust them once in the codex TUI.
- pi: if the extension does not load, add `"+extensions/kb.ts"` to `extensions` in
  `~/.pi/agent/settings.json`.
- Add who the curator is to the instruction files, if agents should name them.

The board is created on the host at the first call, at `~/.local/share/agent-kanban/board.sqlite`.
Set `KB_DB` to use another path; it must lie outside every git working tree.

Hooks record session start, activity and end, and show a session new requests and replies at its
next prompt, and remind it to close answered requests and the tickets it holds, or to continue,
block or release a held ticket that went idle. A guard hook
warns a session that runs git commit, merge or push or docker compose up without holding a
ticket. They never block and always exit 0. Errors go to `hook-errors.log` in the directory
of `KB_DB` (default `~/.local/share/agent-kanban/`), on a guest too. Hook names per harness:
[`docs/workflow.md`](docs/workflow.md).

Without hooks, `kb` still works inside a harness: a session registers itself from the harness's
session id (Claude Code and codex set one in the environment) on its first call
that needs an identity (`kb add`, `kb claim`, `kb note`, ...). Anything else runs
`kb start --as <name>` first and passes the printed token as `--session <token>`; so does a
harness session whose `kb add` reports "missing session token".

## Use

Agents follow the skill. The commands a person needs most:

```sh
kb board                  # open and running tickets, with flags such as idle>24h
kb board --to curator     # approvals and questions waiting for the curator
kb show <id>              # one ticket with its notes
kb sessions               # session names seen in 14 days or holding a ticket; kb whoami: yours
kb --version              # release, plus the commit in a git checkout: kb 0.2.0 (f2ef896)
```

`kb --version` adds `-dirty` to the commit when `kb` differs from it, and prints the bare release
for a copy outside a git checkout or without git. On a guest it reports the guest's own `kb`.

To act as the curator, start a session on the host from a plain terminal, not from an agent:

```sh
kb start --as curator     # prints a token; pass it as --session <token>
```

A session without a harness session id, such as a script, does the same with its own name.

## Host and guests

Prerequisite: each guest reaches its host by ssh without a prompt. `<ssh-dest>` is what you would
pass to `ssh`, for example `user@host` or an alias from `~/.ssh/config`. The host key must already
be in the guest's `known_hosts`; connect once by hand to accept it.

A guest key can run nothing but `kb serve`. `kb authorize` adds it to the host's
`~/.ssh/authorized_keys` with a forced command, `command="<kb> serve --installation <id>",restrict`.
The host takes the guest's installation id from that line, never from the guest. Curator actions
run only on the host, from a plain terminal; a guest and a harness session never act as curator.

To admit a guest, the curator runs on the host:

```sh
kb start --as curator                 # prints a token
export KB_SESSION=<token>
<the printf ... | kb authorize ... - line that kb setup printed on the guest>
```

`kb authorize` validates the key, keeps every other line, writes a backup of the file first, and
does nothing if the key is already authorized.

On a guest, the `kb` commands work unchanged. Each command travels as one request over ssh; its
output and exit code come back unchanged. `restore`, `recover`, `serve` and `authorize` run on
the host only. The host accepts a guest's note text of at most 1 MiB. It drops a request that has
not fully arrived within 30 s.

If the host is unreachable, a command fails with exit 3 and one line:
`kb: board host <host> unreachable: <reason>`. It never falls back to a local board.

A guest hook has a deadline of 1 s from its start, so it stays within the harness's budget.
`KB_HOOK_DEADLINE` sets it in seconds; a value outside (0, 1.3] falls back to 1 s. A start, stop
or end event that does not reach the host in time stays in the local spool,
`~/.local/share/agent-kanban/spool/`. The next start, stop or end hook sends the queued events
first, oldest first; the host stores each event once. `kb spool` counts the queued events, and
`kb spool --drain` sends them all now, without deadline. Run it after an outage. The prompt and
guard hooks are never spooled: without the host they show nothing.

Tickets of a guest session show `git:unknown` on the board: the host does not check a guest's
git state.

## Limits

- Keep confidential content out of ticket titles; a board view or export may show titles to a
  wider audience. Notes are readable by everyone who can read the board file.
- Never put secrets into tickets or notes. Never commit the database.
- The curator identity is not authenticated on the host; `kb` assumes one person decides. Anyone
  with a shell on the host can act as curator.
- One host serves one person's machines. A guest can take any session name except `curator`.

## Development

`~/.local/bin/kb` is a symlink into the main checkout, so every edit there is live for all sessions at once. Develop in a worktree (`git worktree add ../agent-kanban-<topic> -b feature/<topic>`), test with a temporary `KB_DB`, and merge `--no-ff` into `main` only with approval.

## Releases

Releases follow [SemVer](https://semver.org/), starting at 0.x. Each release is an annotated tag
`vX.Y.Z` on `main`; the same commit sets `VERSION` in `kb` to `X.Y.Z`. There is no changelog: the
tag message and `git log` between tags describe a release.

A MAJOR release, or a MINOR one while the version is 0.x, marks an incompatible change to the
host/guest protocol (`REQUEST_VERSION`, the request `kb serve` reads) or to the database schema.
`SCHEMA_VERSION` in `kb` counts schema versions on its own; a release that raises it is at least a
MINOR release while 0.x. Migrations stay automatic: a newer `kb` migrates an older board, and an
older `kb` refuses a newer one.

## Documentation

- [`docs/workflow.md`](docs/workflow.md): steps, identity, hooks, approvals, flags, `kb report`
- [`skills/using-kb/SKILL.md`](skills/using-kb/SKILL.md): what agents do
- [`PLAN.md`](PLAN.md) and [`docs/adr/`](docs/adr/): design and decisions

Tests: `python3 -m unittest discover -s tests`
