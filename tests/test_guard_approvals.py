"""Guard approval tracking and read-only approval hints."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import unittest
from unittest import mock
from test_setup import load_kb as load_kb_module


class GuardApprovalHookTest(unittest.TestCase):
    """Exercise the real guard with an in-memory board, without CLI or host access."""

    def setUp(self):
        self.kb = load_kb_module()
        self.conn = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(self.conn.close)
        self.conn.execute('PRAGMA foreign_keys=ON')
        patches = contextlib.ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(unittest.mock.patch.object(
            self.kb, 'host_installation', return_value='aaaaaaaaaaaaaaaa'))
        patches.enter_context(unittest.mock.patch.object(self.kb, 'connect', return_value=self.conn))
        patches.enter_context(unittest.mock.patch.object(
            self.kb, 'now', return_value='2026-11-05T12:00:00Z'))
        patches.enter_context(unittest.mock.patch.dict(os.environ, PI_SUBAGENT_DEPTH='0'))
        self.errors = patches.enter_context(unittest.mock.patch.object(self.kb, 'log_hook_error'))
        self.kb.migrate(self.conn)
        self.me = self.kb.insert_harness_session(
            self.conn, 'aaaaaaaaaaaaaaaa', 'claude', 'cc-1', 'agent', '')[0]
        self.other = self.kb.insert_harness_session(
            self.conn, 'aaaaaaaaaaaaaaaa', 'claude', 'cc-2', 'agent', '')[0]

    def request(self, status='todo', opener=None, kind='request', to='curator',
                labels='approval', reply=True):
        t = self.kb.now()
        tid = self.conn.execute(
            'INSERT INTO ticket (type, title, status, addressee, opener, labels, created,'
            ' updated, last_activity) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (kind, 'publish change', status, to, opener or self.me, labels, t, t, t)).lastrowid
        if reply:
            self.kb.add_note(self.conn, tid, self.other, 'reply', 'curator: approved')
        return tid

    def guard(self, command='git commit -m fix', hsid='cc-1', harness='claude'):
        data = json.dumps({'session_id': hsid, 'tool_name': 'Bash',
                           'tool_input': {'command': command}})
        out = io.StringIO()
        before = self.conn.total_changes
        with unittest.mock.patch.object(sys, 'stdin', io.StringIO(data)), \
                contextlib.redirect_stdout(out):
            self.assertEqual(self.kb.hook_main(['guard', '--harness', harness]), 0)
        self.errors.assert_not_called()
        self.assertEqual(self.conn.total_changes, before)  # the guard remains read only
        return out.getvalue()

    def test_own_answered_open_approval_counts_as_held_work(self):
        self.request()
        self.assertEqual(self.guard(), '')

    def test_just_closed_answered_approval_counts_as_held_work(self):
        tid = self.request(status='done')
        self.kb.add_note(self.conn, tid, self.me, 'close', 'done', t='2026-11-05T11:00:00Z')
        for command in ('git commit -m fix', 'git merge feature/x', 'git push',
                        'docker compose up -d'):
            with self.subTest(command=command):
                self.assertEqual(self.guard(command), '')

    def test_request_must_be_own_answered_approval_to_curator(self):
        cases = [
            ('unanswered', {'reply': False}, False),
            ('same-name sibling opener', {'opener': self.other}, False),
            ('task', {'kind': 'task'}, False),
            ('other addressee', {'to': 'agent'}, False),
            ('no label', {'labels': ''}, False),
            ('label substring', {'labels': 'approvals'}, False),
            ('multiple labels', {'labels': 'release,approval,review'}, True),
            ('doing request', {'status': 'doing'}, True),
            ('blocked request', {'status': 'blocked'}, True),
            ('abandoned request', {'status': 'abandoned'}, False),
            ('closed without close note', {'status': 'done'}, False),
        ]
        for index, (name, options, held) in enumerate(cases):
            with self.subTest(case=name):
                hsid = 'case-%d' % index
                self.me = self.kb.insert_harness_session(
                    self.conn, 'aaaaaaaaaaaaaaaa', 'claude', hsid, 'agent', '')[0]
                self.request(**options)
                out = self.guard(hsid=hsid)
                if held:
                    self.assertEqual(out, '')
                else:
                    self.assertIn('not blocking', out)
                    self.assertIn('own answered approval request', out)

    def test_old_close_is_not_extended_by_later_activity(self):
        tid = self.request(status='done')
        self.kb.add_note(self.conn, tid, self.me, 'close', 'done', t='2026-11-05T10:59:59Z')
        self.kb.add_note(self.conn, tid, self.me, 'comment', 'follow-up')
        self.conn.execute('UPDATE ticket SET updated=? WHERE id=?', (self.kb.now(), tid))
        self.assertIn('git commit', self.guard())

    def test_recent_close_without_reply_still_warns(self):
        tid = self.request(status='done', reply=False)
        self.kb.add_note(self.conn, tid, self.me, 'close', 'done')
        self.assertIn('git commit', self.guard())

    def test_abandoned_request_with_recent_close_still_warns(self):
        tid = self.request(status='abandoned')
        self.kb.add_note(self.conn, tid, self.me, 'close', 'done')
        self.assertIn('git commit', self.guard())

    def test_reopen_requires_a_fresh_reply_and_close(self):
        tid = self.request(status='done')
        self.kb.add_note(self.conn, tid, self.me, 'close', 'done')
        self.conn.execute('UPDATE ticket SET status=?, reopened=(SELECT max(id) FROM note)'
                          ' WHERE id=?', ('todo', tid))
        self.assertIn('git commit', self.guard())
        self.kb.add_note(self.conn, tid, self.other, 'reply', 'curator: approved again')
        self.assertEqual(self.guard(), '')
        self.conn.execute("UPDATE ticket SET status='done' WHERE id=?", (tid,))
        self.assertIn('git commit', self.guard())  # the earlier close is from the old lifecycle
        self.kb.add_note(self.conn, tid, self.me, 'close', 'done again')
        self.assertEqual(self.guard(), '')

    def test_assigned_doing_and_blocked_tasks_still_count(self):
        tid = self.request(kind='task', reply=False, labels='', to=None)
        for status in ('todo', 'doing', 'blocked', 'done', 'abandoned'):
            with self.subTest(status=status):
                self.conn.execute('UPDATE ticket SET status=?, assignee=? WHERE id=?',
                                  (status, self.me, tid))
                out = self.guard()
                if status in ('doing', 'blocked'):
                    self.assertEqual(out, '')
                else:
                    self.assertIn('not blocking', out)

    def test_harness_and_guest_installation_identity(self):
        codex = self.kb.insert_harness_session(
            self.conn, 'aaaaaaaaaaaaaaaa', 'codex', 'cc-1', 'agent', '')[0]
        self.request(opener=codex)
        self.assertEqual(self.guard(harness='codex'), '')
        self.assertIn('git commit', self.guard())
        guest = self.kb.insert_harness_session(
            self.conn, 'bbbbbbbbbbbbbbbb', 'claude', 'cc-1', 'agent', '')[0]
        with unittest.mock.patch.object(self.kb, 'GUEST', {'inst': 'bbbbbbbbbbbbbbbb'}):
            self.assertIn('git commit', self.guard())
            self.request(opener=guest)
            self.assertEqual(self.guard(), '')
        self.assertIn('git commit', self.guard())

    def test_unknown_session_and_unguarded_commands_stay_silent(self):
        self.assertEqual(self.guard(hsid='unknown'), '')
        self.kb.connect.reset_mock()
        for command in ('git status', 'echo "git push"', 'git merge-base main HEAD'):
            self.assertEqual(self.guard(command), '')
        self.kb.connect.assert_not_called()

    def test_database_error_stays_nonblocking(self):
        self.kb.connect.side_effect = sqlite3.OperationalError('busy')
        with unittest.mock.patch.object(sys, 'stdin', io.StringIO('')), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.kb.hook_main([
                'guard', '--harness', 'claude', '--id', 'cc-1', '--source', 'git commit']), 0)
        self.assertEqual(out.getvalue(), '')
        self.errors.assert_called_once()
