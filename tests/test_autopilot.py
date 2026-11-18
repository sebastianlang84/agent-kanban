"""Autopilot integration tests: real kb commands against temporary boards only."""
import contextlib
import fcntl
import importlib.machinery
import importlib.util
import io
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_kb import KB, REPO, KbCase
from test_guest import GuestCase

AUTOPILOT = os.path.join(REPO, 'tools', 'autopilot')


class AutopilotCase(KbCase):
    def setUp(self):
        super().setUp()
        self.env['XDG_STATE_HOME'] = os.path.join(self.home, '.local', 'state')
        self.env['KB_BIN'] = KB
        self.state = Path(self.env['XDG_STATE_HOME']) / 'agent-kanban' / 'autopilot.json'

    def age_board(self, minutes):
        """Move every recorded time on the board minutes into the past."""
        shift = "strftime('%Y-%m-%dT%H:%M:%SZ', {0}, '-{1} minutes')"
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                conn.execute('DROP TRIGGER note_no_update')
                conn.execute('DROP TRIGGER session_event_no_update')
                for table, cols in (('note', ('time',)), ('session_event', ('time', 'received')),
                                    ('session', ('started', 'last_seen')),
                                    ('ticket', ('created', 'updated', 'claimed_at',
                                                'last_activity'))):
                    conn.execute('UPDATE %s SET %s' % (table, ', '.join(
                        '%s=%s' % (c, shift.format(c, minutes)) for c in cols)))
                conn.execute("CREATE TRIGGER note_no_update BEFORE UPDATE ON note"
                             " BEGIN SELECT RAISE(ABORT, 'note is append-only'); END")
                conn.execute("CREATE TRIGGER session_event_no_update BEFORE UPDATE ON"
                             " session_event BEGIN SELECT RAISE(ABORT,"
                             " 'session_event is append-only'); END")
        finally:
            conn.close()

    def pilot(self, *args, env=None):
        return subprocess.run([sys.executable, AUTOPILOT] + list(args), env=env or self.env,
                              cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=60)

    def pass_ok(self, *args, **kwargs):
        result = self.pilot(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def stored(self):
        return json.loads(self.state.read_text())

    def age_ticket(self, ticket, time='2001-01-01T00:00:00Z'):
        with sqlite3.connect(self.db) as conn:
            conn.execute('UPDATE ticket SET created=?, last_activity=? WHERE id=?',
                         (time, time, ticket))

    def task(self, opener, repo='owner', title='Stalled task', *flags):
        return int(self.ok('add', title, '--repo', repo, '--session', opener, '--claim', '--new', *flags))

    def request(self, opener, to, title='Stalled request', *flags):
        return int(self.ok('add', title, '--type', 'request', '--to', to, '--session', opener,
                           '--new', *flags))

    def requests(self, name='autopilot', include_closed=False):
        return self.query('SELECT t.id, t.addressee, t.title FROM ticket t JOIN session s'
                          ' ON s.token=t.opener WHERE s.name=? AND t.type=\'request\''
                          + ('' if include_closed else
                             " AND t.status IN ('todo', 'doing', 'blocked')") + ' ORDER BY t.id',
                          name)

    def dump(self):
        with sqlite3.connect(self.db) as conn:
            return '\n'.join(conn.iterdump())


class JsonTest(AutopilotCase):
    def test_empty_arrays_and_no_session_registration(self):
        self.assertEqual(self.ok('board', '--json'), '[]')
        before = self.dump()
        for flags in ((), ('--all',)):
            self.assertEqual(self.ok('sessions', '--json', *flags), '[]')
        self.assertEqual(self.dump(), before)

    def test_board_retains_display_labels_and_adds_raw_opener_name(self):
        token = self.start('owner')
        tid = self.task(token)
        row, = json.loads(self.ok('board', '--json'))
        self.assertEqual(row['id'], tid)
        self.assertEqual(row['opener_name'], 'owner')
        self.assertRegex(row['from'], r'^owner·[0-9a-f]{4}$')
        self.assertIn('to', row)
        self.assertIn('flags', row)

    def test_sessions_shape_harnesses_all_and_readonly(self):
        self.ok('start', '--as', 'owner', '--harness', 'pi')
        token = self.ok('start', '--as', 'owner', '--harness', 'codex')
        self.start('retired')
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE session SET started='2001-01-01T00:00:00Z',"
                         " last_seen='2001-01-01T00:00:00Z' WHERE name='retired'")
        task = self.task(token)
        self.ok('block', task, '--session', token)
        before = self.dump()
        row, = json.loads(self.ok('sessions', '--json', '--session', token))
        self.assertEqual(row, dict(name='owner', harnesses=['codex', 'pi'], sessions=2,
                                   seen=row['seen'], doing=0, blocked=1, installations=['host'], clients=[]))
        self.assertEqual({s['name'] for s in json.loads(self.ok('sessions', '--all', '--json'))},
                         {'owner', 'retired'})
        self.assertEqual(self.dump(), before)


