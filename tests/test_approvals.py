"""Revision-bound decisions: the real CLI, isolated from the live board."""
import os
import sqlite3

from test_kb import KbCase


class ApprovalTest(KbCase):
    def setUp(self):
        super().setUp()
        self.opener = self.start('opener')
        self.curator = self.start('curator')
        self.chief = self.start('chief')
        self.request = self.ok('add', 'Run command', '--type', 'request', '--to', 'curator',
                               '--label', 'approval', '--session', self.opener,
                               '--note', 'exact command')

    def rev(self):
        return self.ok('show', self.request).splitlines()[0].split(': ')[1]

    def decide(self, verdict='approve', who=None, rev=None, **kw):
        return self.kb(verdict, self.request, '--rev', rev or self.rev(), '--stdin',
                       '--session', who or self.curator, stdin='Run this exact command', **kw)

    def test_revision_mismatch_and_reply_is_not_approval(self):
        rev = self.rev()
        self.ok('note', self.request, '--reply', 'approved', '--session', self.opener)
        self.assertEqual(self.code('approval', self.request), 1)
        self.assertEqual(self.decide(rev=rev).returncode, 1)
        self.assertEqual(self.query('SELECT count(*) FROM decision'), [(0,)])
        self.assertEqual(self.decide().returncode, 0)
        self.assertEqual(self.ok('approval', self.request), 'Run this exact command')
        self.assertEqual(self.query('SELECT verdict, author FROM decision'),
                         [('approve', self.curator)])
        self.ok('note', self.request, 'later note', '--session', self.opener)
        self.assertEqual(self.code('approval', self.request), 1)

    def test_labels_and_close_reopen_void_approval(self):
        self.assertEqual(self.decide().returncode, 0)
        self.ok('label', self.request, 'sudo', '--session', self.opener)
        self.assertEqual(self.code('approval', self.request), 1)
        self.assertEqual(self.decide().returncode, 0)
        self.ok('close', self.request, '--session', self.opener, '--result', 'done')
        self.assertEqual(self.code('approval', self.request), 1)
        self.ok('reopen', self.request, '--session', self.opener)
        self.assertEqual(self.code('approval', self.request), 1)

    def test_deny_closes_and_audit_is_append_only(self):
        self.assertEqual(self.decide('deny').returncode, 0)
        self.assertEqual(self.ticket(self.request)[0], 'done')
        self.assertEqual(self.code('approval', self.request), 1)
        with sqlite3.connect(self.db) as conn:
            for sql in ('DELETE FROM decision', "UPDATE decision SET verdict='approve'"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                    conn.execute(sql)

    def test_orchestrator_file_only_and_cannot_decide_own_name(self):
        self.assertEqual(self.decide(who=self.chief,
                                    env=dict(self.env, KB_ORCHESTRATOR='chief')).returncode, 1)
        conf = os.path.join(self.home, '.config', 'agent-kanban', 'config')
        with open(conf, 'a') as f:
            f.write('\nKB_ORCHESTRATOR=chief\n')
        self.assertEqual(self.decide(who=self.chief).returncode, 0)
        self.request = self.ok('add', 'Own command', '--new', '--type', 'request',
                               '--to', 'curator', '--label', 'approval', '--session', self.chief)
        self.assertEqual(self.decide(who=self.chief).returncode, 1)
        other_chief = self.start('chief')
        self.assertEqual(self.decide(who=other_chief).returncode, 1)
        self.assertEqual(self.decide().returncode, 0)

    def test_guest_cannot_decide_even_with_configured_name(self):
        self.assertEqual(self.decide(env=dict(self.env, KB_HOST='nowhere')).returncode, 1)
        # The forced-command endpoint has its own allowlist, independent of guest_main.
        import runpy
        kb = runpy.run_path(os.path.join(os.path.dirname(__file__), '..', 'kb'))
        self.assertNotIn('approve', kb['GUEST_COMMANDS'])
        self.assertNotIn('deny', kb['GUEST_COMMANDS'])
        self.assertIn('approval', kb['GUEST_COMMANDS'])

    def test_restore_and_recovery_void_decisions(self):
        self.assertEqual(self.decide().returncode, 0)
        backup = os.path.join(self.tmp, 'backup.sqlite')
        self.ok('backup', backup)
        self.ok('restore', backup, '--session', self.curator)
        self.assertEqual(self.code('approval', self.request), 1)
        self.assertEqual(self.decide().returncode, 1)
        self.ok('recover', '--done', '--session', self.curator)
        self.assertEqual(self.code('approval', self.request), 1)
        self.assertEqual(self.decide().returncode, 0)
        self.assertEqual(self.code('approval', self.request), 0)

    def test_only_open_approval_requests_can_be_decided(self):
        self.assertEqual(self.decide(who=self.opener).returncode, 1)
        self.request = self.ok('add', 'Task', '--session', self.opener)
        self.assertEqual(self.decide().returncode, 1)
        self.request = self.ok('add', 'Ordinary request', '--type', 'request', '--to', 'any',
                               '--session', self.opener)
        self.assertEqual(self.decide().returncode, 1)
