"""Tests for kb: run the real CLI in subprocesses against a temp database (KB_DB)."""
import datetime
import glob
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB = os.path.join(REPO, 'kb')

# The atomic claim in kb, and a read-then-write replacement used for the mutation check.
def context(out):
    """The text a hook adds to the model's context: hookSpecificOutput JSON or plain text."""
    if out.startswith('{'):
        return json.loads(out)['hookSpecificOutput']['additionalContext'] + '\n'
    return out


CLAIM_ORIGINAL = """\
    with transaction(conn):
        if recovering(conn):
            raise Rejected(RECOVERY)
        cur = conn.execute(CLAIM_SQL, params)
        require(conn, cur.rowcount == 1, args.id, 'claim', me, name)
"""
CLAIM_BROKEN = """\
    row = conn.execute('SELECT status, assignee FROM ticket WHERE id=?', (args.id,)).fetchone()
    time.sleep(0.3)
    with transaction(conn):
        require(conn, row[0] == 'todo' and row[1] is None, args.id, 'claim')
        conn.execute("UPDATE ticket SET status='doing', assignee=? WHERE id=?", (me, args.id))
"""


class KbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kb-test-')
        self.db = os.path.join(self.tmp, 'board.sqlite')
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ('KB_SESSION', 'KB_TEST_BARRIER', 'KB_STALE_HOURS',
                                 'CLAUDE_CODE_SESSION_ID', 'CODEX_SESSION_ID', 'PI_SESSION_ID',
                                 'CODEX_THREAD_ID', 'KB_HARNESS', 'KB', 'BRAIN_SESSION_CONTEXT',
                                 'PI_SUBAGENT_DEPTH')}
        self.env['KB_DB'] = self.db
        self.env['KB_TEST_NO_PROC'] = '1'  # the suite may itself run inside an agent harness
        # kb reads and writes its config (installation id) under HOME / XDG_CONFIG_HOME.
        self.home = os.path.join(self.tmp, 'home')
        os.mkdir(self.home)
        self.env.update(HOME=self.home, XDG_CONFIG_HOME=os.path.join(self.home, '.config'),
                        KB_HOST='')
        self.env.pop('KB_INSTALLATION', None)
        self.env.pop('KB_SSH_KEY', None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def kb(self, *args, stdin='', kb=KB, cwd=None, env=None):
        return subprocess.run([kb] + [str(a) for a in args], input=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, env=env or self.env,
                              cwd=cwd or self.tmp, timeout=60)

    def ok(self, *args, **kw):
        r = self.kb(*args, **kw)
        self.assertEqual(r.returncode, 0, 'kb %s: %s' % (' '.join(map(str, args)), r.stderr))
        return r.stdout.strip()

    def code(self, *args, **kw):
        return self.kb(*args, **kw).returncode

    def start(self, name):
        return self.ok('start', '--as', name)

    def query(self, sql, *params):
        conn = sqlite3.connect(self.db)  # fresh connection, independent of kb
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def ticket(self, ticket_id):
        return self.query('SELECT status, assignee, opener, addressee FROM ticket WHERE id=?',
                          ticket_id)[0]

    def notes(self, ticket_id):
        return self.query('SELECT kind, text FROM note WHERE ticket_id=? ORDER BY id', ticket_id)

    def board_line(self, ticket_id, *args):
        for line in self.ok('board', *args).splitlines():
            if line.strip().startswith('#%s ' % ticket_id):
                return line
        return None

    def spawn(self, args, env, kb=KB, stdin_text=None):
        stdin = subprocess.DEVNULL
        if stdin_text is not None:
            path = tempfile.mkstemp(dir=self.tmp)[1]
            with open(path, 'w') as f:
                f.write(stdin_text)
            stdin = open(path)
        try:
            return subprocess.Popen([kb] + [str(a) for a in args], env=env, cwd=self.tmp,
                                    stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True)
        finally:
            if stdin is not subprocess.DEVNULL:
                stdin.close()

    def release_barrier(self, barrier, n):
        deadline = time.time() + 30
        while len(glob.glob(barrier + '.*')) < n:
            self.assertLess(time.time(), deadline, 'processes did not reach the barrier')
            time.sleep(0.01)
        open(barrier, 'w').close()


class RaceTest(KbCase):
    N = 10

    def run_race(self, kb):
        opener = self.start('opener')
        ticket = self.ok('add', '--claim', 'race target', '--type', 'request', '--to', 'any',
                       '--session', opener, kb=kb)
        tokens = [self.start('racer%d' % i) for i in range(self.N)]
        barrier = os.path.join(self.tmp, 'barrier')
        env = dict(self.env, KB_TEST_BARRIER=barrier)
        procs = [self.spawn(['claim', ticket, '--session', t], env, kb=kb) for t in tokens]
        self.release_barrier(barrier, self.N)
        codes = []
        for p in procs:
            p.communicate(timeout=60)
            codes.append(p.returncode)
        stored = self.query('SELECT assignee FROM ticket WHERE id=?', ticket)[0][0]
        return codes, tokens, stored

    def assert_single_winner(self, codes, tokens, stored):
        winners = [t for t, c in zip(tokens, codes) if c == 0]
        self.assertEqual(len(winners), 1, 'exit codes %s' % codes)
        self.assertEqual(sorted(codes), [0] + [1] * (self.N - 1))
        self.assertEqual(stored, winners[0])

    def test_claim_race_has_exactly_one_winner(self):
        self.assert_single_winner(*self.run_race(KB))

    def test_claim_race_detects_read_then_write_claim(self):
        with open(KB) as f:
            src = f.read()
        self.assertEqual(src.count(CLAIM_ORIGINAL), 1, 'claim block in kb changed; update test')
        broken = os.path.join(self.tmp, 'kb-broken')
        with open(broken, 'w') as f:
            f.write(src.replace(CLAIM_ORIGINAL, CLAIM_BROKEN))
        os.chmod(broken, os.stat(broken).st_mode | stat.S_IXUSR)
        codes, tokens, stored = self.run_race(broken)
        self.assertGreater(codes.count(0), 1, 'broken claim should yield several winners')
        with self.assertRaises(AssertionError):
            self.assert_single_winner(codes, tokens, stored)

    def test_concurrent_notes_are_all_stored(self):
        tok = self.start('alice')
        ticket = self.ok('add', '--claim', 'noisy ticket', '--session', tok)
        barrier = os.path.join(self.tmp, 'barrier')
        env = dict(self.env, KB_TEST_BARRIER=barrier)
        procs = []
        for i in range(20):
            procs.append(self.spawn(['note', ticket, '--session', tok], env,
                                    stdin_text='note %d\n' % i))
        self.release_barrier(barrier, 20)
        for p in procs:
            _, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
        texts = [t for k, t in self.notes(ticket) if k == 'comment' and t.startswith('note ')]
        self.assertEqual(sorted(texts), sorted('note %d' % i for i in range(20)))


class TransitionTest(KbCase):
    def setUp(self):
        super().setUp()
        self.alice = self.start('alice')
        self.bob = self.start('bob')
        self.carol = self.start('carol')
        self.cur = self.start('curator')

    def task(self, title='some task', who=None):
        return self.ok('add', '--claim', title, '--new', '--claim', '--session', who or self.alice)

    def request(self, to='bob', title='some request'):
        return self.ok('add', '--claim', title, '--new', '--type', 'request', '--to', to,
                       '--session', self.alice)

    def test_add_task_is_created_and_claimed_by_opener(self):
        c = self.task()
        self.assertEqual(self.ticket(c), ('doing', self.alice, self.alice, None))

    def test_add_request_is_todo_and_addressed(self):
        c = self.request('bob')
        self.assertEqual(self.ticket(c), ('todo', None, self.alice, 'bob'))
        self.assertEqual(self.code('add', '--claim', 'x', '--type', 'request', '--session', self.alice), 3)
        self.assertEqual(self.code('add', '--claim', 'x', '--to', 'bob', '--session', self.alice), 3)

    def test_claim_by_addressee_only(self):
        c = self.request('bob')
        self.assertEqual(self.code('claim', c, '--session', self.carol), 1)
        self.assertEqual(self.code('claim', c, '--session', self.cur), 1)
        self.assertEqual(self.code('claim', c, '--session', self.bob), 0)
        self.assertEqual(self.ticket(c)[:2], ('doing', self.bob))
        self.assertEqual(self.ok('claim', c, '--session', self.bob), 'ticket %s: already yours' % c)
        self.assertEqual(self.code('claim', c, '--session', self.carol), 1)

    def test_claim_any_by_any_session(self):
        c = self.request('any')
        self.assertEqual(self.code('claim', c, '--session', self.carol), 0)
        self.assertEqual(self.ticket(c)[:2], ('doing', self.carol))
        self.assertEqual(self.code('claim', c, '--session', self.bob), 1)

    def test_block_and_resume_by_assignee_only(self):
        c = self.task()
        for who in (self.bob, self.cur):
            self.assertEqual(self.code('block', c, '--session', who), 1)
        self.assertEqual(self.code('block', c, '--session', self.alice), 0)
        self.assertEqual(self.ticket(c)[:2], ('blocked', self.alice))
        for who in (self.bob, self.cur):
            self.assertEqual(self.code('resume', c, '--session', who), 1)
        self.assertEqual(self.code('resume', c, '--session', self.alice), 0)
        self.assertEqual(self.ticket(c)[:2], ('doing', self.alice))

    def test_close_task_by_assignee_only(self):
        c = self.task()
        self.ok('release', c, '--session', self.alice)
        self.ok('claim', c, '--session', self.bob)
        for who in (self.alice, self.carol, self.cur):  # opener, stranger, curator w/o --force
            self.assertEqual(self.code('close', c, '--session', who, stdin='x'), 1)
        self.assertEqual(self.code('close', c, '--session', self.bob, stdin='l1\nl2\nl3\n'), 0)
        self.assertEqual(self.ticket(c)[0], 'done')
        self.assertEqual(self.notes(c)[-1], ('close', 'l1\nl2\nl3'))

    def test_close_blocked_task_by_assignee(self):
        c = self.task()
        self.ok('block', c, '--session', self.alice)
        self.assertEqual(self.code('close', c, '--session', self.alice, stdin='done'), 0)

    def test_request_closed_by_opener_after_reply(self):
        c = self.request('bob')
        self.ok('claim', c, '--session', self.bob)
        self.assertEqual(self.code('close', c, '--session', self.alice, stdin='x'), 1)
        self.ok('note', c, '--kind', 'reply', '--session', self.bob, stdin='here you go')
        self.assertEqual(self.code('close', c, '--session', self.carol, stdin='x'), 1)
        self.assertEqual(self.code('close', c, '--session', self.alice, stdin='thanks'), 0)
        self.assertEqual(self.ticket(c)[0], 'done')

    def test_request_closed_by_assignee_after_reply(self):
        c = self.request('bob')
        self.ok('claim', c, '--session', self.bob)
        self.assertEqual(self.code('close', c, '--session', self.bob, stdin='x'), 1)
        self.ok('note', c, '--kind', 'reply', '--session', self.bob, stdin='done, see notes')
        self.assertEqual(self.code('close', c, '--session', self.bob, stdin='l1\nl2\nl3\n'), 0)
        self.assertEqual(self.ticket(c)[0], 'done')
        self.assertEqual(self.code('reopen', c, '--session', self.alice), 0)  # opener's remedy

    def test_rejection_names_the_unmet_condition(self):
        def err(*args, **kw):
            r = self.kb(*args, **kw)
            self.assertEqual(r.returncode, 1, r.stderr)
            return r.stderr
        c = self.request('bob')
        self.assertIn('only the addressee (bob) may claim', err('claim', c, '--session', self.carol))
        # A todo ticket that still has an assignee (for example after a restore) is rejected for
        # the assignee, not blamed on the addressee.
        held = self.ok('add', '--claim', '--type', 'request', '--to', 'bob', 'held', '--session', self.alice)
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE ticket SET assignee=? WHERE id=?', (self.alice, int(held)))
        conn.commit()
        conn.close()
        msg = err('claim', held, '--session', self.carol)
        self.assertIn('without assignee', msg)
        self.assertNotIn('only the addressee', msg)
        self.ok('claim', c, '--session', self.bob)
        self.assertIn('request %s has no reply note; add one with kb note --kind reply %s' % (c, c),
                      err('close', c, '--session', self.bob, stdin='x'))
        self.ok('note', c, '--kind', 'reply', '--session', self.bob, stdin='answer')
        self.assertIn('only the opener or the assignee may close request %s' % c,
                      err('close', c, '--session', self.carol, stdin='x'))
        self.assertIn('only the assignee or the curator may release',
                      err('release', c, '--session', self.alice))
        self.assertIn('abandon needs todo', err('abandon', c, '--session', self.alice))
        t = self.task()
        self.assertIn('only the assignee may close task %s' % t,
                      err('close', t, '--session', self.bob, stdin='x'))
        self.assertIn('block rejected: only the assignee may block',
                      err('block', t, '--session', self.bob))
        self.assertIn('ticket 999 does not exist', err('close', 999, '--session', self.bob,
                                                       stdin='x'))

    def test_abandon_todo_by_opener_or_curator(self):
        c = self.request('bob')
        self.assertEqual(self.code('abandon', c, '--session', self.bob), 1)
        self.assertEqual(self.code('abandon', c, '--session', self.alice), 0)
        self.assertEqual(self.ticket(c)[0], 'abandoned')
        c2 = self.request('bob', 'other request')
        self.assertEqual(self.code('abandon', c2, '--session', self.cur), 0)
        doing = self.task()
        self.assertEqual(self.code('abandon', doing, '--session', self.alice), 1)
        self.assertEqual(self.code('abandon', doing, '--session', self.cur), 1)

    def test_release_by_assignee_or_curator_only(self):
        c = self.task()
        self.assertEqual(self.code('release', c, '--session', self.bob), 1)
        self.assertEqual(self.ticket(c)[:2], ('doing', self.alice))
        self.assertEqual(self.code('release', c, '--session', self.alice), 0)
        self.assertEqual(self.ticket(c)[:2], ('todo', None))
        self.assertEqual(self.notes(c)[-1][0], 'release')
        self.ok('claim', c, '--session', self.bob)
        self.ok('block', c, '--session', self.bob)
        self.assertEqual(self.code('release', c, '--session', self.alice), 1)  # the opener
        self.assertEqual(self.code('release', c, '--session', self.cur), 0)
        self.assertEqual(self.ticket(c)[:2], ('todo', None))

    def test_force_close_by_curator_only(self):
        c = self.task()
        self.assertEqual(self.code('close', c, '--force', '--session', self.bob, stdin='x'), 1)
        self.assertEqual(self.code('close', c, '--force', '--session', self.cur, stdin='stale'),
                         0)
        self.assertEqual(self.ticket(c)[0], 'done')
        self.assertEqual(self.notes(c)[-1], ('force-close', 'stale'))
        todo = self.request('bob')  # not owned: abandon, not force-close
        self.assertEqual(self.code('close', todo, '--force', '--session', self.cur, stdin='x'), 1)

    def test_reopen_by_opener_or_curator(self):
        c = self.task()
        self.assertEqual(self.code('reopen', c, '--session', self.alice), 1)  # still doing
        self.ok('close', c, '--session', self.alice, stdin='done')
        self.assertEqual(self.code('reopen', c, '--session', self.bob), 1)
        self.assertEqual(self.code('reopen', c, '--session', self.alice), 0)
        self.assertEqual(self.ticket(c)[:2], ('todo', None))
        self.ok('claim', c, '--session', self.bob)
        self.ok('close', c, '--session', self.bob, stdin='done')
        self.assertEqual(self.code('reopen', c, '--session', self.cur), 0)
        a = self.request('bob')
        self.ok('abandon', a, '--session', self.alice)
        self.assertEqual(self.code('reopen', a, '--session', self.alice), 0)
        self.assertEqual(self.ticket(a)[0], 'todo')

    def test_notes_and_touch(self):
        c = self.task()
        for kind in ('comment', 'request'):
            self.ok('note', c, '--kind', kind, '--session', self.bob, stdin=kind + ' text')
        self.ok('note', c, 'positional', 'text', '--session', self.bob)
        self.assertEqual(self.notes(c)[-3:], [('comment', 'comment text'),
                                              ('request', 'request text'),
                                              ('comment', 'positional text')])
        # a reply answers a request, never a task
        self.assertEqual(self.code('note', c, '--reply', '--session', self.bob, stdin='x'), 1)
        self.assertEqual(self.code('note', 999, '--session', self.bob, stdin='x'), 1)
        self.assertEqual(self.code('touch', c, '--session', self.bob), 1)
        self.assertEqual(self.code('touch', c, '--session', self.alice), 0)

    def test_close_and_note_require_stdin_text(self):
        c = self.task()
        for text in ('', '  \n\n'):
            self.assertEqual(self.code('close', c, '--session', self.alice, stdin=text), 3)
            self.assertEqual(self.code('note', c, '--session', self.alice, stdin=text), 3)
        self.assertEqual(self.ticket(c)[0], 'doing')

    def test_leading_bom_is_stripped_from_note_text(self):
        c = self.task()
        self.ok('note', c, '--session', self.alice, stdin='\ufeffpiped from PowerShell')
        self.ok('note', c, '--session', self.alice, stdin='keep \ufeff inside')
        self.assertEqual(self.notes(c)[-2:], [('comment', 'piped from PowerShell'),
                                              ('comment', 'keep \ufeff inside')])

    def test_notes_are_append_only(self):
        c = self.task()
        self.ok('note', c, '--session', self.alice, stdin='original')
        conn = sqlite3.connect(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE note SET text='changed' WHERE ticket_id=?", (c,))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute('DELETE FROM note')
        conn.close()


class SimilarTitleTest(KbCase):
    def test_similar_title_needs_new(self):
        tok = self.start('alice')
        first = self.ok('add', '--claim', 'Fix login bug', '--repo', 'web', '--session', tok)
        r = self.kb('add', '--claim', 'Fix the login bugs', '--repo', 'web', '--session', tok)
        self.assertEqual(r.returncode, 2)
        self.assertIn('#%s ' % first, r.stdout)
        self.assertEqual(self.query('SELECT count(*) FROM ticket')[0][0], 1)
        self.ok('add', '--claim', 'Fix the login bugs', '--repo', 'web', '--new', '--session', tok)
        self.ok('add', '--claim', 'Fix login bug', '--repo', 'api', '--session', tok)  # other repo
        self.ok('add', '--claim', 'Write release notes', '--repo', 'web', '--session', tok)

    def test_closed_tickets_do_not_count(self):
        tok = self.start('alice')
        c = self.ok('add', '--claim', 'Fix login bug', '--repo', 'web', '--session', tok)
        self.ok('close', c, '--session', tok, stdin='done')
        self.ok('add', '--claim', 'Fix login bug', '--repo', 'web', '--session', tok)


class BoardHintTest(KbCase):
    GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid',
               'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}

    def git(self, cwd, *args):
        subprocess.run(['git', '-C', cwd] + list(args), check=True, env=dict(self.env, **self.GIT_ENV),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def make_repo(self, name):
        path = os.path.join(self.tmp, name)
        os.mkdir(path)
        self.git(path, 'init', '-q', '-b', 'main')
        self.git(path, 'commit', '-q', '--allow-empty', '-m', 'init')
        return path

    def test_idle_flag(self):
        tok = self.start('alice')
        c = self.ok('add', '--claim', 'slow work', '--session', tok)
        self.assertNotIn('idle>', self.board_line(c))
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE ticket SET last_activity='2020-01-01T00:00:00Z' WHERE id=?", (c,))
        conn.commit()
        conn.close()
        self.assertIn('idle>24h', self.board_line(c))
        env = dict(self.env, KB_STALE_HOURS='1000000')
        self.assertNotIn('idle>', self.kb('board', env=env).stdout)
        self.ok('touch', c, '--session', tok)
        self.assertNotIn('idle>', self.board_line(c))

    def test_session_ended_flag(self):
        alice, bob = self.start('alice'), self.start('bob')
        c = self.ok('add', '--claim', 'left behind', '--session', alice)
        self.ok('session-ended', '--session', alice)
        self.assertEqual(self.notes(c)[-1][0], 'session-ended')
        self.assertIn('session-ended', self.board_line(c))
        self.ok('release', c, '--session', alice)
        self.ok('claim', c, '--session', bob)
        self.assertNotIn('session-ended', self.board_line(c))

    def test_worktree_gone(self):
        tok = self.start('alice')
        wt = self.make_repo('wt')
        c = self.ok('add', '--claim', 'in a worktree', '--session', tok, cwd=wt)
        self.assertEqual(self.query('SELECT repo, worktree, branch FROM ticket WHERE id=?', c)[0],
                         ('wt', wt, 'main'))
        line = self.board_line(c)
        self.assertNotIn('worktree-gone', line)
        self.assertNotIn('git:unknown', line)
        shutil.rmtree(wt)
        self.assertIn('worktree-gone', self.board_line(c))

    def test_merged(self):
        tok = self.start('alice')
        wt = self.make_repo('repo')
        self.git(wt, 'checkout', '-q', '-b', 'feat')
        self.git(wt, 'commit', '-q', '--allow-empty', '-m', 'feature')
        c = self.ok('add', '--claim', 'feature work', '--merge-target', 'main', '--session', tok, cwd=wt)
        self.assertNotIn('merged', self.board_line(c))
        self.git(wt, 'branch', '-f', 'main', 'feat')
        self.assertIn('merged', self.board_line(c))

    def test_foreign_host_is_unknown(self):
        tok = self.start('alice')
        c = self.ok('add', '--claim', 'on windows', '--worktree', os.path.join(self.tmp, 'nowhere'),
                    '--session', tok)
        self.assertIn('worktree-gone', self.board_line(c))
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE ticket SET host='other-host' WHERE id=?", (c,))
        conn.commit()
        conn.close()
        line = self.board_line(c)
        self.assertIn('git:unknown', line)
        self.assertNotIn('worktree-gone', line)

    def test_board_filters(self):
        alice, bob = self.start('alice'), self.start('bob')
        a = self.ok('add', '--claim', 'alpha', '--repo', 'r1', '--session', alice)
        b = self.ok('add', '--claim', 'beta', '--repo', 'r2', '--session', bob)
        self.ok('close', b, '--session', bob, stdin='done')
        self.assertIsNotNone(self.board_line(a))
        self.assertIsNone(self.board_line(b))
        self.assertIsNotNone(self.board_line(b, '--all'))
        self.assertIsNone(self.board_line(a, '--repo', 'r2'))
        self.assertIsNone(self.board_line(a, '--all', '--mine', '--session', bob))
        self.assertIsNotNone(self.board_line(b, '--all', '--mine', '--session', bob))
        self.assertEqual(self.code('board', '--mine'), 3)


class ApprovalTest(KbCase):
    def test_approval_request_to_curator_is_listed_and_closed_by_opener(self):
        agent = self.start('llmstack')
        other = self.ok('add', '--claim', 'Unrelated work', '--repo', 'r', '--session', agent)
        req = self.ok('add', '--claim', '--type', 'request', '--to', 'curator', '--label', 'approval',
                      '--stdin', 'Commit PLAN.md D9', '--session', agent,
                      stdin='git commit -o PLAN.md\n')
        out = self.ok('board', '--to', 'curator')
        self.assertIn('#%s ' % req, out)
        self.assertNotIn('#%s ' % other, out)
        line = self.board_line(req, '--label', 'approval')
        self.assertIn('from:llmstack', line)
        self.assertIn('#approval', line)
        self.assertRegex(line, r'  \d+m  ')  # waiting age for a todo request
        self.assertIsNone(self.board_line(other, '--label', 'approval'))
        self.assertEqual(self.code('close', req, '--session', agent, stdin='done\n'), 1)
        # The opener records the curator's answer from its conversation.
        self.ok('note', '--kind', 'reply', req, '--session', agent, stdin='curator: approved\n')
        self.assertIn('answered', self.board_line(req, '--to', 'curator'))
        self.ok('close', req, '--session', agent, stdin='committed\n')
        self.assertEqual(self.ticket(req)[0], "done")

    def test_waiting_flag_after_stale_hours(self):
        agent = self.start('llmstack')
        req = self.ok('add', '--claim', '--type', 'request', '--to', 'curator', 'Merge x', '--session', agent)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE ticket SET created='2020-01-01T00:00:00Z' WHERE id=?", (req,))
        conn.commit()
        conn.close()
        self.assertIn('waiting>24h', self.board_line(req))

    def test_label_filter_matches_whole_labels_only(self):
        agent = self.start('a')
        t = self.ok('add', '--claim', 'x', '--repo', 'r', '--label', 'approvals', '--session', agent)
        self.assertIsNone(self.board_line(t, '--label', 'approval'))
        u = self.ok('add', '--claim', 'y', '--repo', 'r', '--label', 'aXb', '--session', agent)
        self.assertIsNone(self.board_line(u, '--label', 'a_b'))
        self.assertIsNone(self.board_line(u, '--label', 'a%'))
        self.assertIsNotNone(self.board_line(u, '--label', 'aXb'))


class IdentityTest(KbCase):
    def test_same_name_sessions_are_told_apart(self):
        a = self.ok('start', '--as', 'llmstack', '--harness', 'claude')
        b = self.ok('start', '--as', 'llmstack', '--harness', 'claude')
        ticket = self.ok('add', '--claim', '--session', a, '--repo', 'r', 'Some task')
        label = self.board_line(ticket).split('@')[1].split()[0]
        self.assertRegex(label, r'^llmstack/claude\u00b7[0-9a-f]{4}$')
        self.assertNotIn(a, self.ok('board') + self.ok('show', ticket))
        r = self.kb('claim', '--session', b, ticket)
        self.assertEqual(r.returncode, 1)
        self.assertIn('held by ' + label, r.stderr)
        other = self.ok('add', '--claim', '--session', b, '--repo', 'r', '--new', 'Some task')
        self.assertNotEqual(label, self.board_line(other).split('@')[1].split()[0])

    def test_missing_or_invalid_token(self):
        tok = self.start('alice')
        c = self.ok('add', '--claim', 'x', '--session', tok)
        self.assertEqual(self.code('touch', c), 3)
        self.assertEqual(self.code('touch', c, '--session', 'deadbeefdeadbeef'), 3)
        self.assertEqual(self.code('add', '--claim', 'y', '--session', 'nope'), 3)
        self.assertEqual(self.code('close', c, '--session', 'nope', stdin='x'), 3)
        self.assertEqual(self.code('touch', c, env=dict(self.env, KB_SESSION=tok)), 0)

    def test_start_validates_name(self):
        for name in ('any', '', 'two words'):
            self.assertEqual(self.code('start', '--as', name), 3)
        tok = self.start('alice')
        self.assertRegex(tok, r'^[0-9a-f]{32}$')
        self.assertNotEqual(tok, self.start('alice'))

    def test_usage_error_exit_code(self):
        self.assertEqual(self.code('claim'), 3)
        self.assertEqual(self.code('frobnicate'), 3)

    def test_database_refused_inside_git_tree(self):
        repo = os.path.join(self.tmp, 'repo')
        os.mkdir(repo)
        subprocess.run(['git', 'init', '-q', repo], check=True)
        env = dict(self.env, KB_DB=os.path.join(repo, 'board.sqlite'))
        self.assertEqual(self.code('start', '--as', 'alice', env=env), 3)
        self.assertFalse(os.path.exists(os.path.join(repo, 'board.sqlite')))
        self.start('alice')
        self.assertEqual(self.code('backup', os.path.join(repo, 'b.sqlite')), 3)

    def test_schema_version(self):
        self.start('alice')
        self.assertEqual(self.query('PRAGMA user_version')[0][0], 6)
        self.assertEqual(self.query('PRAGMA journal_mode')[0][0], 'wal')


class BackupRestoreTest(KbCase):
    def test_restore_blocks_claims_until_recovered(self):
        alice, cur = self.start('alice'), self.start('curator')
        held = self.ok('add', '--claim', 'in progress', '--session', alice)
        req = self.ok('add', '--claim', 'open request', '--type', 'request', '--to', 'any',
                      '--session', alice)
        backup = os.path.join(self.tmp, 'backup.sqlite')
        self.ok('backup', backup)
        later = self.ok('add', '--claim', 'added after backup', '--session', alice)
        bob = self.start('bob')  # a session that exists only after the backup

        self.assertEqual(self.code('restore', backup, '--session', alice), 1)
        out = self.ok('restore', backup, '--session', cur)
        self.assertIn('reconcile #%s ' % held, out)
        self.assertEqual(self.query('SELECT count(*) FROM ticket WHERE id=?', later)[0][0], 0)

        self.assertEqual(self.code('claim', req, '--session', bob), 1)
        self.assertEqual(self.code('add', '--claim', 'new work', '--session', bob), 1)
        self.assertEqual(self.code('recover', '--done', '--session', alice), 1)
        self.assertEqual(self.code('recover', '--done', '--session', cur), 0)
        self.assertEqual(self.code('claim', req, '--session', bob), 0)
        self.assertEqual(self.code('recover', '--done', '--session', cur), 1)

    def test_restore_rejects_non_kb_file(self):
        cur = self.start('curator')
        junk = os.path.join(self.tmp, 'junk')
        with open(junk, 'w') as f:
            f.write('not a database')
        self.assertEqual(self.code('restore', junk, '--session', cur), 3)
        self.assertEqual(self.code('restore', os.path.join(self.tmp, 'none'),
                                   '--session', cur), 3)


# A schema version 1 board as written by kb before the session hooks (commit f858d39).
V1_SCHEMA = """
CREATE TABLE session (token TEXT PRIMARY KEY, name TEXT NOT NULL, harness TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '', worktree TEXT NOT NULL DEFAULT '', started TEXT NOT NULL);
CREATE TABLE card (id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK (type IN ('task', 'request')), title TEXT NOT NULL,
    repo TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('todo', 'doing', 'blocked', 'done', 'abandoned')),
    addressee TEXT, assignee TEXT REFERENCES session(token),
    opener TEXT NOT NULL REFERENCES session(token), labels TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL, updated TEXT NOT NULL, claimed_at TEXT, last_activity TEXT NOT NULL,
    host TEXT NOT NULL DEFAULT '', worktree TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '', merge_target TEXT NOT NULL DEFAULT '');
CREATE TABLE note (id INTEGER PRIMARY KEY AUTOINCREMENT, card_id INTEGER NOT NULL REFERENCES card(id),
    author TEXT NOT NULL REFERENCES session(token), time TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('comment', 'request', 'reply', 'close',
                                      'release', 'force-close', 'session-ended')),
    text TEXT NOT NULL);
CREATE INDEX note_card ON note(card_id);
CREATE TRIGGER note_no_update BEFORE UPDATE ON note
    BEGIN SELECT RAISE(ABORT, 'note is append-only'); END;
CREATE TRIGGER note_no_delete BEFORE DELETE ON note
    BEGIN SELECT RAISE(ABORT, 'note is append-only'); END;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO session VALUES ('aaaaaaaaaaaaaaaa', 'nightshift', 'claude', 'h', '/w', '2026-09-24T09:44:05Z');
INSERT INTO card VALUES (1, 'task', 'old work', 'nightshift', 'doing', NULL, 'aaaaaaaaaaaaaaaa',
    'aaaaaaaaaaaaaaaa', '', '2026-09-24T09:45:00Z', '2026-09-24T09:45:00Z', '2026-09-24T09:45:00Z',
    '2026-09-24T09:45:00Z', 'h', '/w', 'main', '');
INSERT INTO note VALUES (1, 1, 'aaaaaaaaaaaaaaaa', '2026-09-24T09:46:00Z', 'comment', 'hello');
PRAGMA user_version=1;
"""


# The step to schema version 2 as written by kb before the rename to ticket (commit c23171a).
V1_TO_V2 = """
ALTER TABLE session ADD COLUMN harness_session TEXT NOT NULL DEFAULT '';
ALTER TABLE session ADD COLUMN last_seen TEXT;
UPDATE session SET last_seen=started;
CREATE UNIQUE INDEX session_harness ON session(harness, harness_session)
    WHERE harness_session <> '';
CREATE TABLE session_event (id INTEGER PRIMARY KEY AUTOINCREMENT, harness TEXT NOT NULL,
    harness_session TEXT NOT NULL, event TEXT NOT NULL CHECK (event IN ('start', 'stop', 'end')),
    source TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL DEFAULT '', time TEXT NOT NULL);
CREATE INDEX session_event_session ON session_event(harness, harness_session);
CREATE TRIGGER session_event_no_update BEFORE UPDATE ON session_event
    BEGIN SELECT RAISE(ABORT, 'session_event is append-only'); END;
UPDATE session SET harness_session='hs-1' WHERE token='aaaaaaaaaaaaaaaa';
INSERT INTO session_event VALUES (1, 'claude', 'hs-1', 'start', 'startup', '/w',
    '2026-09-24T09:47:00Z');
INSERT INTO note VALUES (2, 1, 'aaaaaaaaaaaaaaaa', '2026-09-24T09:48:00Z', 'reply', 'second');
PRAGMA user_version=2;
"""


def make_v1(path):
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.close()


def make_v2(path):
    make_v1(path)
    conn = sqlite3.connect(path)
    conn.executescript(V1_TO_V2)
    conn.close()


def schema_objects(path):
    conn = sqlite3.connect(path)
    try:
        return sorted(conn.execute('SELECT type, name, tbl_name FROM sqlite_master'))
    finally:
        conn.close()


class MigrationTest(KbCase):
    def test_v1_board_migrates_in_place(self):
        make_v1(self.db)
        before = [self.query('SELECT * FROM %s ORDER BY 1' % t) for t in ('session', 'card', 'note')]
        self.assertIn('old work', self.ok('board'))
        self.assertEqual(self.query('PRAGMA user_version')[0][0], 6)
        self.assertEqual(self.query('SELECT token, name, harness, host, worktree, started'
                                    ' FROM session'), before[0])
        self.assertEqual(self.query('SELECT harness_session, last_seen FROM session'),
                         [('', '2026-09-24T09:44:05Z')])
        self.assertEqual(self.query('SELECT * FROM ticket'), before[1])
        self.assertEqual(self.query('SELECT * FROM note'), before[2])
        self.assertEqual(self.query('SELECT count(*) FROM session_event')[0][0], 0)
        self.ok('note', 1, '--session', 'aaaaaaaaaaaaaaaa', stdin='after migration')
        self.assertEqual(self.notes(1)[-1], ('comment', 'after migration'))

    def test_v2_board_migrates_in_place(self):
        make_v2(self.db)
        tables = ('session', 'card', 'note', 'session_event', 'meta', 'sqlite_sequence')
        before = [self.query('SELECT * FROM %s ORDER BY 1' % t) for t in tables]
        self.assertIn('old work', self.ok('board', '--all'))
        self.assertEqual(self.query('PRAGMA user_version')[0][0], 6)
        after = [self.query('SELECT * FROM %s ORDER BY 1' % t)
                 for t in ('session', 'ticket', 'note', 'session_event', 'meta', 'sqlite_sequence')]
        # v5 appends installation to session and installation, event_id, received to events.
        self.assertEqual([r[:-1] for r in after[0]], before[0])
        self.assertEqual(after[1:3], before[1:3])
        self.assertEqual([r[:-3] for r in after[3]], before[3])
        # v4 adds the prompt-hint cutoff to meta and an empty hint_seen table, v5 the host's id.
        added = ('hints_since', 'installation')
        self.assertEqual([r for r in after[4] if r[0] not in added], before[4])
        self.assertEqual([r[0] for r in after[4] if r[0] in added], list(added))
        self.assertEqual(self.query('SELECT count(*) FROM hint_seen'), [(0,)])
        renamed = [('ticket' if n == 'card' else n, v) for n, v in before[5]]
        self.assertEqual(after[5], sorted(renamed))
        self.assertEqual(self.query('SELECT id, ticket_id FROM note ORDER BY id'), [(1, 1), (2, 1)])
        # No object still names card, and the note foreign key follows the renamed table.
        self.assertEqual(self.query("SELECT name FROM sqlite_master WHERE sql LIKE '%card%'"
                                    " OR name LIKE '%card%'"), [])
        self.assertEqual([(r[2], r[3], r[4]) for r in self.query('PRAGMA foreign_key_list(note)')],
                         [('session', 'author', 'token'), ('ticket', 'ticket_id', 'id')])
        self.assertEqual(self.query('PRAGMA foreign_key_check'), [])
        self.assertEqual(self.query('PRAGMA integrity_check'), [('ok',)])
        conn = sqlite3.connect(self.db)
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            with self.assertRaises(sqlite3.IntegrityError):  # triggers still guard note
                conn.execute("UPDATE note SET text='changed' WHERE ticket_id=1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('DELETE FROM note')
            with self.assertRaises(sqlite3.IntegrityError):  # foreign key to ticket
                conn.execute("INSERT INTO note (ticket_id, author, time, kind, text)"
                             " VALUES (99, 'aaaaaaaaaaaaaaaa', 't', 'comment', 'orphan')")
        finally:
            conn.close()
        self.assertEqual(self.ok('note', 1, '--session', 'aaaaaaaaaaaaaaaa', stdin='v3 note'),
                         'ticket 1: comment noted')
        # A fresh board has the same tables, indexes and triggers as a migrated one.
        fresh = os.path.join(self.tmp, 'fresh.sqlite')
        self.ok('board', env=dict(self.env, KB_DB=fresh))
        self.assertEqual(schema_objects(fresh), schema_objects(self.db))

    def test_restore_accepts_v2_backup(self):
        backup = os.path.join(self.tmp, 'v2.sqlite')
        make_v2(backup)
        cur = self.start('curator')
        self.assertIn('reconcile #1 ', self.ok('restore', backup, '--session', cur))
        self.assertEqual(self.query('PRAGMA user_version')[0][0], 6)
        self.assertEqual(self.notes(1), [('comment', 'hello'), ('reply', 'second')])

    def test_restore_accepts_v1_backup(self):
        backup = os.path.join(self.tmp, 'v1.sqlite')
        make_v1(backup)
        cur = self.start('curator')
        self.ok('hook', 'start', '--harness', 'pi', '--id', 'live-pi', '--cwd', self.tmp)
        self.assertIn('reconcile #1 ', self.ok('restore', backup, '--session', cur))
        self.assertEqual(self.query('PRAGMA user_version')[0][0], 6)
        names = sorted(r[0] for r in self.query('SELECT name FROM session'))
        self.assertEqual(names, sorted(['curator', 'nightshift', os.path.basename(self.tmp)]))
        self.assertEqual(self.query('SELECT harness_session, event FROM session_event'),
                         [('live-pi', 'start')])


class HarnessIdentityTest(KbCase):
    def henv(self, **ids):
        return dict(self.env, **ids)

    def sessions(self):
        return self.query('SELECT token, name, harness, harness_session FROM session ORDER BY rowid')

    def test_harness_id_outranks_inherited_kb_session(self):
        parent = self.start('parent')
        ticket = self.ok('add', '--claim', 'child work', '--repo', 'r',
                       env=self.henv(KB_SESSION=parent, PI_SESSION_ID='pi-9'))
        opener = self.query('SELECT opener FROM ticket WHERE id=?', ticket)[0][0]
        self.assertNotEqual(opener, parent)
        self.assertEqual(self.query('SELECT harness_session FROM session WHERE token=?', opener),
                         [('pi-9',)])

    def test_harness_session_cannot_become_curator(self):
        r = self.kb('start', '--as', 'curator', env=self.henv(CLAUDE_CODE_SESSION_ID='cc-1'))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.query("SELECT count(*) FROM session WHERE name='curator'"), [(0,)])
        self.assertEqual(self.code('start', '--as', 'curator'), 0)  # plain terminal

    def test_env_id_auto_registers_once(self):
        env = self.henv(CLAUDE_CODE_SESSION_ID='cc-1')
        ticket = self.ok('add', '--claim', 'env work', env=env)
        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        token, name, harness, hsid = rows[0]
        self.assertEqual((name, harness, hsid), (os.path.basename(self.tmp), 'claude', 'cc-1'))
        self.assertEqual(self.ticket(ticket)[:2], ('doing', token))
        self.ok('touch', ticket, env=env)
        self.assertEqual(len(self.sessions()), 1)
        other = self.henv(CODEX_SESSION_ID='cc-1')  # same id, other harness: other session
        self.assertEqual(self.code('touch', ticket, env=other), 1)
        self.assertEqual([r[2] for r in self.sessions()], ['claude', 'codex'])

    def test_name_from_git_toplevel(self):
        repo = os.path.join(self.tmp, 'myrepo')
        os.makedirs(os.path.join(repo, 'sub'))
        subprocess.run(['git', 'init', '-q', repo], check=True)
        self.ok('add', '--claim', 'x', '--repo', 'r', env=self.henv(PI_SESSION_ID='pi-1'),
                cwd=os.path.join(repo, 'sub'))
        self.assertEqual(self.query('SELECT name, harness, worktree FROM session'),
                         [('myrepo', 'pi', repo)])

    def test_name_and_repo_from_main_repo_in_linked_worktree(self):
        repo = os.path.join(self.tmp, 'myrepo')
        wt = os.path.join(self.tmp, 'mystifying-bose-b851d4')
        subprocess.run(['git', 'init', '-q', repo], check=True)
        subprocess.run(['git', '-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@t',
                        'commit', '-q', '--allow-empty', '-m', 'init'], check=True)
        subprocess.run(['git', '-C', repo, 'worktree', 'add', '-q', '-b', 'w', wt], check=True)
        ticket = self.ok('add', '--claim', 'Task in worktree', env=self.henv(PI_SESSION_ID='pi-1'), cwd=wt)
        self.assertEqual(self.query('SELECT name, worktree FROM session'), [('myrepo', wt)])
        self.assertEqual(self.query('SELECT repo FROM ticket WHERE id=?', ticket), [('myrepo',)])

    def test_curator_is_never_derived_from_a_directory(self):
        d = os.path.join(self.tmp, 'curator')
        os.mkdir(d)
        self.ok('add', '--claim', 'x', '--repo', 'r', env=self.henv(PI_SESSION_ID='pi-1'), cwd=d)
        self.assertEqual(self.query('SELECT name FROM session'), [('curator-session',)])

    def test_explicit_token_wins_over_env_id(self):
        tok = self.start('alice')
        ticket = self.ok('add', '--claim', 'x', '--session', tok, env=self.henv(CLAUDE_CODE_SESSION_ID='c'))
        self.assertEqual(self.ticket(ticket)[1], tok)
        self.assertEqual(len(self.sessions()), 1)
        ticket2 = self.ok('add', '--claim', 'y', env=self.henv(KB_SESSION=tok))  # no harness id
        self.assertEqual(self.ticket(ticket2)[1], tok)

    def test_several_env_ids_need_harness(self):
        env = self.henv(CLAUDE_CODE_SESSION_ID='c-1', PI_SESSION_ID='p-1')
        r = self.kb('add', '--claim', 'nested', env=env)
        self.assertEqual(r.returncode, 3)
        self.assertIn('--harness', r.stderr)
        self.assertEqual(self.sessions(), [])
        ticket = self.ok('add', '--claim', 'nested', '--harness', 'pi', env=env)
        self.assertEqual([r[2:] for r in self.sessions()], [('pi', 'p-1')])
        self.assertEqual(self.ticket(ticket)[1], self.sessions()[0][0])
        self.assertEqual(self.code('touch', ticket, '--harness', 'pi', env=env), 0)
        self.assertEqual(self.code('touch', ticket, '--harness', 'claude', env=env), 1)
        self.assertEqual(self.code('touch', ticket, '--harness', 'codex', env=env), 3)

    def test_name_renames_the_env_session_and_start_makes_a_token_session(self):
        env = self.henv(CLAUDE_CODE_SESSION_ID='cc-9')
        ticket = self.ok('add', '--claim', 'x', '--repo', 'r', env=env)
        hsess = self.sessions()[0][0]
        self.ok('name', 'partflow', env=env)
        self.assertEqual(self.sessions(), [(hsess, 'partflow', 'claude', 'cc-9')])
        self.assertEqual(self.ticket(ticket)[1], hsess)
        # kb start inside a harness starts a separate token session (scripts, ssh callers)
        tok = self.ok('start', '--as', 'script', env=env)
        self.assertNotEqual(tok, hsess)
        self.assertEqual(self.sessions()[-1], (tok, 'script', '', ''))
        self.ok('name', 'llmstack', env=env)
        self.assertEqual(self.sessions()[0][1], 'llmstack')
        both = self.henv(CLAUDE_CODE_SESSION_ID='cc-9', CODEX_SESSION_ID='cx-1')
        self.assertEqual(self.code('name', 'x', env=both), 3)
        self.ok('name', 'again', '--harness', 'codex', env=both)
        self.assertEqual(self.sessions()[-1][1:], ('again', 'codex', 'cx-1'))

    def test_no_id_at_all_is_an_identity_error(self):
        self.assertEqual(self.code('add', '--claim', 'x'), 3)
        self.assertEqual(self.sessions(), [])


class HookTest(KbCase):
    def hook(self, *args, stdin='', env=None):
        r = self.kb('hook', *args, stdin=stdin, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        if args[0] == 'start':  # the start hook names the session and the takeover candidates
            self.assertNotIn('Traceback', r.stdout)
        else:
            self.assertEqual(r.stdout, '')
        return r

    def events(self):
        return self.query('SELECT harness, harness_session, event, source, cwd FROM session_event'
                          ' ORDER BY id')

    def errors(self, directory=None):
        path = os.path.join(directory or self.tmp, 'hook-errors.log')
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().splitlines()

    def claude(self, event, **fields):
        payload = dict(session_id='cc-1', cwd=self.tmp, hook_event_name=event, **fields)
        return json.dumps(payload)

    def test_resume_after_end_clears_session_ended(self):
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        ticket = self.ok('add', '--claim', 'long task', '--repo', 'r', env=env)
        self.hook('end', '--harness', 'claude', stdin=self.claude('SessionEnd', reason='other'))
        self.assertIn('session-ended', self.board_line(ticket))
        time.sleep(1.1)  # event times have one-second resolution
        self.hook('start', '--harness', 'claude', stdin=self.claude('SessionStart',
                                                                     source='resume'))
        self.assertNotIn('session-ended', self.board_line(ticket))

    def test_end_then_start_in_one_second_clears_session_ended(self):
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        ticket = self.ok('add', '--claim', 'cleared task', '--repo', 'r', env=env)
        for _ in range(5):  # Claude's /clear: end, then start, usually in one second
            while time.time() % 1 > 0.3:
                time.sleep(0.05)
            self.hook('end', '--harness', 'claude', stdin=self.claude('SessionEnd',
                                                                       reason='clear'))
            self.hook('start', '--harness', 'claude', stdin=self.claude('SessionStart',
                                                                         source='clear'))
            times = self.query("SELECT event, time FROM session_event ORDER BY id")[-2:]
            if times[0][1] == times[1][1]:
                break
        self.assertEqual(times[0][1], times[1][1], 'end and start not in one second')
        self.assertIn(('session-ended', 'session ended'), self.notes(ticket))
        self.assertNotIn('session-ended', self.board_line(ticket))

    def test_start_stop_end_via_stdin(self):
        self.hook('start', '--harness', 'claude', stdin=self.claude('SessionStart',
                                                                     source='startup'))
        token, name = self.query('SELECT token, name FROM session')[0]
        self.assertEqual(name, os.path.basename(self.tmp))
        ticket = self.ok('add', '--claim', 'hooked', '--repo', 'r',
                       env=dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1'))
        self.assertEqual(self.ticket(ticket)[1], token)
        self.ok('name', 'renamed', env=dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1'))
        self.hook('start', '--harness', 'claude', stdin=self.claude('SessionStart',
                                                                     source='resume'))
        self.assertEqual(self.query('SELECT name FROM session'), [('renamed',)])  # no rename
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE ticket SET last_activity='2020-01-01T00:00:00Z'")
        conn.execute("UPDATE session SET last_seen='2020-01-01T00:00:00Z'")
        conn.commit()
        conn.close()
        self.hook('stop', '--harness', 'claude', stdin=self.claude('Stop', stop_hook_active=False))
        self.assertNotEqual(self.query('SELECT last_activity FROM ticket')[0][0],
                            '2020-01-01T00:00:00Z')
        self.assertNotEqual(self.query('SELECT last_seen FROM session')[0][0],
                            '2020-01-01T00:00:00Z')
        self.assertNotIn('idle>', self.board_line(ticket))
        self.hook('end', '--harness', 'claude', stdin=self.claude('SessionEnd', reason='other'))
        self.assertEqual(self.notes(ticket)[-1], ('session-ended', 'session ended'))
        self.assertIn('session-ended', self.board_line(ticket))
        self.assertEqual(self.events(), [('claude', 'cc-1', 'start', 'startup', self.tmp),
                                         ('claude', 'cc-1', 'start', 'resume', self.tmp),
                                         ('claude', 'cc-1', 'stop', '', self.tmp),
                                         ('claude', 'cc-1', 'end', 'other', self.tmp)])
        self.assertEqual(self.errors(), [])

    def test_codex_stdin_and_pi_flags(self):
        codex = json.dumps({'session_id': 'cx-1', 'cwd': self.tmp, 'source': 'startup',
                            'model': 'm', 'permission_mode': 'default', 'transcript_path': None})
        self.hook('start', '--harness', 'codex', stdin=codex)
        for event, source in (('start', 'startup'), ('stop', ''), ('end', 'quit')):
            self.hook(event, '--harness', 'pi', '--id', 'pi-1', '--cwd', self.tmp,
                      '--source', source)
        self.assertEqual(self.query('SELECT harness, harness_session FROM session ORDER BY rowid'),
                         [('codex', 'cx-1'), ('pi', 'pi-1')])
        self.assertEqual([e[1:4] for e in self.events()],
                         [('cx-1', 'start', 'startup'), ('pi-1', 'start', 'startup'),
                          ('pi-1', 'stop', ''), ('pi-1', 'end', 'quit')])
        self.assertEqual(self.errors(), [])

    def test_stop_and_end_without_session_only_log(self):
        self.hook('stop', '--harness', 'pi', '--id', 'ghost', '--cwd', self.tmp)
        self.hook('end', '--harness', 'pi', '--id', 'ghost', '--cwd', self.tmp)
        self.assertEqual(self.query('SELECT count(*) FROM session')[0][0], 0)
        self.assertEqual([e[2] for e in self.events()], ['stop', 'end'])

    def test_bad_input_exits_zero_silently_and_logs(self):
        cases = [
            (('start', '--harness', 'claude'), 'not json'),
            (('start', '--harness', 'claude'), '[1, 2]'),
            (('start', '--harness', 'claude'), '{"cwd": "/tmp"}'),
            (('start', '--harness', 'claude'), '{"session_id": 5}'),
            (('start', '--harness', 'vim', '--id', 'x'), ''),
            (('restart', '--harness', 'pi', '--id', 'x'), ''),
            (('start', '--id', 'x'), ''),
            (('start', '--harness', 'pi'), ''),
            (('start', '--harness', 'pi', '--bogus'), ''),
        ]
        for args, stdin in cases:
            self.hook(*args, stdin=stdin)
        self.assertEqual(len(self.errors()), len(cases))
        self.assertFalse(os.path.exists(self.db))  # rejected before the database is opened

    def test_broken_database_exits_zero_and_logs(self):
        as_dir = os.path.join(self.tmp, 'db-is-a-directory')
        os.mkdir(as_dir)
        junk = os.path.join(self.tmp, 'junk.sqlite')
        with open(junk, 'w') as f:
            f.write('not a database at all, just text' * 100)
        for db in (as_dir, junk):
            self.hook('end', '--harness', 'pi', '--id', 'x', env=dict(self.env, KB_DB=db))
        errors = self.errors()
        self.assertEqual(len(errors), 2)
        self.assertIn('OperationalError', errors[0])
        self.assertIn('DatabaseError', errors[1])

    def test_pi_subagent_is_skipped(self):
        env = dict(self.env, PI_SUBAGENT_DEPTH='1')
        self.hook('start', '--harness', 'pi', '--id', 'child', '--cwd', self.tmp, env=env)
        self.assertFalse(os.path.exists(self.db))
        env['PI_SUBAGENT_DEPTH'] = '0'
        self.hook('start', '--harness', 'pi', '--id', 'top', '--cwd', self.tmp, env=env)
        self.assertEqual([e[1] for e in self.events()], ['top'])

    def test_tty_stdin_does_not_block(self):
        import pty
        master, slave = pty.openpty()
        try:
            r = subprocess.run([KB, 'hook', 'start', '--harness', 'claude'], stdin=slave,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env,
                               cwd=self.tmp, timeout=10, universal_newlines=True)
        finally:
            os.close(master)
            os.close(slave)
        self.assertEqual((r.returncode, r.stdout), (0, ''))
        self.assertIn('no session id', self.errors()[0])

    def test_end_hook_is_fast(self):
        tok = self.start('alice')
        self.hook('start', '--harness', 'claude', stdin=self.claude('SessionStart'))
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        for i in range(5):
            self.ok('add', '--claim', 'ticket %d' % i, '--new', env=env)
        del tok
        t0 = time.monotonic()
        self.hook('end', '--harness', 'claude', stdin=self.claude('SessionEnd', reason='exit'))
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5, 'end hook took %.3f s' % elapsed)
        self.assertEqual(self.query("SELECT count(*) FROM note WHERE kind='session-ended'")[0][0], 5)



class GuardHookTest(KbCase):
    """kb hook guard: a warning before a guarded shell command when no ticket is held."""

    def guard(self, command, hsid='cc-1', harness='claude', tool='Bash'):
        payload = json.dumps({'session_id': hsid, 'cwd': self.tmp, 'tool_name': tool,
                              'hook_event_name': 'PreToolUse', 'tool_input': {'command': command}})
        r = self.kb('hook', 'guard', '--harness', harness, stdin=payload)
        self.assertEqual((r.returncode, r.stderr), (0, ''))
        return r.stdout

    def register(self, hsid='cc-1', harness='claude'):
        self.ok('hook', 'start', '--harness', harness,
                stdin=json.dumps({'session_id': hsid, 'cwd': self.tmp}))

    def test_prefilter_matches_and_non_matches(self):
        self.register()
        matches = {
            'git commit -m "fix #3"': 'git commit',
            'cd x && git commit -F - <<EOF\nmsg\nEOF': 'git commit',
            'cd a&&git push origin main': 'git push',
            'git -C /p -c a=b merge --no-ff -F /tmp/m feature/x': 'git merge',
            'GIT_X=1 sudo /usr/bin/git push': 'git push',
            'echo $(git push)': 'git push',
            'git \\\n  push': 'git push',
            'docker compose -f a.yml up -d': 'docker compose up',
            'cd stack; docker-compose up': 'docker compose up',
            'docker --context prod compose -p x up -d': 'docker compose up',
            'result="$(git push)"': 'git push',
            'echo `git push`': 'git push',
            'if git push; then echo done; fi': 'git push',
            '{ git commit -m x; }': 'git commit',
            '>push.log git push': 'git push',
            "printf '%s\\n' '<<EOF'\ngit push": 'git push',
            'git -C /tmp/my\\ repo push': 'git push',
            'docker compose -f "$DIR"/compose.yml up': 'docker compose up',
            'cat <<EOF\n$(git push)\nEOF': 'git push',
            '2>/dev/null git push': 'git push',
            'cat <<<x\ngit push': 'git push',
            'cat <<EOF\nPushed: $(git push origin "$(git branch --show-current)")\nEOF':
                'git push',
            'timeout 30s git push origin main': 'git push',
            'timeout -s KILL -k 5 60s docker compose up -d': 'docker compose up',
            'result="$(printf \'%s)\' "$b"; git push)"': 'git push',
        }
        for command, cls in matches.items():
            out = self.guard(command)
            data = json.loads(out)
            self.assertEqual(list(data), ['hookSpecificOutput'], command)
            text = data['hookSpecificOutput']['additionalContext']
            self.assertEqual(data['hookSpecificOutput']['hookEventName'], 'PreToolUse')
            self.assertIn('this %s runs' % cls, text)
            self.assertIn('not blocking', text)
            self.assertIn('kb add "<goal>"', text)
            self.assertNotIn('\n', text)
        for command in ('echo "git push"', 'git commit-tree x', 'git merge-base a b',
                        'git stash push', 'git status # git push', 'git log | grep push',
                        'docker compose logs', 'gitx push', 'cat <<EOF\ngit push\nEOF\nls',
                        'cat <<123\ngit push\n123', 'cat <<A <<B\ngit push\nA\ngit push\nB',
                        "cat <<-'EOF'\n\tgit push\n\tEOF", 'docker compose logs up',
                        'docker compose exec app echo up', 'echo ";" git push',
                        "cat <<'EOF'\n$(git push)\nEOF", 'git status # $(git push)',
                        'echo "\\$(git push)"',
                        'X=1 ' * 20000 + 'git push'):  # beyond the 64 KiB read
            self.assertEqual(self.guard(command), '', command)
        self.assertEqual(self.guard('git push', tool='apply_patch'), '')
        self.assertEqual(self.errors(), [])

    def errors(self):
        path = os.path.join(self.tmp, 'hook-errors.log')
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().splitlines()

    def test_silent_with_a_held_ticket_warns_without(self):
        self.register()
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        tid = self.ok('add', '--claim', 'guarded work', '--repo', 'r', env=env)
        self.assertEqual(self.guard('git push'), '')
        self.ok('block', tid, '--session', self.ticket(tid)[1], stdin='waiting')
        self.assertEqual(self.guard('git push'), '')  # blocked counts as held
        self.ok('resume', tid, '--session', self.ticket(tid)[1])
        self.ok('close', tid, '--session', self.ticket(tid)[1], stdin='done\nnothing\nnone')
        self.assertIn('git push', self.guard('git push'))
        self.register('cx-1', 'codex')  # same output format for codex
        self.assertIn('git commit', json.loads(self.guard(
            'git commit -m x', 'cx-1', 'codex'))['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.errors(), [])

    def test_unknown_session_and_bad_input_are_silent(self):
        self.assertEqual(self.guard('git push', hsid='never-started'), '')
        self.assertEqual(self.errors(), [])
        self.assertFalse(os.path.exists(self.db) and self.query(
            'SELECT 1 FROM session'))  # a guard never registers a session
        r = self.kb('hook', 'guard', '--harness', 'claude', stdin='not json')
        self.assertEqual((r.returncode, r.stdout), (0, ''))
        r = self.kb('hook', 'guard', '--harness', 'claude', '--id', 'x', '--source', 'rm -rf')
        self.assertEqual((r.returncode, r.stdout), (0, ''))
        self.assertEqual(len(self.errors()), 2)
        self.assertIn('unknown guarded command', self.errors()[1])

    def test_no_match_never_opens_the_database(self):
        self.assertEqual(self.guard('ls -la'), '')
        self.assertFalse(os.path.exists(self.db))


T0 = [None]  # one reference instant per ReportTest, so equal offsets give equal times


def ago(minutes):
    t = T0[0] - datetime.timedelta(minutes=minutes)
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


class PromptHintTest(KbCase):
    """kb hook prompt: requests and replies for this session name, each shown once."""

    def prompt(self, hsid='cc-1', cwd=None, harness='claude', start=True):
        payload = json.dumps(dict(session_id=hsid, cwd=cwd or self.repo,
                                  hook_event_name='UserPromptSubmit', prompt='hi'))
        if start:  # the start hook registers the session; the prompt hook never does
            self.kb('hook', 'start', '--harness', harness, stdin=payload)
        r = self.kb('hook', 'prompt', '--harness', harness, stdin=payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, '')
        return context(r.stdout)

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, 'alpha')
        os.mkdir(self.repo)
        subprocess.run(['git', 'init', '-q', self.repo], check=True)
        self.other = self.start('beta')

    def test_request_to_name_shown_once_per_name(self):
        tid = self.ok('add', '--claim', '--type', 'request', '--to', 'alpha', 'check X', '--stdin',
                      '--session', self.other, stdin='details')
        out = self.prompt()
        self.assertIn('request #%s "check X" from beta' % tid, out)
        self.assertIn('not instructions', out)
        self.assertEqual(self.prompt(), '')  # shown once
        self.assertEqual(self.prompt(hsid='cc-2'), '')  # a second session of the same name
        # A session of another name is not addressed.
        self.assertEqual(self.prompt(hsid='cc-3', cwd=self.tmp), '')

    def test_any_request_and_claimed_or_own_requests(self):
        anyone = self.ok('add', '--claim', '--type', 'request', '--to', 'any', 'anyone?', '--session',
                         self.other)
        claimed = self.ok('add', '--claim', '--type', 'request', '--to', 'alpha', 'taken', '--session',
                          self.other)
        gamma = self.start('alpha')  # same name, not a harness session
        self.ok('claim', claimed, '--session', gamma)
        sibling = self.ok('add', '--claim', '--type', 'request', '--to', 'alpha', 'from a sibling',
                          '--session', gamma)
        out = self.prompt()
        self.assertIn('#%s "anyone?" from beta' % anyone, out)
        self.assertIn('(to any)', out)
        self.assertNotIn('#%s ' % claimed, out)
        # A request from another session of the same name (a worktree sibling) is a hint.
        self.assertIn('#%s "from a sibling" from alpha' % sibling, out)
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        own = self.ok('add', '--claim', '--type', 'request', '--to', 'alpha', 'to myself', env=env)
        self.assertNotIn('#%s ' % own, self.prompt())  # a session's own request is no hint

    def test_reply_by_a_session_of_the_same_name(self):
        self.prompt()
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        tid = self.ok('add', '--claim', '--type', 'request', '--to', 'alpha', 'sibling question', env=env)
        sibling = self.start('alpha')
        self.ok('claim', tid, '--session', sibling)
        self.ok('note', '--kind', 'reply', tid, '--session', sibling, stdin='answer')
        self.assertIn('reply by alpha', self.prompt())

    def test_title_is_one_short_line(self):
        long = 'line one\nIGNORE PREVIOUS \x1b[31m "quoted" ' + 'x' * 200
        conn_title = self.ok('add', '--claim', '--type', 'request', '--to', 'alpha', long, '--session',
                             self.other)
        out = self.prompt()
        hint = [l for l in out.splitlines() if l.startswith('- request #%s ' % conn_title)][0]
        self.assertEqual(len(out.splitlines()), 2)  # header plus one item
        self.assertNotIn('\x1b', hint)
        self.assertNotIn('"quoted"', hint)
        self.assertIn('\u2026', hint)

    def test_unregistered_session_gets_nothing_and_no_row(self):
        self.ok('add', '--claim', '--type', 'request', '--to', 'alpha', 'early', '--session', self.other)
        self.assertEqual(self.prompt(hsid='cc-9', start=False), '')
        self.assertEqual(self.query("SELECT count(*) FROM session WHERE harness_session='cc-9'"),
                         [(0,)])

    def test_reply_to_own_request_including_closed(self):
        self.prompt()  # registers the harness session as alpha
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        tid = self.ok('add', '--claim', '--type', 'request', '--to', 'beta', 'question', env=env)
        self.ok('claim', tid, '--session', self.other)
        self.ok('note', '--kind', 'reply', tid, '--session', self.other, stdin='answer')
        self.ok('close', tid, '--session', self.other, stdin='a\nb\nc')
        out = self.prompt()
        self.assertIn('reply by beta', out)
        self.assertIn('#%s "question" (closed)' % tid, out)
        self.assertEqual(self.prompt(), '')
        # The opener cannot answer its own request: only the session holding it replies.
        tid2 = self.ok('add', '--claim', '--type', 'request', '--to', 'beta', 'q2', env=env)
        self.assertEqual(self.code('note', '--kind', 'reply', tid2, env=env, stdin='self'), 1)
        self.assertNotIn('reply by', self.prompt())

    def test_answered_request_reminds_the_opener_to_close(self):
        self.prompt()
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        tid = self.ok('add', '--claim', '--type', 'request', '--to', 'beta', 'question', env=env)
        self.ok('claim', tid, '--session', self.other)
        self.ok('note', '--kind', 'reply', tid, '--session', self.other, stdin='answer')
        out = self.prompt()
        # The reply line carries the close; no second line for the same request.
        self.assertIn('#%s "question": kb show %s, then kb close %s if answered'
                      % (tid, tid, tid), out)
        self.assertEqual(out.count('#%s ' % tid), 1)
        self.assertEqual(self.prompt(), '')  # shown once per reply
        self.ok('note', '--kind', 'reply', tid, '--session', self.other, stdin='more')
        self.assertIn('kb close %s if answered' % tid, self.prompt())
        self.assertEqual(self.prompt(hsid='cc-2'), '')  # once per name

    def test_close_once_per_request_with_several_replies(self):
        self.prompt()
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        tid = self.ok('add', '--claim', '--type', 'request', '--to', 'beta', 'question', env=env)
        self.ok('claim', tid, '--session', self.other)
        for i in range(2):
            self.ok('note', '--kind', 'reply', tid, '--session', self.other, stdin='a%d' % i)
        out = self.prompt()
        self.assertEqual(out.count('reply by beta'), 2)
        self.assertEqual(out.count('kb close %s' % tid), 1)
        self.assertTrue(out.rstrip().endswith('kb close %s if answered' % tid))  # latest reply
        for i in range(6):
            self.ok('note', '--kind', 'reply', tid, '--session', self.other, stdin='b%d' % i)
        first, second = self.prompt(), self.prompt()
        self.assertEqual((first.count('reply by beta'), second.count('reply by beta')), (5, 1))
        self.assertNotIn('kb close', first)
        self.assertEqual(second.count('kb close %s' % tid), 1)
        self.assertEqual(self.prompt(), '')

    def test_answered_before_hints_since_and_closed(self):
        self.prompt()
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        tid = self.ok('add', '--claim', '--type', 'request', '--to', 'beta', 'old question', env=env)
        done = self.ok('add', '--claim', '--type', 'request', '--to', 'beta', 'settled', env=env)
        for t in (tid, done):
            self.ok('claim', t, '--session', self.other)
            self.ok('note', '--kind', 'reply', t, '--session', self.other, stdin='answer')
        self.ok('close', done, '--session', self.other, stdin='a\nb\nc')
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE meta SET value='2099-01-01T00:00:00Z' WHERE key='hints_since'")
        conn.commit()
        conn.close()
        # The open request is state, not history: reminded although its reply is older.
        out = self.prompt()
        self.assertIn('- your request #%s "old question" is answered: kb close %s'
                      % (tid, tid), out)
        self.assertNotIn('#%s ' % done, out)
        self.assertEqual(self.prompt(), '')

    def test_held_ticket_reminds_once_per_claim(self):
        self.prompt()
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        tid = self.ok('add', '--claim', 'build it', env=env)  # a task is claimed by its opener
        out = self.prompt()
        self.assertIn('- you hold #%s "build it" (doing): kb close %s when the work is done'
                      % (tid, tid), out)
        self.assertEqual(self.prompt(), '')
        # Another session's ticket is no hint, not even for the same name.
        sibling = self.start('alpha')
        self.ok('add', '--claim', 'not mine', '--session', sibling)
        self.assertEqual(self.prompt(), '')
        # A new claim is a new event, also within the same second.
        self.ok('release', tid, env=env)
        self.ok('claim', tid, env=env)
        self.assertIn('- you hold #%s ' % tid, self.prompt())
        self.ok('release', tid, env=env)
        self.ok('claim', tid, env=env)
        self.assertIn('- you hold #%s ' % tid, self.prompt())
        self.assertEqual(self.prompt(), '')

    def test_held_limit_summarises_the_rest(self):
        self.prompt()
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        for i in range(6):
            self.ok('add', '--claim', 'job %d' % i, '--new', env=env)
        out = self.prompt()
        self.assertEqual(out.count('- you hold #'), 5)
        self.assertIn('1 more held tickets follow at the next prompt', out)
        self.assertEqual(self.prompt().count('- you hold #'), 1)

    def set_activity(self, tid, days):
        conn = sqlite3.connect(self.db)
        t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days, hours=1)
        conn.execute('UPDATE ticket SET last_activity=? WHERE id=?',
                     (t.strftime('%Y-%m-%dT%H:%M:%SZ'), int(tid)))
        conn.commit()
        conn.close()

    def test_idle_held_ticket_reminds_once_per_idle_episode(self):
        self.prompt()
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        tid = self.ok('add', '--claim', 'build it', env=env)
        self.assertIn('- you hold #%s ' % tid, self.prompt())
        self.assertEqual(self.prompt(), '')  # fresh: no idle reminder
        self.set_activity(tid, 3)
        out = self.prompt()
        self.assertIn('- you hold #%s "build it" (doing), idle 3d: continue it, kb block %s with'
                      ' a reason, or kb release --stdin %s with a handoff note'
                      % (tid, tid, tid), out)
        self.assertEqual(self.prompt(), '')  # once per idle episode
        # An answer (the stop hook) is new activity; a later idle period is a new episode.
        payload = json.dumps(dict(session_id='cc-1', cwd=self.repo, hook_event_name='Stop'))
        self.kb('hook', 'stop', '--harness', 'claude', stdin=payload)
        self.assertEqual(self.prompt(), '')
        self.set_activity(tid, 2)
        self.assertIn('- you hold #%s "build it" (doing), idle 2d: ' % tid, self.prompt())
        self.assertEqual(self.prompt(), '')
        # A ticket idle at its first hint gets the idle line only.
        new = self.ok('add', '--claim', 'started long ago', '--new', env=env)
        self.set_activity(new, 1)
        out = self.prompt()
        self.assertEqual(out.count('#%s ' % new), 1)
        self.assertIn('#%s "started long ago" (doing), idle ' % new, out)
        # Another session's idle ticket is no hint, not even for the same name.
        sibling = self.start('alpha')
        other = self.ok('add', '--claim', 'not mine', '--session', sibling)
        self.set_activity(other, 3)
        self.assertEqual(self.prompt(), '')

    def test_idle_respects_stale_hours(self):
        self.prompt()
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        tid = self.ok('add', '--claim', 'build it', env=env)
        self.prompt()
        self.set_activity(tid, 1)
        self.env['KB_STALE_HOURS'] = '48'
        self.assertEqual(self.prompt(), '')
        self.env['KB_STALE_HOURS'] = '12'
        self.assertIn('#%s "build it" (doing), idle 1d: ' % tid, self.prompt())
        self.set_activity(tid, 2)  # a new idle episode
        self.env['KB_STALE_HOURS'] = 'soon'  # invalid: the hook falls back to 24 hours
        self.assertIn('#%s "build it" (doing), idle 2d: ' % tid, self.prompt())
        for days, bad in enumerate(('nan', 'inf', '1e300'), 3):  # pass float(), break timedelta
            self.set_activity(tid, days)  # a new idle episode each time
            self.env['KB_STALE_HOURS'] = bad
            self.assertIn('#%s "build it" (doing), idle ' % tid, self.prompt())

    def test_history_before_migration_is_not_hinted(self):
        tid = self.ok('add', '--claim', '--type', 'request', '--to', 'alpha', 'old', '--session', self.other)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE meta SET value='2099-01-01T00:00:00Z' WHERE key='hints_since'")
        conn.commit()
        conn.close()
        self.assertEqual(self.prompt(), '')
        self.assertEqual(self.query('SELECT count(*) FROM hint_seen')[0][0], 0)
        self.assertTrue(tid)

    def test_limit_summarises_the_rest(self):
        for i in range(7):
            self.ok('add', '--claim', '--type', 'request', '--to', 'alpha', 'task %d' % i, '--new',
                    '--session', self.other)
        out = self.prompt()
        self.assertEqual(out.count('- request #'), 5)
        self.assertIn('2 more requests follow at the next prompt', out)
        self.assertEqual(self.prompt().count('- request #'), 2)  # only listed ones were seen
        self.assertEqual(self.prompt(), '')

    def test_errors_never_reach_stdout(self):
        r = self.kb('hook', 'prompt', '--harness', 'claude', stdin='not json')
        self.assertEqual((r.returncode, r.stdout), (0, ''))
        with open(os.path.join(self.tmp, 'hook-errors.log')) as f:
            self.assertIn('kb hook prompt', f.read())


class ReportTest(KbCase):
    """kb report on a board seeded with SQL, so every time is controlled."""

    def setUp(self):
        super().setUp()
        T0[0] = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        self.ok('board')  # creates the schema
        self.conn = sqlite3.connect(self.db, isolation_level=None)
        self.tickets = 0

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def session(self, token, name, harness='claude', hsid=None, started=None):
        started = started or ago(60)
        self.conn.execute('INSERT INTO session (token, name, harness, host, worktree, started,'
                          " harness_session, last_seen) VALUES (?, ?, ?, 'h', '', ?, ?, ?)",
                          (token, name, harness, started, hsid or '', started))
        return token

    def events(self, harness, hsid, *pairs):
        for event, minutes in pairs:
            self.conn.execute('INSERT INTO session_event (harness, harness_session, event,'
                              " source, cwd, time) VALUES (?, ?, ?, '', '', ?)",
                              (harness, hsid, event, ago(minutes)))

    def harness_session(self, token, harness, *pairs, name=None):
        hsid = 'id-' + token
        self.session(token, name or token, harness, hsid, ago(pairs[0][1]))
        self.events(harness, hsid, *pairs)
        return token

    def add_ticket(self, opener, title, minutes, repo='r', type='task', status='doing',
                   assignee=None, updated=None):
        self.tickets += 1
        created = ago(minutes)
        assignee = assignee if assignee is not None else (opener if type == 'task' else None)
        self.conn.execute(
            'INSERT INTO ticket (id, type, title, repo, status, assignee, opener, labels, created,'
            " updated, claimed_at, last_activity) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)",
            (self.tickets, type, title, repo, status, assignee, opener, created,
             updated or created, created if type == 'task' else None, created))
        return self.tickets

    def note(self, ticket_id, author, kind, minutes, text='x'):
        self.conn.execute('INSERT INTO note (ticket_id, author, time, kind, text)'
                          ' VALUES (?, ?, ?, ?, ?)', (ticket_id, author, ago(minutes), kind, text))

    def claim(self, ticket_id, token, minutes):
        self.conn.execute("UPDATE ticket SET status='doing', assignee=?, claimed_at=? WHERE id=?",
                          (token, ago(minutes), ticket_id))
        self.note(ticket_id, token, 'comment', minutes, 'claimed')

    def close(self, ticket_id, token, minutes):
        self.conn.execute("UPDATE ticket SET status='done', updated=? WHERE id=?",
                          (ago(minutes), ticket_id))
        self.note(ticket_id, token, 'close', minutes)

    def report(self, *args):
        return self.ok('report', *args)

    @staticmethod
    def row(out, label):
        """Counts of one session-table row, per harness column then total."""
        for line in out.splitlines():
            if line.startswith('  ' + label) and line[2 + len(label):2 + len(label) + 2] == '  ':
                return [int(t) for t in line[26:].split() if t.isdigit()]
        raise AssertionError('no row %r in:\n%s' % (label, out))

    def test_classifies_each_session(self):
        closer = self.harness_session('closer', 'claude', ('start', 120), ('end', 10))
        t = self.add_ticket(closer, 'build the importer', 100)
        self.close(t, closer, 20)
        leaver = self.harness_session('leaver', 'claude', ('start', 120), ('end', 10))
        t = self.add_ticket(leaver, 'write migration docs', 100)
        self.note(t, leaver, 'session-ended', 10, 'session ended')
        noter = self.harness_session('noter', 'codex', ('start', 120), ('stop', 60))
        req = self.add_ticket(closer, 'please review', 110, type='request', status='todo')
        self.note(req, noter, 'reply', 50)
        self.harness_session('idle', 'pi', ('start', 120), ('end', 60))
        # Resumed after its end: not left open, only other use.
        resumed = self.harness_session('resumed', 'claude', ('start', 120), ('end', 90),
                                       ('start', 80))
        t = self.add_ticket(resumed, 'refactor parser', 100)
        self.note(t, resumed, 'session-ended', 90, 'session ended')
        out = self.report()
        #                                 claude codex pi total
        self.assertEqual(self.row(out, 'started in window'), [3, 1, 1, 5])
        self.assertEqual(self.row(out, 'denominator'), [3, 1, 1, 5])
        self.assertEqual(self.row(out, 'used board'), [3, 1, 0, 4])
        self.assertEqual(self.row(out, '  closed'), [1, 0, 0, 1])
        self.assertEqual(self.row(out, '  left open'), [1, 0, 0, 1])
        self.assertEqual(self.row(out, '  other use'), [1, 1, 0, 2])
        self.assertEqual(self.row(out, 'no board use'), [0, 0, 1, 1])
        self.assertRegex(out, r'\n    closed +1  33% +0 +0% +0 +0% +1  20%\n')
        self.assertIn('session-ended note and no later close: 2', out)

    def test_short_runs_are_excluded_from_the_denominator(self):
        self.harness_session('child', 'claude', ('start', 30), ('stop', 29), ('end', 28))
        self.harness_session('long', 'claude', ('start', 30), ('end', 20))
        self.harness_session('running', 'claude', ('start', 2))  # no end yet: never short
        user = self.harness_session('user', 'claude', ('start', 30), ('end', 28))
        self.add_ticket(user, 'quick fix', 29)
        out = self.report()
        self.assertEqual(self.row(out, 'started in window'), [4, 4])
        self.assertEqual(self.row(out, 'short, no use (excluded)'), [1, 1])
        self.assertEqual(self.row(out, 'denominator'), [3, 3])
        self.assertEqual(self.row(out, 'used board'), [1, 1])
        self.assertEqual(self.row(out, 'no board use'), [2, 2])
        self.assertIn('likely child runs', out)

    def test_window_filter(self):
        day = 24 * 60
        old = self.harness_session('old', 'claude', ('start', 20 * day), ('end', 20 * day - 30))
        self.add_ticket(old, 'old work', 20 * day - 10)
        self.harness_session('new', 'claude', ('start', 60), ('end', 10))
        out = self.report()
        self.assertEqual(self.row(out, 'started in window'), [1, 1])
        self.assertIn('  added           0', out)
        since = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=21)).strftime('%Y-%m-%d')
        out = self.report('--since', since, '--days', '3')
        self.assertEqual(self.row(out, 'used board'), [1, 1])
        self.assertIn('  added           1', out)
        self.assertIn('(UTC, 3 days)', out)
        out = self.report('--days', '30')
        self.assertEqual(self.row(out, 'started in window'), [2, 2])
        self.assertEqual(self.code('report', '--since', '2026-13-01'), 3)
        self.assertEqual(self.code('report', '--days', '0'), 3)

    def test_sessions_without_start_event_are_listed(self):
        win = self.session('win', 'windows-box', harness='', started=ago(90))
        self.add_ticket(win, 'remote task', 80)
        auto = self.session('auto', 'autoreg', hsid='id-auto', started=ago(70))
        self.add_ticket(auto, 'auto task', 70)
        self.session('unused', 'nobody', started=ago(60))  # registered, and kb start is use
        stale = self.session('stale', 'before', hsid='id-stale', started=ago(30 * 24 * 60))
        self.events('claude', 'id-stale', ('start', 30 * 24 * 60))
        t = self.add_ticket(stale, 'long task', 30 * 24 * 60)
        self.note(t, stale, 'comment', 5)
        self.session('ancient', 'ancient', started=ago(30 * 24 * 60))
        out = self.report()
        self.assertIn('without a start event in the window (counted: 4;', out)
        self.assertRegex(out, r'windows-box\S*\s+first use \S+\s+kb start, no harness id')
        self.assertRegex(out, r'autoreg/claude\S*\s+first use \S+\s+auto-registered, no start')
        self.assertRegex(out, r'before/claude\S*\s+first use \S+\s+started before the window')
        self.assertNotIn('ancient', out)
        self.assertEqual(self.row(out, 'denominator'), [0])

    def test_ticket_counts_and_claim_to_close(self):
        s = self.harness_session('s', 'claude', ('start', 600), ('end', 5))
        o = self.session('o', 'opener', started=ago(600))
        a = self.add_ticket(s, 'task a', 500)
        self.close(a, s, 440)                                       # 60 min
        b = self.add_ticket(s, 'task b', 400)
        self.close(b, s, 280)                                       # 120 min
        c = self.add_ticket(o, 'request c', 300, type='request', status='todo')
        self.claim(c, s, 301)
        self.close(c, s, 1)                                         # 300 min
        self.add_ticket(o, 'request d', 200, type='request', status='abandoned',
                        updated=ago(100))
        e = self.add_ticket(s, 'task e', 100)
        self.note(e, s, 'session-ended', 5, 'session ended')
        out = self.report()
        self.assertIn('  added           5  (task 3, request 2)', out)
        self.assertIn('  claimed         4  (kb claim 1, kb add of a task 3)', out)
        self.assertIn('  closed          3', out)
        self.assertIn('  abandoned       1', out)
        self.assertIn('  still doing     1', out)
        self.assertIn('median 2h00m, max 5h00m', out)
        self.assertIn('session-ended note and no later close: 1', out)
        self.assertRegex(out, r'#%d\s+doing\s+task e' % e)

    def test_duplicate_candidates(self):
        s = self.harness_session('s', 'claude', ('start', 600))
        a = self.add_ticket(s, 'add csv export to reports', 500)
        b = self.add_ticket(s, 'add CSV export for reports', 400)    # overlaps a: pair
        self.add_ticket(s, 'add csv export to reports', 400, repo='other')  # other repo
        c = self.add_ticket(s, 'fix login redirect loop', 300)
        self.close(c, s, 250)
        self.add_ticket(s, 'fix login redirect loops', 200)         # after c closed: no pair
        self.add_ticket(s, 'rewrite the scheduler', 100)            # dissimilar
        out = self.report()
        self.assertIn('Duplicate candidates (counted: 1;', out)
        self.assertIn('#%d ~ #%d  ratio' % (a, b), out)
        self.assertIn('repo r\n', out)

    def test_request_opener_close_counts_as_closed(self):
        asker = self.harness_session('asker', 'claude', ('start', 120), ('end', 5))
        worker = self.session('worker', 'worker', started=ago(120))
        req = self.add_ticket(asker, 'please check the index', 110, type='request',
                              status='todo')
        self.note(req, worker, 'reply', 60)
        self.close(req, asker, 30)  # never claimed: the opener closes after the reply
        out = self.report()
        self.assertEqual(self.row(out, '  closed'), [1, 1])

    def test_note_for_a_ticket_missing_from_the_snapshot_does_not_crash(self):
        s = self.harness_session('s', 'claude', ('start', 120), ('end', 5))
        self.note(999, s, 'close', 30)  # no ticket 999: foreign keys are off on this connection
        self.note(998, s, 'session-ended', 5, 'session ended')
        out = self.report()
        self.assertEqual(self.row(out, 'used board'), [1, 1])
        self.assertEqual(self.row(out, '  closed'), [0, 0])
        self.assertIn('  closed          0', out)

    def test_resumed_or_misordered_sessions_are_never_short(self):
        self.harness_session('resumed', 'claude', ('start', 30), ('end', 29), ('start', 28))
        self.harness_session('misordered', 'claude', ('start', 20), ('end', 25))
        self.harness_session('child', 'claude', ('start', 30), ('end', 29))
        out = self.report()
        self.assertEqual(self.row(out, 'short, no use (excluded)'), [1, 1])
        self.assertEqual(self.row(out, 'no board use'), [2, 2])

    def test_touching_held_periods_are_no_duplicates(self):
        s = self.harness_session('s', 'claude', ('start', 600))
        a = self.add_ticket(s, 'add csv export to reports', 300)
        self.close(a, s, 200)
        self.add_ticket(s, 'add csv export for reports', 200)  # starts when a ends
        out = self.report()
        self.assertIn('Duplicate candidates (counted: 0;', out)

    def test_without_bus_import_table_nothing_is_excluded(self):
        s = self.harness_session('s', 'claude', ('start', 60), ('end', 1))
        self.add_ticket(s, 'something', 50)
        self.assertEqual(self.query("SELECT name FROM sqlite_master WHERE name='bus_import'"), [])
        out = self.report()
        self.assertIn('Imported bus history excluded (counted): 0 tickets, 0 notes, 0 sessions'
                      ' of harness bus', out)
        self.assertIn('  added           1', out)

    def test_imported_bus_history_is_excluded(self):
        self.conn.execute('CREATE TABLE bus_import (bus_id TEXT PRIMARY KEY, ticket_id INTEGER,'
                          ' note_id INTEGER UNIQUE, imported TEXT)')

        def imported(ticket_id, author, kind, minutes):
            self.note(ticket_id, author, kind, minutes)
            nid = self.conn.execute('SELECT max(id) FROM note').fetchone()[0]
            self.conn.execute("INSERT INTO bus_import VALUES (?, ?, ?, 'now')",
                              ('bus-%d' % nid, ticket_id, nid))

        alice = self.session('alice-bus', 'alice', harness='bus', started=ago(300))
        bob = self.session('bob-bus', 'bob', harness='bus', started=ago(300))
        done = self.add_ticket(alice, 'bus thread closed', 300, repo='', type='request',
                               status='done', updated=ago(200))
        imported(done, alice, 'request', 300)
        imported(done, bob, 'reply', 250)
        imported(done, alice, 'close', 200)
        open_ = self.add_ticket(alice, 'bus thread open', 280, repo='', type='request',
                                status='todo')
        imported(open_, alice, 'request', 280)
        # Live work on an imported request after the import still counts.
        worker = self.harness_session('worker', 'claude', ('start', 100), ('end', 5))
        self.claim(open_, worker, 90)
        self.note(open_, worker, 'reply', 80)
        live = self.add_ticket(worker, 'live task', 70)
        self.close(live, worker, 60)
        out = self.report()
        self.assertIn('Imported bus history excluded (counted): 2 tickets, 4 notes, 2 sessions'
                      ' of harness bus', out)
        self.assertIn('  added           1  (task 1, request 0)', out)
        self.assertIn('  claimed         2  (kb claim 1, kb add of a task 1)', out)
        self.assertIn('  closed          1', out)
        self.assertIn('  still doing     1', out)
        self.assertIn('without a start event in the window (counted: 0;', out)
        self.assertNotIn('alice', out)
        self.assertEqual(self.row(out, 'used board'), [1, 1])
        self.assertEqual(self.row(out, '  closed'), [1, 1])

    def test_report_writes_nothing(self):
        s = self.harness_session('s', 'claude', ('start', 60), ('end', 1))
        self.add_ticket(s, 'something', 50)
        self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        tables = ('session', 'ticket', 'note', 'session_event', 'meta')

        def counts():
            return [self.query('SELECT count(*) FROM %s' % t)[0][0] for t in tables]

        before, mtime = counts(), os.stat(self.db).st_mtime_ns
        wal = self.db + '-wal'
        wal_size = os.path.getsize(wal) if os.path.exists(wal) else 0
        time.sleep(0.05)
        self.report()
        self.report('--days', '1')
        self.assertEqual(counts(), before)
        self.assertEqual(os.stat(self.db).st_mtime_ns, mtime)
        self.assertEqual(os.path.getsize(wal) if os.path.exists(wal) else 0, wal_size)

    def test_missing_board_is_not_created(self):
        env = dict(self.env, KB_DB=os.path.join(self.tmp, 'none', 'board.sqlite'))
        self.assertEqual(self.code('report', env=env), 3)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'none')))

    def test_fits_100_columns(self):
        s = self.harness_session('s' * 60, 'claude', ('start', 60), ('end', 1))
        self.add_ticket(s, 'x' * 150, 50)
        self.add_ticket(s, 'x' * 149, 40)
        w = self.session('w', 'n' * 80, harness='', started=ago(30))
        self.add_ticket(w, 'y' * 150, 30, repo='z')
        out = self.report()
        self.assertIn('Duplicate candidates (counted: 1;', out)
        self.assertLessEqual(max(len(l) for l in out.splitlines()), 100)


