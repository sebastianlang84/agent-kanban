"""Behaviour ported from the beelink kb (legacy/kb_a.py) onto the pidso base."""
import json
import os
import sqlite3
import stat
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_kb import KbCase, context  # noqa: E402


class PortedTest(KbCase):
    def setUp(self):
        super().setUp()
        self.alice = self.start('alice')
        self.bob = self.start('bob')

    def claude(self, hsid, name='SessionStart', **extra):
        return json.dumps(dict(session_id=hsid, cwd=self.tmp, hook_event_name=name, **extra))

    def hook(self, event, hsid, harness='claude', name='SessionStart', **extra):
        r = self.kb('hook', event, '--harness', harness, stdin=self.claude(hsid, name, **extra))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    # --- the 2026-10-13 incident: a resumed session keeps its tickets --------------------

    def test_end_never_releases_and_resume_clears_the_flag(self):
        self.hook('start', 'cc-dev')
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-dev')
        tid = self.ok('add', 'dns fix', '--repo', 'r', env=env)
        self.assertEqual(self.ticket(tid)[0], 'doing')  # a harness session claims what it adds
        self.hook('end', 'cc-dev', name='SessionEnd', reason='other')
        self.ok('board')  # any other command: nothing is released automatically
        self.assertEqual(self.ticket(tid)[0], 'doing')
        self.assertIn('session-ended', self.board_line(tid))
        # meanwhile another session may take it over only explicitly
        self.assertEqual(self.code('claim', tid, '--session', self.bob), 1)
        err = self.kb('claim', tid, '--session', self.bob).stderr
        self.assertIn('kb claim --take %s' % tid, err)
        # the session resumes (Claude --resume keeps the id): the flag goes, --take is refused
        self.hook('start', 'cc-dev', source='resume')
        self.assertNotIn('session-ended', self.board_line(tid))
        self.assertEqual(self.code('claim', tid, '--take', '--session', self.bob), 1)
        self.assertEqual(self.ticket(tid)[0], 'doing')

    def test_take_over_an_ended_holder(self):
        self.hook('start', 'cc-old')
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-old')
        tid = self.ok('add', 'left behind', '--repo', 'r', env=env)
        self.hook('end', 'cc-old', name='SessionEnd', reason='other')
        self.assertIn('taken over (session-ended',
                      self.ok('claim', tid, '--take', '--session', self.bob))
        self.assertEqual(self.ticket(tid)[1], self.bob)
        self.assertIn(('event', ), [n[:1] for n in self.notes(tid)])

    def test_take_over_an_idle_holder(self):
        tid = self.ok('add', 'slow', '--claim', '--repo', 'r', '--session', self.alice)
        self.assertEqual(self.code('claim', tid, '--take', '--session', self.bob), 1)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE ticket SET last_activity='2020-01-01T00:00:00Z'")
        conn.commit()
        conn.close()
        self.assertIn('idle>24h', self.ok('claim', tid, '--take', '--session', self.bob))

    def test_start_hook_names_the_session_and_takeover_candidates(self):
        out = context(self.hook('start', 'cc-1'))
        self.assertIn('this session is `%s`' % os.path.basename(self.tmp), out)
        self.assertEqual(self.code('hook', 'start', '--harness', 'claude'), 0)

    # --- identity ---------------------------------------------------------------------

    def test_global_session_option_and_name_and_end(self):
        tid = self.ok('--session', self.alice, 'add', 'x', '--claim', '--repo', 'r')
        self.assertEqual(self.ticket(tid)[1], self.alice)
        self.ok('--session', self.alice, 'name', 'alice2')
        self.assertEqual(self.query('SELECT name FROM session WHERE token=?', self.alice),
                         [('alice2',)])
        self.assertIn('1 held ticket', self.ok('end', '--session', self.alice))
        self.assertEqual(self.ticket(tid)[0], 'doing')  # ending never releases
        self.assertEqual(self.notes(tid)[-1], ('session-ended', 'session ended'))

    def test_codex_thread_id_and_pi_kb_session(self):
        env = dict(self.env, CODEX_THREAD_ID='th-1')
        tid = self.ok('add', 'codex work', '--repo', 'r', env=env)
        self.assertEqual(self.query('SELECT harness, harness_session FROM session s JOIN ticket t'
                                    ' ON t.assignee=s.token WHERE t.id=?', tid),
                         [('codex', 'th-1')])
        env = dict(self.env, KB_SESSION='pi:p-7')
        tid = self.ok('add', 'pi work', '--repo', 'r', env=env)
        self.assertEqual(self.query('SELECT harness, harness_session FROM session s JOIN ticket t'
                                    ' ON t.assignee=s.token WHERE t.id=?', tid),
                         [('pi', 'p-7')])

    def test_conflicting_codex_ids_are_refused(self):
        env = dict(self.env, CODEX_SESSION_ID='outer', CODEX_THREAD_ID='inner')
        r = self.kb('add', 'x', '--repo', 'r', env=env)
        self.assertEqual(r.returncode, 3)
        self.assertIn('conflicting codex session ids', r.stderr)
        same = dict(self.env, CODEX_SESSION_ID='one', CODEX_THREAD_ID='one')
        self.assertEqual(self.code('add', 'y', '--repo', 'r', env=same), 0)
        # an unambiguous other harness, selected explicitly, still works
        mixed = dict(env, CLAUDE_CODE_SESSION_ID='cc-x')
        self.assertEqual(self.code('add', 'z', '--repo', 'r', '--harness', 'claude', env=mixed), 0)

    def test_notify_on_first_use_dispatches_the_first_note(self):
        folder = os.path.join(self.tmp, 'notify.d')
        os.mkdir(folder)
        out = os.path.join(self.tmp, 'first')
        script = os.path.join(folder, '10-record')
        with open(script, 'w') as f:
            f.write('#!/bin/sh\necho "$KB_FIRST_NOTE-$KB_LAST_NOTE" >> %s\n' % out)
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
        self.ok('add', 'q', '--type', 'request', '--to', 'bob', '--session', self.alice)
        import time
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.exists(out):
            time.sleep(0.05)
        with open(out) as f:
            self.assertEqual(f.read().split(), ['1-1'])

    def test_curator_close_of_a_held_request_needs_force(self):
        cur = self.start('curator')
        req = self.ok('add', 'q', '--type', 'request', '--to', 'bob', '--session', self.alice)
        self.ok('claim', req, '--session', self.bob)
        self.ok('note', req, '--reply', 'a', '--session', self.bob)
        self.assertEqual(self.code('close', req, '--result', 'x', '--session', cur), 1)
        self.ok('close', req, '--force', '--session', cur, stdin='forced')
        self.assertEqual(self.ticket(req)[0], 'done')

    def test_no_identity_outside_a_terminal_is_refused(self):
        r = self.kb('board', '--mine')
        self.assertEqual(r.returncode, 3)
        self.assertIn('only a human at a terminal acts as curator', r.stderr)

    # --- add, close, notes ---------------------------------------------------------------

    def test_token_session_files_todo_unless_claim(self):
        todo = self.ok('add', 'for later', '--repo', 'r', '--session', self.alice)
        self.assertEqual(self.ticket(todo)[:2], ('todo', None))
        mine = self.ok('add', 'now', '--claim', '--repo', 'r', '--session', self.alice)
        self.assertEqual(self.ticket(mine)[:2], ('doing', self.alice))
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='cc-2')
        later = self.ok('add', 'harness later', '--todo', '--repo', 'r', env=env)
        self.assertEqual(self.ticket(later)[0], 'todo')
        # the opener closes its own todo task
        self.ok('close', todo, '--where', '-', '--changed', 'nothing', '--open', 'nothing',
                '--session', self.alice)
        self.assertEqual(self.ticket(todo)[0], 'done')

    def test_request_gets_a_first_note_and_note_flag(self):
        r1 = self.ok('add', 'question', '--type', 'request', '--to', 'bob', '--session', self.alice)
        self.assertEqual(self.notes(r1), [('request', '(request text is in the title)')])
        r2 = self.ok('add', 'other question', '--type', 'request', '--to', 'bob',
                     '--note', 'details', '--session', self.alice)
        self.assertEqual(self.notes(r2), [('request', 'details')])

    def test_similar_titles_by_words(self):
        self.ok('add', 'Backup timer for the board database', '--repo', 'r',
                '--session', self.alice)
        r = self.kb('add', 'Board database: backup timer', '--repo', 'r', '--session', self.alice)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.code('add', 'Board database: backup timer', '--new', '--repo', 'r',
                                   '--session', self.alice), 0)

    def test_close_flags_and_three_parts(self):
        tid = self.ok('add', 't', '--claim', '--repo', 'r', '--session', self.alice)
        self.assertEqual(self.code('close', tid, '--where', 'x', '--session', self.alice), 3)
        self.ok('close', tid, '--where', 'beelink', '--changed', 'kb', '--open', 'nothing',
                '--session', self.alice)
        self.assertEqual(self.notes(tid)[-1],
                         ('close', 'deployed: beelink\nchanged: kb\nopen: nothing'))

    def test_reply_needs_the_claim_and_request_close_rules(self):
        req = self.ok('add', 'q', '--type', 'request', '--to', 'bob', '--session', self.alice)
        self.assertEqual(self.code('note', req, '--reply', 'answer', '--session', self.bob), 1)
        self.ok('claim', req, '--session', self.bob)
        self.ok('note', req, '--reply', 'the', 'answer', '--session', self.bob)
        self.assertEqual(self.notes(req)[-1], ('reply', 'the answer'))
        self.ok('close', req, '--result', 'thanks', '--session', self.bob)  # the assignee closes
        self.assertEqual(self.ticket(req)[0], 'done')

    def test_withdraw_an_unanswered_request(self):
        req = self.ok('add', 'q', '--type', 'request', '--to', 'bob', '--session', self.alice)
        self.assertEqual(self.code('close', req, '--withdraw', '--session', self.bob), 1)
        self.ok('close', req, '--withdraw', '--session', self.alice)
        self.assertEqual(self.ticket(req)[0], 'abandoned')
        answered = self.ok('add', 'q2', '--type', 'request', '--to', 'bob',
                           '--session', self.alice)
        self.ok('claim', answered, '--session', self.bob)
        self.ok('note', answered, '--reply', 'yes', '--session', self.bob)
        self.assertEqual(self.code('close', answered, '--withdraw', '--session', self.alice), 1)

    def test_release_why_is_the_handoff_note(self):
        tid = self.ok('add', 't', '--claim', '--repo', 'r', '--session', self.alice)
        self.ok('release', tid, '--why', 'done half; tests open', '--session', self.alice)
        self.assertEqual(self.notes(tid)[-1], ('release', 'done half; tests open'))

    # --- labels, backlog, board -----------------------------------------------------------

    def test_label_and_hidden_backlog(self):
        tid = self.ok('add', 'idea', '--repo', 'r', '--session', self.alice)
        self.ok('label', tid, 'backlog', 'idea', '--session', self.alice)
        self.assertEqual(self.query('SELECT labels FROM ticket WHERE id=?', tid), [('backlog,idea',)])
        self.assertEqual(self.notes(tid)[-1], ('event', 'labels: backlog,idea'))
        out = self.ok('board')
        self.assertNotIn('#%s ' % tid, out)
        self.assertIn('1 backlog ticket hidden', out)
        self.assertIn('#%s ' % tid, self.ok('board', '--label', 'backlog'))
        self.assertIn('#%s ' % tid, self.ok('board', '--all'))
        self.ok('label', tid, '--remove', 'backlog', '--session', self.alice)
        self.assertIn('#%s ' % tid, self.ok('board'))
        req = self.ok('add', 'q', '--type', 'request', '--to', 'bob', '--session', self.alice)
        self.assertEqual(self.code('label', req, 'backlog', '--session', self.alice), 1)

    def test_board_json_and_closed(self):
        tid = self.ok('add', 'json me', '--claim', '--repo', 'r', '--label', 'quality-loop',
                      '--session', self.alice)
        items = json.loads(self.ok('board', '--repo', 'r', '--label', 'quality-loop', '--json'))
        self.assertEqual([(i['id'], i['title'], i['status']) for i in items],
                         [(int(tid), 'json me', 'doing')])
        self.ok('close', tid, '--where', '-', '--changed', '-', '--open', '-',
                '--session', self.alice)
        self.assertIn('#%s ' % tid, self.ok('board', '--closed', '1'))
        self.assertNotIn('#%s ' % tid, self.ok('board'))

    def test_board_to_includes_any(self):
        r1 = self.ok('add', 'for bob', '--type', 'request', '--to', 'bob', '--session', self.alice)
        r2 = self.ok('add', 'for anyone', '--type', 'request', '--to', 'any',
                     '--session', self.alice)
        out = self.ok('board', '--to', 'bob')
        self.assertIn('#%s ' % r1, out)
        self.assertIn('#%s ' % r2, out)

    # --- hooks, notify, backup --------------------------------------------------------------

    def test_hooks_respect_opt_out(self):
        for var in ('KB', 'BRAIN_SESSION_CONTEXT'):
            r = self.kb('hook', 'start', '--harness', 'claude', stdin=self.claude('cc-off'),
                        env=dict(self.env, **{var: '0'}))
            self.assertEqual((r.returncode, r.stdout), (0, ''))
        self.assertEqual(self.query("SELECT count(*) FROM session WHERE harness='claude'"), [(0,)])

    def test_hook_debug_log_records_keys_only(self):
        open(os.path.join(self.tmp, 'hook-debug.log'), 'w').close()
        self.hook('prompt', 'cc-3', name='UserPromptSubmit', prompt='secret words')
        with open(os.path.join(self.tmp, 'hook-debug.log')) as f:
            line = json.loads(f.read().splitlines()[-1])
        self.assertEqual(line['event'], 'prompt')
        self.assertIn('prompt', line['keys'])
        self.assertNotIn('secret', json.dumps(line))

    def test_notify_d_runs_after_new_notes(self):
        folder = os.path.join(self.tmp, 'notify.d')
        os.mkdir(folder)
        out = os.path.join(self.tmp, 'notified')
        script = os.path.join(folder, '10-record')
        with open(script, 'w') as f:
            f.write('#!/bin/sh\necho "$KB_FIRST_NOTE-$KB_LAST_NOTE" >> %s\n' % out)
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
        self.ok('board')  # first run: notify.last starts at the newest note
        tid = self.ok('add', 'q', '--type', 'request', '--to', 'bob', '--session', self.alice)
        self.ok('note', tid, 'more', '--session', self.alice)
        import time
        deadline = time.time() + 10
        while time.time() < deadline and not (os.path.exists(out)
                                              and len(open(out).read().split()) >= 2):
            time.sleep(0.05)
        with open(out) as f:
            self.assertEqual(f.read().split(), ['1-1', '2-2'])

    def test_backup_dir_keep(self):
        folder = os.path.join(self.tmp, 'bk')
        for _ in range(3):
            self.ok('backup', '--dir', folder, '--keep', '2')
            import time
            time.sleep(1.1)
        files = sorted(os.listdir(folder))
        self.assertEqual(len(files), 2)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(folder, files[0])).st_mode), 0o600)

    def test_database_in_an_ignored_path_of_a_git_tree(self):
        repo = os.path.join(self.tmp, 'home-repo')
        os.mkdir(repo)
        os.system('git -C %s init -q && echo "state/" > %s/.gitignore' % (repo, repo))
        env = dict(self.env, KB_DB=os.path.join(repo, 'state', 'kb.sqlite'))
        self.assertEqual(self.code('start', '--as', 'x', env=env), 0)
        env = dict(self.env, KB_DB=os.path.join(repo, 'tracked', 'kb.sqlite'))
        self.assertEqual(self.code('start', '--as', 'x', env=env), 3)

    def test_kb_db_from_the_config(self):
        cfg = os.path.join(self.home, '.config', 'agent-kanban')
        os.makedirs(cfg, exist_ok=True)
        other = os.path.join(self.tmp, 'other.sqlite')
        with open(os.path.join(cfg, 'config'), 'a') as f:
            f.write('KB_DB=%s\n' % other)
        env = {k: v for k, v in self.env.items() if k != 'KB_DB'}
        self.ok('start', '--as', 'cfg', env=env)
        self.assertTrue(os.path.exists(other))


if __name__ == '__main__':
    unittest.main()
