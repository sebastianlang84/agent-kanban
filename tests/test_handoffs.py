"""Explicit ticket handoffs and main-checkout-only delivery."""
import datetime
import json
import os
import sqlite3
import subprocess

from test_kb import KbCase, context
from test_guest import GuestCase


class HandoffTest(KbCase):
    def setUp(self):
        super().setUp()
        self.worker = self.start('worker')
        self.receiver = self.start('receiver')
        self.other = self.start('other')
        self.curator = self.start('curator')

    def task(self, who=None, *extra):
        return self.ok('add', 'implemented work', '--new', '--claim', '--repo', 'r',
                       '--worktree', '/source/worktree', '--branch', 'feature/ready',
                       '--session', who or self.worker, *extra)

    def release(self, tid, to='receiver', who=None):
        return self.kb('release', tid, '--to', to, '--stdin', '--session', who or self.worker,
                       stdin='implemented; review and merge, then close')

    def test_validation_metadata_and_only_addressee_claims(self):
        tid = self.task()
        result = self.release(tid, 'typo')
        self.assertEqual(result.returncode, 1)
        self.assertIn('known names:', result.stderr)
        self.assertEqual(self.ticket(tid)[:2], ('doing', self.worker))
        self.assertEqual(self.release(tid).returncode, 0)
        self.assertEqual(self.ticket(tid)[:2], ('todo', None))
        self.assertEqual(self.ticket(tid)[3], 'receiver')
        self.assertEqual(self.notes(tid)[-1], ('release', 'released to receiver (branch'
                          ' feature/ready in /source/worktree): implemented; review and merge,'
                          ' then close'))
        self.assertIsNotNone(self.board_line(tid, '--to', 'receiver'))
        self.assertEqual(self.code('claim', tid, '--session', self.other), 1)
        self.ok('claim', tid, '--session', self.receiver, '--branch', 'main',
                '--worktree', '/destination')
        self.assertIn('feature/ready in /source/worktree', self.notes(tid)[0][1])

    def test_only_last_releaser_or_curator_retargets_an_unclaimed_handoff(self):
        tid = self.task()
        self.assertEqual(self.release(tid).returncode, 0)
        self.assertEqual(self.release(tid, 'other', self.receiver).returncode, 1)
        self.assertEqual(self.release(tid, 'other', self.other).returncode, 1)
        self.assertEqual(self.release(tid, 'other').returncode, 0)
        self.assertEqual(self.ticket(tid)[3], 'other')
        self.assertEqual(self.release(tid, 'receiver', self.curator).returncode, 0)
        self.ok('claim', tid, '--session', self.receiver)
        self.assertEqual(self.release(tid, 'other').returncode, 1)

    def test_failed_handoff_note_rolls_back_target_and_status(self):
        tid = self.task()
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TRIGGER fail_handoff BEFORE INSERT ON note"
                         " WHEN NEW.kind='release' BEGIN"
                         " SELECT RAISE(ABORT, 'injected failure'); END")
        self.assertEqual(self.release(tid).returncode, 1)
        self.assertEqual(self.ticket(tid)[:2], ('doing', self.worker))
        self.assertIsNone(self.ticket(tid)[3])

    def test_plain_release_returns_a_handoff_task_to_everyone(self):
        tid = self.task()
        self.assertEqual(self.release(tid).returncode, 0)
        self.ok('claim', tid, '--session', self.receiver)
        self.ok('release', tid, '--session', self.receiver)
        self.assertIsNone(self.ticket(tid)[3])
        self.ok('claim', tid, '--session', self.other)
        req = self.ok('add', 'question', '--type', 'request', '--to', 'receiver',
                      '--session', self.worker)
        self.ok('claim', req, '--session', self.receiver)
        self.ok('release', req, '--session', self.receiver)
        self.assertEqual(self.ticket(req)[3], 'receiver')

    def test_takeover_keeps_previous_branch_and_worktree(self):
        tid = self.task()
        self.ok('end', '--session', self.worker)
        self.ok('claim', tid, '--take', '--session', self.other,
                '--branch', 'new', '--worktree', '/destination')
        self.assertTrue(any('branch feature/ready in /source/worktree' in text
                            for kind, text in self.notes(tid) if kind == 'event'))
        # A named task stays restricted even when its current holder ends.
        tid = self.task()
        self.assertEqual(self.release(tid).returncode, 0)
        self.ok('claim', tid, '--session', self.receiver)
        self.ok('end', '--session', self.receiver)
        self.assertEqual(self.code('claim', tid, '--take', '--session', self.other), 1)
        next_receiver = self.start('receiver')
        self.ok('claim', tid, '--take', '--session', next_receiver)

    def test_stranded_filters_idle_ended_and_unclaimed_handoffs(self):
        active, idle, ended, recent = [self.task() for _ in range(4)]
        held_curator = self.task(self.curator)
        waiting = self.ok('add', 'waiting', '--todo', '--session', self.worker)
        queued = self.task()
        self.assertEqual(self.release(queued, 'curator').returncode, 0)
        self.assertIsNotNone(self.board_line(queued, '--to', 'curator'))
        parked = self.task()
        self.ok('label', parked, 'backlog', '--session', self.worker)
        self.assertEqual(self.release(parked).returncode, 0)
        stamp = lambda hours: (datetime.datetime.now(datetime.timezone.utc)
                               - datetime.timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
        with sqlite3.connect(self.db) as conn:
            for tid in (idle, held_curator):
                conn.execute('UPDATE ticket SET last_activity=? WHERE id=?', (stamp(25), tid))
            for tid, age in ((ended, 2), (recent, 0.1)):
                conn.execute('UPDATE ticket SET claimed_at=? WHERE id=?', (stamp(3), tid))
                conn.execute('INSERT INTO note (ticket_id, author, time, kind, text)'
                             " VALUES (?, ?, ?, 'session-ended', 'ended')",
                             (tid, self.worker, stamp(age)))
        ids = {str(r['id']) for r in json.loads(self.ok('board', '--stranded', '--json'))}
        self.assertEqual(ids, {idle, ended, queued, parked})
        for tid in (active, recent, held_curator, waiting):
            self.assertNotIn(tid, ids)

    def test_resumed_holder_is_not_stranded(self):
        env = dict(self.env, CODEX_SESSION_ID='resumed-worker')
        self.ok('name', 'resumed', env=env)
        tid = self.ok('add', 'resumed work', '--claim', env=env)
        token = self.query("SELECT token FROM session WHERE name='resumed'")[0][0]
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE ticket SET claimed_at='2026-01-01T00:00:00Z' WHERE id=?", (tid,))
            conn.execute('INSERT INTO note (ticket_id, author, time, kind, text)'
                         " VALUES (?, ?, '2026-01-02T00:00:00Z', 'session-ended', 'ended')",
                         (tid, token))
        self.ok('hook', 'stop', '--harness', 'codex',
                stdin=json.dumps(dict(session_id='resumed-worker', cwd=self.tmp)))
        self.assertIsNone(self.board_line(tid, '--stranded'))

    def test_hints_only_in_main_checkout_per_release_and_session(self):
        repo, linked = os.path.join(self.tmp, 'main'), os.path.join(self.tmp, 'linked')
        subprocess.run(['git', 'init', '-q', repo], check=True)
        subprocess.run(['git', '-C', repo, '-c', 'user.name=Test', '-c', 'user.email=test@local',
                        'commit', '-q', '--allow-empty', '-m', 'initial'], check=True)
        subprocess.run(['git', '-C', repo, 'worktree', 'add', '-q', '--detach', linked], check=True)
        alias = os.path.join(self.tmp, 'alias')
        os.symlink(linked, alias)
        os.mkdir(os.path.join(repo, 'subdir'))
        tid = self.task()
        self.assertEqual(self.release(tid).returncode, 0)
        for hsid in ('receiver-one', 'receiver-two'):
            self.ok('name', 'receiver', env=dict(self.env, CODEX_SESSION_ID=hsid))

        def prompt(cwd, hsid='receiver-one'):
            return context(self.ok('hook', 'prompt', '--harness', 'codex',
                                    stdin=json.dumps(dict(session_id=hsid, cwd=cwd))))

        for cwd in (linked, alias, self.tmp):
            self.assertNotIn('handed to you', prompt(cwd))
        self.assertIn('#%s handed to you by worker' % tid, prompt(os.path.join(repo, 'subdir')))
        self.assertNotIn('handed to you', prompt(repo))
        self.assertIn('handed to you', prompt(repo, 'receiver-two'))
        self.assertEqual(self.release(tid).returncode, 0)  # a new handoff is a new event
        self.assertIn('handed to you', prompt(repo))


class GuestHandoffTest(GuestCase):
    def test_guest_prompt_never_consumes_host_main_checkout_handoff(self):
        repo = os.path.join(self.tmp, 'main')
        subprocess.run(['git', 'init', '-q', repo], check=True)
        ctx = dict(cwd=repo, name='receiver', toplevel=repo,
                   env=dict(CODEX_SESSION_ID='guest-receiver'))
        registered = self.serve(self.request(['add', 'guest setup'], ctx))
        self.assertEqual(registered.returncode, 0, registered.stderr)
        worker = self.start('worker')
        tid = self.ok('add', 'guest handoff', '--claim', '--session', worker)
        self.ok('release', tid, '--to', 'receiver', '--why', 'integrate', '--session', worker)
        payload = json.dumps(dict(session_id='guest-receiver', cwd=repo)).encode()
        result = self.serve(self.request(['hook', 'prompt', '--harness', 'codex'], ctx, payload))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(b'handed to you', result.stdout)
        self.assertEqual(self.query("SELECT count(*) FROM hint_seen WHERE item LIKE 'handoff:%'"),
                         [(0,)])
