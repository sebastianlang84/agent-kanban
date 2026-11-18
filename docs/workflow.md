# Session workflow

How one agent session uses the board. Diagram: [`workflow.html`](workflow.html). Design rationale:
[architecture decisions](adr/). Agents get the same workflow from the
skill [`skills/using-kb/SKILL.md`](../skills/using-kb/SKILL.md).

## Steps

| # | Who | What | Command |
|---|---|---|---|
| 1 | start hook | registers the session (harness, harness session id, folder, repository name) and tells it its name, open requests to that name an earlier session left, and the tickets of its repository it may take over | `kb hook start --harness <h>` |
| 2 | agent | reads open and running tickets of the repository | `kb board --repo <repo>` |
| 3 | agent | claims a matching ticket, or adds a new task that is claimed at once (a token session adds it as todo unless `--claim`) | `kb claim <id>` · `kb add "<goal>"` |
| 4 | stop hook | after every answer, sets the session's `last_seen`; it leaves `last_activity` of its tickets alone, which only ticket actions set (note, `kb touch`, claim, block, resume, close) | `kb hook stop --harness <h>` |
| 5 | agent | closes the ticket with the three closing lines | `kb close <id> --where … --changed … --open …` (or the lines on stdin) |
| 6 | end hook | writes a `session-ended` note on tickets the session left open | `kb hook end --harness <h>` |
| 7 | curator | reads the board and its flags | `kb board` · `kb show <id>` |

Step 1 is not required for correctness: a session without a row registers itself on its first `kb`
call. Steps 4 and 6 only record. No hook blocks; only the start, prompt and guard hooks add
context (see Hooks).

