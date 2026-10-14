"""Test scaffolding: every test runs against a temp ledger and a temp config. Nothing here
signals a process, touches the real ledger or talks to iTerm2."""
import importlib.machinery
import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

# Safety net before anything imports the package: never the real ledger or config.
_SAFE = tempfile.mkdtemp(prefix="ccs-test-")
os.environ["CC_SESSIONS_DB"] = os.path.join(_SAFE, "never.db")
os.environ["CC_SESSIONS_CONFIG"] = os.path.join(_SAFE, "never.json")
os.environ["CC_SESSIONS_LOG"] = os.path.join(_SAFE, "never.log")
REAL_LOG = os.path.expanduser("~/Library/Logs/cc-sessions.log")

from ccsessions import config, ledger, procs  # noqa: E402

REAL_FOREGROUND_JOB = procs.foreground_job  # TempEnv stubs it; its own test calls this

_quiet = logging.getLogger("cc-sessions")
_quiet.addHandler(logging.NullHandler())
_quiet.propagate = False


def load_path(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def load_daemon():
    if "cc_sessions_daemon" not in sys.modules:
        sys.modules["cc_sessions_daemon"] = load_path(
            "cc_sessions_daemon", os.path.join(REPO, "daemon", "cc_sessions_daemon.py"))
    return sys.modules["cc_sessions_daemon"]


class TempEnv(unittest.TestCase):
    """Temp dir with a config file and a ledger; self.cfg and self.conn ready."""

    extra_config = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccs-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # never read this machine's real ttys: tests opt in by setting self.foreground[tty]
        self.foreground = {}
        fg = mock.patch.object(procs, "foreground_job", lambda tty: self.foreground.get(procs.norm_tty(tty)))
        fg.start()
        self.addCleanup(fg.stop)
        self.db = os.path.join(self.tmp, "state", "cc-sessions.db")
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.projects)
        cfgd = {"db": self.db, "claude_projects": self.projects,
                "claude_sessions": os.path.join(self.tmp, "sessions"),
                "claude_settings": os.path.join(self.tmp, "settings.json"),
                "claude_tmp": os.path.join(self.tmp, "ctmp"),
                "log": os.path.join(self.tmp, "cc-sessions.log"),
                "sock_dir": os.path.join(self.tmp, "socks"),
                "bootmark": os.path.join(self.tmp, "bootmark"),
                "legacy_manifests": os.path.join(self.tmp, "hibernate"),
                "launchd_plist": os.path.join(self.tmp, "agent.plist"),
                "park_archive_dir": os.path.join(self.tmp, "archive", "{repo_key}")}
        cfgd.update(self.extra_config)
        self.cfg_path = os.path.join(self.tmp, "config.json")
        with open(self.cfg_path, "w") as fh:
            json.dump(cfgd, fh)
        self._env = {k: os.environ.get(k) for k in ("CC_SESSIONS_DB", "CC_SESSIONS_CONFIG", "CC_SESSIONS_LOG")}
        os.environ["CC_SESSIONS_CONFIG"] = self.cfg_path
        os.environ["CC_SESSIONS_DB"] = self.db
        os.environ["CC_SESSIONS_LOG"] = cfgd["log"]
        self.addCleanup(self._restore_env)
        self.cfg = config.load()
        self.conn = ledger.connect(self.db)
        self.addCleanup(self.conn.close)

    def _restore_env(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def transcript(self, sid, records, cwd_dir="proj", mtime=None):
        d = os.path.join(self.projects, cwd_dir)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "%s.jsonl" % sid)
        with open(p, "w") as fh:
            for r in records:
                fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def insert(self, **kw):
        row = {"session_id": "s1", "pid": 111, "pid_start": "Mon Sep 28 10:00:00 2026",
               "interactive": 1, "tty": "ttys001", "cwd": self.tmp, "launch_cwd": self.tmp,
               "transcript": None, "title": "t", "state": "idle", "state_since": time.time() - 3600,
               "last_event": "Stop", "last_event_at": time.time() - 3600, "subagents": 0,
               "started_at": time.time() - 7200, "ended_at": None, "end_reason": None,
               "source": "hook", "iterm_guid": None, "last_focus_at": None, "resumed_at": None,
               "evicted_at": None}
        row.update(kw)
        cols = sorted(row)
        with ledger.tx(self.conn):
            self.conn.execute("INSERT INTO sessions(%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                              [row[c] for c in cols])
        return ledger.get(self.conn, row["session_id"])


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def user(text, t=None, **kw):
    d = {"type": "user", "timestamp": iso(t or time.time() - 60),
         "message": {"role": "user", "content": text}}
    d.update(kw)
    return d


def assistant(text="ok", t=None, tools=()):
    content = [{"type": "text", "text": text}] + list(tools)
    return {"type": "assistant", "timestamp": iso(t or time.time() - 50),
            "message": {"role": "assistant", "content": content}}
