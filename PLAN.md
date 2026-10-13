# agent-kanban — plan

Status: live since 2026-09-24 (P1, P2 and P3 done; design review outcome in `docs/adr/0003-plan-revised-after-design-review.md`).
Owner: the curator (Sebastian). Done: the bus cutover (D7); D9 guest mode, hooks with deadline and
spool, `kb setup` and its docs, published on Bitbucket (2026-09-28); P5 checks passed (2026-09-30); P3 measured and
reported (2026-10-12, kb ticket #77). Open: release versioning (kb ticket #191).

## 1. Problem

Many agent sessions run at once: on 2026-09-23 the Claude desktop sidebar showed 17 sessions across
10 groups, on svrllm (Debian VM) and the Windows workstation, in three harnesses (Claude Code, codex,
pi). The curator loses the overview: what is being worked on, by whom, since when, and what state a
session left behind when it ended. Duplicate work is rare but real (see 2.3).

Goals, in the curator's words and priority:

- **G1 Overview** — one high-level view of open work, work in progress, who, since when, across all
  repositories and both machines.
- **G2 Status after session end** — what a finished session delivered and what it left open.
- **G3 Agent communication attached to the work** — who asked whom for what, answers, closure; ideally
  replacing `~/agent-bus`.
- **G4 No duplicate work, no collisions** — a session checks before it starts and claims atomically.
- **G5 Harness- and cwd-independent** — works from any harness, any directory, both machines.
- **G6 As lightweight as possible** — explicit curator constraint.

Non-goals: a backlog replacing each repository's `todo.md` or Jira; an orchestrator that starts
agents; enforcement that blocks work.

## 2. Evidence

### 2.1 What exists (as of 2026-09-23)

| Thing | What it is | Gap |
|---|---|---|
| `~/agent-bus/` (no git; deleted 2026-09-24, see D7) | file-per-message drop, protocol v1.1, `inbox.py`; sessions `euaiact`, `llmstack`, `partflow` | messages only, no state; no notification |
| nightshift | own ledger for its overnight runs | only its own runs |
| repo `todo.md` / Jira | backlogs | not "who is doing what now" |
| git worktrees | one per session in most repos | show branches, not intent or outcome |

### 2.2 Session drift (measured 2026-09-23)

- 6 sessions are titled `main`; the title says nothing about the work.
- At least 3 sessions whose worktree no longer exists are still open (work likely finished).
- One branch (`nightshift: claude/happy-lovelace-3c7f83`) is merged but its worktree remains.
- Two partflow worktrees (`e2e-gate-flaky-safety`, `playwright-suite`) have no visible session.

### 2.3 agent-bus content (measured)

13 messages in two days (count taken 2026-09-23 18:22 local). 9 concern the bus itself (2 handshakes,
7 protocol v1/v1.1 including the close). Two content threads:

- Dashboard (3 messages): duplicate work. `euaiact` requested a dashboard feature that `llmstack` had
  already built the same day on the curator's direct order. A visible "done" ticket would have prevented
  the request.
- Invisible mark vs. part search and BOM import (1 message, `euaiact` to `partflow`, `needs-curator:
  yes`): an open cross-session request, still unanswered.

### 2.4 Harness capabilities (measured on svrllm)

- Session hooks exist in all three harnesses but in three formats: Claude Code `SessionStart` /
  `SessionEnd`, codex 0.155.1 feature `hooks` (stable), pi extension events `session_start` /
  `session_shutdown`. Hooks are therefore per-harness adapters, not a shared mechanism.
- MCP is configured in all three (pi via `~/.pi/agent/mcp.json`).
- Every harness can run a shell command; Windows sessions reach svrllm with `ssh SVRLLM`.
- codex and pi read a repository's `AGENTS.md`; Claude Code reads only `CLAUDE.md`, so each repository
  links `CLAUDE.md` to `AGENTS.md` (checked by `~/.agents/check-claude-symlinks.sh`).
- Global instruction files: Claude Code `~/.claude/CLAUDE.md` and pi `~/.pi/agent/AGENTS.md` are
  symlinks to `~/.agents/AGENTS.md`. codex reads `$CODEX_HOME/AGENTS.override.md`, else
  `$CODEX_HOME/AGENTS.md` (default `~/.codex`; codex source `codex-home/src/instructions/mod.rs`); on
  svrllm neither exists and `CODEX_HOME` is unset, so codex sessions get no global rules. Windows not
  checked.

### 2.5 Prior art

Maintenance data from the GitHub API, 2026-09-23.

| Tool | Fit | Maintenance |
|---|---|---|
| beads (`bd`, gastownhall/beads) | tested, see 2.6 | 27.4k stars, 16 authors in last 30 days, releases ~weekly (v1.3.0 2026-09-15), MIT, since 2025-10 |
| beads_rust (`br`) | per-repo SQLite, atomic claim, no expiry | 1.1k stars, **one** human contributor, custom licence ("MIT with rider") |
| odonian | lease + heartbeat semantics fit exactly; REST server, container fleet, merge gate | 0 stars, AGPL |
| mcp_agent_mail | messages + advisory file leases, MCP server | not assessed in depth |
| NEEDLE, gnap, foremerge, wit | orchestrators or code-level locks | wrong problem |

### 2.6 beads v1.3.0 test (measured, container `python:3.11`, no network, `BD_DISABLE_METRICS=1`)

| Goal | Result |
|---|---|
| G1 | `bd list --status in_progress`, `bd ready`, label filters; ~300 ms per command |
| G2 | `bd close <id> --reason`; only the assignee may close without `--force` |
| G3 | assignee = session name, comments carry author and time, label `needs-curator` filterable |
| G4 | 10 concurrent `--claim` on one issue: exactly 1 winner, 9 clean "already claimed". 20 concurrent comments: 20 stored, no lock error (embedded mode serialises via `flock`) |
| G5 | `BEADS_DIR` works from any cwd; identity must be passed (`--actor`), else the git user name is recorded |
| G6 | binary 154 MB, DB 2.1 MB after init, `init` 4.2 s, `bd prime` 5.7 KB of context |

Blockers and costs:

- **Does not run on svrllm natively**: Debian 11 has glibc 2.31, the release binary needs ≥ 2.34. Only
  a glibc build is published. Options: build from source (untested), run via `docker run`, or upgrade
  the host to Debian 12.
- `bd init` writes `AGENTS.md`, `CLAUDE.md`, `.claude/settings.json`, `.codex/config.toml`,
  `.codex/hooks.json`, `.cursor/hooks.json` and a git repo. Harmless in a dedicated directory,
  harmful in a real repository (conflicts with the rule that `AGENTS.md` has one owner).
- Telemetry exists; whether it is off by default is **not verified**. `BD_DISABLE_METRICS=1` must be
  set globally.
- Fast schema churn: v1.3.0 alone applies 13 migrations (v53→v66). Upgrades need attention.
- Large surface (gates, molecules, federation, `bd remember` memory) that overlaps with the brains.

## 3. Decisions

### D1 Engine — beads or own `kb`

| Criterion | A: beads | B: own `kb` (Python stdlib + sqlite3) |
|---|---|---|
| Fit G1–G5 | all met in test | to be built; bus semantics (to `any`, close by opener, needs-curator) map 1:1 |
| G6 footprint | 154 MB binary, Dolt, build or container needed on svrllm | one file, python3 and sqlite3 already installed |
| Correctness risk | proven concurrency | own code; SQLite `BEGIN IMMEDIATE` + `busy_timeout` is standard but untested here |
| Maintenance | upstream, but schema churn and a large surface | ours; estimated a few hundred lines (not measured) |
| Invasiveness | init writes harness files; telemetry to disable | none |
| Exit cost | export via JSONL | trivial schema |

**Decided 2026-09-23 by the curator: B**, without the P0 spike. Reason given in the recommendation: G6 is an explicit constraint and A cannot run on the host without a
build toolchain, a container wrapper or an OS upgrade. Take A instead if any of these holds:
dependency graphs between tasks become necessary; more than one curator uses the board; building
`bd` from source on Debian 11 (P0 spike) succeeds within the time box and a pinned version is
acceptable; or the P0 measurement shows the `docker run` wrapper costs less than building and
maintaining B.

### D2 Interface

One core module; two thin front ends.

1. CLI `kb` first — the only interface every harness has. Windows: `ssh SVRLLM kb …`.
2. MCP over stdio through ssh later (`ssh SVRLLM python3 ~/agent-kanban/kb_mcp.py`): no daemon, no
   port, ssh does authentication. `kb` itself offers no HTTP route; the dashboard reads an export
   (D8). A client without ssh reopens D8.

Note text is read from stdin from the first CLI version (`kb note … < file` or a pipe), so quoting on
the Windows-to-ssh path cannot corrupt a note.

### D3 Data model and lifecycle (option B)

- `ticket`: id, type (`task` | `request`), title, repo (empty for cross-repo requests), status (`todo` |
  `doing` | `blocked` | `done` | `abandoned`), assignee (session, see D4), opener, labels
  (`needs-curator`, topic), created, updated, claimed_at, last_activity, and execution metadata: host,
  worktree path, branch, merge target.
- `note`: ticket id, author, time, kind (`comment` | `request` | `reply` | `close` | `release` |
  `force-close` | `session-ended`), text. Append-only.
- Every transition and its note are written in one `BEGIN IMMEDIATE` transaction. A transition succeeds
  only if its guarded `UPDATE` reports `cursor.rowcount == 1`; `UPDATE … RETURNING` is not available
  (SQLite 3.34.1 on svrllm, **measured**; `RETURNING` needs 3.35). Connections use
  `isolation_level=None`, WAL and an explicit `busy_timeout`.

| Transition | Allowed actor | Rule |
|---|---|---|
| `kb add` task | any session | create-and-claim: the ticket is created as `doing`, owned by the opener |
| `kb add` request | any session | created as `todo`, addressed to a session name or `any` |
| claim `todo` → `doing` | addressee or, for `any`, any session | `WHERE status='todo' AND assignee IS NULL` |
| `doing` ↔ `blocked` | assignee | assignee is kept |
| `doing`/`blocked` → `done` | assignee | close note carries the three closing lines of the global `AGENTS.md` |
| request answered → `done` | opener or assignee | needs a `reply` note; the opener can reopen. Revised 2026-09-24 by the curator (kb card #16): the opener alone was the bus rule, but the opener is usually an ended session, so answered requests stayed `doing` |
| `todo` → `abandoned` | opener or curator | |
| release `doing`/`blocked` → `todo` | assignee, or curator | `release` note; a session never releases another session's claim |
| any other close of an owned ticket | curator only | `--force`, `force-close` note |
| reopen `done`/`abandoned` → `todo` | opener or curator | |

- No automatic claim expiry, and no release by other sessions: auto-expiry and release by "the next
  session" both hand a ticket to a second worker while the first may still be working. `kb board` flags
  a claim as stale; only the curator or the owner releases it.
- Stale signals, all advisory: age of `last_activity` (any note or `kb touch`); a `session-ended` note
  (D6); git state (D5).
- Durability: daily backup of the database file (mechanism settled in P1). After a restore, `kb`
  refuses claims until the curator has reconciled every `doing` ticket.

### D4 Identity

Revised 2026-09-24: the agent no longer handles a token on svrllm.

Each harness exports its own session id to the agent's shell: `CLAUDE_CODE_SESSION_ID`,
`CODEX_SESSION_ID`, `PI_SESSION_ID`. `kb` resolves the session in this order: `--session <token>`,
the harness id, then `KB_SESSION` (a nested run may inherit it). A harness id without a session row registers itself on the first
`kb` call, so correctness does not depend on the start hook. The session name is the repository
name, taken from the main checkout for a linked worktree; `kb start --as <name>` renames it. When
several harness ids are set (nested harnesses), `--harness <h>` picks one; otherwise `kb` fails
instead of guessing.

Ownership compares the session's token only. Name, host and worktree are display and routing
metadata; the board shows a session as `<name>/<harness>·<4 hex of the token hash>`.

Windows sessions reach svrllm over ssh without a harness id. They run `kb start --as <name>` once and
pass `--session <token>` to every call.

### D5 Overview and staleness (G1)

`kb board` prints all tickets grouped by status and repo; `--repo` and `--mine` are filters. It joins git
state only for tickets whose host is svrllm and whose worktree path exists there; every other ticket shows
git state as "unknown", never as stale. A merged branch or a missing worktree is a hint, never proof
of completion. Agents maintain intent and outcome; git supplies hints. A dashboard view is a later
add-on in `~/llmstack/dashboard`.

### D6 Rules and hooks (G4, G5)

- One trigger line in the global instruction file (`~/.agents/AGENTS.md`, read by all three
  harnesses on svrllm): before starting, run `kb board --repo <repo>` and load the `using-kb` skill.
  The skill ships in the repository (`skills/using-kb/SKILL.md`) and holds the workflow: claim or
  add, requests, approvals, close with the three lines. The instruction file keeps only what is
  specific to the installation. Revised 2026-09-24 by the curator (kb ticket #65,
  `docs/adr/0002-ship-agent-skill-with-kb.md`): the rule was in the instruction file alone, so
  other installations got no agent guidance.
- `kb add` lists open and running tickets in the same repo with similar titles and requires `--new` to
  proceed. Semantic duplicates still need judgment; the rule cannot guarantee G4, it makes the check
  cheap.
- Three recording hooks per harness call `kb hook <start|stop|end> --harness <h>`. They print nothing,
  never block and always exit 0; errors go to `hook-errors.log` next to the database.

  Hook names per harness: `docs/workflow.md`.
- Hooks that guard stayed out until the P3 measurement. Decided by the curator on 2026-10-13 after a
  sample of the sessions without board use (4 of 8 readable ones committed, merged, pushed or
  redeployed without a ticket): one hook warns, never blocks, when a session runs `git commit`,
  `git merge`, `git push` or `docker compose up` without a held task (kb ticket #285). One context hook is in, pulled
  forward by the curator on 2026-09-24 (kb ticket #48, poke stage 1): `kb hook prompt` shows a
  session, at its next prompt, new requests addressed to its name and new replies to requests a
  session of its name opened, each once per name (table `hint_seen`, events after the v4
  migration only). Since kb ticket #218 it also reminds with `kb close <id>` about answered,
  still open requests a session of its name opened (once per latest reply) and about tickets the
  session holds (once per claim; since kb ticket #299 again once per idle episode, offering
  continue, block or release, ownership unchanged). It reaches codex and pi sessions too, which
  get no other notification. Stage 2, an active poke of idle Claude sessions, was dropped by the
  curator on 2026-10-13 after P3: `using-kb` already has the sender notify a Claude desktop addressee directly.
- The workflow is documented in `docs/workflow.md`.

### D7 agent-bus (done 2026-09-24)

Decided: migrate the bus into the board. A bus thread became a `request` ticket, each message a note,
keyed by its bus file name so a re-run imported nothing twice. Before cutover the message counts were
reconciled and the curator signed off every open request; `euaiact`, `llmstack` and `partflow`
agreed, and the curator decided.

Done 2026-09-24: 37 messages imported as 8 tickets, reconciled; every session notified. The plan
kept `~/agent-bus` as a read-only archive; the curator revised this the same day and it was deleted,
so no second path to the same content remains. A tarball sits with the board backups; the imported
history on the board is described in `docs/workflow.md`, "Imported bus history". The import script
left the repository with it (git history, commit 7e77d68).

### D8 Confidentiality and storage

Code and ticket data are stored apart. The code in `~/agent-kanban` may get a Bitbucket remote. The
ticket database lives on svrllm outside any git working tree and is never committed or pushed. Its
off-host copy is IT's nightly VM backup (`~/llmstack/docs/adr/20260805-project-wiki-markdown-git-substrate.md`,
decision 17) plus the D3 backup. Decided by the curator 2026-09-23.

Revised by the curator 2026-09-24: the llmstack dashboard shows the board read-only in its tab
"SVRLLM Kanban" on dashboard.pidso.com, readable without login from the Traefik IP allowlist. An
export job in llmstack (`scripts/kb_board_export.py`) copies per ticket: id, type, title, repo,
status, labels, timestamps, assignee, opener and addressee session names. Notes, tokens, session
events, host, worktree, branch and working directory stay on svrllm. A dashboard form feeds new
requests in through an importer that calls `kb add` (label `dashboard`). Consequence: ticket titles
are visible to everyone inside the allowlist, so a title never carries confidential content (for
example ESA-402 material); details go into notes.

### D9 Any two systems: one host, any number of guests (proposed 2026-09-24, reviewed; resolved 2026-09-28)

Goal from the curator: `kb`, including request messaging, is published on Bitbucket so colleagues use
it on their own machines, and it connects any two systems. Chosen form: one system hosts the board,
the others are guests that reach it over ssh. Prerequisite, stated as part of the goal: every guest
has a non-interactive ssh login to its host. Offline work and clients without ssh are out of scope
until a real need shows up (federation, or an HTTPS/MCP front end that reopens D8).

- **Scope of trust.** One host serves one person's machines. An ssh login to the host is full board
  access, and a guest can claim any identity it passes, so a host is never shared between people
  until per-person authentication exists (bind identity to the ssh principal, e.g. a forced command
  per key); that is a separate decision and triggers D1's "more than one curator" condition.
  Colleagues each run their own host.
- **Roles.** An installation is a host (holds `board.sqlite`) or a guest, set by `KB_HOST=<ssh
  destination>` (environment, or `~/.config/agent-kanban/config`). Without it `kb` works locally as
  today. Each installation has a stable installation id generated at install time; host names are
  display only.
- **Forwarding.** On a guest, `kb <args>` runs `ssh -o BatchMode=yes <KB_HOST> kb <args>`: arguments
  quoted for the remote shell, stdin passed through, exit code and stderr returned unchanged.
- **Identity.** The guest resolves everything that depends on its own machine and passes it as
  flags: harness, harness session id, installation id, working directory and repository name (the
  D4 main-checkout rule runs on the guest). The host keys sessions by harness, installation id and
  harness session id. This replaces the live unique index on `(harness, harness_session)`: a schema
  migration adds the installation id (existing rows get the host's own id) and is tested with
  colliding harness ids from two installations.
- **Curator.** Curator actions are allowed only when `kb` runs on the host itself, from a plain
  terminal without a harness id, never through a guest. (Honest limit: anyone with a shell on the
  host can do the same; that matches the single-person scope above.)
- **Hooks on a guest.** Each hook runs under one hard deadline below the harness budget (Claude Code
  SessionEnd 1.5 s default, codex at most 3 s) covering connect, authentication, remote run and
  database wait. On deadline expiry or any failure the event goes to a local spool.
- **Spool.** Every hook event carries a durable event id (installation id plus a random part) and
  its guest occurrence time; the host inserts events idempotently by event id and stores both the
  occurrence time and its own receipt time. Ordering and reports use occurrence time within one
  installation; stale detection uses receipt time. Spool writes and drains take a file lock; replay
  is in occurrence order and removes an entry only after the host acknowledges it. Live calls do
  not wait for the spool; a later start or stop from replay never overrides a newer live end.
- **Unreachable host.** Ordinary commands fail visibly (exit 3, one line), never silently.
- **Data (D8).** Ticket data stays on the host. The published repository holds only code, tests and
  docs.
- **Install.** An install script sets up `kb` on the path, the three harness hooks with local paths,
  the installation id, and the Board rule in the user's global instruction files, and asks host or
  guest. Hard-coded `/home/llmadmin` paths go away.
- **Windows guests.** A guest needs Python 3.9+ and OpenSSH. Windows guest mode is part of P5's
  exit criterion, not an afterthought.

Resolutions 2026-09-28, approved by the curator with the implementation plan and implemented. Where
they differ from the bullets above, they replace them.

- **Access model.** Instead of a full ssh login: each guest has
  its own ssh key, and the host's `authorized_keys` line for it forces `kb serve --installation
  <id>` with `restrict`. The key runs nothing else. The host takes the installation id from that
  line, never from the guest. `kb serve` allows only the non-curator commands and never grants
  curator. `kb authorize` (curator, on the host) writes the line. Arguments travel as one JSON
  request line on the ssh stdin, not in the ssh command line, so no remote shell quotes them.
- **Curator.** Also closed: a harness session on the host holding the curator's token no longer
  acts as curator.
- **Time rule.** As written above, plus one addition: a replayed event moves `last_seen` and
  `last_activity` only to its occurrence time, never backwards, so a session that was offline does
  not look active on replay. A live event moves them to the receipt time.
- **Spool.** One file per event, written atomically, without a file lock (`fcntl` does not exist
  on Windows). Concurrent drains may send an event twice; the host dedupes by event id.
- **Prompt hook.** Live only, never spooled. Accepted loss: a hint the host marked as seen before
  ssh died is not shown again; it stays on `kb board --to <name>`.
- **Install.** `kb setup --host | --guest <ssh-dest>` (`install.sh` runs it; Windows `py -3
  <repo>\kb setup`). It merges the kb hooks into the existing Claude Code and codex hook files,
  keeping every other hook and key, instead of replacing them. ssh reachability, codex hook trust
  and the curator's `kb authorize` stay manual.

## 4. Phases

| Phase | Content | Exit criterion |
|---|---|---|
| P0 | dropped 2026-09-23: D1 decided for B without the spike | — |
| P1 | core + CLI + tests: claim race, concurrent notes, transition table, stale join, backup and restore | tests green; the race test starts 10 independent CLI processes behind a start barrier, exactly one reports success, its token is the stored owner when read through a fresh connection, all others report rejection; the same test turns red against a deliberately broken read-then-write claim |
| P2 | rule in every harness's global instruction file on both machines; session-start log and session-end note hooks; bus migration proposal as a `bus` message | rule visible in a fresh session per harness and machine; three bus sessions agreed; curator decided |
| P3 | two-week measurement: sessions that claimed before working and closed (or got `session-ended`) / sessions in the start log; duplicate tickets (similar titles, same repo, overlapping time); measured by `kb report` (`docs/workflow.md`) | numbers reported to curator |
| P5 | D9 step 1 (implemented 2026-09-28): guest mode (forwarding, identity context, installation id and migration, hook deadline and spool); step 2: install script and README (implemented 2026-09-28), published on Bitbucket 2026-09-28 | a Linux and a Windows guest each claim, note and close a ticket on the host; tests cover colliding harness ids across installations, hook deadline with an unreachable or slow host, spool replay without duplicates and in order, concurrent spool writers, and refusal of curator actions through a guest; install on a clean account passes on both. Done: Linux guest over a real sshd with the forced command (isolated test instance on svrllm, 2026-09-28). Windows guest on PIDSOBOOK23, all steps passed, findings fixed in #138 (2026-09-29). Clean-account install passed as a simulation: fresh clone, empty home directories, isolated sshd; a real new account's shell profile and PATH are not covered (2026-09-30, #132). P5 done |
| P4 (optional) | MCP over ssh; dashboard view; context-injecting session-start hook per harness | only if P3 shows a need |

## 5. Risks

- Agents ignore the rule → compliance measured in P3 against the start log; only then add
  context-injecting hooks.
- Board becomes a second backlog → non-goal; tickets are session goals, not a planning tool.
- Ticket data reaches git or an external route → D8: database outside any working tree, no route.
- Own code has a concurrency bug → P1 race test with barrier and a mutation check.
- A session loses or never passes its token → mutating calls fail visibly; the curator reassigns.
- svrllm or ssh is unreachable → the session cannot claim; it states this in its reply and carries on
  without a ticket. Behaviour beyond that is undefined.
- Database lost or restored from an older backup → D3 durability; claims blocked until reconciled.
- Scope creep toward beads-like features → the D1 switch conditions.

## 6. Open questions for the reviewers

1. Is B really lighter once maintenance is counted, or is A with a pinned source build cheaper?
2. Is "no automatic expiry" right, or will stale claims pile up?
3. Is the AGENTS.md rule without hooks enough to reach useful compliance?
4. Should the bus really be absorbed, or stay separate for cross-session requests?
5. What is missing from G1–G6 or from the evidence?

Answered by two reviews on 2026-09-23; findings and verdicts in
`docs/adr/0003-plan-revised-after-design-review.md`.

## 7. Next step

Release versioning, kb ticket #191: step 1 a release number, an annotated tag and `kb --version`;
step 2 a version check in `kb serve` and guests installing a tag.
