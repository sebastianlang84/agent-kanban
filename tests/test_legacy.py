"""A -> B migration contract, using the actual legacy CLI to create the input board."""
import datetime
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
import unittest

from tests.test_kb import KB, KbCase

LEGACY = Path(__file__).resolve().parents[1] / 'legacy' / 'kb_a.py'


def iso(epoch):
    return (datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
            .strftime('%Y-%m-%dT%H:%M:%SZ')) if epoch is not None else None


class LegacyCase(KbCase):
    def setUp(self):
        super().setUp()
        self.env.pop('CODEX_THREAD_ID', None)
        self.env.pop('KB_HARNESS', None)
        self.env.pop('KB', None)

    def legacy(self, *args, as_=None, stdin='', env=None):
        e = dict(self.env, KB_HARNESS='none')
        for key in ('KB_SESSION', 'CLAUDE_CODE_SESSION_ID', 'CODEX_SESSION_ID',
                    'CODEX_THREAD_ID', 'PI_SESSION_ID', 'KB', 'KB_HARNESS'):
            e.pop(key, None)
        e.update(KB_HARNESS='none')
        e.update(env or {})
        command = [sys.executable, str(LEGACY)]
        if as_:
            command += ['--session', as_]
        result = subprocess.run(command + [str(a) for a in args], env=e, cwd=self.tmp,
                                input=stdin, capture_output=True, text=True,
                                start_new_session=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def legacy_hook(self, event, harness, sid):
        return self.legacy('hook', event, '--harness', harness,
                           stdin=json.dumps(dict(session_id=sid, cwd=self.tmp)))

    def legacy_add(self, title, token, *args):
        out = self.legacy('add', '--new', title, *args, as_=token)
        return int(re.search(r'#(\d+)', out).group(1))

    def build_legacy(self):
        opener = self.legacy('start', '--as', 'opener')
        helper = self.legacy('start', '--as', 'helper')
        ended = self.legacy('start', '--as', 'ended')
        self.assertRegex(opener, r'^[0-9a-f]{32}$')
        self.legacy_hook('start', 'claude', 'migration-claude')
        self.legacy_hook('start', 'codex', 'migration-codex')
        self.legacy_hook('start', 'pi', 'migration-pi')
        open_task = self.legacy_add('open parser task', opener)
        claimed_task = self.legacy_add('claimed import task', helper, '--claim')
        closed_task = self.legacy_add('finished export task', helper, '--claim')
        self.legacy('note', closed_task, 'ordinary note\nwith unicode: Grüße', as_=helper)
        self.legacy('close', closed_task, '--where', 'test board', '--changed', 'exported',
                    '--open', 'nothing', as_=helper)
        self.legacy('label', open_task, 'backlog', 'planning', as_=opener)
        open_request = self.legacy_add('pending question', opener, '--type', 'request',
                                       '--to', 'helper', '--note', 'please inspect')
        answered = self.legacy_add('answered consultation', opener, '--type', 'request',
                                  '--to', 'helper')
        self.legacy('claim', answered, as_=helper)
        self.legacy('note', answered, '--reply', 'answer remains open', as_=helper)
        closed_request = self.legacy_add('accepted review', opener, '--type', 'request',
                                         '--to', 'helper', '--label', 'review')
        self.legacy('claim', closed_request, as_=helper)
        self.legacy('note', closed_request, '--reply', 'review complete', as_=helper)
        self.legacy('close', closed_request, '--result', 'accepted answer', as_=opener)
        withdrawn = self.legacy_add('cancelled request', opener, '--type', 'request', '--to', 'any')
        self.legacy('close', withdrawn, '--withdraw', as_=opener)
        released = self.legacy_add('ended owner task', ended, '--claim')
        self.legacy('end', as_=ended)
        self.legacy('board', '--all')  # A releases ended owners, leaving an event by pseudo-author kb
        # Prompt hints populate A's shown table; migration must discard them.
        self.legacy_add('hook reader request', opener, '--type', 'request',
                        '--to', Path(self.tmp).name)
        self.legacy_hook('prompt', 'claude', 'migration-claude')
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertGreater(conn.execute('SELECT count(*) FROM shown').fetchone()[0], 0)
            self.assertEqual(set(r[0] for r in conn.execute('SELECT DISTINCT kind FROM notes')),
                             {'note', 'reply', 'result', 'event'})
            self.assertIsNotNone(conn.execute('SELECT ended_at FROM sessions WHERE token=?',
                                              (ended,)).fetchone()[0])
            # Fixture-only edge cases not expressible through the non-terminal CLI:
            # curator identity and a NULL repo from older A rows. All tickets/notes came from A.
            source = conn.execute('SELECT * FROM sessions WHERE token=?', (opener,)).fetchone()
            conn.execute('INSERT INTO sessions (id, harness, name, cwd, repo, host, token,'
                         ' started_at, last_activity) VALUES (?,?,?,?,?,?,?,?,?)',
                         ('curator', 'human', 'curator', source['cwd'], source['repo'],
                          source['host'], None, source['started_at'], source['last_activity']))
            conn.execute('UPDATE tickets SET repo=NULL WHERE id=?', (open_request,))
            # Ensure max(last_activity, closed_at) is tested in both directions.
            conn.execute('UPDATE tickets SET last_activity=closed_at+10 WHERE id=?', (closed_task,))
            # Sequences may exceed surviving ids after deletions; they must never shrink.
            conn.execute("UPDATE sqlite_sequence SET seq=seq+100 WHERE name IN ('tickets','notes')")
            snapshot = {table: [dict(r) for r in conn.execute('SELECT * FROM ' + table)]
                        for table in ('sessions', 'tickets', 'notes')}
            snapshot['sequence'] = dict(conn.execute('SELECT name, seq FROM sqlite_sequence'))
        return snapshot, opener

    def test_migration_preserves_sessions_tickets_notes_and_tokens(self):
        before, old_token = self.build_legacy()
        lower = iso(datetime.datetime.now(datetime.timezone.utc).timestamp())
        # This opens A in place; no import command or separate destination database.
        out = self.ok('board', '--all')
        upper = iso(datetime.datetime.now(datetime.timezone.utc).timestamp())
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 6)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'session', 'ticket', 'note', 'session_event', 'hint_seen', 'meta'} <= tables)
            self.assertFalse({'sessions', 'tickets', 'notes', 'shown'} & tables)
            inst = conn.execute("SELECT value FROM meta WHERE key='installation'").fetchone()[0]
            self.assertTrue(inst)
            hints_since = conn.execute("SELECT value FROM meta WHERE key='hints_since'").fetchone()[0]
            self.assertLessEqual(lower, hints_since)
            self.assertLessEqual(hints_since, upper)
            self.assertEqual(conn.execute('SELECT count(*) FROM hint_seen').fetchone()[0], 0)
            mapping = {}
            sessions = [dict(r) for r in conn.execute('SELECT * FROM session')]
            self.assertEqual(len(sessions), len(before['sessions']) + 1)  # pseudo-author kb
            for old in before['sessions']:
                with self.subTest(session=old['id']):
                    rows = [s for s in sessions if s['name'] == old['name']
                            and s['harness'] == ('human' if old['id'] == 'curator'
                                                else old['id'].split(':', 1)[0])]
                    if old['id'].startswith(('claude:', 'codex:', 'pi:')):
                        rows = [s for s in rows if s['harness_session'] == old['id'].split(':', 1)[1]]
                    if old['token']:
                        rows = [s for s in rows if s['token'] == old['token']]
                    self.assertEqual(len(rows), 1)
                    new = rows[0]
                    mapping[old['id']] = new['token']
                    self.assertRegex(new['token'], r'^[0-9a-f]+$')
                    self.assertEqual(new['host'], old['host'])
                    self.assertEqual(new['worktree'], old['cwd'])
                    self.assertEqual(new['started'], iso(old['started_at']))
                    self.assertEqual(new['last_seen'], iso(old['last_activity']))
                    self.assertEqual(new['installation'], inst)
                    if old['id'].startswith('cli:') or old['id'] == 'curator':
                        self.assertEqual(new['harness_session'], '')
            system = [s for s in sessions if s['name'] == 'kb' and s['harness'] == 'kb']
            self.assertEqual(len(system), 1)
            mapping['kb'] = system[0]['token']
            replies = {n['ticket_id'] for n in before['notes'] if n['kind'] == 'reply'}
            for old in before['tickets']:
                with self.subTest(ticket=old['id']):
                    new = dict(conn.execute('SELECT * FROM ticket WHERE id=?', (old['id'],)).fetchone())
                    status = {'open': 'todo', 'claimed': 'doing', 'closed': 'done'}[old['status']]
                    if old['status'] == 'closed' and old['type'] == 'request' and old['id'] not in replies:
                        status = 'abandoned'
                    self.assertEqual(new['status'], status)
                    for field in ('type', 'title', 'labels'):
                        self.assertEqual(new[field], old[field])
                    self.assertEqual(new['repo'], old['repo'] or '')
                    self.assertEqual(new['addressee'], old['to_name'])
                    self.assertEqual(new['opener'], mapping[old['creator_session']])
                    self.assertEqual(new['assignee'], mapping.get(old['owner_session']))
                    self.assertEqual(new['created'], iso(old['created_at']))
                    self.assertEqual(new['claimed_at'], iso(old['claimed_at']))
                    self.assertEqual(new['last_activity'], iso(old['last_activity']))
                    self.assertEqual(new['updated'], iso(max(old['last_activity'], old['closed_at'] or 0)))
                    self.assertIn(old['title'], out)
            self.assertEqual(conn.execute('SELECT count(*) FROM ticket').fetchone()[0], len(before['tickets']))
            self.assertEqual(conn.execute('SELECT count(*) FROM note').fetchone()[0], len(before['notes']))
            for old in before['notes']:
                with self.subTest(note=old['id']):
                    new = dict(conn.execute('SELECT * FROM note WHERE id=?', (old['id'],)).fetchone())
                    self.assertEqual(new['ticket_id'], old['ticket_id'])
                    self.assertEqual(new['author'], mapping[old['session_id']])
                    self.assertEqual(new['text'], old['body'])
                    self.assertEqual(new['time'], iso(old['created_at']))
                    self.assertEqual(new['kind'], dict(note='comment', reply='reply',
                                                       result='close', event='event')[old['kind']])
            sequences = dict(conn.execute('SELECT name, seq FROM sqlite_sequence'))
            for old, new in (('tickets', 'ticket'), ('notes', 'note')):
                self.assertGreaterEqual(sequences[new], before['sequence'][old])
            rows_after = {table: conn.execute('SELECT * FROM ' + table).fetchall()
                          for table in ('session', 'ticket', 'note')}
        # Existing 32-hex tokens work with the legacy global --session spelling too.
        self.assertIn('opener', self.ok('--session', old_token, 'whoami'))
        self.assertIn('ordinary note', self.ok('show', next(n['ticket_id'] for n in before['notes']
                                               if n['body'].startswith('ordinary note'))))
        self.ok('board', '--all')
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            for table, rows in rows_after.items():
                if table == 'session':  # whoami may refresh last_seen; tokens must stay stable
                    self.assertEqual([r['token'] for r in rows],
                                     [r['token'] for r in conn.execute('SELECT * FROM session')])
                else:
                    self.assertEqual(rows, conn.execute('SELECT * FROM ' + table).fetchall())
        added = int(self.ok('add', 'post-migration task', '--new', '--session', old_token))
        self.assertGreater(added, before['sequence']['tickets'])
        self.ok('note', added, '--session', old_token, stdin='post-migration note')
        self.assertGreater(self.query('SELECT max(id) FROM note')[0][0], before['sequence']['notes'])

    def test_same_second_legacy_request_is_not_hinted_again(self):
        opener = self.legacy('start', '--as', 'opener')
        self.legacy_hook('start', 'claude', 'same-second')
        ticket = self.legacy_add('already shown legacy request', opener, '--type', 'request',
                                  '--to', Path(self.tmp).name)
        self.legacy_hook('prompt', 'claude', 'same-second')
        created = self.query('SELECT created_at FROM tickets WHERE id=?', ticket)[0][0]
        # Freeze Python and SQLite clocks in the CLI subprocess: creation and migration
        # share a second regardless of scheduler speed. Exercise the real main/migrations.
        code = """
import importlib.machinery, importlib.util, sqlite3, sys
loader = importlib.machinery.SourceFileLoader('merged_kb', sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
kb = importlib.util.module_from_spec(spec)
loader.exec_module(kb)
frozen = sys.argv[2]
kb.now = lambda: frozen
original_connect = sqlite3.connect
def connect(*args, **kwargs):
    conn = original_connect(*args, **kwargs)
    conn.create_function('strftime', -1, lambda *args: frozen)
    return conn
sqlite3.connect = connect
sys.exit(kb.main(sys.argv[3:]))
"""
        def frozen_cli(*args, stdin=''):
            result = subprocess.run([sys.executable, '-c', code, KB, iso(created), *args],
                                    input=stdin, env=self.env, cwd=self.tmp,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout
        frozen_cli('board')
        self.assertEqual(self.query("SELECT value FROM meta WHERE key='hints_since'")[0][0],
                         iso(created))
        out = frozen_cli('hook', 'prompt', '--harness', 'claude',
                         stdin=json.dumps(dict(session_id='same-second', cwd=self.tmp)))
        self.assertNotIn('already shown legacy request', out)

    def test_failed_migration_rolls_back_in_place(self):
        opener = self.legacy('start', '--as', 'opener')
        self.legacy_add('rollback fixture', opener)
        with sqlite3.connect(self.db) as conn:
            # SQLite accepts a nonnumeric value in a REAL column. Fail after creating
            # the B tables so the whole migration's transaction, including DDL, is tested.
            conn.execute("UPDATE tickets SET created_at='invalid epoch'")
            before = list(conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name"))
            data = {table: conn.execute('SELECT * FROM ' + table).fetchall()
                    for table in ('sessions', 'tickets', 'notes', 'shown', 'sqlite_sequence')}
        result = self.kb('board')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertEqual(before, list(conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY name")))
            for table, rows in data.items():
                self.assertEqual(rows, conn.execute('SELECT * FROM ' + table).fetchall())
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')


if __name__ == '__main__':
    unittest.main()
