"""Exercise board helpers against a temporary database populated by the real kb CLI."""
import json
import os
import time
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import unittest

from tests.test_kb import KbCase

TOOLS = Path(__file__).resolve().parents[1] / 'tools'


class ToolsCase(KbCase):
    def setUp(self):
        super().setUp()
        self.env.pop('CODEX_THREAD_ID', None)
        self.env.pop('KB_HARNESS', None)
        self.env.pop('KB', None)
        self.reader = self.start('reader')
        self.writer = self.start('writer')
        self.cursor = Path(self.tmp) / 'watch-reader.last'
        self.env.pop('KB_FIRST_NOTE', None)

    def add(self, title, owner=None, to=None):
        args = ['add', '--new', title]
        if to is not None:
            args += ['--type', 'request', '--to', to]
        else:
            args += ['--claim']
        return int(self.ok(*args, '--session', owner or self.writer))

    def note(self, ticket, owner=None, kind='comment', text='new activity'):
        self.ok('note', ticket, '--kind', kind, '--session', owner or self.writer, stdin=text)
        return self.query('SELECT max(id) FROM note')[0][0]

    def watch(self, *args, env=None):
        return subprocess.run([sys.executable, str(TOOLS / 'kb-watch'), '--for', 'reader',
                               '--once', *args], env=env or self.env, cwd=self.tmp,
                              capture_output=True, text=True, timeout=15)

    def watch_ok(self, *args, **kw):
        result = self.watch(*args, **kw)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def from_start(self, last=0):
        self.cursor.write_text(str(last) + '\n')

    def test_once_cursor_and_first_run(self):
        ticket = self.add('addressed request', to='reader')
        old = self.note(ticket)
        self.assertEqual(self.watch_ok(), '')  # first run skips existing history
        self.assertEqual(json.loads(self.cursor.read_text())[1], old)
        new = self.note(ticket, text='next note')
        self.assertEqual(self.watch_ok(), 'kb #%s writer comment: addressed request\n' % ticket)
        self.assertEqual(json.loads(self.cursor.read_text())[1], new)
        self.assertEqual(self.watch_ok(), '')  # restart does not repeat a delivery

    def test_finds_board_from_config(self):
        config_home = Path(self.tmp) / 'config'
        config = config_home / 'agent-kanban' / 'config'
        config.parent.mkdir(parents=True)
        config.write_text('KB_DB = ./board.sqlite\n', encoding='utf-8')
        env = dict(self.env, XDG_CONFIG_HOME=str(config_home))
        del env['KB_DB']

        ticket = self.add('configured board', to='reader')
        self.assertEqual(self.watch_ok(env=env), '')
        new = self.note(ticket)
        self.assertEqual(self.watch_ok(env=env), 'kb #%s writer comment: configured board\n' % ticket)
        self.assertEqual(json.loads(self.cursor.read_text())[1], new)

    def test_first_note_environment(self):
        ticket = self.add('hook notification', to='reader')
        self.note(ticket, text='before hook')
        first = self.note(ticket, text='hook note')
        out = self.watch_ok(env=dict(self.env, KB_FIRST_NOTE=str(first)))
        self.assertEqual(out.count('kb #'), 1)
        self.assertEqual(json.loads(self.cursor.read_text())[1], first)

    def restore_setup(self):
        ticket = self.add('restore notification', to='reader')
        self.note(ticket, text='backup history')
        backup = str(Path(self.tmp) / 'backup.sqlite')
        self.ok('backup', backup)
        for _ in range(4):
            self.note(ticket, text='discarded activity')
        self.watch_ok()
        return ticket, backup, self.start('curator')

    def test_restart_after_restore_delivers_reused_ids_without_replay(self):
        ticket, backup, curator = self.restore_setup()
        inode = os.stat(self.db).st_ino
        # A deployed integer cursor is also invalidated by the first generation change.
        self.from_start(self.query('SELECT max(id) FROM note')[0][0])
        self.ok('restore', backup, '--session', curator)
        self.assertEqual(os.stat(self.db).st_ino, inode)
        new = self.note(ticket, text='after restore')
        self.assertEqual(self.watch_ok().count('kb #'), 1)
        self.assertEqual(json.loads(self.cursor.read_text())[1], new)
        self.assertEqual(self.watch_ok(), '')
        # Repeated restores of the same backup must create distinct generations.
        previous = json.loads(self.cursor.read_text())[0]
        self.ok('restore', backup, '--session', curator)
        self.note(ticket, text='after second restore')
        self.assertEqual(self.watch_ok().count('kb #'), 1)
        self.assertNotEqual(json.loads(self.cursor.read_text())[0], previous)

    def test_upgrade_of_old_restore_preserves_integer_watch_cursor(self):
        ticket = self.add('old restore history', to='reader')
        seen = self.note(ticket, text='seen history')
        self.from_start(seen)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE meta SET value='older-restore' WHERE key='restore_generation'")
            conn.execute("DELETE FROM meta WHERE key='restore_note_id'")
        self.assertEqual(self.watch_ok(), '')
        self.note(ticket, text='first note after upgrade')
        self.assertEqual(self.watch_ok().count('kb #'), 1)

    def test_delayed_first_hook_after_restore_does_not_skip_current_notes(self):
        ticket, backup, curator = self.restore_setup()
        self.cursor.unlink()
        self.ok('restore', backup, '--session', curator)
        self.note(ticket, text='must survive the delayed old hook')
        out = self.watch_ok(env=dict(self.env, KB_FIRST_NOTE='9999', KB_GENERATION='old'))
        self.assertEqual(out.count('kb #'), 1)
        self.assertEqual(self.watch_ok(), '')

    def test_running_watcher_notices_in_place_restore(self):
        ticket, backup, curator = self.restore_setup()
        self.cursor.unlink()
        process = subprocess.Popen([sys.executable, str(TOOLS / 'kb-watch'), '--for', 'reader',
                                    '--until-first', '--interval', '0.05'], env=self.env,
                                   cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        try:
            deadline = time.monotonic() + 10
            while not self.cursor.exists():
                self.assertLess(time.monotonic(), deadline, 'watcher did not start')
                time.sleep(0.02)
            self.ok('restore', backup, '--session', curator)
            self.note(ticket, text='wake after in-place restore')
            out, err = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, err)
            self.assertEqual(out.count('kb #'), 1, out)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_relevance_joins_and_filters(self):
        direct = self.add('direct request', to='reader')
        anyone = self.add('any request', to='any')
        extra = self.add('extra addressee', to='curator')
        own = self.add('reader owns task', owner=self.reader)
        opened = self.add('reader opened request', owner=self.reader, to='writer')
        held = self.add('assigned to reader')
        self.ok('release', held, '--session', self.writer)
        self.ok('claim', held, '--session', self.reader)
        participated = self.add('reader participated task')
        self.note(participated, owner=self.reader)
        unrelated = self.add('unrelated task')
        last = self.query('SELECT coalesce(max(id), 0) FROM note')[0][0]
        for ticket in (direct, anyone, extra, own, opened, held, participated, unrelated):
            self.note(ticket)
        self.note(direct, owner=self.reader)  # no notification for own notes
        self.from_start(last)
        out = self.watch_ok()
        for ticket in (direct, anyone, own, opened, held, participated):
            self.assertIn('kb #%s writer comment:' % ticket, out)
        for ticket in (extra, unrelated):
            self.assertNotIn('kb #%s ' % ticket, out)
        self.assertEqual(out.count('kb #'), 6)
        self.assertNotIn(' reader comment:', out)
        self.from_start(last)
        out = self.watch_ok('--no-any', '--also-to', 'curator,another')
        self.assertNotIn('kb #%s ' % anyone, out)
        self.assertIn('kb #%s ' % extra, out)
        self.from_start(last)
        self.assertEqual(self.watch_ok('--ignore-author', 'someone,writer'), '')

    def test_session_ended_is_skipped(self):
        ticket = self.add('owned task')
        self.note(ticket, owner=self.reader)
        self.from_start(self.query('SELECT max(id) FROM note')[0][0])
        self.note(ticket)
        self.ok('session-ended', '--session', self.writer)
        self.assertIn(('session-ended',), self.query('SELECT kind FROM note WHERE ticket_id=?', ticket))
        out = self.watch_ok()
        self.assertEqual(out.count('kb #'), 1)
        self.assertNotIn('session-ended', out)

    def test_event_is_skipped(self):
        ticket = self.add('labelled task', owner=self.reader)
        self.ok('label', ticket, 'reviewed', '--session', self.writer)
        self.assertIn(('event',), self.query('SELECT kind FROM note WHERE ticket_id=?', ticket))
        self.note(ticket)
        self.from_start()
        out = self.watch_ok()
        self.assertEqual(out.count('kb #'), 1)
        self.assertNotIn(' event:', out)

    def test_exec_environment_and_failed_delivery_retried(self):
        ticket = self.add('exec environment', to='reader')
        note = self.note(ticket)
        self.from_start()
        result = self.watch('--exec', 'exit 7')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(self.cursor.read_text()), 0)
        self.assertEqual(result.stdout, '')
        capture = Path(self.tmp) / 'capture.py'
        output = Path(self.tmp) / 'env.json'
        capture.write_text('import json, os, sys\n'
                           'with open(sys.argv[1], "w") as f:\n'
                           '    json.dump({k: os.environ[k] for k in '
                           '["KB_TICKET", "KB_NOTE", "KB_AUTHOR", "KB_KIND", "KB_TITLE"]}, f)\n')
        command = ' '.join(shlex.quote(str(p)) for p in (sys.executable, capture, output))
        self.watch_ok('--exec', command)
        self.assertEqual(json.loads(output.read_text()),
                         dict(KB_TICKET=str(ticket), KB_NOTE=str(note), KB_AUTHOR='writer',
                              KB_KIND='comment', KB_TITLE='exec environment'))
        self.assertEqual(self.watch_ok('--exec', command), '')


if __name__ == '__main__':
    unittest.main()
