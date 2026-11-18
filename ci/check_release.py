#!/usr/bin/env python3
"""Validate an annotated release tag without importing or executing historical kb code."""
import argparse
import ast
import re
import subprocess


SEMVER = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')
NAMES = ('VERSION', 'SCHEMA_VERSION', 'REQUEST_VERSION')


class InvalidRelease(ValueError):
    pass


def version(text):
    match = SEMVER.fullmatch(text) if isinstance(text, str) else None
    if not match:
        raise InvalidRelease('expected X.Y.Z, got %r' % text)
    return tuple(map(int, match.groups()))


def constants(source):
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise InvalidRelease('unreadable kb constants: %s' % exc) from exc
    result, literal_targets = {}, set()
    # Also reject conditional, augmented, annotated and chained writes to constants.
    for node in ast.walk(tree):
        targets = (node.targets if isinstance(node, ast.Assign) else
                   [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr))
                   else [])
        names = {n.id for target in targets for n in ast.walk(target)
                 if isinstance(n, ast.Name) and n.id in NAMES}
        for name in names:
            if (node not in tree.body or not isinstance(node, ast.Assign)
                    or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name)
                    or not isinstance(node.value, ast.Constant) or name in result):
                raise InvalidRelease('%s must have exactly one plain literal assignment' % name)
            result[name] = node.value.value
            literal_targets.add(id(node.targets[0]))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and node.id in NAMES
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and id(node) not in literal_targets):
            raise InvalidRelease('%s has an additional binding' % node.id)
        # Some bindings store their name as a string rather than an ast.Name.
        bound = (node.asname or node.name.split('.')[0] if isinstance(node, ast.alias)
                 else node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                      ast.ClassDef, ast.ExceptHandler))
                 else getattr(node, 'name', None) if type(node).__name__ in
                 ('MatchAs', 'MatchStar') else getattr(node, 'rest', None)
                 if type(node).__name__ == 'MatchMapping' else None)
        if bound in NAMES:
            raise InvalidRelease('%s has an additional binding' % bound)
    if set(result) != set(NAMES):
        raise InvalidRelease('missing release constants')
    version(result['VERSION'])
    for name in NAMES[1:]:
        if type(result[name]) is not int or result[name] < 1:
            raise InvalidRelease('%s must be a positive integer literal' % name)
    return result


class Checker:
    def __init__(self, repo='.', main='origin/main'):
        self.repo, self.main, self.cache = repo, main, {}

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.repo), *args],
                                capture_output=True, text=True)
        if result.returncode:
            raise InvalidRelease('git %s: %s' % (' '.join(args), result.stderr.strip()))
        return result.stdout.strip()

    def read(self, commit):
        return constants(self.git('show', commit + ':kb'))

    def check(self, tag, expected=None):
        if not tag.startswith('v'):
            raise InvalidRelease('release tag must be vX.Y.Z')
        current = version(tag[1:])
        ref = 'refs/tags/' + tag
        if self.git('cat-file', '-t', ref) != 'tag':
            raise InvalidRelease('release tag must be annotated')
        commit = self.git('rev-parse', ref + '^{commit}')
        if expected and commit != self.git('rev-parse', expected + '^{commit}'):
            raise InvalidRelease('tag does not point to the built commit')
        if tag in self.cache:
            cached = self.cache[tag]
            if isinstance(cached, InvalidRelease):
                raise cached
            return cached
        try:
            self.git('merge-base', '--is-ancestor', commit, self.main)
            data = self.read(commit)
            if version(data['VERSION']) != current:
                raise InvalidRelease('tag and VERSION differ')
            parent = self.git('rev-parse', commit + '^1')
            before = self.read(parent)
            if current <= version(before['VERSION']):
                raise InvalidRelease('release commit must increase VERSION over its first parent')
            candidates = []
            for older in self.git('for-each-ref', '--merged=' + parent,
                                  '--format=%(refname:strip=2)', 'refs/tags').splitlines():
                try:
                    candidates.append((version(older[1:]) if older.startswith('v') else
                                       version(''), older))
                except InvalidRelease:
                    continue
            baseline = None
            for _, older in sorted(candidates, reverse=True):
                try:
                    baseline = self.check(older)
                    break
                except InvalidRelease:
                    continue
            if baseline and current <= version(baseline['VERSION']):
                raise InvalidRelease('release must rise over the highest valid annotated release')
            for previous in (before, baseline):
                if previous and any(data[n] != previous[n] for n in NAMES[1:]):
                    old = version(previous['VERSION'])
                    if not (current[0] > old[0] or
                            current[0] == old[0] == 0 and current[1] > old[1]):
                        raise InvalidRelease('schema/protocol change needs MINOR while 0.x, else MAJOR')
        except InvalidRelease as exc:
            self.cache[tag] = exc
            raise
        self.cache[tag] = data
        return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tag')
    parser.add_argument('--commit', default='HEAD')
    parser.add_argument('--main', default='origin/main')
    args = parser.parse_args()
    try:
        Checker(main=args.main).check(args.tag, args.commit)
    except InvalidRelease as exc:
        parser.exit(1, 'release check failed: %s\n' % exc)
    print('release check passed: ' + args.tag)


if __name__ == '__main__':
    main()
