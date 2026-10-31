"""Chief completion is an audited disposition, never a curator approval."""
import hashlib
import json
import sqlite3
from contextlib import closing

from test_kb import KbCase


class ChiefCloseTest(KbCase):
    def setUp(self):
        super().setUp()
        self.chief = self.start('chief')
        self.opener = self.start('opener')

    def request(self, to='curator', who=None):
        return self.ok('add', 'question', '--new', '--type', 'request', '--to', to,
                       '--session', who or self.opener)

    def close(self, ticket, *extra, who=None):
        return self.kb('close', ticket, '--result', 'already completed',
                       '--evidence', 'verified command output in note 42',
                       '--session', who or self.chief, *extra)

    def recipient(self, hsid='worker-1', name='worker'):
        env = dict(self.env, CODEX_SESSION_ID=hsid)
        # Auto-registration is supported without a start hook. Tests emit the lifecycle
        # events they need explicitly, avoiding accidental wall-clock ties in end fixtures.
        self.ok('name', name, env=env)
        token = self.query('SELECT token FROM session WHERE harness_session=?', hsid)[0][0]
        return token

    def event(self, event, hsid='worker-1'):
        self.ok('hook', event, '--harness', 'codex',
                stdin=json.dumps({'session_id': hsid, 'cwd': self.tmp}))

    def test_curator_completion_without_reply_preserves_approval_history(self):
        ticket = self.request()
        self.assertEqual(self.close(ticket).returncode, 0)
        self.assertEqual(self.ticket(ticket)[:2], ('done', None))
        self.assertEqual(self.notes(ticket)[-1], (
            'close', 'chief completion: already completed\n'
            'evidence: verified command output in note 42'))
        self.assertEqual(self.query("SELECT count(*) FROM note WHERE ticket_id=?"
                                    " AND kind='reply'", ticket), [(0,)])
        self.assertEqual(self.close(ticket).returncode, 1)

    def test_existing_reply_is_preserved(self):
        ticket = self.request()
        self.ok('note', ticket, '--reply', 'Sebastian approved in conversation',
                '--session', self.opener)
        before = self.notes(ticket)
        self.assertEqual(self.close(ticket).returncode, 0)
        self.assertEqual(self.notes(ticket)[:-1], before)

    def test_human_curator_holder_requires_a_recorded_decision(self):
        curator = self.ok('start', '--as', 'curator', '--harness', 'human')
        self.assertEqual(self.query('SELECT harness, harness_session FROM session WHERE token=?',
                                    curator), [('human', '')])
        ticket = self.request()
        self.ok('claim', ticket, '--session', curator)
        r = self.close(ticket)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('held by curator without a reply', r.stderr)
        self.assertEqual(self.ticket(ticket)[:2], ('doing', curator))
        self.ok('note', ticket, '--reply', 'decision recorded by curator', '--session', curator)
        before = self.notes(ticket)
        self.assertEqual(self.close(ticket).returncode, 0)
        self.assertEqual(self.ticket(ticket)[:2], ('done', curator))
        self.assertEqual(self.notes(ticket)[:-1], before)

    def test_note_failure_rolls_back_completion_and_withdrawal(self):
        self.recipient()
        self.event('end')
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TRIGGER fail_chief_note BEFORE INSERT ON note"
                         " WHEN NEW.text LIKE 'chief %' BEGIN"
                         " SELECT RAISE(ABORT, 'injected note failure'); END")
            conn.commit()
        for to, extra in (('curator', ()), ('worker', ('--withdraw',))):
            with self.subTest(addressee=to):
                ticket = self.request(to)
                before = self.notes(ticket)
                r = self.close(ticket, *extra)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn('injected note failure', r.stderr)
                self.assertEqual(self.ticket(ticket)[:2], ('todo', None))
                self.assertEqual(self.notes(ticket), before)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('DROP TRIGGER fail_chief_note')
            conn.commit()
        for ticket, to in self.query('SELECT id, addressee FROM ticket'):
            extra = ('--withdraw',) if to == 'worker' else ()
            self.assertEqual(self.close(ticket, *extra).returncode, 0)

    def test_proof_and_result_are_both_required(self):
        ticket = self.request()
        before = self.notes(ticket)
        self.assertEqual(self.code('close', ticket, '--session', self.chief), 1)
        for flags in (['--evidence', 'proof'], ['--evidence', ' ', '--result', 'done'],
                      ['--evidence', 'proof', '--result', ' ']):
            with self.subTest(flags=flags):
                self.assertEqual(self.code('close', ticket, '--session', self.chief, *flags), 3)
        self.assertEqual(self.ticket(ticket)[0], 'todo')
        self.assertEqual(self.notes(ticket), before)

    def test_stranger_tasks_and_force_are_rejected(self):
        ticket = self.request()
        self.assertEqual(self.close(ticket, who=self.opener).returncode, 1)
        self.assertEqual(self.close(ticket, '--force').returncode, 3)
        task = self.ok('add', 'task', '--claim', '--session', self.opener)
        self.assertEqual(self.close(task).returncode, 1)
        self.assertEqual(self.close(ticket, '--where', 'host').returncode, 3)
        self.assertEqual(self.close(999).returncode, 1)

    def test_other_requests_and_curator_withdrawal_are_rejected(self):
        self.assertEqual(self.close(self.request('worker')).returncode, 1)
        self.assertEqual(self.close(self.request(), '--withdraw').returncode, 1)

    def test_plain_name_or_label_of_ended_recipient_can_be_withdrawn(self):
        token = self.recipient()
        self.event('end')
        label = 'worker/codex·' + hashlib.sha256(token.encode()).hexdigest()[:4]
        for name in ('worker', label):
            with self.subTest(addressee=name):
                ticket = self.request(name)
                self.assertEqual(self.close(ticket, '--withdraw').returncode, 0)
                self.assertEqual(self.ticket(ticket)[:2], ('abandoned', None))
                self.assertIn('chief withdrawal:', self.notes(ticket)[-1][1])

    def test_unknown_idle_or_token_only_recipient_is_not_proven_ended(self):
        self.start('token-only')
        self.recipient()
        for name in ('missing', 'any', 'worker', 'token-only'):
            with self.subTest(addressee=name):
                ticket = self.request(name)
                # Mere age, without a lifecycle end, must not permit abandonment.
                with closing(sqlite3.connect(self.db)) as conn:
                    conn.execute("UPDATE ticket SET last_activity='2000-01-01T00:00:00Z'"
                                 ' WHERE id=?', (ticket,))
                    conn.commit()
                self.assertEqual(self.close(ticket, '--withdraw').returncode, 1)

    def test_resumed_recipient_is_protected_even_in_end_second(self):
        self.recipient()
        self.event('end')
        ticket = self.request('worker')
        self.event('start')
        self.assertEqual(self.close(ticket, '--withdraw').returncode, 1)
        self.event('end')
        self.event('stop')
        self.assertEqual(self.close(ticket, '--withdraw').returncode, 1)

    def test_reordered_same_second_host_end_does_not_prove_an_end(self):
        self.recipient()
        with closing(sqlite3.connect(self.db)) as conn:
            inst = conn.execute("SELECT installation FROM session"
                                " WHERE harness_session='worker-1'").fetchone()[0]
            for event in ('start', 'end'):
                conn.execute('INSERT INTO session_event'
                             ' (installation, harness, harness_session, event, time)'
                             " VALUES (?, 'codex', 'worker-1', ?, '2000-01-01T00:00:00Z')",
                             (inst, event))
            conn.commit()
        ticket = self.request('worker')
        r = self.close(ticket, '--withdraw')
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('ambiguous end', r.stderr)

    def test_one_active_session_of_same_name_blocks_withdrawal(self):
        token = self.recipient()
        self.event('end')
        self.recipient('worker-2')
        ticket = self.request('worker')
        self.assertEqual(self.close(ticket, '--withdraw').returncode, 1)
        # An explicit label still selects just the ended session.
        labelled = self.request('worker/codex·' + hashlib.sha256(token.encode()).hexdigest()[:4])
        self.assertEqual(self.close(labelled, '--withdraw').returncode, 0)

    def test_answered_orphan_cannot_be_withdrawn(self):
        token = self.recipient()
        ticket = self.request('worker')
        self.ok('claim', ticket, '--session', token)
        self.ok('note', ticket, '--reply', 'answer', '--session', token)
        self.event('end')
        before = self.notes(ticket)
        self.assertEqual(self.close(ticket, '--withdraw').returncode, 1)
        self.assertEqual(self.notes(ticket), before)

    def test_ended_holder_required_for_completion_and_withdrawal(self):
        # A curator request can already be held by a worker.
        for to, extra in (('curator', ()), ('worker', ('--withdraw',))):
            with self.subTest(addressee=to):
                hsid = 'holder-' + to
                token = self.recipient(hsid)
                ticket = self.request(to)
                with closing(sqlite3.connect(self.db)) as conn:
                    conn.execute("UPDATE ticket SET assignee=?, status='doing' WHERE id=?",
                                 (token, ticket))
                    conn.commit()
                self.assertEqual(self.close(ticket, *extra).returncode, 1)
                self.event('end', hsid)
                self.assertEqual(self.close(ticket, *extra).returncode, 0)
                self.assertEqual(self.ticket(ticket)[1], None if extra else token)
