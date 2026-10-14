"""dotclaude#35 (on_evict replay after a cancelled eviction) and #36 (the kernel's
foreground group corrects a lagging iTerm2 jobName)."""
import asyncio
import os
import sqlite3
import stat
import time

from helpers import TempEnv, load_daemon

from ccsessions import ledger, procs

from test_daemon import FakeApp, FakeSession
from test_fix_round1 import Clock, Tabs

D = load_daemon()
NOW = 2_000_000_000.0


def arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def hook_at(conn, sid):
    return conn.execute("SELECT hook_at FROM evictions WHERE session_id=? ORDER BY id DESC",
                        (sid,)).fetchone()[0]


# ------------------------------------------------------------------ #35 schema

class SchemaV3(TempEnv):
    def test_v2_ledger_gains_hook_at_and_old_evictions_count_as_run(self):
        path = os.path.join(self.tmp, "v2.db")
        c = sqlite3.connect(path)
        c.executescript(ledger.SESSIONS_TABLE + ";" + """
            CREATE TABLE evictions(id INTEGER PRIMARY KEY, session_id TEXT, ts REAL,
              pid INTEGER, free_pct REAL, rss_mb REAL, signal TEXT, outcome TEXT, detail TEXT);
            CREATE TABLE events(id INTEGER PRIMARY KEY, ts REAL, session_id TEXT, event TEXT, detail TEXT);
            CREATE TABLE requests(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, session_id TEXT,
              done_at REAL, result TEXT);
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO evictions(session_id, ts, outcome) VALUES ('a', 100.0, 'term');
            PRAGMA user_version=2;""")
        c.close()
        conn = ledger.connect(path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT hook_at FROM evictions").fetchone()[0], 100.0)
        conn.close()
        ledger.connect(path, readonly=True).close()


# ------------------------------------------------------------------ #35 stamping + replay

class HookReplay(TempEnv):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.hooks = []
        self.cfg["on_evict"] = "/bin/true"

    def evictor(self, tabs):
        dead = []
        return D.Evictor(self.conn, self.cfg, tabs, kill=lambda p, s: dead.append(1),
                         identity=lambda r: "gone" if dead else "alive", clock=self.clock,
                         sleep=self.clock.sleep, on_evict=lambda exe, env: self.hooks.append(env),
                         children=lambda r: [], child_probe=lambda p, s: "gone")

    def row(self, sid):
        return self.insert(session_id=sid, state="idle", last_event_at=NOW - 3600,
                           iterm_guid="G-" + sid, pid=300, pid_start=1000, tty="ttys001",
                           title="Title " + sid)

    def test_a_normal_eviction_stamps_its_hook(self):
        r = self.row("n1")
        self.assertEqual(arun(self.evictor(Tabs()).evict(r, 10, 1)), "term")
        self.assertEqual(len(self.hooks), 1)
        self.assertIsNotNone(hook_at(self.conn, "n1"))

    def test_no_hook_configured_still_stamps(self):
        self.cfg["on_evict"] = None
        r = self.row("n2")
        arun(self.evictor(Tabs()).evict(r, 10, 1))
        self.assertEqual(self.hooks, [])
        self.assertIsNotNone(hook_at(self.conn, "n2"))

    def test_cancelled_eviction_is_replayed_by_the_sweep(self):
        class Cancelled(Tabs):
            async def job_name(self, guid):
                raise asyncio.CancelledError()
        r = self.row("c1")
        ev = self.evictor(Cancelled())
        with self.assertRaises(asyncio.CancelledError):
            arun(ev.evict(r, 10, 1))
        self.assertEqual(ledger.state_of(self.conn, "c1"), "hibernated")
        self.assertEqual(self.hooks, [])
        self.assertIsNone(hook_at(self.conn, "c1"))
        ts = self.conn.execute("SELECT ts FROM evictions WHERE session_id='c1'").fetchone()[0]
        # too young: the eviction may still be preparing its tab
        self.assertEqual(ev.sweep_hooks(ts + 10), 0)
        self.assertEqual(self.hooks, [])
        self.assertEqual(ev.sweep_hooks(ts + D.HOOK_SWEEP_MIN_AGE_S + 1), 1)
        self.assertEqual(len(self.hooks), 1)
        env = self.hooks[0]
        self.assertEqual((env["CC_SESSION_ID"], env["CC_SESSION_TITLE"], env["CC_EVICT_OUTCOME"]),
                         ("c1", "Title c1", "term"))
        self.assertIsNotNone(hook_at(self.conn, "c1"))
        # once stamped, never again
        self.assertEqual(ev.sweep_hooks(ts + 10_000), 0)
        self.assertEqual(len(self.hooks), 1)

    def _unstamped(self, sid, state, ts=NOW - 1000, outcome="term"):
        self.insert(session_id=sid, state=state, pid=301, pid_start=1000, tty="ttys001", title="T")
        with ledger.tx(self.conn):
            self.conn.execute("INSERT INTO evictions(session_id, ts, outcome) VALUES (?,?,?)",
                              (sid, ts, outcome))

    def test_sweep_only_replays_sessions_still_hibernated(self):
        ev = self.evictor(Tabs())
        self._unstamped("resumed", "idle")
        self._unstamped("pending", "evicting", outcome="unknown")
        self._unstamped("aborted", "hibernated", outcome="aborted")
        self.assertEqual(ev.sweep_hooks(NOW), 0)
        self.assertEqual(self.hooks, [])
        self.assertIsNotNone(hook_at(self.conn, "resumed"))  # nothing to park any more
        self.assertIsNone(hook_at(self.conn, "pending"))     # reconcile decides first
        self.assertIsNone(hook_at(self.conn, "aborted"))     # never hibernated by it
        # reconcile settles the unknown eviction as gone: now it gets its hook
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET state='hibernated' WHERE session_id='pending'")
        self.assertEqual(ev.sweep_hooks(NOW), 1)
        self.assertEqual(self.hooks[0]["CC_EVICT_OUTCOME"], "unknown")

    def test_only_the_newest_eviction_of_a_session_is_replayed(self):
        ev = self.evictor(Tabs())
        self._unstamped("twice", "hibernated", ts=NOW - 5000)
        with ledger.tx(self.conn):
            self.conn.execute("INSERT INTO evictions(session_id, ts, outcome) VALUES ('twice', ?, 'killed')",
                              (NOW - 1000,))
        self.assertEqual(ev.sweep_hooks(NOW), 1)
        self.assertEqual([h["CC_EVICT_OUTCOME"] for h in self.hooks], ["killed"])
        self.assertEqual(ev.sweep_hooks(NOW), 0)


class RunHookStamp(TempEnv):
    def script(self, body):
        p = os.path.join(self.tmp, "hook.sh")
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        return p

    def test_done_runs_after_the_hook_exits(self):
        seen = []
        arun(D._run_hook(self.script("exit 3"), dict(os.environ), lambda: seen.append(1)))
        self.assertEqual(seen, [1])

    def test_done_runs_for_a_hook_that_cannot_start(self):
        seen = []
        arun(D._run_hook(os.path.join(self.tmp, "missing"), dict(os.environ), lambda: seen.append(1)))
        self.assertEqual(seen, [1])

    def test_cancellation_leaves_the_eviction_unstamped(self):
        seen = []
        exe = self.script("sleep 0.5")

        async def go():
            t = asyncio.ensure_future(D._run_hook(exe, dict(os.environ), lambda: seen.append(1)))
            await asyncio.sleep(0.2)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.6)  # let the orphaned child exit before the loop closes
        arun(go())
        self.assertEqual(seen, [])


