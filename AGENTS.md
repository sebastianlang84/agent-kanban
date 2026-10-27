# agent-kanban

The session in this checkout has two jobs:

1. **Board orchestrator.** At the start and whenever asked, read `kb board` and route every open
   ticket owned by another repository to that repository's session, following "Orchestrating the
   board" in the `using-kb` skill. Do not claim, answer or edit another repository's tickets or
   code. Tickets that only Sebastian can do (logins, approvals to the curator, decisions on his
   own files) stay where they are; name them to him in one line.
   - File one routing request per owning repository that lists its ticket ids, not one per
     ticket; name `doing` tickets flagged `session-ended` as takeovers (`kb claim --take`).
   - A request addressed to `any` goes to the repository its title names.
   - Ticket repo to working directory: `<repo>` is `~/dev/<repo>`; `.agents` and `skills` are
     `~/.agents`.
   - Notify a running session of the owner (`list_sessions` by working directory, then
     `SendMessage`); where none runs, start one in that directory with the request as its brief.
2. **kb development.** Tickets with repo `agent-kanban` are ordinary work in this repository
   (see [README.md](README.md), [PLAN.md](PLAN.md), `docs/adr/`).
