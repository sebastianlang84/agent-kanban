"""Tests for kb setup and install.sh, always against a temporary HOME."""
import contextlib
import glob
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import types
import unittest
import unittest.mock

from test_kb import KB, REPO, KbCase

BOARD_LINE = ('- Run `kb board --repo <repo>` before starting a task and load the `using-kb`'
              ' skill.')


def read(path):
    with open(path) as f:
        return f.read()


def backups(path):
    return sorted(glob.glob(glob.escape(path) + '.bak-*'))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


class SetupTest(KbCase):
    def setUp(self):
        super().setUp()
        for d in ('.claude', '.codex', os.path.join('.pi', 'agent')):
            os.makedirs(self.h(d))
        self.env.update(CODEX_HOME=self.h('.codex'))
        del self.env['KB_HOST']  # setup's own KB_HOST in the config file is what counts
        self.link = self.h('.local', 'bin', 'kb')

    def h(self, *parts):
        return os.path.join(self.home, *parts)

    def setup_kb(self, *args, code=0, kb=KB):
        r = self.kb('setup', *args, kb=kb)
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        return r.stdout

    def config(self):
        path = self.h('.config', 'agent-kanban', 'config')
        return dict(l.strip().split('=', 1) for l in read(path).splitlines() if '=' in l)

    def tree(self):
        """Every path under the temp dir with its link target or content."""
        out = {}
        for root, dirs, files in os.walk(self.tmp):
            for name in dirs + files:
                p = os.path.join(root, name)
                if os.path.islink(p):
                    out[p] = ('link', os.readlink(p))
                elif os.path.isfile(p):
                    out[p] = ('file', read(p) if not p.endswith('.sqlite') else None,
                              os.stat(p).st_mode)
                else:
                    out[p] = ('dir',)
        return out

    def test_host_writes_installation_without_kb_host(self):
        out = self.setup_kb('--host')
        cfg = self.config()
        self.assertRegex(cfg['KB_INSTALLATION'], r'^[0-9a-f]{16}$')
        self.assertNotIn('KB_HOST', cfg)
        self.assertNotIn('KB_SSH_KEY', cfg)
        self.assertNotIn('authorize', out)
        self.assertEqual(os.readlink(self.link), os.path.realpath(KB))
        self.assertEqual(os.path.realpath(self.h('.agents', 'skills', 'using-kb')),
                         os.path.join(os.path.realpath(REPO), 'skills', 'using-kb'))
        self.assertEqual(os.readlink(self.h('.claude', 'skills', 'using-kb')),
                         self.h('.agents', 'skills', 'using-kb'))
        # kb itself then uses the id setup wrote.
        self.start('someone')
        self.assertEqual(self.config()['KB_INSTALLATION'], cfg['KB_INSTALLATION'])

    def test_host_setup_reuses_the_board_record(self):
        self.start('someone')  # a board and a config with its id
        inst = self.config()['KB_INSTALLATION']
        os.remove(self.h('.config', 'agent-kanban', 'config'))
        self.setup_kb('--host')
        self.assertEqual(self.config()['KB_INSTALLATION'], inst)

    def test_guest_key_and_authorize_line_accepted_by_a_host(self):
        out = self.setup_kb('--guest', 'me@boardhost')
        cfg = self.config()
        inst = cfg['KB_INSTALLATION']
        self.assertEqual(cfg['KB_HOST'], 'me@boardhost')
        self.assertEqual(cfg['KB_SSH_KEY'], self.h('.ssh', 'kb_guest_%s' % inst))
        r = subprocess.run(['ssh-keygen', '-y', '-P', '', '-f', cfg['KB_SSH_KEY']],
                           stdout=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(r.returncode, 0)  # a real key without passphrase
        self.assertTrue(r.stdout.startswith('ssh-ed25519 '))
        lines = [l for l in out.splitlines() if 'kb authorize' in l]
        self.assertEqual(len(lines), 1, out)
        # The curator runs that line on a separate host, with kb on its PATH.
        host = os.path.join(self.tmp, 'host')
        bindir = os.path.join(host, 'bin')
        os.makedirs(bindir)
        os.symlink(KB, os.path.join(bindir, 'kb'))
        henv = dict(self.env, HOME=host, XDG_CONFIG_HOME=os.path.join(host, '.config'),
                    KB_DB=os.path.join(host, 'board.sqlite'), KB_HOST='',
                    PATH=bindir + os.pathsep + self.env['PATH'])
        henv['KB_SESSION'] = self.ok('start', '--as', 'curator', env=henv)
        r = subprocess.run(['/bin/sh', '-c', lines[0]], cwd=host, env=henv,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('| kb authorize --installation %s --label ' % inst, lines[0])
        self.assertTrue(lines[0].endswith(' -'))
        self.assertEqual(sorted(os.listdir(host)), ['.config', '.ssh', 'bin', 'board.sqlite'])
        auth = read(os.path.join(host, '.ssh', 'authorized_keys'))
        blob = read(cfg['KB_SSH_KEY'] + '.pub').split()[1]
        self.assertIn('serve --installation %s",restrict ssh-ed25519 %s kb-guest-'
                      % (inst, blob), auth)
        # setup is local on a guest: a rerun with KB_HOST set is not forwarded.
        self.assertIn('ok: guest key', self.setup_kb('--guest', 'me@boardhost'))

    def test_no_terminal_needs_a_role(self):
        r = self.kb('setup')
        self.assertEqual(r.returncode, 3)
        self.assertIn('--host or --guest', r.stderr)

    def test_existing_different_kb_on_path_is_refused(self):
        write(self.link, '#!/bin/sh\necho other kb\n')
        out = self.setup_kb('--host', code=1)
        self.assertIn('refused: %s exists' % self.link, out)
        self.assertEqual(read(self.link), '#!/bin/sh\necho other kb\n')
        # The hooks then call kb by its own path, not the foreign one.
        hooks = json.loads(read(self.h('.claude', 'settings.json')))['hooks']
        self.assertEqual(hooks['Stop'][0]['hooks'][0]['command'],
                         '%s hook stop --harness claude' % os.path.realpath(KB))

    def test_real_skill_directory_is_never_replaced(self):
        write(self.h('.agents', 'skills', 'using-kb', 'SKILL.md'), 'mine\n')
        out = self.setup_kb('--host', code=1)
        self.assertIn('is a directory', out)
        self.assertEqual(read(self.h('.agents', 'skills', 'using-kb', 'SKILL.md')), 'mine\n')

    def test_board_line_once_across_symlinked_instruction_files(self):
        shared = self.h('shared-AGENTS.md')
        write(shared, '# rules\n- be brief')  # no trailing newline
        for p in (self.h('.claude', 'CLAUDE.md'), self.h('.codex', 'AGENTS.md')):
            os.symlink(shared, p)
        self.setup_kb('--host')
        self.setup_kb('--host')
        self.assertEqual(read(shared), '# rules\n- be brief\n%s\n' % BOARD_LINE)
        self.assertTrue(os.path.islink(self.h('.claude', 'CLAUDE.md')))
        self.assertEqual(read(self.h('.pi', 'agent', 'AGENTS.md')), BOARD_LINE + '\n')

    def test_claude_md_importing_a_target_gets_no_second_board_line(self):
        shared = self.h('.agents', 'AGENTS.md')
        write(shared, '# rules\n')
        os.symlink(shared, self.h('.codex', 'AGENTS.md'))
        claude = self.h('.claude', 'CLAUDE.md')
        write(claude, '\n@~/.agents/AGENTS.md\n')
        out = self.setup_kb('--host')
        self.assertIn('ok: %s imports %s' % (claude, shared), out)
        self.assertEqual(read(claude), '\n@~/.agents/AGENTS.md\n')
        self.assertEqual(backups(claude), [])
        self.assertEqual(read(shared), '# rules\n%s\n' % BOARD_LINE)
        self.assertIn('ok: %s imports' % claude, self.setup_kb('--host'))
        self.assertEqual(read(shared), '# rules\n%s\n' % BOARD_LINE)
        # A CLAUDE.md with the line as well as the import is only pointed out, not changed.
        write(claude, '@~/.agents/AGENTS.md\n%s\n' % BOARD_LINE)
        out = self.setup_kb('--host')
        self.assertIn('note: %s has the Board line and also imports %s, which has it; remove it'
                      ' from %s' % (claude, shared, claude), out)
        self.assertNotIn('refused', out)
        self.assertEqual(read(claude), '@~/.agents/AGENTS.md\n%s\n' % BOARD_LINE)
        # An import of a file no harness reads the line from does not count.
        write(claude, '@elsewhere.md\n')
        self.setup_kb('--host')
        self.assertEqual(read(claude), '@elsewhere.md\n%s\n' % BOARD_LINE)

    def test_missing_harness_dirs_are_skipped(self):
        for d in ('.claude', '.codex'):
            os.rmdir(self.h(d))
        self.setup_kb('--host')
        self.assertFalse(os.path.exists(self.h('.claude')))
        self.assertFalse(os.path.exists(self.h('.codex')))

    def test_claude_settings_merge_keeps_other_hooks(self):
        path = self.h('.claude', 'settings.json')
        other_kb = os.path.join(self.tmp, 'opt', 'bin', 'kb')  # kb at another, existing path
        write(other_kb, '')
        os.chmod(other_kb, 0o755)
        original = {
            'model': 'x', 'permissions': {'allow': ['Bash(ls)']},
            'hooks': {
                'SessionStart': [{'hooks': [{'type': 'command', 'command': '~/other.sh',
                                             'timeout': 20}]}],
                'Stop': [{'hooks': [{'type': 'command',
                                     'command': other_kb + ' hook stop --harness claude',
                                     'timeout': 10}]}],
                # kb for another event and harness does not count as the prompt hook
                'UserPromptSubmit': [{'hooks': [{'type': 'command',
                                                 'command': other_kb + ' hook stop'
                                                            ' --harness codex'}]}]}}
        write(path, json.dumps(original))
        out = self.setup_kb('--host')
        self.assertIn('SessionStart, SessionEnd, UserPromptSubmit', out)
        self.assertEqual([json.loads(read(b)) for b in backups(path)], [original])
        data = json.loads(read(path))
        self.assertEqual(data['model'], 'x')
        self.assertEqual(data['permissions'], original['permissions'])
        hooks = data['hooks']
        self.assertEqual(hooks['Stop'], original['hooks']['Stop'])  # kb at another path counts
        self.assertEqual(hooks['SessionStart'][0], original['hooks']['SessionStart'][0])
        self.assertEqual(hooks['SessionStart'][1]['hooks'],
                         [{'type': 'command', 'command': '%s hook start --harness claude'
                           % self.link, 'timeout': 10}])
        self.assertEqual(hooks['SessionEnd'][0]['hooks'][0]['timeout'], 5)
        self.assertEqual(hooks['UserPromptSubmit'][0], original['hooks']['UserPromptSubmit'][0])
        self.assertEqual(hooks['UserPromptSubmit'][1]['hooks'][0]['command'],
                         '%s hook prompt --harness claude' % self.link)
        self.assertEqual(hooks['PreToolUse'], [{'matcher': 'Bash', 'hooks': [
            {'type': 'command', 'command': '%s hook guard --harness claude' % self.link,
             'timeout': 5}]}])
        before = read(path)
        self.assertIn('kb hooks in %s' % path, self.setup_kb('--host'))
        self.assertEqual(read(path), before)

    def test_invalid_json_is_refused_and_unchanged(self):
        path = self.h('.claude', 'settings.json')
        write(path, '{"hooks": {,}')
        out = self.setup_kb('--host', code=1)
        self.assertIn('refused: %s: invalid JSON' % path, out)
        self.assertIn('"UserPromptSubmit"', out)  # the snippet to add by hand
        self.assertEqual(read(path), '{"hooks": {,}')
        self.assertEqual(backups(path), [])

    def test_codex_hooks_merge(self):
        path = self.h('.codex', 'hooks.json')
        write(path, json.dumps({'hooks': {'SessionEnd': [{'hooks': [
            {'type': 'command', 'command': '%s %s hook end --harness codex'
             % (sys.executable, KB), 'timeout': 3}]}]}}))  # the Python fallback form
        out = self.setup_kb('--host')
        self.assertIn('trust them in its TUI', out)
        hooks = json.loads(read(path))['hooks']
        self.assertEqual(len(hooks['SessionEnd']), 1)
        self.assertEqual(hooks['Stop'][0]['hooks'][0],
                         {'type': 'command', 'command': '%s hook stop --harness codex'
                          % self.link, 'timeout': 10})
        self.assertEqual(hooks['PreToolUse'], [{'matcher': 'Bash', 'hooks': [
            {'type': 'command', 'command': '%s hook guard --harness codex' % self.link,
             'timeout': 5}]}])
        before = read(path)
        self.assertNotIn('trust them', self.setup_kb('--host'))
        self.assertEqual(read(path), before)

    def test_powershell_call_operator_is_no_kb_hook_on_posix(self):
        path = self.h('.codex', 'hooks.json')
        write(path, json.dumps({'hooks': {'Stop': [{'hooks': [
            {'type': 'command', 'command': '& "%s" hook stop --harness codex' % self.link}]}]}}))
        self.setup_kb('--host')
        commands = [h['command'] for g in json.loads(read(path))['hooks']['Stop']
                    for h in g['hooks']]
        self.assertIn('%s hook stop --harness codex' % self.link, commands)

    def test_pi_extension_with_absolute_kb_path(self):
        path = self.h('.pi', 'agent', 'extensions', 'kb.ts')
        older = '// installed by kb setup: an older version\n'
        write(path, older)
        self.setup_kb('--host')
        text = read(path)
        self.assertIn('const KB: string[] = %s;' % json.dumps([self.link]), text)
        self.assertNotIn('@KB_COMMAND@', text)
        self.assertEqual([read(b) for b in backups(path)], [older])
        self.assertEqual(text, read(os.path.join(REPO, 'hooks', 'pi', 'kb.ts')).replace(
            '@KB_COMMAND@', json.dumps([self.link])))

    def test_pi_extension_not_installed_by_setup_is_kept(self):
        path = self.h('.pi', 'agent', 'extensions', 'kb.ts')
        write(path, '// my own kb extension\n')
        out = self.setup_kb('--host', code=1)
        self.assertIn('refused: %s was not installed by kb setup' % path, out)
        self.assertEqual(read(path), '// my own kb extension\n')
        self.assertEqual(backups(path), [])

    def test_cut_off_board_line_gets_the_whole_line(self):
        path = self.h('.pi', 'agent', 'AGENTS.md')
        write(path, BOARD_LINE[:-1])  # an interrupted write: the line without its end
        self.setup_kb('--host')
        self.assertEqual(read(path), '%s\n%s\n' % (BOARD_LINE[:-1], BOARD_LINE))

    def test_existing_guest_key_is_verified(self):
        self.setup_kb('--guest', 'boardhost')
        key = self.config()['KB_SSH_KEY']
        os.chmod(key, 0o644)
        self.assertIn('mode 644', self.setup_kb('--guest', 'boardhost', code=1))
        os.chmod(key, 0o600)
        other = os.path.join(self.tmp, 'other')
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', other], check=True)
        os.replace(other + '.pub', key + '.pub')
        self.assertIn('does not hold its public key',
                      self.setup_kb('--guest', 'boardhost', code=1))
        os.replace(key, other)
        os.symlink(other, key)
        self.assertIn('not a regular file', self.setup_kb('--guest', 'boardhost', code=1))

    def test_symlinked_config_is_refused_and_mode_kept(self):
        cfg = self.h('.config', 'agent-kanban', 'config')
        real = self.h('real-config')
        write(real, '# mine\n')
        os.makedirs(os.path.dirname(cfg))
        os.symlink(real, cfg)
        self.assertIn('is a symlink', self.setup_kb('--host', code=1))
        self.assertTrue(os.path.islink(cfg))
        self.assertEqual(read(real), '# mine\n')
        os.remove(cfg)
        write(cfg, '# mine\n')
        os.chmod(cfg, 0o640)
        self.setup_kb('--host')
        self.assertEqual(os.stat(cfg).st_mode & 0o777, 0o640)
        os.remove(cfg)
        self.setup_kb('--host')
        self.assertEqual(os.stat(cfg).st_mode & 0o777, 0o600)

    def test_stale_kb_hook_is_refused_not_counted(self):
        path = self.h('.claude', 'settings.json')
        stale = '~/gone/kb hook stop --harness claude'
        write(path, json.dumps({'hooks': {'Stop': [{'hooks': [
            {'type': 'command', 'command': stale}]}]}}))
        out = self.setup_kb('--host', code=1)
        self.assertIn('refused: %s: the Stop hook runs `%s`, but ~/gone/kb is not an'
                      ' executable file'
                      % (path, stale), out)
        hooks = json.loads(read(path))['hooks']
        self.assertEqual(hooks['Stop'], [{'hooks': [{'type': 'command', 'command': stale}]}])
        self.assertEqual(len(hooks['SessionStart']), 1)  # the other events are added

    def test_backups_never_overwrite_or_follow(self):
        path = self.h('.claude', 'settings.json')
        write(path, '{}')
        write(path + '.bak', 'an older backup\n')
        self.setup_kb('--host')
        self.assertEqual(read(path + '.bak'), 'an older backup\n')
        self.assertEqual([read(b) for b in backups(path)], ['{}'])
        # A path already at the next backup name, even a symlink, is left alone.
        kb = load_kb()
        kb.now = lambda: '2026-09-28T10:00:00Z'
        victim = os.path.join(self.tmp, 'victim')
        write(victim, 'victim\n')
        os.symlink(victim, path + '.bak-20260928T100000Z')
        write(path + '.bak-20260928T100000Z-1', 'taken\n')
        backup = kb.replace_file(path, '{"new": 1}', read(path))
        self.assertEqual(backup, path + '.bak-20260928T100000Z-2')
        self.assertEqual(read(victim), 'victim\n')
        self.assertEqual(read(path + '.bak-20260928T100000Z-1'), 'taken\n')
        self.assertEqual(json.loads(read(backup))['hooks']['Stop'][0]['hooks'][0]['timeout'], 10)

    def test_symlinked_pi_extension_is_refused(self):
        path = self.h('.pi', 'agent', 'extensions', 'kb.ts')
        target = os.path.join(self.tmp, 'elsewhere.ts')
        write(target, '// installed by kb setup\n')
        os.makedirs(os.path.dirname(path))
        os.symlink(target, path)
        self.assertIn('refused: %s is a symlink' % path, self.setup_kb('--host', code=1))
        self.assertEqual(read(target), '// installed by kb setup\n')
        self.assertEqual(backups(target), [])

    def test_authorize_reads_the_key_from_stdin(self):
        key = os.path.join(self.tmp, 'k')
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', key], check=True)
        env = dict(self.env, KB_HOST='', KB_SESSION=self.start('curator'))
        args = ('authorize', '--installation', '0123456789abcdef', '--label', 'g', '-')
        self.assertEqual(self.kb(*args, stdin='ssh-ed25519 AAAA\nsecond line\n',
                                 env=env).returncode, 3)
        self.assertEqual(self.kb(*args, stdin='not a key\n', env=env).returncode, 3)
        r = self.kb(*args, stdin=read(key + '.pub'), env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('kb-guest-g', read(self.h('.ssh', 'authorized_keys')))

    def test_non_executable_kb_hook_counts_as_stale(self):
        plain = os.path.join(self.tmp, 'plain', 'kb')
        write(plain, '')  # a regular file, mode 644
        path = self.h('.claude', 'settings.json')
        write(path, json.dumps({'hooks': {'Stop': [{'hooks': [
            {'type': 'command', 'command': plain + ' hook stop --harness claude'}]}]}}))
        self.assertIn('%s is not an executable file' % plain, self.setup_kb('--host', code=1))
        os.chmod(plain, 0o755)
        self.assertNotIn('refused', self.setup_kb('--host'))
        self.assertEqual(len(json.loads(read(path))['hooks']['Stop']), 1)

    def test_board_line_counts_only_as_a_whole_line(self):
        path = self.h('.pi', 'agent', 'AGENTS.md')
        write(path, 'Old note: %s (removed)\n' % BOARD_LINE)
        self.setup_kb('--host')
        self.assertEqual(read(path), 'Old note: %s (removed)\n%s\n' % (BOARD_LINE, BOARD_LINE))
        write(path, '  %s  \n' % BOARD_LINE)  # surrounding whitespace is fine
        self.setup_kb('--host')
        self.assertEqual(read(path), '  %s  \n' % BOARD_LINE)

    def test_guest_refuses_a_local_board(self):
        self.start('someone')  # creates the board at KB_DB
        out = self.setup_kb('--guest', 'boardhost', code=1)
        self.assertIn('this installation holds a board (%s)' % self.db, out)
        self.assertNotIn('KB_HOST', self.config())

    def test_installation_in_the_environment_must_match(self):
        env_inst = '0123456789abcdef'
        self.env['KB_INSTALLATION'] = env_inst
        out = self.setup_kb('--host', code=1)
        self.assertIn('KB_INSTALLATION=%s in the environment conflicts with none' % env_inst,
                      out)
        self.assertFalse(os.path.exists(self.h('.config', 'agent-kanban', 'config')))
        write(self.h('.config', 'agent-kanban', 'config'), 'KB_INSTALLATION=%s\n' % env_inst)
        self.setup_kb('--host')
        write(self.h('.config', 'agent-kanban', 'config'), 'KB_INSTALLATION=%s\n' % ('f' * 16))
        self.assertIn('conflicts with ffffffffffffffff', self.setup_kb('--host', code=1))

    def test_config_id_must_match_the_board(self):
        self.start('someone')
        recorded = self.config()['KB_INSTALLATION']
        write(self.h('.config', 'agent-kanban', 'config'), 'KB_INSTALLATION=%s\n' % ('e' * 16))
        out = self.setup_kb('--host', code=1)
        self.assertIn('does not match the board %s, which records %s' % (self.db, recorded), out)

    def test_temp_file_never_follows_a_symlink(self):
        kb = load_kb()
        path = self.h('target.json')
        write(path, 'old')
        victim = os.path.join(self.tmp, 'victim')
        write(victim, 'victim\n')
        for pid in range(os.getpid() - 2, os.getpid() + 3):  # the old predictable names
            os.symlink(victim, '%s.kb-tmp.%d' % (path, pid))
        with unittest.mock.patch.object(kb.tempfile, 'mkstemp', wraps=kb.tempfile.mkstemp) as m:
            kb.replace_file(path, 'new', 'old')
        self.assertEqual(m.call_args[1]['dir'], os.path.dirname(path))
        self.assertEqual(read(path), 'new')
        self.assertEqual(read(victim), 'victim\n')
        self.assertEqual([n for n in os.listdir(self.home) if 'kb-tmp-' in n], [])

    def test_dry_run_writes_nothing(self):
        write(self.h('.claude', 'settings.json'), '{}')
        before = self.tree()
        for role in (['--host'], ['--guest', 'boardhost']):
            out = self.setup_kb(*role, '--dry-run')
            self.assertIn('would link', out)
            self.assertEqual(self.tree(), before)

    def test_guest_dry_run_prints_no_id_it_will_not_use(self):
        out = self.setup_kb('--guest', 'boardhost', '--dry-run')
        self.assertNotRegex(out, r'[0-9a-f]{16}')
        self.assertIn('would write KB_INSTALLATION=<generated on the real run>', out)
        self.assertIn('the real run prints the kb authorize line', out)
        self.assertNotIn('| kb authorize', out)

    def test_rerun_changes_nothing(self):
        write(self.h('.claude', 'settings.json'), '{"hooks": {}}')
        self.setup_kb('--guest', 'boardhost')
        after = self.tree()
        out = self.setup_kb('--guest', 'boardhost')
        self.assertEqual(self.tree(), after)
        self.assertNotIn('would ', out)
        for line in out.splitlines():
            self.assertFalse(line.startswith(('write', 'link', 'append', 'add', 'generate')),
                             line)

    def test_install_sh_runs_setup(self):
        install = os.path.join(REPO, 'install.sh')
        self.assertTrue(os.access(install, os.X_OK))
        r = self.kb('--host', kb=install)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(os.readlink(self.link), os.path.realpath(KB))


def load_kb():
    """kb as a module, to run Setup's Windows branches on this machine."""
    loader = importlib.machinery.SourceFileLoader('kb_setup_under_test', KB)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    return mod


class WindowsSetupTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix='kb-test-')
        self.addCleanup(__import__('shutil').rmtree, self.tmp, True)
        self.kb = load_kb()
        self.s = self.kb.Setup(False)
        self.s.home, self.s.windows = self.tmp, True

    def quiet(self, func, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            func(*args)
        return out.getvalue()

    def test_kb_cmd_used_only_when_setup_wrote_or_verified_it(self):
        cmd = os.path.join(self.tmp, '.local', 'bin', 'kb.cmd')
        self.quiet(self.s.path_entry)
        self.assertEqual(self.s.kb_command(), [cmd])
        self.assertEqual(self.s.hook_command('stop', 'claude'),
                         '"%s" hook stop --harness claude' % cmd)
        s2 = self.kb.Setup(False)
        s2.home, s2.windows = self.tmp, True
        self.quiet(s2.path_entry)  # identical file: verified
        self.assertEqual(s2.kb_command(), [cmd])
        write(cmd, '@echo another kb\r\n')
        s3 = self.kb.Setup(False)
        s3.home, s3.windows = self.tmp, True
        self.assertIn('refused', self.quiet(s3.path_entry))
        self.assertEqual(s3.kb_command(), [sys.executable, s3.kb])
        self.assertEqual(s3.hook_command('end', 'codex'),
                         '& "%s" "%s" hook end --harness codex' % (sys.executable, s3.kb))

    def test_kb_cmd_refused_for_a_path_cmd_would_expand(self):
        self.s.kb = os.path.join(self.tmp, '100%repo', 'kb')
        out = self.quiet(self.s.path_entry)
        self.assertIn('refused: %s holds %% or "' % self.s.kb, out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, '.local', 'bin', 'kb.cmd')))
        self.assertFalse(self.s.cmd_ok)

    def test_junction_via_winapi(self):
        calls = []
        fake = types.ModuleType('_winapi')
        fake.CreateJunction = lambda target, link: calls.append((target, link))
        target = os.path.join(self.tmp, 'skill')
        os.mkdir(target)
        link = os.path.join(self.tmp, 'links', 'using-kb')
        with unittest.mock.patch.dict(sys.modules, {'_winapi': fake}):
            self.quiet(self.s.link, link, target)
        self.assertEqual(calls, [(target, link)])
        self.assertEqual(self.s.refused, 0)

        def fail(target, link):
            raise OSError(5, 'Access is denied')
        fake.CreateJunction = fail
        with unittest.mock.patch.dict(sys.modules, {'_winapi': fake}):
            out = self.quiet(self.s.link, link + '2', target)
        self.assertIn('refused: cannot create the junction', out)
        self.assertEqual(self.s.refused, 1)

    def test_windows_hook_paths_are_always_quoted(self):
        self.s.cmd_ok = True
        cmd = os.path.join(self.tmp, '.local', 'bin', 'kb.cmd')
        self.assertEqual(self.s.hook_command('stop', 'claude'),
                         '"%s" hook stop --harness claude' % cmd)
        self.s.home = os.path.join(self.tmp, '100%')
        with self.assertRaises(self.kb.UsageError):
            self.s.hook_command('stop', 'claude')
        os.makedirs(os.path.join(self.s.home, '.claude'))
        out = self.quiet(self.s.harness_hooks, 'claude',
                         os.path.join(self.s.home, '.claude', 'settings.json'))
        self.assertIn('refused', out)
        self.assertIn('cannot quote', out)
        self.assertFalse(os.path.exists(os.path.join(self.s.home, '.claude', 'settings.json')))

    def test_windows_codex_hook_uses_the_powershell_call_operator(self):
        # codex runs a Windows hook through PowerShell, which reads a leading quoted path as a
        # string and fails with UnexpectedToken unless the call operator & precedes it (#184)
        self.s.cmd_ok = True
        cmd = os.path.join(self.tmp, '.local', 'bin', 'kb.cmd')
        self.assertEqual(self.s.hook_command('prompt', 'codex'),
                         '& "%s" hook prompt --harness codex' % cmd)
        write(cmd, '@echo kb\r\n')
        os.chmod(cmd, 0o755)  # missing_program checks the mode on this POSIX test host
        path = os.path.join(self.tmp, '.codex', 'hooks.json')
        os.makedirs(os.path.dirname(path))
        self.quiet(self.s.harness_hooks, 'codex', path)
        first = read(path)
        self.assertEqual(json.loads(first)['hooks']['Stop'][0]['hooks'][0]['command'],
                         '& "%s" hook stop --harness codex' % cmd)
        self.assertEqual(json.loads(first)['hooks']['PreToolUse'][0]['hooks'][0]['command'],
                         '& "%s" hook guard --harness codex' % cmd)
        self.assertIn('kb hooks in', self.quiet(self.s.harness_hooks, 'codex', path))
        self.assertEqual(read(path), first)

    def test_windows_codex_hook_refuses_what_powershell_expands(self):
        for name in ('a$b', 'a`b'):
            self.s.home = os.path.join(self.tmp, name)
            self.s.cmd_ok = True
            for harness in ('codex', 'claude'):  # PowerShell and Git Bash
                with self.assertRaises(self.kb.UsageError):
                    self.s.hook_command('stop', harness)
        self.s.home = os.path.join(self.tmp, 'a\u201db')
        with self.assertRaises(self.kb.UsageError):
            self.s.hook_command('stop', 'codex')
        self.s.hook_command('stop', 'claude')

    def test_windows_codex_hook_reads_command_windows(self):
        self.s.cmd_ok = True
        cmd = os.path.join(self.tmp, '.local', 'bin', 'kb.cmd')
        write(cmd, '@echo kb\r\n')
        os.chmod(cmd, 0o755)
        path = os.path.join(self.tmp, '.codex', 'hooks.json')
        write(path, json.dumps({'hooks': {name: [{'hooks': [{
            'type': 'command', 'command': '"C:\\gone\\kb.cmd" hook %s --harness codex' % event,
            'commandWindows': '& "%s" hook %s --harness codex' % (cmd, event)}]}]
            for event, name in self.kb.HOOK_NAMES}}))
        before = read(path)
        out = self.quiet(self.s.harness_hooks, 'codex', path)
        self.assertNotIn('refused', out)
        self.assertEqual(read(path), before)

    def test_windows_parser_reads_a_single_quoted_program(self):
        entry = {'commandWindows': "& 'C:\\Tools\\kb.cmd' hook stop --harness codex"}
        with unittest.mock.patch.object(self.kb.os, 'name', 'nt'):  # shlex without posix
            self.assertEqual(self.kb.kb_hook_program(entry, 'stop', 'codex', True),
                             ['C:\\Tools\\kb.cmd'])


if __name__ == '__main__':
    unittest.main()
