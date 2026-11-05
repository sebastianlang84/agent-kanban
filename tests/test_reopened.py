"""A reopened request needs an answer from its current lifetime."""
import json
import sqlite3

from test_kb import KbCase, context


class ReopenedRequestTest(KbCase):
    def setUp(self):
        super().setUp()
        self.henv = dict(self.env, CODEX_SESSION_ID='opener-test')
        self.ok('name', 'opener', env=self.henv)
        self.opener = self.query("SELECT token FROM session WHERE name='opener'")[0][0]
        self.worker = self.start('worker')
        self.request = self.ok('add', 'question', '--type', 'request', '--to', 'worker',
                               '--session', self.opener)

    def answer(self, text='answer'):
        self.ok('claim', self.request, '--session', self.worker)
        self.ok('note', self.request, '--reply', text, '--session', self.worker)

    def close(self):
        return self.kb('close', self.request, '--result', 'done', '--session', self.opener)

    def reopen(self, *extra, stdin=''):
        self.ok('reopen', self.request, '--session', self.opener, *extra, stdin=stdin)

    def prompt(self):
        return context(self.ok('hook', 'prompt', '--harness', 'codex',
                               stdin=json.dumps(dict(session_id='opener-test', cwd=self.tmp))))

    def test_old_reply_neither_answers_nor_allows_close(self):
        self.answer()
        self.assertEqual(self.close().returncode, 0)
        self.reopen('--stdin', stdin='please reconsider')
        self.assertNotIn('answered', self.board_line(self.request))
        result = self.close()
        self.assertEqual(result.returncode, 1)
        self.assertIn('no reply note since its last reopen', result.stderr)
        self.answer('new answer')
        self.assertIn('answered', self.board_line(self.request))
        self.assertEqual(self.close().returncode, 0)

    def test_prompt_ignores_old_reply_until_new_answer(self):
        self.answer()
        self.assertEqual(self.close().returncode, 0)
        self.reopen()
        out = self.prompt()  # old reply was never delivered to this opener
        self.assertNotIn('reply by', out)
        self.assertNotIn('kb close', out)
        self.answer('new answer')
        self.assertIn('kb close %s' % self.request, self.prompt())

    def test_reopened_unanswered_request_can_be_withdrawn(self):
        self.answer()
        self.assertEqual(self.close().returncode, 0)
        self.reopen()
        self.ok('close', self.request, '--withdraw', '--session', self.opener)
        self.assertEqual(self.ticket(self.request)[0], 'abandoned')

    def test_chief_does_not_use_old_reply_for_curator_holder(self):
        curator, chief = self.start('curator'), self.start('chief')
        self.request = self.ok('add', 'curator question', '--type', 'request', '--to', 'curator',
                               '--session', self.opener)
        self.ok('note', self.request, '--reply', 'old decision', '--session', self.opener)
        self.assertEqual(self.close().returncode, 0)
        self.reopen()
        self.ok('claim', self.request, '--session', curator)
        result = self.kb('close', self.request, '--result', 'done', '--evidence', 'proof',
                         '--session', chief)
        self.assertEqual(result.returncode, 1)
        self.assertIn('held by curator without a reply', result.stderr)

    def test_reopen_watermark_rolls_back_when_note_fails(self):
        self.answer()
        self.assertEqual(self.close().returncode, 0)
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TRIGGER fail_reopen BEFORE INSERT ON note"
                         " WHEN NEW.text='reopened' BEGIN"
                         " SELECT RAISE(ABORT, 'injected failure'); END")
        self.assertEqual(self.code('reopen', self.request, '--session', self.opener), 1)
        self.assertEqual(self.query('SELECT status, reopened FROM ticket WHERE id=?',
                                    self.request), [('done', 0)])

    def test_migration_backfills_known_lifetime_markers(self):
        markers = [('close', 'done'), ('force-close', 'forced'), ('comment', 'abandoned'),
                   ('comment', 'reopened'), ('comment', 'curator approved: command')]
        tickets = []
        for kind, text in markers:
            tid = self.ok('add', text, '--new', '--type', 'request', '--to', 'worker',
                          '--session', self.opener)
            with sqlite3.connect(self.db) as conn:
                notes = [('reply', 'old answer'), (kind, text)]
                # Include reply -> close -> reply -> reopen: only the final answer counts.
                if kind == 'close':
                    notes += [('reply', 'answer while closed'), ('comment', 'reopened')]
                for note_kind, note_text in notes:
                    conn.execute('INSERT INTO note (ticket_id, author, time, kind, text)'
                                 ' VALUES (?, ?, ?, ?, ?)',
                                 (tid, self.worker, '2026-11-05T10:00:00Z', note_kind, note_text))
                last = conn.execute('SELECT max(id) FROM note').fetchone()[0]
            tickets.append((tid, last))
        with sqlite3.connect(self.db) as conn:
            conn.execute('ALTER TABLE ticket DROP COLUMN reopened')
            conn.execute('PRAGMA user_version=8')
        self.ok('board')  # migrate like the first command against a real old board
        for tid, marker in tickets:
            with self.subTest(marker=marker):
                self.assertEqual(self.query('SELECT reopened FROM ticket WHERE id=?', tid),
                                 [(marker,)])
                self.assertNotIn('answered', self.board_line(tid))
                self.assertEqual(self.code('close', tid, '--result', 'done',
                                           '--session', self.opener), 1)
