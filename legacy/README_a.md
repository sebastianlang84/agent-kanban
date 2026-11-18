# Earlier board compatibility fixture

`kb_a.py` is the unversioned board CLI retained for migration tests. It is not the current CLI
and should not be installed for new boards. The old hook adapter and duplicate historical test
suite have been removed; `tests/test_legacy.py` exercises the import contract with this fixture.

The current `kb` detects the earlier `sessions`, `tickets`, `notes`, and `shown` tables on first
open and migrates them in place. The tests create an old-format board with `kb_a.py`, open it with
the current CLI, and check identities, tickets, notes, hook history, sequence counters, rollback,
and idempotence. Keep this fixture while that import path is supported.

Back up an old database before opening it with the current CLI. To inspect a backup with the old
CLI, set `KB_DB` to a separate copy; never run both implementations against the live database.
