# Measuring automatic release of idle claims

This is the follow-up protocol for [ADR 0005](adr/0005-automatic-release-of-idle-claims.md).
Evaluate after at least 14 days of operation. Record the observation window and effective
release settings; a preparation snapshot alone cannot establish whether the rule works.

## Collection

Run on the board host, opening the configured database with SQLite `mode=ro`.
Use one read transaction for a consistent snapshot; do not run start hooks to
prepare the data. Keep database copies and event-level extracts outside Git and
publish only aggregates and relevant ticket references.

Record the UTC observation time, deployed Git revision, effective `KB_RELEASE_DAYS`
and `KB_RELEASE_BLOCKED_DAYS` settings (environment overrides host config, defaults
7 and 30; zero disables), and any known configuration changes during the interval.
If settings changed, split the observations by configuration or mark attribution
unknown. Use `last_activity`, not session `last_seen`, for ticket inactivity.

At evaluation time T:

1. Count current `doing` tickets with `last_activity < T - 24 hours`. Report the
   curator-held subset separately, and also count those older than the effective
   doing-release threshold when enabled. Compare with earlier snapshots only when
   their observation times and configuration are known.
2. Select `note` rows with `kind = 'release'` and text starting
   `released automatically:` in `[deployment time, T)`. The note's `author`
   is the previous holder's session token. Count release events and distinct
   `ticket_id` values separately; a ticket may be released repeatedly.
3. For each release, inspect all subsequent claim notes for the same ticket
   (`kind = 'comment'`, `text = 'claimed'`), ordered by note `id`, up to the
   next release note. A reclaim qualifies when a claim's `author` equals the
   release's `author` and its elapsed time is from 0 through 24 hours. Another
   holder's intervening claim does not exclude a later qualifying return by the
   previous holder. Compare session tokens, not display names.
   For the ADR's ticket-based rate, the denominator is distinct tickets with at
   least one release at least 24 hours old at T. The numerator is those tickets
   with a qualifying reclaim after any such mature release. Count each ticket
   once in each set; keep release-event counts as a separate diagnostic. Exclude
   younger releases from both sets and report their count separately. With no
   eligible tickets the rate is unavailable, not zero. A rate **above 20%** means
   the threshold is too short under ADR 0005; do not silently tune it from immature data.
4. Report the current status and backlog label of each distinct released ticket
   as mutually exclusive buckets: closed (`done`), abandoned, parked (`backlog`
   on an open ticket), held (`doing`/`blocked`), or `todo`. For `todo`, separate
   tickets with no later ticket-action note from those subsequently touched or
   released again. Session-hook notes do not count as ticket actions. This is a
   snapshot of outcomes, separate from the mature-ticket reclaim-rate denominator.
5. Inspect subsequent notes on released tickets and linked requests for documented
   double work. Record the ticket references and evidence, or say no documented
   case was found; absence of notes does not prove absence of double work.
   **One documented case reverses the decision**, as specified by ADR 0005.

Publish the counts, reclaim fraction, outcome buckets, evidence limitations, and
whether either ADR decision trigger fired in a board note. Keep the raw event
extract private. If no mature releases exist or history/configuration is incomplete,
state what remains unmeasured and retain a dated follow-up rather than declaring
the rule successful.