class JsonGuestTest(GuestCase):
    def test_json_round_trip_through_guest_and_serve(self):
        guest = self.make_guest('laptop')
        token = self.ok('start', '--as', 'guest-owner', env=guest)
        tid = int(self.ok('add', 'Guest task', '--repo', 'guest-owner', '--session', token,
                          env=guest))
        before = self.query('SELECT * FROM ticket')
        for flags in ((), ('--repo', 'guest-owner'), ('--all',), ('--mine', '--session', token)):
            self.assertEqual(json.loads(self.ok('board', '--json', *flags, env=guest)),
                             json.loads(self.ok('board', '--json', *flags)))
        for flags in ((), ('--all',)):
            names = json.loads(self.ok('sessions', '--json', '--session', token, *flags,
                                       env=guest))
            self.assertIn('guest-owner', [s['name'] for s in names])
        self.assertEqual(self.query('SELECT * FROM ticket'), before)
        self.assertEqual(before[0][0], tid)
        self.assertFalse(os.path.exists(guest['KB_DB']))


class RoutingTest(AutopilotCase):
    def test_once_per_stall_then_note_and_prune(self):
        token = self.start('owner')
        tid = self.task(token)
        self.age_ticket(tid)
        out = self.pass_ok()
        self.assertIn('stalled=1 newly_reported=1 requests_added=1 notes_added=0', out)
        req, = self.requests()
        self.assertEqual(req[1:], ('owner', 'Stalled tickets for owner'))
        body = self.notes(req[0])[0][1]
        self.assertIn('- #%d task doing "Stalled task" owner:owner last activity ' % tid, body)
        self.assertIn('kb show %d' % tid, body)
        state = self.stored()
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(self.query("SELECT harness FROM session WHERE name='autopilot'"),
                         [('cli',)])
        self.assertIn('newly_reported=0 requests_added=0 notes_added=0', self.pass_ok())
        self.assertEqual(self.stored(), state)
        self.ok('touch', tid, '--session', token)
        self.age_ticket(tid, '2002-01-01T00:00:00Z')
        self.assertIn('newly_reported=1 requests_added=0 notes_added=1', self.pass_ok())
        self.assertEqual(self.requests(), [req])
        self.assertEqual([kind for kind, _ in self.notes(req[0])], ['request', 'comment'])
        self.ok('close', tid, '--session', token, '--where', 'test', '--changed', 'done', '--open', 'nothing')
        self.pass_ok()
        self.assertNotIn(str(tid), self.stored()['reported'])

    def test_groups_two_tasks_and_never_reads_note_text_or_changes_originals(self):
        token = self.start('owner')
        tickets = [self.task(token, title=title) for title in ('First', 'Second')]
        for tid in tickets:
            self.ok('note', tid, '--session', token, stdin='Private note body not to forward')
            self.age_ticket(tid)
        before = (self.query('SELECT * FROM ticket ORDER BY id'),
                  self.query('SELECT * FROM note ORDER BY id'))
        self.pass_ok()
        req, = self.requests()
        body = self.notes(req[0])[0][1]
        for tid in tickets:
            self.assertIn('kb show %d' % tid, body)
            self.assertEqual(self.query('SELECT * FROM ticket WHERE id=?', tid),
                             [before[0][tid - 1]])
        self.assertNotIn('Private note body', body)
        self.assertEqual(self.query('SELECT * FROM note WHERE ticket_id IN (?, ?) ORDER BY id',
                                    *tickets), before[1])

    def test_curator_collects_unknown_any_empty_approval_and_curator(self):
        token = self.start('owner')
        tickets = [self.task(token, 'unknown'), self.task(token, ''),
                   self.request(token, 'any'), self.request(token, 'curator'),
                   self.task(token, 'owner', 'Approval', '--label', 'approval'),
                   self.request(token, 'owner', 'Decision', '--label', 'needs-curator')]
        for tid in tickets:
            self.age_ticket(tid)
        self.pass_ok()
        req, = self.requests()
        self.assertEqual(req[1:], ('curator', 'Curator: open decisions and owner-less stalled tickets'))
        self.assertEqual(self.query('SELECT labels FROM ticket WHERE id=?', req[0]),
                         [('needs-curator',)])
        body = self.notes(req[0])[0][1]
        self.assertTrue(body.startswith('Stalled tickets that need a curator decision'))
        for tid in tickets:
            self.assertIn('kb show %d' % tid, body)

    def test_subsession_and_full_session_label_map_to_base(self):
        token = self.start('owner')
        opener = self.start('sender')
        tickets = [self.request(opener, 'owner/worker'),
                   self.request(opener, 'owner/codex\u00b7' + token[:4])]
        for tid in tickets:
            self.age_ticket(tid)
        self.pass_ok()
        req, = self.requests()
        self.assertEqual(req[1], 'owner')
        self.assertEqual(self.notes(req[0])[0][1].count('owner:owner '), 2)

    def test_full_session_label_without_harness_routes_to_active_owner(self):
        token = self.start('owner')
        label = 'owner·' + hashlib.sha256(token.encode()).hexdigest()[:4]
        tid = self.request(self.start('sender'), label)
        self.age_ticket(tid)
        self.pass_ok()
        self.assertEqual(self.requests()[0][1], 'owner')
        self.assertIn('owner:owner ', self.notes(self.requests()[0][0])[0][1])

    def test_exact_raw_name_wins_over_display_suffix_normalization(self):
        self.start('owner')
        raw = 'owner·abcd'
        self.start(raw)
        tid = self.request(self.start('sender'), raw)
        self.age_ticket(tid)
        self.pass_ok()
        self.assertEqual(self.requests()[0][1], raw)

    def test_own_requests_and_own_owner_are_skipped(self):
        token = self.start('autopilot')
        opener = self.start('sender')
        own = self.request(token, 'curator')
        addressed = self.request(opener, 'autopilot')
        for tid in (own, addressed):
            self.age_ticket(tid)
        before = self.query('SELECT * FROM ticket'), self.query('SELECT * FROM note')
        self.pass_ok()
        self.assertEqual((self.query('SELECT * FROM ticket'), self.query('SELECT * FROM note')),
                         before)
        self.assertEqual(self.stored()['reported'], {})

    def test_closed_request_replaced_for_next_stall(self):
        token = self.start('owner')
        tid = self.task(token)
        self.age_ticket(tid)
        self.pass_ok()
        req, = self.requests()
        self.ok('answer', req[0], '--session', token, stdin='handled')
        next_ticket = self.task(token, title='New stalled ticket')
        self.age_ticket(next_ticket)
        self.pass_ok()
        new, = self.requests()
        self.assertGreater(new[0], req[0])
        self.assertEqual(len(self.requests(include_closed=True)), 2)
        self.assertEqual([kind for kind, _ in self.notes(req[0])],
                         ['request', 'comment', 'reply', 'close'])

    def test_lowest_open_request_wins(self):
        token = self.start('autopilot')
        first = self.request(token, 'owner', 'First collecting request')
        second = self.request(token, 'owner', 'Second collecting request')
        tid = self.task(self.start('owner'))
        self.age_ticket(tid)
        self.pass_ok()
        self.assertEqual([kind for kind, _ in self.notes(first)], ['request', 'comment'])
        self.assertEqual([kind for kind, _ in self.notes(second)], ['request'])

    def test_stranded_handoff_waits_for_stale_hours_then_routes_by_repo(self):
        token = self.start('owner')
        self.start('receiver')
        tid = self.task(token)
        self.ok('release', tid, '--to', 'receiver', '--stdin', '--session', token,
                stdin='handoff')
        self.assertIn('"id": %d' % tid, self.ok('board', '--stranded', '--json'))
        self.assertIn('stalled=0', self.pass_ok())
        self.assertEqual(self.requests(), [])
        self.age_ticket(tid)
        self.pass_ok()
        req, = self.requests()
        self.assertEqual(req[1], 'owner')

    def test_session_ended_waits_for_stranded_grace(self):
        token = self.start('owner')
        tid = self.task(token)
        self.age_board(1)  # a ticket opened in the second of the end note counts as the holder back
        self.ok('session-ended', '--session', token)
        self.assertEqual(self.ok('board', '--stranded', '--json'), '[]')
        self.assertIn('stalled=0', self.pass_ok())
        self.assertEqual(self.requests(), [])
        self.age_board(120)  # past the one-hour grace, still within KB_STALE_HOURS
        self.assertIn('"id": %d' % tid, self.ok('board', '--stranded', '--json'))
        self.pass_ok()
        self.assertIn(str(tid), self.stored()['reported'])
        self.assertEqual(self.requests()[0][1], 'owner')

    def test_request_of_ended_holder_takes_no_more_items(self):
        token = self.start('owner')
        first = self.task(token)
        self.age_ticket(first)
        self.pass_ok()
        req, = self.requests()
        self.ok('claim', req[0], '--session', token)
        self.age_board(1)  # a claim in the second of the end note counts as the holder back
        self.ok('session-ended', '--session', token)
        self.age_board(120)
        self.start('owner')  # a new session of the name, seen now
        second = self.task(self.start('other'), 'owner')
        self.age_ticket(second)
        self.pass_ok()
        old, new = self.requests()
        self.assertEqual(old[0], req[0])
        self.assertNotIn('kb show %d' % second, ' '.join(n[1] for n in self.notes(old[0])))
        self.assertIn('kb show %d' % second, self.notes(new[0])[0][1])

    def test_request_of_ended_holder_is_reported_once_with_unchanged_items(self):
        token = self.start('owner')
        first = self.task(token)
        self.age_ticket(first)
        self.pass_ok()
        req, = self.requests()
        self.ok('claim', req[0], '--session', token)
        self.age_board(1)  # a claim in the second of the end note counts as the holder back
        self.ok('session-ended', '--session', token)
        self.age_board(120)  # past the grace; this also moves first's last_activity
        self.start('owner')  # a new session of the name, seen now
        state = self.stored()  # first stays reported at its current, unchanged activity
        state['reported'][str(first)] = self.query(
            'SELECT last_activity FROM ticket WHERE id=?', first)[0][0]
        self.state.write_text(json.dumps(state))
        self.pass_ok()
        old, new = self.requests()
        self.assertEqual(old[0], req[0])
        self.assertIn('kb show %d' % req[0], self.notes(new[0])[0][1])
        self.assertIn('newly_reported=0', self.pass_ok())  # once per stall

    def test_dormant_holder_goes_to_curator(self):
        token = self.start('dormant')
        tid = self.task(token, 'dormant')
        self.age_board(20 * 24 * 60)  # holder and ticket last seen 20 days ago
        self.assertIn('"name": "dormant"', self.ok('sessions', '--json'))
        self.pass_ok()
        req, = self.requests()
        self.assertEqual(req[1], 'curator')
        self.assertIn('kb show %d' % tid, self.notes(req[0])[0][1])

    def test_fresh_tickets_ignored_and_zero_stale_hours_supported(self):
        token = self.start('owner')
        tid = self.task(token)
        req = self.request(token, 'owner')
        self.assertIn('stalled=0', self.pass_ok())
        self.assertEqual(self.requests(), [])
        self.assertIn('newly_reported=2', self.pass_ok(env=dict(self.env, KB_STALE_HOURS='0')))
        self.assertEqual(set(self.stored()['reported']), {str(tid), str(req)})

    def test_idle_release_stays_independent_and_preserves_deduplication(self):
        token = self.start('owner')
        tid = self.task(token)
        self.age_ticket(tid)
        self.pass_ok()
        self.assertEqual(self.ticket(tid)[:2], ('doing', token))
        original_requests = self.requests()
        self.ok('hook', 'start', '--harness', 'pi', '--id', 'release-trigger',
                '--cwd', self.tmp)
        self.assertEqual(self.ticket(tid)[:2], ('todo', None))
        self.assertIn('stalled=1 newly_reported=0', self.pass_ok())
        self.assertEqual(self.requests(), original_requests)
        self.age_ticket(tid, '2002-01-01T00:00:00Z')
        self.assertIn('newly_reported=1 requests_added=0 notes_added=1', self.pass_ok())

    def test_decision_label_routes_to_curator(self):
        tid = self.task(self.start('owner'), 'owner', 'Decision', '--label', 'decision')
        self.age_ticket(tid)
        self.pass_ok()
        self.assertEqual(self.requests()[0][1], 'curator')

    def test_todo_staleness_uses_last_activity_not_creation(self):
        token = self.start('owner')
        req = self.request(token, 'owner')
        self.age_ticket(req)
        self.ok('note', req, '--session', token, stdin='fresh activity')
        self.assertIn('stalled=0', self.pass_ok())


