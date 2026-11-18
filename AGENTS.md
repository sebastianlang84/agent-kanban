# Contributor guidance

Read [README.md](README.md), [docs/workflow.md](docs/workflow.md), and the relevant
[architecture decisions](docs/adr/) before changing behavior.

- Keep the CLI compatible with Python 3.9 and the standard library.
- Preserve guarded SQLite transitions, migration compatibility, and host/guest boundaries.
- Use a temporary `KB_DB` outside the checkout for manual checks. An installed `kb` may point
  into a live checkout; use a separate worktree when developing against an installed board.
- Run the tests described in [Development](README.md#development).
- Update the workflow and the shipped `using-kb` skill when public behavior changes.