Live start hooks release idle `doing` claims after 7 days and idle `blocked` claims after
30 days by default; see [Hooks](#hooks) for settings and exemptions. A held ticket whose
session ended shows `session-ended` until a start or stop of that session follows (Claude `--resume` keeps the
session id); one without a ticket action for `KB_STALE_HOURS` shows `idle>24h`, even while its
session is alive. A session that keeps working on a ticket without notes runs `kb touch <id>`. With either flag
another session may take it over: `kb claim --take <id>` (an `event` note records it). Without a
flag, ask the holder with a request, or the curator to release it.

### Answering requests

`kb answer <id>` reads a nonempty answer from stdin, claims an unclaimed todo request
addressed to your name or `any`, writes a reply and closes it in one transaction. It also
answers requests you already hold, including blocked ones. Tasks, approval requests and
requests held by another session are refused without changing the ticket; use `kb approve`
or `kb deny` for approvals. The ordinary host terminal identity rules apply.

After the curator answers a non-approval request in the conversation, any session, including
one on a guest, may run `kb answer <id> --curator-approved`, quoting the curator's words on
stdin. It is limited to open requests addressed to `curator` that are unclaimed or held by the
caller. The reply is attributed to the caller and prefixed `curator answered (relayed by
<name>):`; it does not create a command approval. Agents record the answer themselves.

`kb abandon <id> --curator-approved --stdin` and `kb reopen <id> --curator-approved --stdin`
also let any session relay the curator's approval with a nonempty quote. The audit comment is
`curator approved: <quote>`, and the existing status rules and reopen reply boundary still apply.
Approval requests continue to use `kb approve` / `kb deny`.

### Target machines

Each ticket has a target host, independent of the holder's execution metadata. New tasks and
requests default to the calling host (the guest host when forwarded), even when unclaimed;
`kb add --target <host>` overrides it. Both add and retarget reject empty or whitespace targets.
`kb board --host <host>` filters targets case-insensitively and composes with other board filters.
Text lines carry `on:<host>`; JSON has `target` and the same flag. `kb show` prints `target`.

`kb retarget <id> <host>` is available to the opener, assignee or host curator, including through
a guest's ordinary session. It records `target: <old> -> <new>` without changing execution
host, worktree or branch. Claiming, releasing and reopening preserve the target. Schema 10
backfills from the opener's session host, including empty legacy hosts, rather than the holder.

### Explicit handoffs

`kb release <id> --to <name> --stdin` releases a claim to a session name the board has seen.
The ticket becomes unclaimed `todo`, addressed only to that name; both normal claims and
takeovers respect it. Unknown names are rejected with known names to check. The release note
preserves the source branch and worktree before the next claim replaces that metadata. Ordinary
takeovers also preserve the previous holder's branch and worktree in their audit note.

Write what is done and what remains (for example review, merge, verification or close). Only the
last releasing session or the curator may retarget an unclaimed handoff with another
`release --to`. Once claimed, only its new holder or the curator may release it. `board --to
<name>` includes these tasks as well as requests, so handoffs to `curator` remain visible.
Plain `release` returns a task to the general pool; a request keeps its addressee.

At a prompt in a repository's main checkout, the addressee gets a handoff reminder to read,
claim and check remaining work before closing. It is delivered once per release note and session
token; one session never consumes another's reminder. The nearest `.git` entry must be a
directory after resolving cwd symlinks. Linked worktrees (including symlinks into them),
non-repository directories and guests receive no such reminder. The prompt performs no git
calls. Sub-session names alone do not replace this cwd rule: sessions may be explicitly renamed.

`kb board --stranded` collects doing/blocked tickets with a non-curator holder that ended at
least one hour ago without resuming, or is idle for `KB_STALE_HOURS` (24 by default), plus
unclaimed task handoffs. It includes backlog handoffs and composes with repo/addressee/JSON
filters. The end cooldown applies to this sweep, not to immediate explicit takeover. Route work
to a live responsible session rather than its ended holder. Neither a handoff nor a merged
branch proves completion; no git-ancestry close reminder is added.

## Identity

A harness session passes no token. `kb` takes the session from the harness's own environment
variable (`CLAUDE_CODE_SESSION_ID`; `CODEX_SESSION_ID` or `CODEX_THREAD_ID`; `PI_SESSION_ID` or
`KB_SESSION=pi:<id>`, which the pi adapter exports). The session name is the repository name; in
a linked worktree it is the main checkout's name. `kb name <name>` renames the calling session.

A live hook session reserves its name. A new or resumed session gets
`<name>-sub-<4 hex>` when a live session holds that name. In the main checkout only a holder
in the same main checkout on this installation counts; reviewers and workers in linked
worktrees cannot demote it. A session moving between checkouts records its current location.
Live means its latest start, stop or end is not an end and it was seen within
`KB_STALE_HOURS` (for replayed guest events: as of occurrence time). Sessions without hook
events never reserve a name. A main-checkout sub-session recovers the base name on resume
or a CLI/prompt call once it is free there. Automatic renames carry already-seen hints forward. Replayed events cannot rename
a session after newer activity; historical stops only record liveness.
`kb name <name>` refuses a name held by another live session. Requests to a name reach
sessions with that name; sub-sessions can receive requests addressed to their own name.
Nested harnesses (a codex call from a Claude session) inherit each other's variables: if several
are set, the nearest harness process above `kb` decides (Linux `/proc`), else pass `--harness
<claude|codex|pi>`. `KB_HARNESS=<h>|none` overrides the detection.

Each installation, host or guest, has an installation id (`KB_INSTALLATION` in the config, written
by `kb setup`). The host keys a harness session by installation, harness and harness session id,
so equal harness ids on two machines are two sessions. A guest registers its sessions the same way,
through the host: the guest resolves the harness id, the session name (with the main-checkout
rule), the working directory and the branch, and sends them with each request. The host takes the
installation from the guest key's forced command, never from the request. A guest can use only
session tokens of its own installation.

`kb whoami` prints the calling session's name, label, harness, installation and repository without
registering it; `kb sessions` lists the session names seen in the last 14 days or holding a
doing or blocked ticket (`--all`: every name). `kb add --type request --to <name>` warns on
stderr when no session currently has that name, and says when no session of that name was seen
within `KB_STALE_HOURS` (it sees the request at its next prompt or start).

A script, or another machine over plain ssh, runs `kb start --as <name>` once and passes the
printed token as `--session <token>` (before or after the command, or as `KB_SESSION`) on every
call. `kb start` always starts a new token session, also inside a harness. `kb end` ends it: its
held tickets get a `session-ended` note and stay held.

The curator works on the host, from a plain terminal: a call without token and without harness,
from a terminal with a controlling tty, acts as the session `curator`; `kb start --as curator`
gives an explicit token. A harness session cannot take the name `curator`, and a harness session
holding the curator's token does not get curator rights. A guest can neither start nor use a
curator session. This guards against mistakes; it is not authentication.

## Hooks

| Hook | Claude Code | codex | pi | Config |
|---|---|---|---|---|
| start | `SessionStart` | `SessionStart` | `session_start` | see below |
| stop | `Stop` | `Stop` | `agent_end` | |
| end | `SessionEnd` | `SessionEnd` | `session_shutdown` | |
| prompt | `UserPromptSubmit` | `UserPromptSubmit` | `before_agent_start` | |
| guard | `PreToolUse`, matcher `Bash` | `PreToolUse`, matcher `Bash` | not installed | |

- Claude Code: `~/.claude/settings.json`, key `hooks`.
- codex: `~/.codex/hooks.json`. codex runs a user hook only after the curator trusts it once in the
  codex TUI; a changed command needs trust again. On Windows, codex runs the hook through
  PowerShell, so the command starts with the call operator `&`. An entry from a setup before
  this change lacks it and fails; add `& ` in front of its command by hand.
- pi: extension `~/.pi/agent/extensions/kb.ts`, written by `kb setup` from `hooks/pi/kb.ts`,
  enabled by `+extensions/kb.ts` in `~/.pi/agent/settings.json`.

`kb setup` adds the Claude Code and codex entries where no hook for that event already runs
`kb hook`.

The guard hook (`kb hook guard --harness <h>`) warns at the moment of the action: when a shell
call runs `git commit`, `git merge`, `git push`, `docker compose ... up` or `docker-compose up`
while the session holds no `doing` or `blocked` ticket and has no own answered approval
request to `curator`, it adds one line to the model's context
(PreToolUse `hookSpecificOutput.additionalContext`) naming the command and `kb add "<goal>"` /
`kb claim <id>`. An approval request counts only for its opener token, with the exact
`approval` label and a reply after its last reopen. It counts while open, or when done
with a close note after the last reopen and within the past hour (including the boundary).
Abandoned requests and later comments do not extend this window. Command approval still
uses the validity rules under [Approvals](#approvals). It never blocks or asks. It reads the command in-process first and opens the
board only for a match; a session unknown to the board, a held ticket or any error gives no
output. The first 64 KiB of the command are read as shell words and split into simple commands
at unquoted `;`, `&`, `|`, `(` and `)`; `$( )` and backtick bodies are read too, also inside a
heredoc that expands them, other heredoc bodies are skipped. So `cd x && git commit`,
`if git push; then`, `2>/dev/null git push`, `timeout 30s git push` and `x="$(git push)"`
count while `echo "git push"`, `git commit-tree`, `git merge-base` and `docker compose logs up`
do not. Not seen: commands inside `bash -c`, `eval` or an alias. pi has no guard hook.

Hooks always exit 0; only the start, prompt and guard hooks print. The start hook names the
session (`kb: this session is <name> ...`), lists open unclaimed requests to that name that an
earlier session of the name was already hinted but left without a reply (`open request #N ...
waiting <age>`, at most five; prompt hints go out once per name, so these would otherwise reach no
later session; each once per session, so a resume does not repeat it),
and lists held tickets of its repository whose holder ended or went idle, with
`kb claim --take <id>`. Start and prompt output is
`hookSpecificOutput.additionalContext` JSON for Claude Code and codex, plain text for pi or with
`--format text`. `KB=0` or `BRAIN_SESSION_CONTEXT=0` turns all hooks off for automated runs.
Each live host or guest start (not a replayed spool event) also returns idle `doing` tickets to
`todo` after `KB_RELEASE_DAYS` (default 7) days without a ticket action, and idle `blocked` tickets after
`KB_RELEASE_BLOCKED_DAYS` (default 30). Both settings come from the host config or environment
(environment wins), apply board-wide, and accept 0 to disable that status. An invalid setting
releases nothing and is logged without failing the hook. Curator-held tickets and recovery are
exempt. Session starts/stops never count as ticket activity, even for the starting session's own
tickets. Release keeps host, branch and worktree; tasks lose their addressee, requests keep it.
Touching `hook-debug.log` beside the database logs each hook call (event and payload keys, never
prompt text). At each user prompt the
prompt hook lists, once per session
name, requests addressed to that name or to `any` that nobody has claimed and that the session
did not open itself, and replies by another session to requests that a session of this name
opened, also when the request is already closed. It also reminds with `kb close <id>` about
requests a session of this name opened that are `answered` and still open, once per latest reply,
and about tickets the session holds, once per claim. A held ticket whose `last_activity` is
older than `KB_STALE_HOURS` (the board's `idle>` flag) gets, instead, a reminder to continue it,
`kb block` it or `kb release --stdin` it with a handoff note, once per idle episode (each new
`last_activity`). It also gives the automatic release date in UTC and says a note or
`kb touch <id>` keeps the claim; that addition is omitted when the matching threshold is 0.
After automatic release the previous holder gets `#N "<title>" was released after <N>d idle:
kb claim N to continue it`, once per release note and session name, only while the ticket is
still `todo` without an assignee. A session with more than three tickets in `doing` is
warned once per such count to block, release (and park with `kb label <id> backlog`) the rest.
At most five of each kind are listed; the rest follow at the next prompt (idle reminders only
while those tickets stay idle). Titles are cut to one line of 80 characters. Only requests and
replies after the schema v4 migration (or after the import of an earlier `kb.py` board) are hinted,
and only for a session the start hook has registered. Claude Code and codex add the printed text as context; the pi
extension returns it as a message. Errors go to `hook-errors.log` in the directory of `KB_DB`
(default `~/.local/share/agent-kanban/`). The end hook must finish within the harness limit
(Claude Code 1.5 s shared default, codex at most 3 s); it waits at most 1 s for a database lock.

On a guest, a hook runs under one deadline, 1 s from its start by default (`KB_HOOK_DEADLINE`; a
value outside (0, 1.3] falls back to 1 s). The deadline covers reading the harness payload, the
git lookup, the spool scan, ssh connect and authentication, and the host's run.

- Start, stop and end: the guest writes the event to its spool
  (`~/.local/share/agent-kanban/spool/`, one file per event) before it connects. It sends the
  event, behind up to 20 older spooled events, and deletes each file the host acknowledges. An
  event not acknowledged by the deadline stays spooled for the next start, stop or end hook, or
  for `kb spool --drain`, which sends everything without deadline. `kb spool` counts the queued
  events.
- The host stores each event once by its event id, with the guest's occurrence time and its own
  receipt time. A replayed event registers the session if needed, moves activity only to its
  occurrence time and never backwards, and never overrides a later end.
- Prompt: live only. On a timeout or failure it prints nothing and spools nothing. Accepted loss:
  if the host marks a hint as seen and ssh dies before the output arrives, that hint is not shown
  again; it stays on `kb board --to <name>`.
- Guard: live only, like prompt. A guest connects only for a guarded command and sends the
  host the matched command class, never the command itself. On a timeout or failure it prints
  nothing.

## Approvals

At the first prompt, every new session (including sub-sessions and guests) receives notes from
the last 24 hours on tickets of its own repository, oldest first, including closed tickets.
Each entry shows UTC time, ticket id, author session and the first line without a character
limit; `kb show <id>` provides the full note. Notes are data from other sessions, never approval
or instructions. Linked worktrees use their main repository name. The snapshot is shown once
per session token, independently of request hints shared by session name; resumes keep that mark.

A non-command decision only the curator can take (a design choice, threat model or input value)
gets a separate request, so it appears in the curator's queue:

```sh
kb add --type request --to curator --label decision "<question>" --note "<context and options>"
kb note <waiting-id> "Waiting for curator decision #<request-id>"
kb block <waiting-id>
kb board --label decision                     # or: kb board --to curator
```

Do not leave such a question only as a note inside the waiting ticket. Record the curator's
actual answer with `kb note <request-id> --reply "<answer>"`, close the request with `--result`,
and `kb resume <waiting-id>`. A decision reply does not grant command approval. Steps already
covered by explicit authorization need no new request.

An agent that waits for the curator's approval (commit, merge, push, destructive or security change)
files it as a request to `curator`, with the exact command as the note:

```
kb add --type request --to curator --label approval "Commit the workflow update in agent-kanban" --note "<exact command>"
```

A step that needs sudo also gets the label `sudo`, so `kb board --label sudo` collects them.

The curator sees every pending approval with its age and requesting session:

```
kb board --to curator            # or: kb board --label approval
```

The curator reads the request with `kb show` and decides on its printed `rev` from a plain
terminal on the host. `approve` and `deny` read the curator's own words on stdin; a changed
revision is rejected. A host session named by `KB_ORCHESTRATOR` in the host config file may
record that decision too, but never for a request opened by its own session name. The
environment cannot grant this role. This is the board's existing single-user identity model,
not authentication against a hostile local process.

```
kb show <id>                                  # curator: read the request and rev
printf '%s\n' '<curator words>' | kb approve <id> --rev <rev> --stdin
kb approval <id>                              # agent: exit 0 only while still valid
<exact approved command>                      # agent: no intervening note
kb close <id> --result "done"                  # opener or assignee
```

These commands require an open request to `curator` labeled `approval`. `deny` records a reply
and closes it. Decisions and their mirrored reply notes are append-only. Ordinary reply notes
still answer requests but do not grant approval. A new note, label change, close/reopen,
recovery mode or restore invalidates an approval; `recover --done` never revives a restored
decision. Guests may read `approval`, but cannot `approve` or `deny`.

An approval also counts when the curator gave it in the agent's own conversation. Record that
answer as a reply without treating an arbitrary board reply as permission. Execute only the
covered command. A board approval remains reusable until it is invalidated or closed: an atomic
one-use reservation for the crash window between command and close is still open.

A request with a recorded reply since its last reopen shows `answered` until it is closed;
the prompt hook reminds the opener to close it. Reopening preserves history but requires a
new reply for the flag, reply/close reminders and normal close. A normal request close requires
a current reply; its opener, its assignee, the
curator or a later session with the opener's name closes it. `kb close <id> --withdraw` drops an
unanswered request (abandoned). A rejected command names the condition that failed, for example
the missing reply note or the session that may act. One that waits longer than `KB_STALE_HOURS`
shows `waiting>24h`.

Request hints are delivered once; also sweep `kb board --to <your-name>` and `kb board --to any`
for unclaimed requests. Sub-sessions have their own names, so their hints cannot consume the
main session's addressed requests. See [Backup and restore](../README.md#backup-and-restore) for
restore locking and pre-restore snapshots, and [Limits](../README.md#limits) for the host's advisory
identity model.

The schema-9 migration backfills open tickets from their latest close, force-close, or standard
`abandoned`/`reopened`/`curator approved: ...` comment. Historical reopens with custom text
cannot always be identified; their history must be checked manually. Future reopens record the
boundary atomically regardless of their note text.

### Coordinator disposition

A host session named by `KB_COORDINATOR` (default `coordinator`) may close a completed request to `curator`, or to a named
addressee without a running session, without a reply:

```sh
kb close <id> --result "Already completed: <what>" --evidence "<checked output, path or note>"
```

Both arguments must contain text. The close note records `coordinator completion` and the evidence;
it never inserts a reply or grants approval. Evidence is supplied by the Coordinator and is not
verified by kb. The Coordinator must inspect it before closing. This adds limited rights to the
advisory session name, not authentication or general curator rights; a guest named `coordinator`
gets no such rights, even if its token is used in a host shell. A held request requires a recorded
end for its holder first. A human curator holding the request is exempt only after a reply records
its decision; an undecided curator-held request stays open. This exception never permits withdrawal.

For a named addressee other than `curator`, every matching session must have a recorded end
as its latest hook, with no start or stop in the same second. A never-registered addressee
is allowed for completion because the Coordinator supplies independent evidence. A known session
with no lifecycle record, or one that resumed, blocks completion. A full displayed session
label selects just that session; a plain name checks every session of that name, on all
installations. Empty addressees and `any` are excluded. A reply already on the ticket is
preserved; none is synthesized. Claim and reply rights of repository sub-sessions do not change.

The Coordinator may also drop an unanswered request to a recorded ended session:

```sh
kb close <id> --withdraw --result "Obsolete: <why>" --evidence "<checked proof>"
```

This records `coordinator withdrawal` and marks the request abandoned. The addressee may be a session
name or the full displayed label (`name/harness·hash`). All matching sessions, and any holder,
must have an end as their latest hook event, ordered by occurrence time then event id. A later
start or stop protects a resumed session. Any start or stop tied with the latest end's occurrence
time is ambiguous and blocks withdrawal regardless of arrival order, for host and guest hooks.
This also rejects a stop and end emitted in the same second by a finished run: the Coordinator leaves
such requests open for the curator. Rejections name the sessions and the lifecycle problem.
Missing lifecycle data (including plain token sessions), an unknown addressee, `any`, `curator`, or mere inactivity
does not prove an end. Answered requests cannot be withdrawn. These checks and the disposition
note run in one write transaction. Neither command applies to tasks or combines with `--force`.
An offline guest's undelivered resume is invisible to the host; the Coordinator must check that guest's
state and drain pending lifecycle events on the guest with `kb spool --drain` (for example,
`ssh <guest> kb spool --drain`) before withdrawing its requests. If guest access or state cannot be
checked, leave the request open. The board's recorded end alone cannot prove an offline guest
is still ended.

## Board flags

| Flag | Meaning |
|---|---|
| `idle>24h` | no ticket action (note, `kb touch`, claim, block, resume) for `KB_STALE_HOURS` (default 24); the stop hook does not count |
| `waiting>24h` | a todo ticket nobody has claimed for `KB_STALE_HOURS` |
| `auto-released` | a todo ticket released automatically and not claimed since; check its previous work before claiming |
| `from:<session>` | the session that opened a request |
| `answered` | a request has a reply since its last reopen but is not closed yet |
| `session-ended` | the owning session ended without `kb close` and did not resume; `kb claim --take` may take it |
| `worktree-gone` | the ticket's worktree no longer exists (tickets of a host session only) |
| `merged` | the ticket's branch is merged into its merge target |
| `git:unknown` | git state not checkable (a guest session's ticket, or no worktree) |

| `#<label>` | a label; `#backlog` hides only todo tasks; doing/blocked remain visible. Show parked tasks with `--all`, `--label backlog` or `--to` |

Flags are hints. Manual release belongs to the owner or curator
(`kb release <id> --why "<handoff>"`); another session takes over a held `session-ended` or `idle>`
ticket with `kb claim --take <id>`. After automatic release, plain `kb claim <id>` suffices
(requests still obey their addressee). It prints the previous holder's label, last-seen age and
branch/worktree from the release note so the claimer can check for ongoing work.

## Autopilot

`tools/autopilot` runs one pass through the CLI on the Linux host, suitable for an hourly
scheduler. Preview it with `tools/autopilot --dry-run`; run `tools/autopilot` to write routing
requests. No timer is installed or enabled by setup. `KB_BIN` selects the CLI; `--state`
selects a private state file (default `$XDG_STATE_HOME/agent-kanban/autopilot.json`, or
`~/.local/state/agent-kanban/autopilot.json`). A file lock prevents overlapping passes.
`--name` chooses its session name; keep the same name with the same state file.

The pass reads `kb board --json`, `kb board --stranded --json` and `kb sessions --all --json`.
Waiting unclaimed tickets stall after `KB_STALE_HOURS` without ticket activity. Held tickets
stall after that interval or once `--stranded` lists them, including its one-hour end grace.
Tasks route to their repository and requests to their addressee. Unknown or dormant owners,
`any`, approvals and `decision`/`needs-curator` items go into a curator collecting request.
Each active owner gets one open collecting request; later items become notes there. Requests
with stranded holders take no further items and are themselves reported once. Backlog tasks
hidden by the normal board view stay parked.

The autopilot reports each source ticket once per `last_activity`, retains successful reports
if a later write fails, and prunes closed source tickets from its state. It reads only board
metadata and never forwards note text, claims, answers, closes or modifies the source tickets.
A dry run changes neither board nor state file (it may create the state directory and lock).

Automatic idle-claim release remains the start hook's independent job: after its configured
7/30-day thresholds it releases stale claims; the autopilot only routes reminders at the
shorter stale-hours threshold. Automatic release preserves `last_activity`, so it does not duplicate an already
reported stall; a real ticket action starts a new activity episode. Running the autopilot does not fire a start hook or refresh source activity.
Leave the timer disabled until a dry-run on the target board has been checked for useful
routing and duplicate reminders alongside idle release.

`kb sessions --json` uses the same rows as the text view and returns name, sorted harnesses,
session count, last-seen time, doing/blocked counts, sorted installations and clients.
`--all` includes dormant names. The existing board JSON contract (`from`, `to`, display labels
in `assignee`, and `flags`) is preserved; `opener_name` adds the raw name so consumers
can identify their own requests without parsing display labels.

## Compliance report

`kb report [--since YYYY-MM-DD] [--days N]` prints board-use measurements for a
window in UTC, by default the last 14 days. It reads the database read-only in one snapshot and
needs no identity. Opening the board read-only can leave empty `board.sqlite-wal` and
`board.sqlite-shm` files next to it; they hold no data, and SQLite deletes them when the last
read-write connection closes.

- Denominator: harness sessions with a start event in the window, per harness and in total.
- Short runs are counted apart and left out of the denominator: the session's last event is an
  end, its first start to last end is under 5 minutes, and it has no board use; this is how
  reviewer child runs (`claude -p`, `codex exec`, `pi -p`) look. A resumed session is never short.
- Board use: a ticket or note write by the session in the window (add, claim, note, close, ...);
  the end hook's `session-ended` note does not count. `kb board` and `kb touch` leave no record
  and do not count. Each session gets one class: `left open` (it held a `doing` or `blocked`
  ticket at its last end), else `closed` (it closed a ticket it held, or a request it opened),
  else `other use`; or `no board use`.
- Sessions that used kb without a start event in the window (`kb start` sessions without a
  harness id, auto-registered sessions, sessions started earlier) are listed separately.
- Tickets: added, claimed, closed, abandoned, still doing at the window end; tickets with a
  `session-ended` note and no later close; median and maximum `claimed_at` to close.
- Duplicate candidates: ticket pairs in one repository whose held periods overlap and whose titles
  reach a title sequence similarity of 0.75 (the sequence threshold used by `kb add`).
- History imported from the bus is left out (see "Imported bus history").
- Known limits: a release and a later reclaim count as one held period; `abandoned` and claim to
  close use the current ticket state, so a later reopen changes past reports.

## Imported bus history

The former file-based message bus was imported into the board on 2026-09-24 and then deleted
(for compatibility with earlier message logs). Each bus thread is a `request` ticket labelled `bus-import`; each bus file is one
note whose text is `bus-id: <id>` followed by the whole file. Senders are sessions of harness `bus`
without a harness id. The table `bus_import` maps every bus id to its ticket and note. The one-shot
import script is in the git history (commit 7e77d68).

`kb report` leaves the imported history out: tickets listed in `bus_import` do not count as
added, their imported notes count nowhere, and sessions of harness `bus` are not listed. Later
live work on an imported ticket (claim, reply, close) counts as usual. The report's second line
gives the excluded counts.

## Known limits

- Reviewer runs (`claude -p`, `codex exec`, `pi -p`) fire the hooks too and appear in the session log.
- Whether a pi `reload` or a Claude `--resume` keeps the session id is not verified; if it does, the
  end hook of the old process flags a ticket whose session continues.
- codex hooks run only after the curator has trusted them.
- A guest's prompt hint lost to a dropped ssh connection is not shown again (see Hooks).
