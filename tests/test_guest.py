"""Tests for guest mode (D9): kb forwarding over a fake ssh to kb serve, and kb authorize."""
import json
import os
import shutil
import pty
import secrets
import socket
import sqlite3
import stat
import subprocess
import time
import unittest

from test_kb import KB, REPO, KbCase, context, schema_objects

# Board schema version 4, as written by kb before guest mode (this commit's parent on main).
V4_COMMIT = '9debe4b'

# Stands in for ssh as sshd would run it: the requested command is ignored and the forced
# command of the key's authorized_keys line runs in the host's environment.
FAKE_SSH = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys, time
args, key = sys.argv[1:], None
while args and args[0].startswith('-'):
    opt = args.pop(0)
    if opt in ('-o', '-i'):
        val = args.pop(0)
        if opt == '-i':
            key = val
dest, requested = args[0], ' '.join(args[1:])
mode = os.environ.get('FAKE_SSH_MODE', 'ok')
if mode == 'down':
    sys.stderr.write('ssh: connect to host %s port 22: Connection refused\n' % dest)
    sys.exit(255)
if mode == 'slow':
    time.sleep(10)
host = json.load(open(os.environ['FAKE_SSH_HOST_ENV']))
blob = open(key + '.pub').read().split()[1]
auth = os.path.join(host['HOME'], '.ssh', 'authorized_keys')
for line in open(auth):
    if blob in line.split():
        m = re.match(r'command="([^"]*)"', line)
        break
else:
    m = None
if not m:
    sys.stderr.write('%s: Permission denied (publickey).\n' % dest)
    sys.exit(255)
host.update(SSH_CONNECTION='127.0.0.1 40000 127.0.0.1 22', SSH_ORIGINAL_COMMAND=requested)
os.chdir(host['HOME'])
if mode == 'ack255':  # the host answers, then the connection drops
    r = subprocess.run(['/bin/sh', '-c', m.group(1)], env=host, stdout=subprocess.PIPE)
    sys.stdout.buffer.write(r.stdout)
    sys.stderr.write('Connection to %s closed by remote host.\n' % dest)
    sys.exit(255)
if mode == 'hang':  # deliver, then hang: the host commits, its answer never arrives
    subprocess.run(['/bin/sh', '-c', m.group(1)], env=host, stdout=subprocess.DEVNULL)
    time.sleep(10)
    sys.exit(0)
