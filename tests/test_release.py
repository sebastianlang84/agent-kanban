"""Release policy regression tests in tiny independent Git repositories."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'check_release', Path(__file__).resolve().parents[1] / 'ci' / 'check_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ConstantsTest(unittest.TestCase):
    def test_rejects_unreadable_or_nonliteral_constants(self):
        source = "VERSION = '0.2.0'\nSCHEMA_VERSION = 1\nREQUEST_VERSION = 1\n"
        self.assertEqual(release.constants(source)['VERSION'], '0.2.0')
        for bad in ("VERSION = '0.2.0' + '-dev'", "VERSION: str = '0.2.0'",
                    "VERSION = '0.2.0'\nVERSION += '-dev'",
                    "VERSION = '0.2.0'\nVERSION = '0.2.0'",
                    "VERSION = OTHER = '0.2.0'", "if True:\n VERSION = '0.2.0'",
                    "VERSION = '00.2.0'", "VERSION = f'0.2.0'", "pass"):
            with self.subTest(bad=bad), self.assertRaises(release.InvalidRelease):
                release.constants(source.replace("VERSION = '0.2.0'", bad))
        with self.assertRaises(release.InvalidRelease):
            release.constants(source.replace('SCHEMA_VERSION = 1', 'SCHEMA_VERSION = True'))
        for binding in ('for SCHEMA_VERSION in (2,): pass', 'del SCHEMA_VERSION',
                        'import sys as SCHEMA_VERSION', 'def SCHEMA_VERSION(): pass',
                        'try: pass\nexcept Exception as SCHEMA_VERSION: pass',
                        'with something() as SCHEMA_VERSION: pass'):
            with self.subTest(binding=binding), self.assertRaises(release.InvalidRelease):
                release.constants(source + binding)


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='kb-release-')
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Release test')
        self.commit('0.1.0')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args],
                                       text=True, stderr=subprocess.PIPE).strip()

    def commit(self, value, schema=1, request=1):
        (self.repo / 'kb').write_text('VERSION = %r\nSCHEMA_VERSION = %d\nREQUEST_VERSION = %d\n'
                                      % (value, schema, request))
        self.git('add', 'kb')
        self.git('commit', '-qm', 'release ' + value)
        return self.git('rev-parse', 'HEAD')

    def tag(self, name, annotated=True):
        self.git('tag', *(['-am', name] if annotated else []), name)

    def check(self, tag):
        return release.Checker(self.repo, 'main').check(tag, 'HEAD')

    def test_valid_release(self):
        self.commit('0.2.0', schema=2)
        self.tag('v0.2.0')
        self.assertEqual(self.check('v0.2.0')['SCHEMA_VERSION'], 2)

    def test_major_release_accepts_protocol_change_after_one_x(self):
        self.commit('1.0.0')
        self.tag('v1.0.0')
        self.commit('2.0.0', request=2)
        self.tag('v2.0.0')
        self.assertEqual(self.check('v2.0.0')['REQUEST_VERSION'], 2)

    def test_lightweight_and_wrong_version_and_wrong_commit_rejected(self):
        self.commit('0.2.0')
        self.tag('v0.2.0', annotated=False)
        with self.assertRaisesRegex(release.InvalidRelease, 'annotated'):
            self.check('v0.2.0')
        self.tag('v0.3.0')
        with self.assertRaisesRegex(release.InvalidRelease, 'differ'):
            self.check('v0.3.0')
        self.commit('0.2.1')
        with self.assertRaisesRegex(release.InvalidRelease, 'built commit'):
            self.check('v0.3.0')

    def test_version_must_change_in_tagged_commit(self):
        (self.repo / 'note').write_text('no version change')
        self.git('add', 'note')
        self.git('commit', '-qm', 'other work')
        self.tag('v0.1.0')
        with self.assertRaisesRegex(release.InvalidRelease, 'first parent'):
            self.check('v0.1.0')

    def test_tag_must_be_on_main(self):
        self.git('checkout', '-qb', 'side')
        self.commit('0.2.0')
        self.tag('v0.2.0')
        with self.assertRaises(release.InvalidRelease):
            self.check('v0.2.0')

    def test_schema_and_protocol_require_minor_or_major(self):
        for base, new in (('0.1.1', '0.1.2'), ('1.0.0', '1.1.0')):
            for field in ('schema', 'request'):
                with self.subTest(base=base, field=field):
                    self.commit(base)
                    self.commit(new, **{field: 2})
                    tag = 'v' + new
                    self.tag(tag)
                    with self.assertRaisesRegex(release.InvalidRelease, 'MINOR'):
                        self.check(tag)
                    self.git('tag', '-d', tag)

    def test_changes_before_release_commit_still_need_minor(self):
        self.commit('0.2.0')
        self.tag('v0.2.0')
        self.commit('0.2.0', schema=2)
        self.commit('0.2.1', schema=2)
        self.tag('v0.2.1')
        with self.assertRaisesRegex(release.InvalidRelease, 'MINOR'):
            self.check('v0.2.1')

    def test_loop_cannot_hide_schema_change_in_patch_release(self):
        self.commit('0.2.0')
        self.tag('v0.2.0')
        self.commit('0.2.1')
        with (self.repo / 'kb').open('a') as stream:
            stream.write('for SCHEMA_VERSION in (2,): pass\n')
        self.git('add', 'kb')
        self.git('commit', '--amend', '--no-edit', '-q')
        self.tag('v0.2.1')
        with self.assertRaisesRegex(release.InvalidRelease, 'additional binding'):
            self.check('v0.2.1')

    def test_invalid_and_lightweight_tags_are_not_baselines(self):
        self.commit('0.2.0')
        self.tag('v0.2.0')
        self.commit('0.3.0')
        self.tag('v99.0.0', annotated=False)
        self.tag('v98.0.0')  # annotated but mismatched VERSION
        self.commit('0.4.0')
        self.tag('v0.4.0')
        self.assertEqual(self.check('v0.4.0')['VERSION'], '0.4.0')

    def test_highest_valid_tag_not_nearest_tag_is_baseline(self):
        self.commit('0.8.0')
        self.tag('v0.8.0')
        self.commit('0.2.0')
        self.commit('0.3.0')
        self.tag('v0.3.0')  # invalid: decreases from valid v0.8.0
        self.commit('0.4.0')
        self.tag('v0.4.0')
        with self.assertRaisesRegex(release.InvalidRelease, 'highest valid'):
            self.check('v0.4.0')

    def test_matching_but_invalid_annotated_tag_is_not_baseline(self):
        self.commit('0.2.0')
        self.tag('v0.2.0')
        self.git('checkout', '-qb', 'bad-release')
        self.commit('99.0.0')
        (self.repo / 'note').write_text('tagged commit does not change VERSION')
        self.git('add', 'note')
        self.git('commit', '-qm', 'not a release')
        self.tag('v99.0.0')
        self.git('checkout', '-q', 'main')
        self.git('merge', '-q', '-s', 'ours', 'bad-release', '-m', 'merge history only')
        self.commit('0.3.0')
        self.tag('v0.3.0')
        self.assertEqual(self.check('v0.3.0')['VERSION'], '0.3.0')


if __name__ == '__main__':
    unittest.main()
