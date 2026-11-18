"""Exercise board helpers against a temporary database populated by the real kb CLI."""
import ast
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
        self.concierge = self.start('concierge')
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

    def test_remote_and_concierge_use_configurable_paths(self):
        state = Path(self.tmp) / 'helper state'
        env = dict(self.env, KB_STATE_DIR=str(state), SSH_ORIGINAL_COMMAND='kb board --json')
        # The default executable resolves to this checkout even outside its directory.
        result = subprocess.run([sys.executable, str(TOOLS / 'kb-remote'), 'concierge'],
                                env=env, cwd=self.home, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        token = (state / 'concierge.token').read_text().strip()
        self.assertEqual(len(token), 32)
        result = subprocess.run([str(TOOLS / 'kb-concierge'), 'whoami'], env=env,
                                cwd=self.home, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('concierge', result.stdout)
        # An alternate CLI can live at a path containing spaces; it is not shell-expanded.
        stub = Path(self.tmp) / 'alternate cli'
        stub.write_text('print("alternate CLI")\n')
        for tool, args in (('kb-remote', ['concierge']), ('kb-concierge', ['whoami'])):
            with self.subTest(tool=tool):
                result = subprocess.run([str(TOOLS / tool), *args],
                                        env=dict(env, KB_BIN=str(stub)), cwd=self.home,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'alternate CLI\n')

    def test_concierge_counts_follow_generation_but_keep_daily_budget(self):
        path = TOOLS / 'kb-agent-concierge'
        tree = ast.parse(path.read_text(), filename=str(path))
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'daily_counts']
        namespace = {}
        exec(compile(ast.Module(body=funcs, type_ignores=[]), str(path), 'exec'), namespace)
        counts = {'day': '2026-11-16', 'total': 9, 'tickets': {'1': 6}, 'attempts': {'5': 3}}
        reset = namespace['daily_counts']
        self.assertEqual(reset(dict(counts), '2026-11-16', '0')['attempts'], {'5': 3})
        changed = reset(dict(counts), '2026-11-16', 'new-generation')
        self.assertEqual(changed, dict(day='2026-11-16', total=9, tickets={}, attempts={},
                                       generation='new-generation'))
        self.assertEqual(reset(changed, '2026-11-17', 'new-generation')['total'], 0)

    def test_concierge_rechecks_generation_before_counter_decisions(self):
        import contextlib
        import fcntl
        path = TOOLS / 'kb-agent-concierge'
        tree = ast.parse(path.read_text(), filename=str(path))
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == 'counter_generation']
        namespace = dict(contextlib=contextlib, fcntl=fcntl, sqlite3=sqlite3, sys=sys,
                         DB=Path(self.db), generation='0')
        exec(compile(ast.Module(body=funcs, type_ignores=[]), str(path), 'exec'), namespace)
        # The startup check saw 0, but a restore occurs before counters are read/written.
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE meta SET value='restored' WHERE key='restore_generation'")
        with self.assertRaises(SystemExit) as stopped:
            with namespace['counter_generation']():
                self.fail('stale invocation reached counter decisions')
        self.assertEqual(stopped.exception.code, 0)

    def test_concierge_ignores_hook_from_replaced_generation(self):
        # No token or Claude binary exists in this HOME: a stale trigger must exit before either.
        result = subprocess.run([sys.executable, str(TOOLS / 'kb-agent-concierge')],
                                env=dict(self.env, KB_TICKET='1', KB_NOTE='1', KB_GENERATION='old'),
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((Path(self.tmp) / 'concierge.token').exists())

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

    def concierge_reads(self, ticket):
        # Import only the pure DB functions: importing the entry point would invoke Claude,
        # mutate its token/counters and run a real agent. SQL is taken from the actual helper.
        path = TOOLS / 'kb-agent-concierge'
        tree = ast.parse(path.read_text(), filename=str(path))
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in ('newest_own_note', 'ticket_state', 'open_escalation')]
        self.assertEqual(len(funcs), 3)
        namespace = dict(sqlite3=sqlite3, DB=self.db, ticket=str(ticket))
        exec(compile(ast.Module(body=funcs, type_ignores=[]), str(path), 'exec'), namespace)
        return namespace['newest_own_note'], namespace['ticket_state']

    def test_concierge_finds_its_open_escalation(self):
        path = TOOLS / 'kb-agent-concierge'
        tree = ast.parse(path.read_text(), filename=str(path))
        func = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'open_escalation']
        namespace = dict(sqlite3=sqlite3, DB=self.db)
        exec(compile(ast.Module(body=func, type_ignores=[]), str(path), 'exec'), namespace)
        find = namespace['open_escalation']
        title = 'concierge: ticket 7 needs attention'
        self.assertEqual(find(title), 0)
        self.add(title, owner=self.writer, to='curator')  # same title, other opener
        self.assertEqual(find(title), 0)
        self.add(title, owner=self.concierge, to='concierge')  # other addressee
        self.assertEqual(find(title), 0)
        mine = self.add(title, owner=self.concierge, to='curator')
        self.add('concierge: ticket 8 needs attention', owner=self.concierge, to='curator')
        self.assertEqual(find(title), mine)
        self.ok('close', mine, '--withdraw', '--session', self.concierge)
        self.assertEqual(find(title), 0)

    def test_concierge_reads_author_join_kind_and_ticket(self):
        ticket = self.add('concierge request', to='concierge')
        other = self.add('another request', to='concierge')
        newest, state = self.concierge_reads(ticket)
        self.assertEqual(newest(), 0)
        self.assertEqual(newest('reply'), 0)
        self.assertEqual(state(), ('request', 'concierge', 'todo'))
        self.note(ticket)  # somebody else's note is not concierge's trace
        self.assertEqual(newest(), 0)
        comment = self.note(ticket, owner=self.concierge)
        self.assertEqual(newest(), comment)
        self.assertEqual(newest('reply'), 0)
        self.ok('claim', ticket, '--session', self.concierge)
        self.assertEqual(state(), ('request', 'concierge', 'doing'))
        reply = self.note(ticket, owner=self.concierge, kind='reply')
        self.note(other, owner=self.concierge)  # scope stays on the triggering ticket
        self.assertEqual(newest(), reply)
        self.assertEqual(newest('reply'), reply)
        self.ok('close', ticket, '--session', self.writer,
                stdin='where: temporary board\nchanged: replied\nopen: nothing\n')
        self.assertEqual(state(), ('request', 'concierge', 'done'))
        self.assertEqual(newest('reply'), reply)
        task = self.add('concierge owned task', owner=self.concierge)
        _, task_state = self.concierge_reads(task)
        self.assertEqual(task_state(), ('task', None, 'doing'))

    def escalate(self, ticket, note, kb_bin=None):
        # The real script, with HOME pointing at the test home: attempts are preset to the limit,
        # so it escalates and exits before it would start Claude.
        logs = Path(self.tmp) / 'agent-concierge'
        logs.mkdir(parents=True, exist_ok=True)
        import datetime
        (logs / 'counts.json').write_text(json.dumps(
            {'day': datetime.date.today().isoformat(), 'total': 0, 'tickets': {},
             'attempts': {str(note): 3}}))
        env = dict(self.env, KB_TICKET=str(ticket), KB_NOTE=str(note),
                   KB_BIN=str(kb_bin or (TOOLS.parent / 'kb')))
        return subprocess.run([sys.executable, str(TOOLS / 'kb-agent-concierge')], env=env,
                              cwd=self.tmp, capture_output=True, text=True, timeout=30)

    def test_concierge_escalates_once_per_ticket(self):
        first = self.escalate(133, 1)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.escalate(133, 2)
        self.assertEqual(second.returncode, 0, second.stderr)
        other = self.escalate(134, 3)
        self.assertEqual(other.returncode, 0, other.stderr)
        rows = self.query("SELECT id, title FROM ticket WHERE type='request' AND addressee='curator'"
                          " ORDER BY id")
        self.assertEqual([r[1] for r in rows], ['concierge: ticket 133 needs attention',
                                                'concierge: ticket 134 needs attention'])
        notes = self.query('SELECT text FROM note WHERE ticket_id=? ORDER BY id', rows[0][0])
        self.assertEqual(len(notes), 2)
        self.assertIn('note 2', notes[1][0])
        # A failed note keeps the trigger for a retry instead of counting it as handled.
        stub = Path(self.tmp) / 'kb-note-fails'
        stub.write_text('import os, sys\n'  # the script runs it as `python3 <kb> ...`
                        'if sys.argv[1] == "note": sys.exit("locked")\n'
                        'os.execvp("python3", ["python3", %r] + sys.argv[1:])\n'
                        % str(TOOLS.parent / 'kb'))
        failed = self.escalate(133, 4, kb_bin=stub)
        self.assertEqual(failed.returncode, 1)
        self.assertIn('locked', failed.stderr)
        self.assertEqual(len(self.query('SELECT id FROM note WHERE ticket_id=?', rows[0][0])), 2)


if __name__ == '__main__':
    unittest.main()
