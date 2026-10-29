# agent-kanban

1. `~/chief` orchestrates the board; do not distribute other repositories' tickets here.
2. Work on `agent-kanban` tickets here.
   See [README.md](README.md), [PLAN.md](PLAN.md), and `docs/adr/`.
3. The installed `kb` points into the main checkout.
   Develop in a separate worktree and use a temporary `KB_DB` for testing.
   Follow [Development](README.md#development) for integration.
