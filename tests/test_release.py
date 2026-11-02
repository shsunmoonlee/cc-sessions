"""Repo hygiene guards: no absolute home paths in the tree, 3.9-safe syntax in the hook
path, and `make release-check` driven by an external deny-list file."""
import ast
import os
import re
import subprocess
import sys
import tempfile
import unittest

from helpers import REPO

SKIP_DIRS = {".git", "__pycache__", "build"}


def tree_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            # in a git worktree .git is a file holding an absolute gitdir path
            if f in SKIP_DIRS:
                continue
            yield os.path.join(root, f)


class Hygiene(unittest.TestCase):
    def test_no_absolute_home_paths(self):
        pat = re.compile(r"/(Users|home)/[A-Za-z0-9._-]+/")
        hits = []
        for p in tree_files():
            if p == os.path.abspath(__file__):
                continue
            try:
                with open(p, errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if pat.search(line):
                            hits.append("%s:%d" % (os.path.relpath(p, REPO), i))
            except OSError:
                continue
        self.assertEqual(hits, [])

    def test_stdlib_side_parses_as_python39(self):
        files = [os.path.join(REPO, "bin", "cc-sessions"), os.path.join(REPO, "bin", "cc-resume"),
                 os.path.join(REPO, "daemon", "cc_sessions_daemon.py")]
        files += [os.path.join(REPO, "lib", "ccsessions", f) for f in os.listdir(os.path.join(REPO, "lib", "ccsessions"))
                  if f.endswith(".py")]
        for p in files:
            with open(p) as fh:
                src = fh.read()
            kw = {"feature_version": (3, 9)} if sys.version_info >= (3, 8) else {}
            tree = ast.parse(src, p, **kw)
            for node in ast.walk(tree):
                self.assertNotEqual(type(node).__name__, "Match", p)

    def test_release_check_uses_deny_list_file(self):
        if subprocess.run(["which", "make"], capture_output=True).returncode != 0:
            self.skipTest("make not available")
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("\n" + "zq" + "xv" + "-never-in-the-tree\n")
            clean = fh.name
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("choose_candidates\n")
            dirty = fh.name
        try:
            env = dict(os.environ, CC_SESSIONS_PRIVATE_STRINGS=clean)
            r = subprocess.run(["make", "-s", "-C", REPO, "release-check"], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            env["CC_SESSIONS_PRIVATE_STRINGS"] = dirty
            r = subprocess.run(["make", "-s", "-C", REPO, "release-check"], env=env, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            env.pop("CC_SESSIONS_PRIVATE_STRINGS")
            r = subprocess.run(["make", "-s", "-C", REPO, "release-check"], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0)
            self.assertIn("skipped", r.stdout)
        finally:
            os.unlink(clean)
            os.unlink(dirty)