os.execve('/bin/sh', ['sh', '-c', m.group(1)], host)
'''


def read(path):
    with open(path) as f:
        return f.read()


class GuestCase(KbCase):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, 'bin')
        os.mkdir(self.bin)
        ssh = os.path.join(self.bin, 'ssh')
        with open(ssh, 'w') as f:
            f.write(FAKE_SSH)
        os.chmod(ssh, 0o755)
        self.host_env_file = os.path.join(self.tmp, 'host-env.json')
        with open(self.host_env_file, 'w') as f:
            json.dump(self.env, f)  # KbCase's env: host KB_DB and HOME, KB_HOST='', no ids
        self.curator = self.start('curator')
        self.guests = 0

    def make_guest(self, label, **extra):
        """A guest home with its own key, authorized on the host; its environment."""
        self.guests += 1
        home = os.path.join(self.tmp, 'guest%d' % self.guests)
        os.makedirs(os.path.join(home, '.ssh'))
        key = os.path.join(home, '.ssh', 'kb_guest')
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'me@guest',
                        '-f', key], check=True)
        inst = secrets.token_hex(8)
        self.ok('authorize', '--installation', inst, '--label', label, key + '.pub',
                '--session', self.curator)
        env = dict(self.env, HOME=home, XDG_CONFIG_HOME=os.path.join(home, '.config'),
                   KB_DB=os.path.join(home, 'guest-board.sqlite'), KB_HOST='boardhost',
                   KB_INSTALLATION=inst, KB_SSH_KEY=key, FAKE_SSH_HOST_ENV=self.host_env_file,
                   PATH=self.bin + os.pathsep + self.env['PATH'])
        env.update(extra)
        return env

    def host_installation(self):
        path = os.path.join(self.home, '.config', 'agent-kanban', 'config')
        with open(path) as f:
            return dict(l.strip().split('=', 1) for l in f if '=' in l)['KB_INSTALLATION']

    def serve(self, payload, inst=None, extra_env=None):
        """Run kb serve directly as the forced command would, with raw stdin bytes."""
        env = dict(self.env, SSH_CONNECTION='127.0.0.1 1 127.0.0.1 22', **(extra_env or {}))
        return subprocess.run([KB, 'serve', '--installation', inst or 'ab' * 8], input=payload,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                              cwd=self.tmp, timeout=60)

    def request(self, argv, ctx=None, rest=b''):
        base = {'host': 'g', 'cwd': '/', 'name': 'gs', 'toplevel': '', 'repo': '', 'branch': '',
                'env': {}}
        base.update(ctx or {})
        return json.dumps({'v': 1, 'argv': argv, 'ctx': base}).encode() + b'\n' + rest


class ForwardingTest(GuestCase):
    def test_arguments_and_stdin_arrive_unchanged(self):
        g = self.make_guest('laptop')
        tok = self.ok('start', '--as', 'gw', env=g)
        marker = os.path.join(self.tmp, 'pwned')
        title = 'it\'s "q" $(touch %s); `touch %s` ; rm -rf ~ ü€ \\ *' % (marker, marker)
        tid = self.ok('add', '--claim', title, '--repo', 'r', '--session', tok, env=g)
        self.assertEqual(self.query('SELECT title FROM ticket WHERE id=?', tid), [(title,)])
        self.assertFalse(os.path.exists(marker))
        # Inner whitespace, blank lines, CR LF and non-ASCII arrive byte for byte; kb strips
        # leading and trailing whitespace of every note by design (read_text), local or not.
        text = ' \n  first  line\n\n\n"two" $(x);\t`y`  \n\t\u00fc\u20ac\U0001f600\r\nlast\n\n'
        expected = ('first  line\n\n\n"two" $(x);\t`y`  \n\t\u00fc\u20ac\U0001f600\r\nlast')
        for argv in (['note', tid], ['close', tid]):
            r = subprocess.run([KB] + argv + ['--session', tok], input=text.encode('utf-8'),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=g,
                               cwd=self.tmp, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.notes(tid)[-2:], [('comment', expected), ('close', expected)])

    def test_leading_bom_is_stripped_on_the_host(self):
        g = self.make_guest('laptop')
        tok = self.ok('start', '--as', 'gw', env=g)
        tid = self.ok('add', '--claim', 'bom', '--repo', 'r', '--session', tok, env=g)
        for text in ('\ufeffpiped from PowerShell', 'keep \ufeff inside'):
            r = subprocess.run([KB, 'note', tid, '--session', tok], input=text.encode('utf-8'),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=g,
                               cwd=self.tmp, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.notes(tid)[-2:], [('comment', 'piped from PowerShell'),
                                                ('comment', 'keep \ufeff inside')])

    def test_exit_codes_and_stderr_unchanged(self):
        g = self.make_guest('laptop')
        gtok = self.ok('start', '--as', 'gw', env=g)
        htok = self.start('hw')
        tid = self.ok('add', '--claim', 'held by guest', '--repo', 'r', '--session', gtok, env=g)
        hid = self.ok('add', '--claim', 'other thing on the host', '--repo', 'r', '--session', htok)
        # 1: rule rejection, compared with the same rejection on the host
        rg = self.kb('close', hid, '--session', gtok, env=g, stdin='x')
        other = self.start('other')
        rl = self.kb('close', hid, '--session', other, stdin='x')
        self.assertEqual(rg.returncode, 1)
        self.assertEqual(rg.stderr, rl.stderr)
        # 2: needs --new
        r = self.kb('add', '--claim', 'held by guest', '--repo', 'r', '--session', gtok, env=g)
        self.assertEqual(r.returncode, 2)
        self.assertIn('#%s doing held by guest' % tid, r.stdout)
        self.assertIn('rerun with --new', r.stderr)
        # 3: identity error from the host, and a usage error that stays on the guest
        r = self.kb('board', '--mine', env=g)
        self.assertEqual((r.returncode, r.stderr),
                         (3, self.kb('board', '--mine').stderr))
        self.assertEqual(self.code('claim', env=g), 3)
        self.assertEqual(self.code('show', 99, env=g), 1)

    def test_board_with_open_stdin_pipe_does_not_hang(self):
        g = self.make_guest('laptop')
        tok = self.ok('start', '--as', 'gw', env=g)
        self.ok('add', '--claim', 'visible', '--repo', 'r', '--session', tok, env=g)
        p = subprocess.Popen([KB, 'board'], env=g, cwd=self.tmp, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            p.wait(timeout=20)  # stdin stays open: only a command that needs it may read it
            out = p.stdout.read()
        finally:
            p.stdin.close()
            p.kill()
            p.stdout.close()
            p.stderr.close()
        self.assertEqual(p.returncode, 0)
        self.assertIn(b'visible', out)

    def test_unreachable_host_exits_3_with_one_line(self):
        g = self.make_guest('laptop', FAKE_SSH_MODE='down')
        r = self.kb('board', env=g)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(r.stderr.splitlines(), [
            'kb: board host boardhost unreachable: ssh: connect to host boardhost port 22:'
            ' Connection refused'])
        self.assertFalse(os.path.exists(g['KB_DB']))

    def test_guest_without_installation_or_key(self):
        g = self.make_guest('laptop')
        for missing in ('KB_INSTALLATION', 'KB_SSH_KEY'):
            env = dict(g)
            del env[missing]
            r = self.kb('board', env=env)
            self.assertEqual(r.returncode, 3)
            self.assertIn('run kb setup', r.stderr)
        self.assertFalse(os.path.exists(g['KB_DB']))

    def test_config_file_and_empty_host_forces_local(self):
        g = self.make_guest('laptop')
        conf = os.path.join(g['XDG_CONFIG_HOME'], 'agent-kanban')
        os.makedirs(conf)
        with open(os.path.join(conf, 'config'), 'w') as f:
            f.write('# guest\nKB_HOST=boardhost\nKB_INSTALLATION=%s\nKB_SSH_KEY=%s\n'
                    % (g['KB_INSTALLATION'], g['KB_SSH_KEY']))
        env = {k: v for k, v in g.items() if k not in ('KB_HOST', 'KB_INSTALLATION',
                                                      'KB_SSH_KEY')}
        self.ok('board', env=dict(env, KB_HOST=''))
        self.assertTrue(os.path.exists(g['KB_DB']))  # KB_HOST='' ran locally
        os.remove(g['KB_DB'])
        tok = self.ok('start', '--as', 'fromconf', env=env)
        self.assertEqual(self.query('SELECT installation FROM session WHERE token=?', tok),
                         [(g['KB_INSTALLATION'],)])
        self.assertFalse(os.path.exists(g['KB_DB']))

    def test_version_runs_on_the_guest(self):
        g = self.make_guest('laptop', FAKE_SSH_MODE='down')
        r = self.kb('--version', env=g)
        self.assertEqual((r.returncode, r.stderr), (0, ''))
        self.assertTrue(r.stdout.startswith('kb '), r.stdout)

    def test_host_only_commands_are_refused_on_the_guest(self):
        g = self.make_guest('laptop')
        for argv in (['restore', 'x'], ['recover', '--done'], ['serve', '--installation', 'x'],
                     ['authorize', '--installation', 'x', '--label', 'l', 'k']):
            r = self.kb(*argv, env=g)
            self.assertEqual(r.returncode, 1, argv)
            self.assertEqual(r.stderr, 'kb: %s runs on the host only\n' % argv[0])


class IdentityTest(GuestCase):
    def git(self, *args):
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t'] + list(args),
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_guest_metadata_and_git_unknown(self):
        repo = os.path.join(self.tmp, 'mainrepo')
        wt = os.path.join(self.tmp, 'linked')
        self.git('init', '-q', repo)
        self.git('-C', repo, 'commit', '-q', '--allow-empty', '-m', 'init')
        self.git('-C', repo, 'worktree', 'add', '-q', '-b', 'feat', wt)
        g = self.make_guest('laptop', CLAUDE_CODE_SESSION_ID='c-1')
        tid = self.ok('add', '--claim', 'guest work', env=g, cwd=wt)
        self.assertEqual(self.query(
            'SELECT s.name, s.installation, s.harness, s.harness_session, s.host, t.repo,'
            ' t.host, t.worktree, t.branch FROM ticket t JOIN session s ON s.token=t.opener'
            ' WHERE t.id=?', tid),
            [('mainrepo', g['KB_INSTALLATION'], 'claude', 'c-1', socket.gethostname(),
              'mainrepo', socket.gethostname(), wt, 'feat')])
        self.assertIn('git:unknown', self.board_line(tid))
        # The same worktree held by a host session gets a real git hint.
        hid = self.ok('add', '--claim', 'unrelated item', '--session', self.start('h'), cwd=wt)
        self.assertNotIn('git:unknown', self.board_line(hid))

    def test_session_and_kb_session_through_the_guest(self):
        g = self.make_guest('laptop')
        tok = self.ok('start', '--as', 'gw', env=g)
        tid = self.ok('add', '--claim', 'a', '--repo', 'r', env=dict(g, KB_SESSION=tok))
        self.ok('note', tid, '--session', tok, env=g, stdin='n')
        self.assertEqual(self.ticket(tid)[1], tok)
        # Several harness ids on the guest still need --harness, decided by the host.
        env = dict(g, CLAUDE_CODE_SESSION_ID='c', PI_SESSION_ID='p')
        r = self.kb('board', '--mine', env=env)
        self.assertEqual(r.returncode, 3)
        self.assertIn('pass --harness', r.stderr)
        self.ok('board', '--mine', '--harness', 'pi', env=env)

    def test_whoami_and_sessions_through_the_guest(self):
        g = self.make_guest('laptop', CLAUDE_CODE_SESSION_ID='c-1')
        repo = os.path.join(self.tmp, 'guestrepo')
        self.git('init', '-q', repo)
        out = self.ok('whoami', env=g, cwd=repo)
        self.assertIn('session: guestrepo/claude (not registered yet)', out)
        self.assertIn('installation: %s' % g['KB_INSTALLATION'], out)
        tid = self.ok('add', '--claim', 'guest task', env=g, cwd=repo)
        token = self.query('SELECT opener FROM ticket WHERE id=?', tid)[0][0]
        out = self.ok('whoami', env=g, cwd=repo)
        self.assertEqual(out.splitlines()[0], 'name: guestrepo')
        self.assertIn('installation: %s\nrepo: guestrepo' % g['KB_INSTALLATION'], out)
        self.assertNotIn(token, out)
        plain = {k: v for k, v in g.items() if k != 'CLAUDE_CODE_SESSION_ID'}
        tok = self.ok('start', '--as', 'gw', env=plain)
        out = self.ok('whoami', '--session', tok, env=plain)
        self.assertIn('installation: %s' % g['KB_INSTALLATION'], out)
        self.assertNotIn(tok, out)
        # A guest cannot read another installation's token through whoami.
        self.assertEqual(self.code('whoami', '--session', self.start('h'), env=plain), 1)
        out = self.ok('sessions', env=g)
        line = [l.split() for l in out.splitlines() if l.startswith('guestrepo ')][0]
        self.assertEqual(line[1:], ['claude', '1', '0m', '1/0', g['KB_INSTALLATION']])
        for t, in self.query('SELECT token FROM session'):
            self.assertNotIn(t, out)

    def test_colliding_harness_ids_from_two_guests_and_host(self):
        a = self.make_guest('a', CLAUDE_CODE_SESSION_ID='same')
        b = self.make_guest('b', CLAUDE_CODE_SESSION_ID='same')
        host = dict(self.env, CLAUDE_CODE_SESSION_ID='same')
        ta = self.ok('add', '--claim', 'work of a', '--repo', 'r', env=a)
        tb = self.ok('add', '--claim', 'work of b', '--repo', 'r2', env=b)
        th = self.ok('add', '--claim', 'work of host', '--repo', 'r3', env=host)
        rows = self.query("SELECT installation FROM session WHERE harness_session='same'")
        self.assertEqual(sorted(r[0] for r in rows),
                         sorted([a['KB_INSTALLATION'], b['KB_INSTALLATION'],
                                 self.host_installation()]))
        self.assertEqual(self.code('close', ta, env=b, stdin='no'), 1)
        self.assertEqual(self.code('close', ta, env=host, stdin='no'), 1)
        self.ok('session-ended', env=a)
        self.assertEqual([self.notes(t) for t in (ta, tb, th)],
                         [[('session-ended', 'session ended')], [], []])
        # kb report counts one started session per installation.
        conn = sqlite3.connect(self.db)
        t = self.query("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 hour')")[0][0]
        conn.executemany("INSERT INTO session_event (installation, harness, harness_session,"
                         " event, time) VALUES (?, 'claude', 'same', 'start', ?)",
                         [(i, t) for i, in rows])
        conn.commit()
        conn.close()
        started = [l for l in self.ok('report').splitlines() if 'started in window' in l][0]
        self.assertEqual(started.split()[-1], '3')


class ServeTest(GuestCase):
    def one_line_rejection(self, r):
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(len(r.stderr.splitlines()), 1, r.stderr)
        self.assertEqual(r.stdout, b'')

    def test_disallowed_commands_exit_1(self):
        for argv in (['restore', 'x'], ['recover', '--done'], ['serve', '--installation', 'x'],
                     ['authorize', '--installation', 'x', '--label', 'l', 'k'], ['backup', 'x'],
                     ['setup'], ['hook', 'start', '--harness', 'pi', '--id', 'x'], ['hook'],
                     ['hook', 'ingest', 'x'], ['spool'], ['frobnicate'],
                     ['start', '--as', 'curator'], ['start', '--as=curator'],
                     ['start', '--as', ' curator']):
            self.one_line_rejection(self.serve(self.request(argv)))

    def test_ctx_cannot_set_installation(self):
        inst = 'cd' * 8
        r = self.serve(self.request(['start', '--as', 'x'], {'inst': self.host_installation(),
                                                            'installation': 'ef' * 8}), inst)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.query('SELECT installation FROM session WHERE token=?',
                                    r.stdout.decode().strip()), [(inst,)])
        # The host's own installation id is never served.
        self.one_line_rejection(self.serve(self.request(['board']), self.host_installation()))

    def test_token_of_another_installation_is_rejected(self):
        a, b = self.make_guest('a'), self.make_guest('b')
        ta = self.ok('start', '--as', 'ga', env=a)
        tid = self.ok('add', '--claim', 'x', '--repo', 'r', '--session', ta, env=a)
        htok = self.start('hostside')
        for tok in (ta, htok, self.curator):
            r = self.kb('note', tid, '--session', tok, env=b, stdin='t')
            self.assertEqual(r.returncode, 1, tok)
            self.assertIn('session token rejected', r.stderr)
        self.assertEqual(self.code('note', tid, env=dict(b, KB_SESSION=ta), stdin='t'), 1)

    def test_refuses_without_ssh_connection_or_with_tty(self):
        env = {k: v for k, v in self.env.items() if k != 'SSH_CONNECTION'}
        r = subprocess.run([KB, 'serve', '--installation', 'ab' * 8],
                           input=self.request(['board']), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, timeout=60)
        self.one_line_rejection(r)
        master, slave = pty.openpty()
        try:
            r = subprocess.run([KB, 'serve', '--installation', 'ab' * 8], stdin=slave,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                               env=dict(env, SSH_CONNECTION='1 2 3 4'))
        finally:
            os.close(master)
            os.close(slave)
        self.one_line_rejection(r)

    def test_stdin_body_over_1_mib_is_rejected(self):
        inst = 'cd' * 8
        tok = self.serve(self.request(['start', '--as', 'g']), inst).stdout.decode().strip()
        tid = self.serve(self.request(['add', '--claim', 'x', '--repo', 'r', '--session', tok]),
                         inst).stdout.decode().strip()
        limit = 1024 * 1024
        r = self.serve(self.request(['note', tid, '--session', tok], rest=b'y' * limit), inst)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.one_line_rejection(self.serve(self.request(['note', tid, '--session', tok],
                                                        rest=b'y' * (limit + 1)), inst))
        self.assertEqual([len(t) for _, t in self.notes(tid)], [limit])

    def test_stalled_request_hits_the_deadline(self):
        env = dict(self.env, SSH_CONNECTION='1 2 3 4', KB_SERVE_DEADLINE='1')
        inst = 'cd' * 8
        tok = self.serve(self.request(['start', '--as', 'g']), inst).stdout.decode().strip()
        tid = self.serve(self.request(['add', '--claim', 'x', '--repo', 'r', '--session', tok]),
                         inst).stdout.decode().strip()
        for partial in (self.request(['board'])[:20],  # inside the request line
                        self.request(['note', tid, '--session', tok], rest=b'half a body')):
            p = subprocess.Popen([KB, 'serve', '--installation', inst], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
            p.stdin.write(partial)
            p.stdin.flush()
            t0 = time.time()
            try:
                p.wait(timeout=20)  # stdin stays open: the guest has stalled
            finally:
                p.stdin.close()
            out, err = p.stdout.read(), p.stderr.read()
            p.stdout.close()
            p.stderr.close()
            self.assertLess(time.time() - t0, 10)
            self.assertEqual((p.returncode, out), (1, b''))
            self.assertEqual(err.decode().splitlines(),
                             ['kb: serve rejected: request not received within 1 s'])
        self.assertEqual(self.notes(tid), [])

    def test_malformed_or_oversized_request_exits_1(self):
        big = json.dumps({'v': 1, 'argv': ['board'], 'ctx': {'pad': 'x' * 70000}}).encode()
        for payload in (b'', b'not json\n', b'[1]\n', b'{"v": 2, "argv": ["board"]}\n',
                        self.request(['board']).rstrip(b'\n'), big + b'\n',
                        self.request([]), self.request(['board'], {'host': 5}),
                        self.request(['board'], {'env': {'PATH': '/x'}}), b'\xff\n',
                        b'[' * 60000 + b'\n', b'{"v": 1, "argv": ' + b'[' * 60000 + b'\n'):
            self.one_line_rejection(self.serve(payload))


class CuratorTest(GuestCase):
    def test_curator_actions_via_guest_rejected_but_work_on_host(self):
        g = self.make_guest('laptop')
        alice = self.start('alice')
        held = self.ok('add', '--claim', 'held', '--repo', 'r', '--session', alice)
        todo = self.ok('add', '--claim', 'req', '--type', 'request', '--to', 'x', '--session', alice)
        self.assertEqual(self.code('start', '--as', 'curator', env=g), 1)
        gtok = self.ok('start', '--as', 'gw', env=g)
        for argv in (['close', held, '--force'], ['release', held], ['abandon', todo]):
            self.assertEqual(self.code(*argv, '--session', gtok, env=g, stdin='x'), 1, argv)
            self.assertEqual(self.code(*argv, '--session', self.curator, env=g, stdin='x'), 1)
        self.assertEqual(self.ticket(held)[0], 'doing')
        # The same curator token works from a plain terminal on the host.
        self.ok('close', held, '--force', '--session', self.curator, stdin='forced')
        self.ok('abandon', todo, '--session', self.curator)

    def test_harness_session_holding_the_curator_token_is_not_curator(self):
        alice = self.start('alice')
        held = self.ok('add', '--claim', 'held', '--repo', 'r', '--session', alice)
        env = dict(self.env, CODEX_SESSION_ID='cx-1')
        r = self.kb('close', held, '--force', '--session', self.curator, env=env, stdin='x')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.code('release', held, '--session', self.curator, env=env), 1)
        self.assertEqual(self.code('restore', 'x', '--session', self.curator, env=env), 1)
        self.ok('release', held, '--session', self.curator)


def iso(offset=0):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + offset))


class GuestHookTest(GuestCase):
    """Hooks on a guest (D9 step 5): deadline, spool, replay and kb hook ingest."""

    def home_of(self, g):
        return os.path.dirname(g['KB_DB'])

    def spool(self, g):
        d = os.path.join(self.home_of(g), 'spool')
        return sorted(n for n in os.listdir(d) if n.endswith('.json')) if os.path.isdir(d) else []

    def guest_errors(self, g):
        path = os.path.join(self.home_of(g), 'hook-errors.log')
        return read(path).splitlines() if os.path.exists(path) else []

    def host_errors(self):
        path = os.path.join(self.tmp, 'hook-errors.log')
        return read(path).splitlines() if os.path.exists(path) else []

    def hook(self, g, event, hsid='c-1', **extra):
        """A claude hook on the guest; (result, wall seconds). Exit 0 always."""
        payload = json.dumps({'session_id': hsid, 'cwd': self.tmp})
        t0 = time.monotonic()
        r = self.kb('hook', event, '--harness', 'claude', stdin=payload, env=dict(g, **extra))
        wall = time.monotonic() - t0
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, '')
        return r, wall

    def events(self, inst):
        return self.query('SELECT event_id, event, harness_session, time, received'
                          ' FROM session_event WHERE installation=? ORDER BY id', inst)

    def ev(self, g, event, hsid, t, live=False, inst=None, **extra):
        inst = inst or g['KB_INSTALLATION']
        d = {'id': '%s-%s' % (inst, secrets.token_hex(8)), 'inst': inst, 'harness': 'claude',
             'hsid': hsid, 'event': event, 'source': '', 'cwd': self.tmp, 'host': 'laptop',
             'name': 'gs', 'toplevel': '', 'repo': '', 'time': t, 'live': live}
        d.update(extra)
        return d

    def ingest(self, g, *evs, raw=b''):
        """kb hook ingest directly as the forced command; the acknowledged ids."""
        body = raw + b''.join(json.dumps(e).encode() + b'\n' for e in evs)
        r = self.serve(self.request(['hook', 'ingest'], rest=body), g['KB_INSTALLATION'])
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.decode().splitlines()

    def held_ticket(self, g, hsid):
        tid = self.ok('add', '--claim', 'work of %s' % hsid, '--repo', 'r-' + hsid,
                      env=dict(g, CLAUDE_CODE_SESSION_ID=hsid))
        token = self.ticket(tid)[1]
        return tid, token

    def test_down_host_spools_and_exits_0_silently(self):
        g = self.make_guest('laptop', FAKE_SSH_MODE='down')
        r, _ = self.hook(g, 'start')
        self.assertEqual(r.stdout, '')
        files = self.spool(g)
        self.assertEqual(len(files), 1)
        ev = json.loads(read(os.path.join(self.home_of(g), 'spool', files[0])))
        self.assertTrue(ev['id'].startswith(g['KB_INSTALLATION'] + '-'))
        self.assertEqual(files[0], '%020d-%s.json' % (ev['ns'], ev['id']))
        self.assertEqual((ev['event'], ev['hsid'], ev['name']),
                         ('start', 'c-1', os.path.basename(self.tmp)))
        self.assertEqual(self.events(g['KB_INSTALLATION']), [])
        self.assertEqual(len(self.guest_errors(g)), 1)
        self.assertIn('board host boardhost: exit 255: ssh: connect to host boardhost',
                      self.guest_errors(g)[0])
        self.assertFalse(os.path.exists(g['KB_DB']))  # a guest hook never opens a database
        self.assertEqual(self.ok('spool', env=g), '1 spooled hook event(s)')

    def test_slow_host_hits_the_deadline_and_spools(self):
        g = self.make_guest('laptop', FAKE_SSH_MODE='slow')
        r, wall = self.hook(g, 'end')
        self.assertLess(wall, 1.5)
        self.assertEqual(r.stdout, '')
        self.assertEqual(len(self.spool(g)), 1)
        self.assertIn('no answer within the deadline', self.guest_errors(g)[0])

    def test_deliver_then_hang_then_ok_stores_each_event_once(self):
        g = self.make_guest('laptop', FAKE_SSH_MODE='hang')
        self.hook(g, 'start')
        self.assertEqual(len(self.spool(g)), 1)  # committed on the host, never acknowledged
        self.assertEqual(len(self.events(g['KB_INSTALLATION'])), 1)
        self.hook(g, 'stop', FAKE_SSH_MODE='ok')
        self.assertEqual(self.spool(g), [])
        rows = self.events(g['KB_INSTALLATION'])
        self.assertEqual([r[1] for r in rows], ['start', 'stop'])
        self.assertEqual(len({r[0] for r in rows}), 2)

    def test_replay_in_order_registers_and_notes_the_end(self):
        g = self.make_guest('laptop')
        tid, token = self.held_ticket(g, 'r-1')
        down = dict(g, FAKE_SSH_MODE='down')
        for event in ('start', 'stop', 'end'):
            self.hook(down, event, 'r-1')
        self.assertEqual(len(self.spool(g)), 3)
        self.assertEqual(self.ok('spool', env=g), '3 spooled hook event(s)')
        self.assertEqual(self.ok('spool', '--drain', env=g), 'sent 3, acknowledged 3, 0 left'
                         ' in the spool')
        self.assertEqual(self.spool(g), [])
        self.assertEqual([r[1:3] for r in self.events(g['KB_INSTALLATION'])],
                         [('start', 'r-1'), ('stop', 'r-1'), ('end', 'r-1')])
        self.assertEqual(self.query("SELECT token FROM session WHERE harness_session='r-1'"),
                         [(token,)])
        self.assertEqual(self.notes(tid)[-1], ('session-ended', 'session ended'))
        self.assertIn('session-ended', self.board_line(tid))

    def test_drain_with_the_host_down_reports_unreachable(self):
        g = dict(self.make_guest('laptop'), FAKE_SSH_MODE='down')
        self.hook(g, 'start', 'd-1')
        r = self.kb('spool', '--drain', env=g)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(r.stdout, 'sent 0, acknowledged 0, 1 left in the spool\n')
        self.assertEqual(r.stderr.splitlines(), [
            'kb: board host boardhost unreachable: ssh: connect to host boardhost port 22:'
            ' Connection refused'])
        self.assertEqual(len(self.spool(g)), 1)

    def test_drain_counts_acknowledgements_before_the_connection_drops(self):
        g = self.make_guest('laptop')
        down = dict(g, FAKE_SSH_MODE='down')
        for hsid in ('a-1', 'a-2'):
            self.hook(down, 'start', hsid)
        r = self.kb('spool', '--drain', env=dict(g, FAKE_SSH_MODE='ack255'))
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(r.stdout, 'sent 2, acknowledged 2, 0 left in the spool\n')
        self.assertEqual(r.stderr.splitlines(), [
            'kb: board host boardhost unreachable: Connection to boardhost closed by remote'
            ' host.'])
        self.assertEqual(self.spool(g), [])
        self.assertEqual(len(self.events(g['KB_INSTALLATION'])), 2)

    def test_end_delivered_before_start(self):
        g = self.make_guest('laptop')
        end = self.ev(g, 'end', 'e-2', iso(-60), name='fresh')
        self.assertEqual(self.ingest(g, end), [end['id']])
        # A replayed event moves last_seen to its occurrence, a live one to its receipt.
        self.assertEqual(self.query("SELECT name, started, last_seen, installation FROM session"
                                    " WHERE harness_session='e-2'"),
                         [('fresh', end['time'], end['time'], g['KB_INSTALLATION'])])
        live = self.ev(g, 'stop', 'e-3', iso(-60), live=True)
        self.ingest(g, live)
        self.assertEqual(self.query("SELECT count(*) FROM session s JOIN session_event e"
                                    " ON e.harness_session=s.harness_session"
                                    " WHERE s.harness_session='e-3' AND s.last_seen=e.received"
                                    " AND s.last_seen>?", live['time']), [(1,)])
        start = self.ev(g, 'start', 'e-2', iso(-120))
        self.ingest(g, start)
        self.assertEqual(self.query("SELECT started FROM session WHERE harness_session='e-2'"),
                         [(start['time'],)])  # lowered to the earliest occurrence
        tid, _ = self.held_ticket(g, 'e-1')
        end = self.ev(g, 'end', 'e-1', iso())
        self.ingest(g, end)
        self.ingest(g, self.ev(g, 'start', 'e-1', iso(-120)))
        self.assertEqual(self.ingest(g, end), [end['id']])  # a duplicate: acknowledged only
        self.assertEqual(self.notes(tid), [('session-ended', 'session ended')])
        self.assertEqual(self.query("SELECT time FROM note WHERE kind='session-ended'"),
                         [(end['time'],)])
        self.assertIn('session-ended', self.board_line(tid))

    def test_newer_live_end_is_not_overridden_by_older_replay(self):
        g = self.make_guest('laptop')
        tid, token = self.held_ticket(g, 'n-1')
        activity = self.query('SELECT last_activity FROM ticket WHERE id=?', tid)[0][0]
        self.ingest(g, self.ev(g, 'end', 'n-1', iso(), live=True))
        seen = self.query('SELECT last_seen FROM session WHERE token=?', token)[0][0]
        self.ingest(g, self.ev(g, 'start', 'n-1', iso(-120)),
                    self.ev(g, 'stop', 'n-1', iso(-60)))
        self.assertIn('session-ended', self.board_line(tid))
        self.assertEqual(self.notes(tid), [('session-ended', 'session ended')])
        self.assertEqual(self.query('SELECT last_seen FROM session WHERE token=?', token),
                         [(seen,)])
        self.assertEqual(self.query('SELECT last_activity FROM ticket WHERE id=?', tid),
                         [(activity,)])

    def test_older_replayed_end_after_a_live_start_writes_no_notes(self):
        g = self.make_guest('laptop')
        tid, _ = self.held_ticket(g, 'o-1')
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE ticket SET claimed_at='2020-01-01T00:00:00Z'")
        conn.commit()
        conn.close()
        self.ingest(g, self.ev(g, 'start', 'o-1', iso(), live=True))
        self.ingest(g, self.ev(g, 'end', 'o-1', iso(-60)))
        self.assertEqual(self.notes(tid), [])
        # Without the later start the same replayed end notes the ticket.
        self.ingest(g, self.ev(g, 'end', 'o-1', iso(1)))  # capped at receipt
        self.assertEqual(self.notes(tid), [('session-ended', 'session ended')])

    def test_start_replayed_a_day_late_counts_on_its_occurrence_day(self):
        g = self.make_guest('laptop')
        day = time.strftime('%Y-%m-%d', time.gmtime(time.time() - 86400))
        self.ingest(g, self.ev(g, 'start', 'd-1', day + 'T12:00:00Z'))

        def started(since):
            out = self.ok('report', '--since', since, '--days', '1')
            lines = [l for l in out.splitlines() if 'started in window' in l]
            return lines[0].split()[-1] if lines else '0'
        self.assertEqual(started(day), '1')
        self.assertEqual(started(time.strftime('%Y-%m-%d', time.gmtime())), '0')

    def test_concurrent_hook_writers_write_distinct_files(self):
        barrier = os.path.join(self.tmp, 'barrier')
        g = self.make_guest('laptop', FAKE_SSH_MODE='down', KB_TEST_BARRIER=barrier)
        procs = [self.spawn(['hook', 'start', '--harness', 'pi', '--id', 'w-%d' % i, '--cwd',
                             self.tmp], g) for i in range(10)]
        self.release_barrier(barrier, 10)
        for p in procs:
            p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0)
        files = self.spool(g)
        self.assertEqual(len(files), 10)
        ids = {json.loads(read(os.path.join(self.home_of(g), 'spool', f)))['hsid']
               for f in files}
        self.assertEqual(ids, {'w-%d' % i for i in range(10)})

    def test_two_concurrent_drains_store_no_duplicates(self):
        g = self.make_guest('laptop', FAKE_SSH_MODE='down')
        for i in range(10):
            self.hook(g, 'stop', 'dr-%d' % i)
        self.assertEqual(len(self.spool(g)), 10)
        ok = dict(g, FAKE_SSH_MODE='ok')
        procs = [self.spawn(['spool', '--drain'], ok) for _ in range(2)]
        for p in procs:
            p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0)
        rows = self.events(g['KB_INSTALLATION'])
        self.assertEqual(len(rows), 10)
        self.assertEqual(len({r[0] for r in rows}), 10)
        self.assertEqual(self.spool(g), [])

    def test_hundred_spooled_events_and_a_slow_host_stay_within_the_deadline(self):
        g = self.make_guest('laptop', FAKE_SSH_MODE='slow')
        d = os.path.join(self.home_of(g), 'spool')
        os.makedirs(d)
        ns = time.time_ns() - 10**12
        for i in range(100):
            e = self.ev(g, 'stop', 'h-%d' % i, iso(-1000))
            with open(os.path.join(d, '%020d-%s.json' % (ns + i, e['id'])), 'w') as f:
                json.dump(e, f)
        _, wall = self.hook(g, 'end')
        self.assertLess(wall, 1.5)
        self.assertEqual(len(self.spool(g)), 101)

    def test_prompt_via_guest(self):
        g = self.make_guest('laptop')
        self.hook(g, 'start', 'p-1')
        name = self.query("SELECT name FROM session WHERE harness_session='p-1'")[0][0]
        tid = self.ok('add', '--claim', 'please look', '--type', 'request', '--to', name,
                      '--session', self.curator)
        r, _ = self.hook(g, 'prompt', 'p-1')
        self.assertIn('request #%s "please look"' % tid, context(r.stdout))
        self.assertEqual(self.hook(g, 'prompt', 'p-1')[0].stdout, '')  # shown once
        self.ok('add', '--claim', 'second ask', '--type', 'request', '--to', name, '--session',
                self.curator)
        r, wall = self.hook(g, 'prompt', 'p-1', FAKE_SSH_MODE='slow')
        self.assertLess(wall, 1.5)
        self.assertEqual(r.stdout, '')
        self.assertEqual(self.spool(g), [])
        r, _ = self.hook(g, 'prompt', 'p-1', FAKE_SSH_MODE='down')
        self.assertEqual((r.stdout, self.spool(g)), ('', []))

    def test_guard_via_guest(self):
        g = self.make_guest('laptop')
        self.hook(g, 'start', 'w-1')

        def guard(command, **extra):
            payload = json.dumps({'session_id': 'w-1', 'cwd': self.tmp, 'tool_name': 'Bash',
                                  'tool_input': {'command': command}})
            t0 = time.monotonic()
            r = self.kb('hook', 'guard', '--harness', 'claude', stdin=payload,
                        env=dict(g, **extra))
            self.assertEqual((r.returncode, r.stderr), (0, ''))
            return r.stdout, time.monotonic() - t0
        out, _ = guard('cd x && git push')
        self.assertIn('this git push runs', json.loads(out)['hookSpecificOutput']
                      ['additionalContext'])
        self.held_ticket(g, 'w-1')
        self.assertEqual(guard('git push')[0], '')
        errors = len(self.guest_errors(g))
        # No ssh for a command that is not guarded: the down host logs nothing.
        self.assertEqual(guard('ls', FAKE_SSH_MODE='down')[0], '')
        self.assertEqual(len(self.guest_errors(g)), errors)
        for mode in ('down', 'slow'):
            out, wall = guard('git commit -m x', FAKE_SSH_MODE=mode)
            self.assertLess(wall, 1.5)
            self.assertEqual((out, self.spool(g)), ('', []))
        self.assertEqual(len(self.guest_errors(g)), errors + 2)
        self.assertIn('no answer within the deadline', self.guest_errors(g)[-1])
        self.assertEqual(self.host_errors(), [])

    def test_poison_event_is_acknowledged_and_logged(self):
        g = self.make_guest('laptop')
        bogus = self.ev(g, 'bogus', 'x-1', iso())
        badtimes = [self.ev(g, 'stop', 'x-1', t) for t in (
            'yesterday', '2026-9-1T00:00:00Z', '2026-09-01T0:00:00Z', ' 2026-09-01T00:00:00Z',
            '2026-09-01T00:00:00+00:00', '2026-09-01 00:00:00Z', '\u0662026-09-01T00:00:00Z')]
        good = self.ev(g, 'stop', 'x-1', iso())
        acked = self.ingest(g, bogus, *badtimes, good, raw=b'not json\n')
        self.assertEqual(acked, [e['id'] for e in [bogus] + badtimes + [good]])
        self.assertEqual([r[0] for r in self.events(g['KB_INSTALLATION'])], [good['id']])
        errors = self.host_errors()
        self.assertEqual(len(errors), 2 + len(badtimes))
        self.assertIn('not JSON', errors[0])
        self.assertIn('%s is malformed' % bogus['id'], errors[1])
        for e, line in zip(badtimes, errors[2:]):
            self.assertIn('%s has an invalid time' % e['id'], line)

    def test_event_of_another_installation_is_rejected(self):
        g = self.make_guest('laptop')
        other = 'ef' * 8
        foreign = self.ev(g, 'start', 'f-1', iso(), inst=other)
        spoofed = dict(self.ev(g, 'start', 'f-2', iso()), id=other + '-' + '01' * 8)
        self.assertEqual(self.ingest(g, foreign, spoofed), [foreign['id'], spoofed['id']])
        self.assertEqual(self.query('SELECT count(*) FROM session_event'), [(0,)])
        self.assertEqual(self.query("SELECT count(*) FROM session WHERE harness_session"
                                    " IN ('f-1', 'f-2')"), [(0,)])
        self.assertEqual(len(self.host_errors()), 2)
        self.assertIn('not of installation %s' % g['KB_INSTALLATION'], self.host_errors()[0])

    def test_deadline_is_clamped_and_logged(self):
        g = self.make_guest('laptop', FAKE_SSH_MODE='slow')
        for value in ('5', 'abc', '0'):
            _, wall = self.hook(g, 'stop', KB_HOOK_DEADLINE=value)
            self.assertLess(wall, 1.5, value)
        logged = [l for l in self.guest_errors(g) if 'KB_HOOK_DEADLINE' in l]
        self.assertEqual(len(logged), 3)
        self.assertEqual(len(self.spool(g)), 3)

    def test_event_is_spooled_before_any_network_io(self):
        g = self.make_guest('laptop', FAKE_SSH_MODE='slow')
        p = self.spawn(['hook', 'end', '--harness', 'pi', '--id', 's-1', '--cwd', self.tmp], g)
        time.sleep(0.6)  # ssh is still sleeping
        during = self.spool(g)
        p.communicate(timeout=60)
        self.assertEqual(len(during), 1)
        self.assertEqual(self.spool(g), during)

    def test_end_then_start_in_one_second_clears_the_flag(self):
        g = self.make_guest('laptop')
        tid, _ = self.held_ticket(g, 'cl-1')
        t = iso()
        self.ingest(g, self.ev(g, 'end', 'cl-1', t, live=True))
        self.assertIn('session-ended', self.board_line(tid))
        self.ingest(g, self.ev(g, 'start', 'cl-1', t, live=True))  # Claude's /clear
        self.assertNotIn('session-ended', self.board_line(tid))
        # A start stored before an end of the same second does not resume the session.
        tid2, _ = self.held_ticket(g, 'cl-2')
        t = iso()
        self.ingest(g, self.ev(g, 'start', 'cl-2', t, live=True))
        self.ingest(g, self.ev(g, 'end', 'cl-2', t, live=True))
        self.assertEqual(self.notes(tid2), [('session-ended', 'session ended')])
        self.assertIn('session-ended', self.board_line(tid2))

    def test_report_orders_events_by_occurrence(self):
        g = self.make_guest('laptop')
        self.held_ticket(g, 'rp-1')
        self.ingest(g, self.ev(g, 'start', 'rp-1', iso(-120), live=True))
        self.ingest(g, self.ev(g, 'end', 'rp-1', iso(), live=True))
        self.ingest(g, self.ev(g, 'start', 'rp-1', iso(-60)))  # replayed after the end
        time.sleep(1.1)  # the report window ends at now, exclusive, to the second
        out = self.ok('report')
        left = [l for l in out.splitlines() if l.strip().startswith('left open')]
        self.assertEqual(left[0].split()[-2], '1')  # total count, then its percentage

    def test_ingest_takes_at_most_50_events_within_its_deadline(self):
        g = self.make_guest('laptop')
        evs = [self.ev(g, 'stop', 'b-%d' % i, iso()) for i in range(60)]
        self.assertEqual(self.ingest(g, *evs), [e['id'] for e in evs[:50]])
        self.assertEqual(len(self.events(g['KB_INSTALLATION'])), 50)
        body = json.dumps(self.ev(g, 'stop', 'b-x', iso())).encode() + b'\n'
        r = self.serve(self.request(['hook', 'ingest'], rest=body), g['KB_INSTALLATION'],
                       {'KB_INGEST_DEADLINE': '0'})
        self.assertEqual((r.returncode, r.stdout), (0, b''))

    def test_oversized_events_never_block_the_spool(self):
        g = self.make_guest('laptop', FAKE_SSH_MODE='down')
        self.kb('hook', 'stop', '--harness', 'pi', '--id', 'big', '--cwd', 'x' * 9000, env=g)
        self.assertEqual(self.spool(g), [])
        self.assertIn('over 8192 bytes', self.guest_errors(g)[-1])
        d = os.path.join(self.home_of(g), 'spool')
        os.makedirs(d)
        big = self.ev(g, 'stop', 'big', iso(), cwd='x' * 9000)
        with open(os.path.join(d, '%020d-%s.json' % (1, big['id'])), 'w') as f:
            json.dump(big, f)
        for i in range(25):
            self.hook(g, 'stop', 'q-%d' % i)
        self.hook(g, 'stop', 'q-last', FAKE_SSH_MODE='ok')  # one batch: the 20 oldest
        self.assertEqual(len(self.spool(g)), 6)
        self.assertEqual(len(self.events(g['KB_INSTALLATION'])), 20)
        self.assertTrue(os.path.exists(os.path.join(d, '%020d-%s.json.bad' % (1, big['id']))))
        self.assertEqual(self.ok('spool', '--drain', env=dict(g, FAKE_SSH_MODE='ok')),
                         'sent 6, acknowledged 6, 0 left in the spool')


class AuthorizeTest(GuestCase):
    def setUp(self):
        super().setUp()
        self.key = os.path.join(self.tmp, 'k')
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'c', '-f',
                        self.key], check=True)
        self.pub = read(self.key + '.pub')
        self.auth = os.path.join(self.home, '.ssh', 'authorized_keys')
        os.mkdir(os.path.dirname(self.auth), 0o700)
        with open(self.auth, 'w') as f:
            f.write('ssh-ed25519 AAAAotherkey someone@else')  # no final newline
        os.chmod(self.auth, 0o600)
        self.inst = '0123456789abcdef'

    def authorize(self, pub=None, label='laptop', inst=None):
        path = self.key + '.pub'
        if pub is not None:
            path = os.path.join(self.tmp, 'pub-%d' % len(os.listdir(self.tmp)))
            with open(path, 'w') as f:
                f.write(pub)
        return self.kb('authorize', '--installation', inst or self.inst, '--label', label, path,
                       '--session', self.curator)

    def backups(self):
        return [n for n in os.listdir(os.path.dirname(self.auth)) if '.kb-backup-' in n]

    def test_appends_forced_command_idempotently_keeping_other_lines(self):
        r = self.authorize()
        self.assertEqual(r.returncode, 0, r.stderr)
        typ, blob = self.pub.split()[:2]
        lines = read(self.auth).splitlines()
        self.assertEqual(lines, ['ssh-ed25519 AAAAotherkey someone@else',
                                 'command="%s serve --installation %s",restrict %s %s'
                                 ' kb-guest-laptop' % (KB, self.inst, typ, blob)])
        self.assertEqual(stat.S_IMODE(os.stat(self.auth).st_mode), 0o600)
        self.assertEqual(len(self.backups()), 1)
        with open(os.path.join(os.path.dirname(self.auth), self.backups()[0])) as f:
            self.assertEqual(f.read(), 'ssh-ed25519 AAAAotherkey someone@else')
        before = read(self.auth)
        r = self.authorize()
        self.assertEqual((r.returncode, read(self.auth), len(self.backups())),
                         (0, before, 1))
        self.assertIn('already authorized', r.stdout)
        # The same key with another installation or label is refused, not added twice.
        self.assertEqual(self.authorize(inst='fedcba9876543210').returncode, 1)
        self.assertEqual(read(self.auth), before)

    def test_refuses_an_installation_id_of_another_key(self):
        self.assertEqual(self.authorize().returncode, 0)
        other = os.path.join(self.tmp, 'k2')
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', other], check=True)
        before = read(self.auth)
        r = self.kb('authorize', '--installation', self.inst, '--label', 'other', other + '.pub',
                    '--session', self.curator)
        self.assertEqual(r.returncode, 1)
        self.assertIn('already belongs to another key', r.stderr)
        self.assertEqual(read(self.auth), before)
        self.assertEqual(self.authorize().returncode, 0)  # same key and id: idempotent
        self.assertEqual(read(self.auth), before)

    def patched_kb(self, locked):
        """kb with a pause between reading and replacing authorized_keys, lock kept or not."""
        with open(KB) as f:
            src = f.read()
        read_end = "    except FileNotFoundError:\n        text = None\n    for line in (text or '')"
        self.assertEqual(src.count(read_end), 1)
        src = src.replace(read_end, read_end.replace('    for line', '    time.sleep(0.3)\n'
                                                     '    for line'))
        if not locked:
            src = src.replace('fcntl.flock(lock, fcntl.LOCK_EX)', 'pass')
        path = os.path.join(self.tmp, 'kb_%s' % ('locked' if locked else 'unlocked'))
        with open(path, 'w') as f:
            f.write(src)
        os.chmod(path, 0o755)
        return path

    def run_concurrent(self, kb, n=6):
        """n concurrent authorize calls; the kb-guest labels found afterwards."""
        procs = []
        for i in range(n):
            key = os.path.join(self.tmp, 'c%d' % i)
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', key],
                           check=True)
            procs.append(self.spawn(['authorize', '--installation', '%016x' % (i + 1),
                                     '--label', 'c%d' % i, key + '.pub',
                                     '--session', self.curator], self.env, kb=kb))
        for p in procs:
            _, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
        return sorted(l.split()[-1] for l in read(self.auth).splitlines()[1:])

    def test_concurrent_calls_keep_every_key(self):
        self.assertEqual(self.run_concurrent(self.patched_kb(True)),
                         ['kb-guest-c%d' % i for i in range(6)])
        self.assertEqual(len(self.backups()), 6)

    def test_concurrency_test_detects_a_missing_lock(self):
        self.assertLess(len(self.run_concurrent(self.patched_kb(False))), 6)

    def test_refuses_a_symlinked_authorized_keys(self):
        target = os.path.join(self.tmp, 'real_keys')
        os.rename(self.auth, target)
        os.symlink(target, self.auth)
        before = read(target)
        r = self.authorize()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(r.stderr.splitlines()), 1)
        self.assertIn('symlink', r.stderr)
        self.assertTrue(os.path.islink(self.auth))
        self.assertEqual((read(target), self.backups()), (before, []))

    def test_refuses_a_key_that_exists_without_the_forced_command(self):
        with open(self.auth, 'a') as f:
            f.write('\n' + self.pub)
        before = read(self.auth)
        self.assertEqual(self.authorize().returncode, 1)
        self.assertEqual(read(self.auth), before)

    def test_strict_validation(self):
        before = read(self.auth)
        typ, blob = self.pub.split()[:2]
        for label in ('a\nb', 'a\x1bb', 'a b', '', 'x' * 41, 'a"b', 'a,command=x'):
            self.assertEqual(self.authorize(label=label).returncode, 3, repr(label))
        for inst in ('0123', 'ABCDEF0123456789', '0123456789abcdeg'):
            self.assertEqual(self.authorize(inst=inst).returncode, 3, inst)
        for pub in (self.pub + self.pub, '%s %s c\nssh-ed25519 AAAAx y\n' % (typ, blob),
                    '%s %s a\rb\n' % (typ, blob), '%s %s a\x1b b\n' % (typ, blob),
                    'command="x" %s %s\n' % (typ, blob), 'ssh-dss AAAA c\n', '',
                    '%s AAAAnotakey c\n' % typ, '%s %s\n\n' % (typ, blob)):
            self.assertEqual(self.authorize(pub=pub).returncode, 3, repr(pub))
        self.assertEqual(read(self.auth), before)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.authorize(pub='%s %s\n' % (typ, blob)).returncode, 0)

    def test_curator_only(self):
        r = self.kb('authorize', '--installation', self.inst, '--label', 'l', self.key + '.pub',
                    '--session', self.start('alice'))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.backups(), [])


def old_kb(tmp):
    """kb as of V4_COMMIT, the last version with board schema version 4."""
    path = os.path.join(tmp, 'kb_v4')
    shutil.copy(os.path.join(REPO, 'tests', 'fixtures', 'kb_v4'), path)  # git show V4_COMMIT:kb
    os.chmod(path, 0o755)
    return path


class MigrationV5Test(KbCase):
    def test_v4_board_migrates_with_host_installation(self):
        v4 = old_kb(self.tmp)
        tok = self.ok('start', '--as', 'alice', kb=v4)
        tid = self.ok('add', 'old', '--repo', 'r', '--session', tok, kb=v4)
        self.ok('note', tid, '--session', tok, stdin='n', kb=v4)
        self.ok('hook', 'start', '--harness', 'pi', '--id', 'p-1', '--cwd', self.tmp, kb=v4)
        self.ok('hook', 'end', '--harness', 'pi', '--id', 'p-1', '--cwd', self.tmp, kb=v4)
        self.assertEqual(self.query('PRAGMA user_version'), [(4,)])
        tables = ('session', 'ticket', 'note', 'session_event', 'hint_seen')
        before = [self.query('SELECT * FROM %s ORDER BY 1' % t) for t in tables]
        self.assertIn('old', self.ok('board'))
        self.assertEqual(self.query('PRAGMA user_version'), [(6,)])
        conf = os.path.join(self.home, '.config', 'agent-kanban', 'config')
        with open(conf) as f:
            inst = f.read().strip().split('=', 1)[1]
        self.assertRegex(inst, r'^[0-9a-f]{16}$')
        after = [self.query('SELECT * FROM %s ORDER BY 1' % t) for t in tables]
        self.assertEqual(after[0], [r + (inst,) for r in before[0]])
        self.assertEqual(after[1:3], before[1:3])
        self.assertEqual(after[3], [r + (inst, None, None) for r in before[3]])
        self.assertEqual(self.query("SELECT value FROM meta WHERE key='installation'"),
                         [(inst,)])
        # Later calls keep the id; a fresh board has the migrated board's schema.
        self.ok('hook', 'stop', '--harness', 'pi', '--id', 'p-1', '--cwd', self.tmp)
        self.assertEqual(len(self.query('SELECT DISTINCT installation FROM session_event')), 1)
        self.assertEqual(len(self.query('SELECT token FROM session')), 2)
        fresh = os.path.join(self.tmp, 'fresh.sqlite')
        self.ok('board', env=dict(self.env, KB_DB=fresh))
        self.assertEqual(schema_objects(fresh), schema_objects(self.db))
        conn = sqlite3.connect(self.db)
        try:
            with self.assertRaises(sqlite3.IntegrityError):  # the trigger is back
                conn.execute("UPDATE session_event SET source='x'")
            conn.execute("INSERT INTO session (token, name, started, harness, harness_session,"
                         " installation) VALUES ('t2', 'n', 't', 'pi', 'p-1', 'other')")
            with self.assertRaises(sqlite3.IntegrityError):  # unique per installation
                conn.execute("INSERT INTO session (token, name, started, harness,"
                             " harness_session, installation) VALUES ('t3', 'n', 't', 'pi',"
                             " 'p-1', 'other')")
        finally:
            conn.close()

    def test_restore_of_a_v4_backup_uses_the_live_installation(self):
        backup = os.path.join(self.tmp, 'v4.sqlite')
        v4env = dict(self.env, KB_DB=backup)
        self.ok('start', '--as', 'old', kb=old_kb(self.tmp), env=v4env)
        cur = self.start('curator')
        conf = os.path.join(self.home, '.config', 'agent-kanban', 'config')
        live = read(conf)
        os.remove(conf)  # the live id is then only in the live board's meta
        self.ok('restore', backup, '--session', cur)
        self.assertEqual(read(conf), live)
        inst = live.strip().split('=', 1)[1]
        self.assertEqual(self.query('SELECT DISTINCT installation FROM session'), [(inst,)])
        self.assertEqual(self.query("SELECT value FROM meta WHERE key='installation'"),
                         [(inst,)])

    def test_restore_of_another_hosts_backup_keeps_the_live_installation(self):
        other_home = os.path.join(self.tmp, 'other')
        os.mkdir(other_home)
        backup = os.path.join(self.tmp, 'other.sqlite')
        oenv = dict(self.env, KB_DB=backup, HOME=other_home,
                    XDG_CONFIG_HOME=os.path.join(other_home, '.config'))
        self.ok('start', '--as', 'elsewhere', env=oenv)
        other = self.query_db(backup, "SELECT value FROM meta WHERE key='installation'")[0][0]
        cur = self.start('curator')
        conf = os.path.join(self.home, '.config', 'agent-kanban', 'config')
        live = read(conf)
        inst = live.strip().split('=', 1)[1]
        self.assertNotEqual(inst, other)
        self.ok('restore', backup, '--session', cur)
        self.assertEqual(read(conf), live)
        self.assertEqual(self.query("SELECT value FROM meta WHERE key='installation'"),
                         [(inst,)])
        self.assertEqual(sorted(self.query('SELECT name, installation FROM session')),
                         [('curator', inst), ('elsewhere', other)])

    def query_db(self, path, sql):
        conn = sqlite3.connect(path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_installation_id_not_matching_the_board_is_refused(self):
        self.start('alice')
        conf = os.path.join(self.home, '.config', 'agent-kanban', 'config')
        board_id = read(conf).strip().split('=', 1)[1]
        other = 'f' * 16
        msg = ["kb: installation id %s does not match this board's %s" % (other, board_id)]
        r = self.kb('board', env=dict(self.env, KB_INSTALLATION=other))
        self.assertEqual((r.returncode, r.stderr.splitlines()), (3, msg))
        with open(conf, 'w') as f:
            f.write('KB_INSTALLATION=%s\n' % other)
        r = self.kb('start', '--as', 'bob')
        self.assertEqual((r.returncode, r.stderr.splitlines()), (3, msg))
        # A hook still exits 0 and logs the refusal, recording nothing.
        r = self.kb('hook', 'start', '--harness', 'pi', '--id', 'p-9', '--cwd', self.tmp)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))
        self.assertIn('does not match', read(os.path.join(self.tmp, 'hook-errors.log')))
        self.assertEqual(self.query('SELECT count(*) FROM session_event'), [(0,)])
        self.assertEqual([r[0] for r in self.query('SELECT name FROM session')], ['alice'])

    def test_missing_config_reuses_the_board_record(self):
        self.start('alice')
        conf = os.path.join(self.home, '.config', 'agent-kanban', 'config')
        with open(conf) as f:
            first = f.read()
        os.remove(conf)
        self.start('bob')
        with open(conf) as f:
            self.assertEqual(f.read(), first)


if __name__ == '__main__':
    unittest.main()