# ------------------------------------------------------------------ #36 foreground group

class ParseForeground(TempEnv):
    def test_shell_holding_the_terminal(self):
        out = "43984 43984 44024 Ss   /usr/bin/login\n44024 44024 44024 S+   -zsh\n"
        self.assertEqual(procs.parse_foreground(out.replace("Ss   ", "").replace("S+   ", "")), "-zsh")

    def test_claude_group_holds_the_terminal(self):
        out = ("17565 17565 17565 claude\n17823 17565 17565 node\n"
               "43264 43264 17565 /usr/bin/login\n43265 43265 17565 -zsh\n")
        self.assertEqual(procs.parse_foreground(out), "claude")

    def test_no_foreground_group(self):
        self.assertIsNone(procs.parse_foreground("100 100 -1 -zsh\n"))
        self.assertIsNone(procs.parse_foreground(""))
        self.assertIsNone(procs.parse_foreground(None))

    def test_full_paths_and_spaces(self):
        out = "5 5 5 /opt/claude/versions/2.1.296\n6 5 5 npm exec obsidian-mcp /a\n"
        self.assertEqual(procs.parse_foreground(out), "2.1.296")


class SettledJob(TempEnv):
    def test_lagging_job_is_replaced_by_a_foreground_shell(self):
        calls = []

        async def run(fn, *a):
            calls.append(a)
            return fn(*a)
        self.foreground["ttys001"] = "-zsh"
        self.assertEqual(arun(D.settled_job("2.1.296", "ttys001", run)), "-zsh")
        self.foreground["ttys001"] = "claude"
        self.assertEqual(arun(D.settled_job("2.1.296", "ttys001", run)), "2.1.296")
        self.foreground.clear()
        self.assertEqual(arun(D.settled_job("node", "ttys001", run)), "node")
        n = len(calls)
        self.assertEqual(arun(D.settled_job("-zsh", "ttys001", run)), "-zsh")  # no ps for a shell
        self.assertEqual(arun(D.settled_job(None, "ttys001", run)), None)
        self.assertEqual(len(calls), n)

    def test_a_failing_probe_keeps_the_job(self):
        async def run(fn, *a):
            raise RuntimeError("ps gone")
        self.assertEqual(arun(D.settled_job("node", "ttys001", run)), "node")


