"""Restore is an exclusive operation; concurrent CLI writers wait for its replacement."""
import glob
import json
import os
import sqlite3
import time
import unittest

from test_kb import KbCase

try:
    import fcntl
except ImportError:
    fcntl = None


@unittest.skipUnless(fcntl, 'restore requires host flock support')
class RestoreBarrierTest(KbCase):
    def setUp(self):
        super().setUp()
        self.alice, self.curator = self.start('alice'), self.start('curator')
        self.ticket = self.ok('add', '--claim', 'restore target', '--session', self.alice)
        self.closed = self.ok('add', '--new', '--claim', 'close target', '--session', self.alice)
        self.released = self.ok('add', '--new', '--claim', 'release target', '--session', self.alice)
        self.backup = os.path.join(self.tmp, 'backup.sqlite')
        self.ok('backup', self.backup)
        self.processes = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def launch(self, args, env=None, stdin=None):
        process = self.spawn(args, env or self.env, stdin_text=stdin)
        self.processes.append(process)
        return process

    def wait_at_barrier(self, barrier):
        deadline = time.monotonic() + 15
        while not glob.glob(barrier + '.*'):
            if time.monotonic() >= deadline:
                self.fail('process did not reach barrier')
            time.sleep(0.01)

    def finish(self, process):
        out, err = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, err)
        return out

    def test_writers_wait_and_survive_restore_with_pre_restore_snapshot(self):
        self.ok('note', self.ticket, 'live note after backup', '--session', self.alice)
        barrier = os.path.join(self.tmp, 'restore-ready')
        restore = self.launch(['restore', self.backup, '--session', self.curator],
                              dict(self.env, KB_TEST_BARRIER=barrier))
        self.wait_at_barrier(barrier)
        # A deterministic proof that restore excludes *all* shared writer connections.
        with open(self.db + '.restore.lock', 'a') as gate:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(gate, fcntl.LOCK_SH | fcntl.LOCK_NB)
        note = self.launch(['note', self.ticket, 'concurrent note', '--session', self.alice])
        close = self.launch(['close', self.closed, '--session', self.alice,
                             '--where', 'test', '--changed', 'done', '--open', 'nothing'])
        release = self.launch(['release', self.released, '--session', self.alice,
                               '--why', 'concurrent release'])
        hook = self.launch(['hook', 'start', '--harness', 'claude'],
                           stdin=json.dumps(dict(session_id='concurrent-hook', cwd=self.tmp,
                                                 hook_event_name='SessionStart')))
        with open(barrier, 'w'):
            pass
        out = self.finish(restore)
        for process in (note, close, release, hook):
            self.finish(process)
        self.assertEqual(self.query('SELECT status FROM ticket WHERE id=?', self.closed),
                         [('done',)])
        self.assertEqual(self.query('SELECT status FROM ticket WHERE id=?', self.released),
                         [('todo',)])
        notes = self.query('SELECT text FROM note WHERE ticket_id=?', self.ticket)
        self.assertIn(('concurrent note',), notes)
        self.assertNotIn(('live note after backup',), notes)
        self.assertEqual(self.query("SELECT count(*) FROM session_event"
                                    " WHERE harness_session='concurrent-hook'"), [(1,)])
        snapshots = glob.glob(self.db + '.pre-restore-*.sqlite')
        self.assertEqual(len(snapshots), 1)
        self.assertIn(snapshots[0], out)
        self.assertEqual(os.stat(snapshots[0]).st_mode & 0o777, 0o600)
        with sqlite3.connect(snapshots[0]) as conn:
            self.assertIn(('live note after backup',), conn.execute(
                'SELECT text FROM note WHERE ticket_id=?', (self.ticket,)).fetchall())

    def test_restore_drains_writers_that_already_connected(self):
        barrier = os.path.join(self.tmp, 'writer-ready')
        writer = self.launch(['note', self.ticket, 'already connected', '--session', self.alice],
                             dict(self.env, KB_TEST_BARRIER=barrier))
        self.wait_at_barrier(barrier)
        with open(self.db + '.restore.lock', 'a') as gate:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        restore = self.launch(['restore', self.backup, '--session', self.curator])
        with open(barrier, 'w'):
            pass
        self.finish(writer)
        self.finish(restore)
        snapshot, = glob.glob(self.db + '.pre-restore-*.sqlite')
        with sqlite3.connect(snapshot) as conn:
            self.assertIn(('already connected',), conn.execute(
                'SELECT text FROM note WHERE ticket_id=?', (self.ticket,)).fetchall())

    def test_invalid_restore_does_not_enter_recovery(self):
        invalid = os.path.join(self.tmp, 'invalid.sqlite')
        with open(invalid, 'w') as stream:
            stream.write('not sqlite')
        self.assertEqual(self.code('restore', invalid, '--session', self.curator), 3)
        self.assertEqual(self.query("SELECT * FROM meta WHERE key='recovery'"), [])
        self.assertEqual(glob.glob(self.db + '.pre-restore-*.sqlite'), [])
