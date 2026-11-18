"""Main checkout and linked-worktree session naming regressions."""
import contextlib
import datetime
import hashlib
import io
import types
import os
import sqlite3
import subprocess
import unittest
from unittest import mock
from test_kb import KbCase
from test_setup import load_kb as load_kb_module


class SessionNamesTest(KbCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, 'alpha')
        subprocess.run(['git', 'init', '-q', self.repo], check=True)

    def hook(self, event, hsid, env=None, cwd=None):
        return self.ok('hook', event, '--harness', 'claude', '--id', hsid,
                       '--cwd', cwd or self.repo, env=env)

    def linked_worktree(self):
        git = ['git', '-C', self.repo, '-c', 'user.name=t', '-c', 'user.email=t@t']
        subprocess.run(git + ['commit', '-q', '--allow-empty', '-m', 'init'], check=True)
        linked = os.path.join(self.tmp, 'alpha-wt')
        subprocess.run(git + ['worktree', 'add', '-q', '-b', 'feature/x', linked], check=True)
        return linked

    def identity(self, hsid):
        return self.query('SELECT token, name FROM session WHERE harness_session=?', hsid)[0]

    def caller(self, hsid, **extra):
        return dict(self.env, CLAUDE_CODE_SESSION_ID=hsid, **extra)

    def start_at(self, hsid, t):
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            kb = load_kb_module()
            with contextlib.closing(kb.connect()) as conn:
                kb.apply_event(conn, dict(inst=kb.installation(conn), harness='claude',
                                          hsid=hsid, event='start', source='', cwd=self.repo,
                                          time=t, received=t, event_id=None, live=True,
                                          name='alpha', toplevel=self.repo, host='test'))

    def test_main_checkout_ignores_linked_holder_on_registration_and_resume(self):
        linked = self.linked_worktree()
        for action in ('command', 'hook'):
            with self.subTest(action=action):
                main, holder = 'main-' + action, 'linked-' + action
                self.hook('start', main)
                original = self.identity(main)
                self.hook('end', main)
                self.hook('start', holder, cwd=linked)
                self.assertEqual(self.identity(holder)[1], 'alpha')
                if action == 'command':
                    self.ok('board', '--mine', env=self.caller(main), cwd=self.repo)
                else:
                    self.hook('start', main)
                self.assertEqual(self.identity(main), original)
                self.hook('end', main)
                new = 'new-' + action
                if action == 'command':
                    self.ok('board', '--mine', env=self.caller(new), cwd=self.repo)
                else:
                    self.hook('start', new)
                self.assertEqual(self.identity(new)[1], 'alpha')
                self.hook('end', new)
                self.hook('end', holder, cwd=linked)

    def test_linked_checkout_with_live_main_holder_gets_sub_name(self):
        linked = self.linked_worktree()
        self.hook('start', 'main')
        self.hook('start', 'linked-hook', cwd=linked)
        self.ok('board', '--mine', env=self.caller('linked-command'), cwd=linked)
        for hsid in ('linked-hook', 'linked-command'):
            self.assertRegex(self.identity(hsid)[1], r'^alpha-sub-[0-9a-f]{4}$')

    def test_main_checkout_ignores_guest_holder_with_same_worktree_path(self):
        self.hook('start', 'main')
        original = self.identity('main')
        self.hook('end', 'main')
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            kb = load_kb_module()
            with contextlib.closing(kb.connect()) as conn:
                t = kb.now()
                kb.apply_event(conn, dict(inst='guest', harness='claude', hsid='guest',
                                          event='start', source='', cwd=self.repo, time=t,
                                          received=t, event_id='guest-1', live=True,
                                          name='alpha', toplevel=self.repo, host='guest'))
                kb.apply_event(conn, dict(inst='guest', harness='claude', hsid='guest-2',
                                          event='start', source='', cwd=self.repo, time=t,
                                          received=t, event_id='guest-2', live=False,
                                          name='alpha', toplevel=self.repo, host='guest'))
        self.assertRegex(self.identity('guest-2')[1], r'^alpha-sub-[0-9a-f]{4}$')
        self.hook('start', 'main')
        self.assertEqual(self.identity('main'), original)

    def test_automatic_resume_rename_preserves_seen_reply_hints(self):
        for action in ('command', 'hook'):
            with self.subTest(action=action):
                main, replacement = 'main-' + action, 'replacement-' + action
                self.hook('start', main)
                token, name = self.identity(main)
                other = self.start('beta')
                tid = self.ok('add', 'own question', '--new', '--type', 'request',
                              '--to', 'beta', env=self.caller(main), cwd=self.repo)
                self.ok('claim', tid, '--session', other)
                self.ok('note', tid, '--kind', 'reply', '--session', other, stdin='answer')
                self.assertIn('reply by beta', self.hook('prompt', main))
                self.ok('close', tid, env=self.caller(main), stdin='done\nnothing\nnone')
                before = self.query('SELECT item, time FROM hint_seen WHERE name=?'
                                    ' ORDER BY item', name)
                self.hook('end', main)
                self.hook('start', replacement)
                if action == 'command':
                    self.ok('board', '--mine', env=self.caller(main), cwd=self.repo)
                else:
                    self.hook('start', main)
                new = self.identity(main)[1]
                self.assertNotEqual(new, name)
                self.assertEqual(self.query('SELECT item, time FROM hint_seen WHERE name=?'
                                            ' ORDER BY item', new), before)
                out = self.hook('prompt', main)
                self.assertNotIn('reply by beta', out)
                self.assertEqual(self.hook('prompt', main), '')
                self.ok('note', tid, '--kind', 'reply', '--session', other, stdin='new answer')
                self.assertIn('reply by beta', self.hook('prompt', main))
                self.hook('end', main)
                self.hook('end', replacement)

    def test_new_sub_session_inherits_seen_hints(self):
        self.hook('start', 'main')
        other = self.start('beta')
        tid = self.ok('add', 'question', '--type', 'request', '--to', 'alpha',
                      '--session', other)
        self.assertIn('request #%s ' % tid, self.hook('prompt', 'main'))
        before = self.query("SELECT item, time FROM hint_seen WHERE name='alpha' ORDER BY item")
        self.hook('start', 'side')
        self.ok('board', '--mine', env=self.caller('auto'), cwd=self.repo)
        for hsid in ('side', 'auto'):
            self.assertEqual(self.query('SELECT item, time FROM hint_seen WHERE name=?'
                                        ' ORDER BY item', self.identity(hsid)[1]), before)

    def test_main_checkout_restores_own_sub_name_and_seen_hints(self):
        linked = self.linked_worktree()
        for action in ('command', 'hook'):
            with self.subTest(action=action):
                main, side = 'main-' + action, 'side-' + action
                self.hook('start', main)
                self.hook('start', side)
                token, sub = self.identity(side)
                self.assertEqual(sub, 'alpha-sub-' + hashlib.sha256(token.encode()).hexdigest()[:4])
                # A live holder in this checkout still prevents recovery.
                self.ok('board', '--mine', env=self.caller(side), cwd=self.repo)
                self.assertEqual(self.identity(side), (token, sub))
                other = self.start('beta')
                tid = self.ok('add', 'side question', '--new', '--type', 'request',
                              '--to', 'beta', env=self.caller(side), cwd=self.repo)
                self.ok('claim', tid, '--session', other)
                self.ok('note', tid, '--kind', 'reply', '--session', other, stdin='answer')
                self.assertIn('reply by beta', self.hook('prompt', side))
                self.ok('close', tid, env=self.caller(side), stdin='done\nnothing\nnone')
                before = self.query('SELECT item, time FROM hint_seen WHERE name=?', sub)
                self.hook('end', side)
                self.hook('end', main)
                self.hook('start', 'linked-' + action, cwd=linked)
                self.assertEqual(self.identity('linked-' + action)[1], 'alpha')
                with contextlib.closing(sqlite3.connect(self.db)) as conn:
                    # Existing base marks win on conflict; marks unique to the sub name copy.
                    item = before[0][0]
                    conn.execute('INSERT OR REPLACE INTO hint_seen (name, item, time)'
                                 " VALUES ('alpha', ?, '2000-01-01T00:00:00Z')", (item,))
                    conn.commit()
                base_before = dict(self.query("SELECT item, time FROM hint_seen WHERE name='alpha'"))
                if action == 'command':
                    self.ok('board', '--mine', env=self.caller(side), cwd=self.repo)
                else:
                    self.hook('start', side)
                self.assertEqual(self.identity(side), (token, 'alpha'))
                expected = dict(before)
                expected.update(base_before)
                self.assertEqual(dict(self.query("SELECT item, time FROM hint_seen WHERE name='alpha'")),
                                 expected)
                self.assertNotIn('reply by beta', self.hook('prompt', side))
                self.hook('end', side)
                self.hook('end', 'linked-' + action, cwd=linked)

    def test_session_moving_into_main_checkout_blocks_a_second_main_session(self):
        linked = self.linked_worktree()
        self.hook('start', 'mover', cwd=linked)
        self.assertEqual(self.identity('mover')[1], 'alpha')
        self.hook('start', 'mover', cwd=self.repo)
        self.assertEqual(self.identity('mover')[1], 'alpha')
        self.hook('start', 'second', cwd=self.repo)
        self.assertTrue(self.identity('second')[1].startswith('alpha-sub-'))

    def test_session_leaving_main_checkout_frees_its_name_there(self):
        linked = self.linked_worktree()
        self.hook('start', 'leaver', cwd=self.repo)
        self.assertEqual(self.identity('leaver')[1], 'alpha')
        self.ok('board', '--mine', env=self.caller('leaver'), cwd=linked)
        self.hook('start', 'second', cwd=self.repo)
        self.assertEqual(self.identity('second')[1], 'alpha')

    def test_resume_outside_main_checkout_keeps_sub_name_without_git(self):
        linked = self.linked_worktree()
        self.hook('start', 'main')
        self.hook('start', 'side', cwd=linked)
        original = self.identity('side')
        self.hook('end', 'main')
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            kb = load_kb_module()
            with contextlib.closing(kb.connect()) as conn:
                with unittest.mock.patch.object(kb, 'git', side_effect=AssertionError('resume ran git')):
                    for cwd in (linked, self.tmp):
                        self.assertEqual(kb.register(conn, 'claude', 'side', cwd), original)

    def test_main_checkout_stat_path_matches_derive_name_and_resume_needs_no_git(self):
        nested = os.path.join(self.repo, 'nested')
        os.mkdir(nested)
        alias = os.path.join(self.tmp, 'alias')
        os.symlink(nested, alias)
        self.hook('start', 'main')
        original = self.identity('main')
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            kb = load_kb_module()
            with contextlib.closing(kb.connect()) as conn:
                for cwd in (self.repo, nested, alias):
                    with self.subTest(cwd=cwd):
                        name, worktree = kb.derive_name(cwd)
                        self.assertEqual(kb.main_checkout_path(cwd), worktree)
                        self.assertEqual(kb.safe_name(kb.repo_from_common(os.path.join(worktree, '.git'))),
                                         name)
                        with unittest.mock.patch.object(kb, 'git', side_effect=AssertionError('resume ran git')):
                            self.assertEqual(kb.register(conn, 'claude', 'main', cwd), original)

    def test_name_cannot_take_a_live_name(self):
        self.hook('start', 'main')
        self.hook('start', 'side')
        original = self.identity('side')
        r = self.kb('name', 'alpha', env=self.caller('side'), cwd=self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn('another live session', r.stderr)
        self.assertEqual(self.identity('side'), original)
        self.hook('end', 'main')
        self.ok('name', 'alpha', env=self.caller('side'), cwd=self.repo)
        self.assertEqual(self.identity('side'), (original[0], 'alpha'))

    def test_prompt_and_stop_track_checkout_movement(self):
        linked = self.linked_worktree()
        for event in ('prompt', 'stop'):
            with self.subTest(event=event):
                mover, second, third = event + '-mover', event + '-second', event + '-third'
                self.hook('start', mover, cwd=linked)
                self.hook(event, mover, cwd=self.repo)
                self.assertEqual(self.query('SELECT worktree FROM session WHERE harness_session=?',
                                            mover), [(self.repo,)])
                self.hook('start', second)
                self.assertRegex(self.identity(second)[1], r'^alpha-sub-[0-9a-f]{4}$')
                self.hook('end', second)
                self.hook(event, mover, cwd=linked)
                self.hook('start', third)
                self.assertEqual(self.identity(third)[1], 'alpha')
                self.hook('end', mover, cwd=linked)
                self.hook('end', third)

    def test_replayed_existing_guest_resume_checks_occurrence_time(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            kb = load_kb_module()
            with contextlib.closing(kb.connect()) as conn:
                host = kb.host_installation(conn)
                current = datetime.datetime.now(datetime.timezone.utc)
                def event(inst, hsid, kind, minutes, live):
                    t = kb.fmt_time(current - datetime.timedelta(minutes=minutes))
                    kb.apply_event(conn, dict(inst=inst, harness='claude', hsid=hsid,
                        event=kind, source='', cwd=self.repo, time=t, received=kb.now(),
                        event_id=None, live=live, name='alpha', toplevel=self.repo, host='test'))
                event('guest', 'guest', 'start', 50, False)
                event('guest', 'guest', 'end', 45, False)
                event(host, 'host', 'start', 40, True)
                event(host, 'host', 'end', 20, True)
                event('guest', 'guest', 'start', 30, False)
                self.assertRegex(self.identity('guest')[1], r'^alpha-sub-[0-9a-f]{4}$')

    def test_superseded_replay_cannot_rename_current_custom_name(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            kb = load_kb_module()
            with contextlib.closing(kb.connect()) as conn:
                current = datetime.datetime.now(datetime.timezone.utc)
                def event(hsid, kind, minutes):
                    t = kb.fmt_time(current - datetime.timedelta(minutes=minutes))
                    kb.apply_event(conn, dict(inst='guest', harness='claude', hsid=hsid,
                        event=kind, source='', cwd=self.repo, time=t, received=t,
                        event_id=None, live=False, name='focus', toplevel=self.repo, host='test'))
                event('holder', 'start', 50)
                event('side', 'start', 55)
                event('holder', 'end', 20)
                event('side', 'start', 10)
                with kb.transaction(conn):
                    conn.execute("UPDATE session SET name='focus', last_seen=?"
                                 " WHERE harness_session='side'", (kb.now(),))
                for kind in ('stop', 'start'):
                    event('side', kind, 30)
                    self.assertEqual(self.identity('side')[1], 'focus')

    def test_prompt_rereads_name_after_acquiring_transaction(self):
        self.hook('start', 'main')
        self.hook('start', 'side')
        token, sub = self.identity('side')
        self.hook('end', 'main')
        with mock.patch.dict(os.environ, self.env, clear=True):
            kb = load_kb_module()
            original = kb.find_harness_session
            reads = []
            def concurrent_rename(conn, *key):
                row = original(conn, *key)
                reads.append(row)
                if len(reads) == 1:
                    conn.execute("UPDATE session SET name='focus' WHERE token=?", (token,))
                return row
            args = types.SimpleNamespace(format='text', harness='claude')
            event = dict(event='prompt', source='', harness='claude', hsid='side', cwd=self.repo)
            with mock.patch.object(kb, 'hook_input', return_value={}), \
                    mock.patch.object(kb, 'hook_event', return_value=event), \
                    mock.patch.object(kb, 'find_harness_session', side_effect=concurrent_rename), \
                    contextlib.redirect_stdout(io.StringIO()):
                kb.run_hook(args)
        self.assertGreaterEqual(len(reads), 2)
        self.assertEqual(self.identity('side'), (token, 'focus'))
