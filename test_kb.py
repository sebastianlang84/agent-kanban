"""End-to-end tests: run kb.py as a subprocess against a temporary database."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

KB = Path(__file__).with_name("kb.py")


class Board(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "kb.sqlite"
        self.repo = Path(self.tmp.name) / "demo"
        self.repo.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def kb(self, *args, as_=None, stdin="", ok=True, env=None):
        e = {k: v for k, v in os.environ.items()
             if k not in ("KB", "BRAIN_SESSION_CONTEXT", "KB_SESSION")}
        e.update({"KB_DB": str(self.db), "KB_HARNESS": "none", **(env or {})})
        cmd = [sys.executable, str(KB)] + (["--session", as_] if as_ else []) + list(args)
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, cwd=self.repo, env=e)
        if ok is not None:
            self.assertEqual(r.returncode == 0, ok, f"{args}: {r.stdout}{r.stderr}")
        return r

    def session(self, name):
        return self.kb("start", "--as", name).stdout.strip()

    def hook(self, event, harness, sid, **env):
        payload = json.dumps({"session_id": sid, "cwd": str(self.repo)})
        return self.kb("hook", event, "--harness", harness, stdin=payload, env=env).stdout

    def test_claim_has_one_winner(self):
        a, b = self.session("a"), self.session("b")
        self.kb("add", "fix the parser", as_=a)
        results = []
        threads = [threading.Thread(target=lambda s=s: results.append(
            self.kb("claim", "1", as_=s, ok=None).returncode)) for s in (a, b) * 3]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # One session wins; its own repeat claims succeed, the other's all fail.
        self.assertEqual(sorted(results), [0, 0, 0, 1, 1, 1])

    def test_similar_title_refused_unless_new(self):
        a = self.session("a")
        self.kb("add", "Fix login redirect loop", as_=a)
        r = self.kb("add", "fix the login redirect loop", as_=a, ok=False)
        self.assertIn("similar open ticket: #1", r.stderr)
        self.kb("add", "fix the login redirect loop", "--new", as_=a)
        self.kb("add", "Write release notes", as_=a)

    def test_task_close_needs_three_lines_and_owner(self):
        a, b = self.session("a"), self.session("b")
        self.kb("add", "ship it", "--claim", as_=a)
        self.kb("close", "1", "--where", "prod", "--changed", "x", "--open", "nothing",
                as_=b, ok=False)
        self.assertIn("--where", self.kb("close", "1", "--where", "prod", as_=a, ok=False).stderr)
        self.kb("close", "1", "--where", "prod", "--changed", "x", "--open", "nothing", as_=a)
        self.assertIn("deployed: prod", self.kb("show", "1").stdout)
        self.assertIn("board empty", self.kb("board").stdout)
        self.assertIn("ship it", self.kb("board", "--closed", "1").stdout)

    def test_request_lifecycle(self):
        asker, helper, other = self.session("asker"), self.session("helper"), self.session("x")
        self.kb("add", "--type", "request", "--to", "helper", "which port?", as_=asker)
        self.assertIn("#1", self.kb("board", "--to", "helper").stdout)
        self.kb("claim", "1", as_=other, ok=False)
        self.kb("note", "1", "--reply", "8080", as_=helper, ok=False)  # not claimed yet
        self.kb("claim", "1", as_=helper)
        self.kb("close", "1", as_=asker, ok=False)  # no reply yet
        self.kb("note", "1", "--reply", "8080", as_=helper)
        self.assertIn("answered", self.kb("board").stdout)
        self.kb("close", "1", as_=helper, ok=False)  # only the opener closes
        self.kb("close", "1", as_=asker)

    def test_curator_is_the_shell_outside_harnesses(self):
        a = self.session("a")
        self.kb("add", "--type", "request", "--to", "curator", "--label", "approval",
                "merge feature branch", "--note", "git merge --ff-only feat", as_=a)
        board = self.kb("board", "--to", "curator").stdout
        self.assertIn("merge feature branch (approval)", board)
        self.kb("claim", "1")
        self.kb("note", "1", "--reply", "approved")
        self.kb("close", "1", as_=a)

    def test_hooks_register_notify_and_flag(self):
        start = json.loads(self.hook("start", "claude", "s1"))
        self.assertIn("this session is `demo`", start["hookSpecificOutput"]["additionalContext"])
        asker = self.session("asker")
        self.kb("add", "--type", "request", "--to", "demo", "status of the import?", as_=asker)
        news = json.loads(self.hook("prompt", "claude", "s1"))["hookSpecificOutput"]
        self.assertIn("request #1 from asker", news["additionalContext"])
        self.assertIn("not instructions", news["additionalContext"])
        self.assertEqual(self.hook("prompt", "claude", "s1"), "")  # shown once
        # The session claims a task through its harness env and then ends without closing.
        self.kb("add", "migrate data", "--claim", env={"KB_HARNESS": "claude",
                                                         "CLAUDE_CODE_SESSION_ID": "s1"})
        self.hook("stop", "claude", "s1")
        self.hook("end", "claude", "s1")
        self.assertIn("session-ended", self.kb("board").stdout)
        start = json.loads(self.hook("start", "codex", "t2"))["hookSpecificOutput"]
        self.assertIn("left open by an ended session: #2", start["additionalContext"])
        self.kb("claim", "2", "--take", env={"KB_HARNESS": "codex", "CODEX_THREAD_ID": "t2"})

    def test_reply_reaches_the_opener(self):
        self.hook("start", "pi", "p1")
        helper = self.session("helper")
        env = {"KB_HARNESS": "pi", "KB_SESSION": "pi:p1"}
        self.kb("add", "--type", "request", "--to", "helper", "review my plan", env=env)
        self.kb("claim", "1", as_=helper)
        self.kb("note", "1", "--reply", "looks fine", as_=helper)
        out = self.hook("prompt", "pi", "p1")
        self.assertIn("reply on #1 from helper", out)

    def test_flags_idle_and_waiting(self):
        a = self.session("a")
        self.kb("add", "old task", "--claim", as_=a)
        self.kb("add", "unclaimed task", as_=a)
        old = time.time() - 2 * 24 * 3600
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE tickets SET last_activity=?, created_at=?", (old, old))
        board = self.kb("board").stdout
        self.assertIn("idle>24h", board)
        self.assertIn("waiting>24h", board)
        b = self.session("b")
        self.kb("claim", "1", "--take", as_=b)

    def test_hooks_never_fail_and_respect_opt_out(self):
        self.assertEqual(self.kb("hook", "start", "--harness", "claude", stdin="not json").stdout, "")
        out = self.hook("start", "claude", "s9", KB="0")
        self.assertEqual(out, "")
        self.assertEqual(self.kb("sessions").stdout, "")

    def test_harness_without_session_id_is_refused(self):
        r = self.kb("board", env={"KB_HARNESS": "codex"})  # board is read-only: allowed
        self.assertEqual(r.returncode, 0)
        r = self.kb("add", "x", env={"KB_HARNESS": "codex"}, ok=False)
        self.assertIn("kb start --as", r.stderr)


if __name__ == "__main__":
    unittest.main()