class ForegroundInPrepareTab(HookReplay):
    def test_jobname_stuck_on_the_dead_binary_still_gets_banner_and_command(self):
        self.foreground["ttys001"] = "-zsh"
        t = Tabs(job="2.1.296")
        r = self.row("f1")
        self.assertEqual(arun(self.evictor(t).evict(r, 10, 1)), "term")
        self.assertEqual(t.calls, ["name", "inject", "send"])
        self.assertEqual(t.args["name"], "[zz] Title f1")

    def test_claude_still_in_the_foreground_types_nothing(self):
        self.foreground["ttys001"] = "claude"
        t = Tabs(job="2.1.296")
        r = self.row("f2")
        arun(self.evictor(t).evict(r, 10, 1))
        self.assertEqual(t.calls, ["name"])


class ForegroundOnFocus(TempEnv):
    def daemon(self):
        d = D.Daemon(self.cfg, self.conn, FakeApp([]), None, False)

        async def ident(r):
            return D.procs.GONE
        d.identity = ident

        async def off(fn, *a):
            return fn(*a)
        d.off = off
        return d

    def test_focus_resumes_a_tab_whose_jobname_lags(self):
        self.insert(session_id="hib", tty="ttys002", state="hibernated", iterm_guid="G2",
                    evicted_at=time.time() - 600, pid=222, cwd="/w/x", title="T")
        self.foreground["ttys002"] = "-zsh"
        s = FakeSession("G2", "ttys002", job="node")
        arun(self.daemon().on_focus(s))
        self.assertEqual(len(s.sent), 1)
        self.assertEqual(ledger.state_of(self.conn, "hib"), "resuming")

    def test_focus_refuses_when_the_foreground_is_not_a_shell(self):
        self.insert(session_id="hib", tty="ttys002", state="hibernated", iterm_guid="G2",
                    evicted_at=time.time() - 600, pid=222, cwd="/w/x", title="T")
        self.foreground["ttys002"] = "vim"
        s = FakeSession("G2", "ttys002", job="vim")
        arun(self.daemon().on_focus(s))
        self.assertEqual(s.sent, [])
        self.assertEqual(ledger.state_of(self.conn, "hib"), "hibernated")