class OperationTest(AutopilotCase):
    def test_dry_run_without_token_changes_neither_board_nor_state(self):
        token = self.start('owner')
        tid = self.task(token)
        self.age_ticket(tid)
        before = self.dump()
        out = self.pass_ok('--dry-run')
        self.assertIn('"command": ["add"', out)
        self.assertEqual(self.dump(), before)
        self.assertFalse(self.state.exists())

    def test_dry_run_with_token_plans_note_without_writing(self):
        tid = self.task(self.start('owner'))
        self.age_ticket(tid)
        self.pass_ok()
        self.age_ticket(tid, '2002-01-01T00:00:00Z')
        before, state = self.dump(), self.state.read_bytes()
        out = self.pass_ok('--dry-run')
        self.assertIn('"command": ["note"', out)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.state.read_bytes(), state)

    def test_held_lock_exits_without_board_or_state_writes(self):
        self.ok('board')
        self.state.parent.mkdir(parents=True)
        before = self.dump()
        with open(str(self.state) + '.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn('lock held', self.pass_ok())
        self.assertEqual(self.dump(), before)
        self.assertFalse(self.state.exists())

    def test_identity_environment_removed_and_custom_name_state(self):
        token = self.start('owner')
        tid = self.task(token)
        self.age_ticket(tid)
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc', CODEX_SESSION_ID='cx',
                   PI_SESSION_ID='pi', PI_SUBAGENT_DEPTH='1', KB_SESSION=token,
                   CODEX_THREAD_ID='thread-id', KB_HARNESS='codex')
        custom = Path(self.tmp) / 'custom.json'
        custom.write_text(json.dumps({'reported': {}}))
        self.pass_ok('--name', 'router', '--state', str(custom), env=env)
        self.pass_ok('--name', 'router', '--state', str(custom), env=env)
        self.assertEqual(self.query("SELECT name, harness, harness_session FROM session"
                                    " WHERE name='router'"), [('router', 'cli', '')])
        self.assertEqual(len(self.requests(name='router')), 1)
        self.assertFalse(self.state.exists())

    def test_bad_stale_hours_rejected_before_start(self):
        self.ok('board')
        before = self.dump()
        for value in ('nope', 'nan', 'inf', '1e100', '-1'):
            result = self.pilot(env=dict(self.env, KB_STALE_HOURS=value))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('KB_STALE_HOURS must be a number of hours', result.stderr)
        self.assertEqual(self.dump(), before)
        self.assertFalse(self.state.exists())

    def test_failed_write_stops_and_preserves_successful_reports(self):
        first = self.task(self.start('first'), 'a')
        second = self.task(self.start('second'), 'b')
        self.start('a')
        self.start('b')
        for tid in (first, second):
            self.age_ticket(tid)
        loader = importlib.machinery.SourceFileLoader('autopilot_under_test', AUTOPILOT)
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        original = module.Board.call
        writes = []

        def fail_second(board, *args, **kwargs):
            if args[0] in ('add', 'note'):
                writes.append(args)
                if len(writes) == 2:
                    raise RuntimeError('kb add failed (exit 1): test rejection')
            return original(board, *args, **kwargs)

        error = io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(sys, 'argv', [AUTOPILOT, '--state', str(self.state)]), \
                patch.object(module.Board, 'call', fail_second), \
                contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(), 1)
        self.assertIn('test rejection', error.getvalue())
        self.assertEqual(len(writes), 2)
        self.assertEqual(set(self.stored()['reported']), {str(first)})
        self.assertEqual(len(self.requests()), 1)
        self.assertIn('newly_reported=1 requests_added=1', self.pass_ok())
        self.assertEqual(len(self.requests()), 2)

    def test_kb_failure_is_reported_on_stderr(self):
        result = self.pilot(env=dict(self.env, KB_BIN='/bin/false'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('kb start failed (exit 1)', result.stderr)
        self.assertFalse(self.state.exists())


if __name__ == '__main__':
    unittest.main()
