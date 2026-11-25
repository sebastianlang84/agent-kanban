"""Revision-bound decisions: the real CLI, isolated from the live board."""
import os
import sqlite3
import subprocess
import unittest

from test_kb import KbCase


class ApprovalTest(KbCase):
    def setUp(self):
        super().setUp()
        self.opener = self.start('opener')
        self.curator = self.start('curator')
        self.coordinator = self.start('coordinator')
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
        self.assertEqual(self.decide(who=self.coordinator,
                                    env=dict(self.env, KB_ORCHESTRATOR='coordinator')).returncode, 1)
        conf = os.path.join(self.home, '.config', 'agent-kanban', 'config')
        with open(conf, 'a') as f:
            f.write('\nKB_ORCHESTRATOR=coordinator\n')
        self.assertEqual(self.decide(who=self.coordinator).returncode, 0)
        self.request = self.ok('add', 'Own command', '--new', '--type', 'request',
                               '--to', 'curator', '--label', 'approval', '--session', self.coordinator)
        self.assertEqual(self.decide(who=self.coordinator).returncode, 1)
        other_coordinator = self.start('coordinator')
        self.assertEqual(self.decide(who=other_coordinator).returncode, 1)
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


class CommitBoundApprovalTest(KbCase):
    """kb 901: an approval request filed in a git worktree binds its approval to HEAD."""
    GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid',
               'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, 'repo')
        os.mkdir(self.repo)
        self.git('init', '-q', '-b', 'main')
        self.commit('one')
        self.opener = self.start('opener')
        self.curator = self.start('curator')

    def git(self, *args):
        return subprocess.run(['git', '-C', self.repo] + list(args), check=True,
                              env=dict(self.env, **self.GIT_ENV), stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, universal_newlines=True).stdout.strip()

    def commit(self, msg, *extra):
        self.git('commit', '-q', '--allow-empty', '-m', msg, *extra)
        return self.git('rev-parse', 'HEAD')

    def request(self, *extra):
        return self.ok('add', 'Push it', '--new', '--type', 'request', '--to', 'curator',
                       '--label', 'approval', '--session', self.opener, '--note', 'git push',
                       *extra, cwd=self.repo)

    def approve(self, tid):
        rev = self.ok('show', tid).splitlines()[0].split(': ')[1]
        self.assertEqual(self.kb('approve', tid, '--rev', rev, '--stdin', '--session',
                                 self.curator, stdin='push this').returncode, 0)

    def show(self, tid):
        return dict(l.split(': ', 1) for l in self.ok('show', tid).splitlines()[:21] if ': ' in l)

    def test_new_commit_amend_and_rebase_void_approval(self):
        head = self.git('rev-parse', 'HEAD')
        tid = self.request()
        self.assertEqual(self.show(tid)['commit'], head)
        self.approve(tid)
        self.assertEqual(self.ok('approval', tid, cwd=self.repo), 'push this')
        self.commit('two')
        r = self.kb('approval', tid, cwd=self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn('code changed since approval', r.stderr)
        self.assertNotIn(head[:12], r.stderr)
        self.git('reset', '-q', '--hard', head)
        self.assertEqual(self.ok('approval', tid, cwd=self.repo), 'push this')
        self.commit('one amended', '--amend')
        self.assertEqual(self.code('approval', tid, cwd=self.repo), 1)
        self.git('reset', '-q', '--hard', head)
        self.git('checkout', '-q', '--orphan', 'base')
        self.commit('base')
        self.git('checkout', '-q', 'main')
        self.write('f', 'x')
        self.git('add', 'f')
        self.commit('with file')
        self.git('rebase', '-q', 'base')
        self.assertEqual(self.code('approval', tid, cwd=self.repo), 1)
        self.git('checkout', '-q', '--detach', head)
        self.assertEqual(self.ok('approval', tid, cwd=self.repo), 'push this')
        # Outside any worktree there is no code to compare with.
        self.assertEqual(self.code('approval', tid), 1)

    def write(self, name, text):
        with open(os.path.join(self.repo, name), 'w') as f:
            f.write(text)

    def test_uncommitted_changes_are_bound_too(self):
        self.write('a', 'reviewed')
        tid = self.request()
        self.assertIn('with uncommitted changes', self.show(tid)['commit'])
        self.approve(tid)
        self.assertEqual(self.ok('approval', tid, cwd=self.repo), 'push this')
        self.write('a', 'changed after approval')
        self.assertEqual(self.code('approval', tid, cwd=self.repo), 1)
        self.write('a', 'reviewed')
        self.write('new', 'untracked')
        self.assertEqual(self.code('approval', tid, cwd=self.repo), 1)
        os.remove(os.path.join(self.repo, 'new'))
        self.git('add', 'a')  # staging alone does not change the code
        self.assertEqual(self.ok('approval', tid, cwd=self.repo), 'push this')
        # A clean request does not pass once edits appear.
        os.remove(os.path.join(self.repo, 'a'))
        clean = self.request()
        self.approve(clean)
        self.git('reset', '-q')
        self.write('b', 'edit')
        self.assertEqual(self.code('approval', clean, cwd=self.repo), 1)

    def test_unbound_requests_keep_working(self):
        outside = self.ok('add', 'Run sudo', '--type', 'request', '--to', 'curator',
                          '--label', 'approval', '--session', self.opener)
        self.assertIn('none', self.show(outside)['commit'])
        explicit = self.request('--commit', 'none')
        sudo = self.request('--label', 'sudo')
        self.assertIn('none', self.show(sudo)['commit'])
        for tid in (outside, explicit, sudo):
            self.approve(tid)
            self.commit('more %s' % tid)
            self.assertEqual(self.ok('approval', tid, cwd=self.repo), 'push this')

    def test_commit_is_part_of_the_revision_and_only_for_approvals(self):
        self.commit('two')
        first = self.request('--commit', 'HEAD~1')
        second = self.request()
        unbound = self.request('--commit', 'none')
        commits = {self.show(t)['commit'] for t in (first, second)}
        self.assertEqual(commits, {self.git('rev-parse', 'HEAD~1'), self.git('rev-parse', 'HEAD')})
        revs = {self.show(t)['rev'] for t in (first, second, unbound)}
        self.assertEqual(len(revs), 3)
        self.assertEqual(self.code('add', 'task', '--commit', 'HEAD', '--session', self.opener,
                                   cwd=self.repo), 3)
        self.assertEqual(self.code('add', 'x', '--type', 'request', '--to', 'curator',
                                   '--label', 'approval', '--commit', 'nosuch', '--session',
                                   self.opener, cwd=self.repo), 3)

    def test_label_change_cannot_create_an_unbound_code_approval(self):
        plain = self.ok('add', 'Push', '--type', 'request', '--to', 'curator',
                        '--session', self.opener, cwd=self.repo)
        self.assertEqual(self.code('label', plain, 'approval', '--session', self.opener), 1)
        sudo = self.request('--label', 'sudo')
        self.assertEqual(self.code('label', sudo, '--remove', 'sudo', '--session', self.opener), 1)
        bound = self.request()
        self.ok('label', bound, 'merge', '--session', self.opener)

    @unittest.skipIf(os.geteuid() == 0, 'root reads a mode-0 file')
    def test_unreadable_worktree_and_host_head_are_refused(self):
        self.write('locked', 'x')
        os.chmod(os.path.join(self.repo, 'locked'), 0)
        try:
            r = self.kb('add', 'x', '--type', 'request', '--to', 'curator', '--label', 'approval',
                        '--session', self.opener, cwd=self.repo)
        finally:
            os.remove(os.path.join(self.repo, 'locked'))
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn('cannot record the uncommitted code state', r.stderr)
        self.assertNotIn('none', r.stderr)
        tid = self.request()
        self.approve(tid)
        self.assertEqual(self.code('approval', tid, '--head', self.git('rev-parse', 'HEAD')), 3)
