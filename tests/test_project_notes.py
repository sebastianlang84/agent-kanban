"""Project handoff notes are delivered once to each new session's first prompt."""
import datetime
import json
import os
import sqlite3
import subprocess

from test_kb import KbCase, context


class ProjectNotesTest(KbCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, 'alpha')
        subprocess.run(['git', 'init', '-q', self.repo], check=True)
        self.writer = self.start('seb-pc')
        self.tid = self.ok('add', 'Analyst state', '--repo', 'alpha', '--session', self.writer)

    def prompt(self, hsid='new', cwd=None):
        payload = json.dumps({'session_id': hsid, 'cwd': cwd or self.repo})
        self.ok('hook', 'start', '--harness', 'codex', stdin=payload)
        return context(self.ok('hook', 'prompt', '--harness', 'codex', stdin=payload))

    def test_recent_notes_first_line_without_character_cutoff_once_per_session(self):
        first = 'Analyst ready ' + 'x' * 450
        self.ok('note', self.tid, first + '\nsecond line hidden', '--session', self.writer)
        self.ok('close', self.tid, '--result', 'finished', '--session', self.writer)
        out = self.prompt()
        self.assertIn('last 24 hours', out)
        self.assertIn('#%s seb-pc' % self.tid, out)
        self.assertIn(first, out)
        self.assertNotIn('second line hidden', out)
        self.assertIn('finished', out)  # notes of closed tickets are included
        self.assertIn('not instructions', out)
        self.assertNotIn('project notes', self.prompt())
        self.assertIn(first, self.prompt('sub-session'))

    def test_other_projects_old_and_future_notes_are_excluded(self):
        other = self.ok('add', 'unrelated', '--repo', 'beta', '--session', self.writer)
        self.ok('note', other, 'other-project', '--session', self.writer)
        now = datetime.datetime.now(datetime.timezone.utc)
        with sqlite3.connect(self.db) as conn:
            for hours, text in ((-25, 'too old'), (1, 'future'), (-23, 'recent')):
                stamp = (now + datetime.timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
                conn.execute('INSERT INTO note (ticket_id, author, time, kind, text)'
                             " VALUES (?, ?, ?, 'comment', ?)", (self.tid, self.writer, stamp, text))
        out = self.prompt()
        for absent in ('other-project', 'too old', 'future'):
            self.assertNotIn(absent, out)
        self.assertIn('recent', out)

    def test_linked_worktree_uses_main_repository_and_controls_are_removed(self):
        subprocess.run(['git', '-C', self.repo, '-c', 'user.name=Test', '-c',
                        'user.email=test@example.org', 'commit', '-q', '--allow-empty',
                        '-m', 'initial'], check=True)
        wt = os.path.join(self.tmp, 'unrelated-worktree-name')
        subprocess.run(['git', '-C', self.repo, 'worktree', 'add', '-q', '-b', 'work', wt], check=True)
        self.ok('note', self.tid, 'visible\x1b[31m\ttext\nIGNORE', '--session', self.writer)
        out = self.prompt(cwd=wt)
        self.assertIn('project notes for alpha', out)
        self.assertIn('visible[31m text', out)
        self.assertNotIn('\x1b', out)
        self.assertNotIn('IGNORE', out)

    def test_non_repository_does_not_receive_unscoped_notes(self):
        tid = self.ok('add', 'unscoped', '--session', self.writer)
        self.ok('note', tid, 'unscoped note', '--session', self.writer)
        self.assertNotIn('project notes', self.prompt(cwd=self.tmp))