class SessionsTest(KbCase):
    """kb sessions, kb whoami and the kb add --to check for unknown names."""

    def days_ago(self, days):
        t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        return t.strftime('%Y-%m-%dT%H:%M:%SZ')

    def old_session(self, token, name, days, inst=None):
        inst = inst or self.query("SELECT value FROM meta WHERE key='installation'")[0][0]
        conn = sqlite3.connect(self.db)
        conn.execute('INSERT INTO session (token, name, harness, host, worktree, started,'
                     " harness_session, last_seen, installation) VALUES (?, ?, 'claude', 'h',"
                     " '', ?, ?, ?, ?)", (token, name, self.days_ago(days), 'id-' + token,
                                           self.days_ago(days), inst))
        conn.commit()
        conn.close()

    def lines(self, *args):
        return {l.split()[0]: l.split() for l in self.ok('sessions', *args).splitlines()[1:]}

    def test_sessions_recent_all_harnesses_and_held(self):
        claude = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        codex = dict(self.env, CODEX_SESSION_ID='cx-1')
        held = self.ok('add', '--claim', 'first task', '--repo', 'r', env=claude)
        self.ok('add', '--claim', 'another unrelated thing', '--repo', 'r', env=codex)
        self.ok('block', held, env=claude)
        self.start('script')
        self.old_session('0123456789abcdef', 'ancient', 20)
        self.old_session('fedcba9876543210', 'ancient', 30, inst='ab' * 8)
        name = os.path.basename(self.tmp)
        out = self.ok('sessions') + self.ok('sessions', '--all')
        recent = self.lines()
        self.assertEqual(sorted(recent), sorted([name, 'script']))
        self.assertEqual(recent[name], [name, 'claude,codex', '2', '0m', '1/1', 'host'])
        self.assertEqual(recent['script'], ['script', '-', '1', '0m', '0/0', 'host'])
        every = self.lines('--all')
        self.assertEqual(every['ancient'], ['ancient', 'claude', '2', '20d', '0/0',
                                            'host,' + 'ab' * 8])
        self.assertEqual(list(every)[-1], 'ancient')  # last seen first
        for t, in self.query('SELECT token FROM session'):
            self.assertNotIn(t, out)

    def test_held_ticket_lists_an_old_name_and_others_do_not_move_seen(self):
        self.ok('board')  # creates the schema
        self.old_session('0123456789abcdef', 'veteran', 20)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO ticket (type, title, repo, status, assignee, opener, created,"
                     " updated, claimed_at, last_activity) VALUES ('task', 't', 'r', 'doing',"
                     " ?, ?, ?, ?, ?, ?)", ('0123456789abcdef', '0123456789abcdef')
                     + (self.days_ago(20),) * 4)
        conn.commit()
        conn.close()
        self.old_session('1123456789abcdef', 'retired', 20)
        self.assertEqual(sorted(self.lines()), ['veteran'])  # held, though seen 20 days ago
        # Another session's note on the held ticket does not move its SEEN.
        self.ok('note', 1, '--session', self.start('bob'), stdin='a note by bob')
        self.assertEqual(self.lines()['veteran'][3:5], ['20d', '1/0'])
        self.assertEqual(self.lines()['bob'][3], '0m')
        self.ok('touch', 1, '--session', '0123456789abcdef')  # its own activity does
        self.assertEqual(self.lines()['veteran'][3:5], ['0m', '1/0'])

    def test_whoami_harness_session_and_token(self):
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-1')
        name = os.path.basename(self.tmp)
        out = self.ok('whoami', env=env)
        self.assertIn('session: %s/claude (not registered yet)' % name, out)
        self.assertEqual(self.query('SELECT count(*) FROM session'), [(0,)])  # read-only
        self.ok('add', '--claim', 'some task', '--repo', 'r', env=env)
        token = self.query('SELECT token FROM session')[0][0]
        out = self.ok('whoami', env=env)
        self.assertEqual(out.splitlines()[0], 'name: ' + name)
        self.assertRegex(out, r'session: %s/claude\u00b7[0-9a-f]{4}\n' % name)
        self.assertIn('installation: host', out)
        self.assertNotIn(token, out)
        tok = self.start('script')
        out = self.ok('whoami', '--session', tok)
        self.assertEqual(out.splitlines()[0], 'name: script')
        self.assertRegex(out, r'session: script\u00b7[0-9a-f]{4}\nharness: -\n')
        self.assertNotIn(tok, out)
        self.assertEqual(self.code('whoami'), 3)
        empty = dict(self.env, KB_DB=os.path.join(self.tmp, 'empty.sqlite'))
        self.assertEqual(self.ok('sessions', '--all', env=empty), 'no sessions yet')
        self.assertEqual(self.code('whoami', '--session', 'nope'), 3)

    def test_whoami_shows_the_repo(self):
        repo = os.path.join(self.tmp, 'myrepo')
        subprocess.run(['git', 'init', '-q', repo], check=True)
        out = self.ok('whoami', env=dict(self.env, PI_SESSION_ID='p-1'), cwd=repo)
        self.assertEqual(out.splitlines()[0], 'name: myrepo')
        self.assertEqual(out.splitlines()[-1], 'repo: myrepo')

    def test_request_to_unknown_name_warns_and_is_added(self):
        me = self.start('alice')
        self.start('bob')
        r = self.kb('add', '--claim', 'question for kanban', '--type', 'request', '--to', 'kanban',
                    '--session', me)
        self.assertEqual(r.returncode, 0, r.stderr)
        prefix = 'kb: no session currently has the name kanban; known names: '
        self.assertTrue(r.stderr.startswith(prefix), r.stderr)
        self.assertEqual(sorted(r.stderr[len(prefix):].strip().split(', ')), ['alice', 'bob'])
        self.assertEqual(self.ticket(r.stdout.strip())[3], 'kanban')
        for to in ('bob', 'any', 'curator'):
            r = self.kb('add', '--claim', 'question for kanban', '--type', 'request', '--to', to,
                        '--new', '--session', me)
            self.assertEqual((r.returncode, r.stderr), (0, ''), to)
        # The similar-title check still exits 2 before any warning.
        r = self.kb('add', '--claim', 'question for kanban', '--type', 'request', '--to', 'kanban',
                    '--session', me)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn('no session currently has', r.stderr)


class VersionTest(KbCase):
    def version(self, kb=KB, env=None):
        r = self.kb('--version', kb=kb, env=env)
        self.assertEqual((r.returncode, r.stderr), (0, ''))
        return r.stdout

    def copy_kb(self, d):
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, 'kb')
        shutil.copy2(KB, path)
        return path

    def git(self, repo, *args):
        return subprocess.run(['git', '-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@t']
                              + list(args), check=True, stdout=subprocess.PIPE,
                              universal_newlines=True).stdout.strip()

    def test_checkout_prints_version_and_commit(self):
        self.assertRegex(self.version(), r'^kb \d+\.\d+\.\d+ \([0-9a-f]{4,}(-dirty)?\)\n$')
        repo = os.path.join(self.tmp, 'repo')
        self.git(self.tmp, 'init', '-q', repo)
        kb = self.copy_kb(repo)
        self.git(repo, 'add', 'kb')
        self.git(repo, 'commit', '-q', '-m', 'kb')
        head = self.git(repo, 'rev-parse', '--short', 'HEAD')
        version = re.match(r'kb (\S+)', self.version()).group(1)
        self.assertEqual(self.version(kb=kb), 'kb %s (%s)\n' % (version, head))
        with open(os.path.join(repo, 'other'), 'w') as f:
            f.write('x')  # another file's change does not make kb dirty
        self.assertEqual(self.version(kb=kb), 'kb %s (%s)\n' % (version, head))
        with open(kb, 'a') as f:
            f.write('\n')
        self.assertEqual(self.version(kb=kb), 'kb %s (%s-dirty)\n' % (version, head))
        self.git(repo, 'update-index', '--assume-unchanged', 'kb')  # hides it from git status
        self.assertEqual(self.version(kb=kb), 'kb %s (%s-dirty)\n' % (version, head))
        self.git(repo, 'update-index', '--no-assume-unchanged', 'kb')
        self.git(repo, 'add', 'kb')
        shutil.copy2(KB, kb)  # the staged change undone in the file alone
        self.assertEqual(self.version(kb=kb), 'kb %s (%s)\n' % (version, head))

    def test_copy_outside_a_checkout_prints_the_bare_version(self):
        self.assertRegex(self.version(kb=self.copy_kb(os.path.join(self.tmp, 'plain'))),
                         r'^kb \d+\.\d+\.\d+\n$')
        repo = os.path.join(self.tmp, 'repo')
        self.git(self.tmp, 'init', '-q', repo)
        self.git(repo, 'commit', '-q', '--allow-empty', '-m', 'empty')
        self.assertRegex(self.version(kb=self.copy_kb(repo)), r'^kb \d+\.\d+\.\d+\n$')
        self.git(repo, 'config', 'status.showUntrackedFiles', 'no')  # hides untracked files
        self.assertRegex(self.version(kb=os.path.join(repo, 'kb')), r'^kb \d+\.\d+\.\d+\n$')

    def test_missing_git_prints_the_bare_version(self):
        bin_dir = os.path.join(self.tmp, 'bin')
        os.mkdir(bin_dir)
        os.symlink(sys.executable, os.path.join(bin_dir, 'python3'))  # for the shebang
        self.assertRegex(self.version(env=dict(self.env, PATH=bin_dir)),
                         r'^kb \d+\.\d+\.\d+\n$')


if __name__ == '__main__':
    unittest.main()
