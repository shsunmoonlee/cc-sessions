"""dotclaude#35 (on_evict replay after a cancelled eviction or a dead daemon) and #36 (the
kernel's foreground group decides whether a tab is at a shell, not iTerm2's jobName)."""
import argparse
import asyncio
import contextlib
import io
import os
import sqlite3
import stat
import time
from unittest import mock

from helpers import REAL_FOREGROUND_JOB, TempEnv, load_daemon

from ccsessions import cli, ledger, procs

from test_daemon import FakeApp, FakeSession, FakeTabs
from test_fix_round1 import Clock, Tabs
from test_idle_hibernate import IdleBase

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


def outcome_of(conn, sid):
    return conn.execute("SELECT outcome FROM evictions WHERE session_id=? ORDER BY id DESC",
                        (sid,)).fetchone()[0]


V2_LEDGER = ledger.SESSIONS_TABLE + ";" + """
    CREATE TABLE evictions(id INTEGER PRIMARY KEY, session_id TEXT, ts REAL,
      pid INTEGER, free_pct REAL, rss_mb REAL, signal TEXT, outcome TEXT, detail TEXT);
    CREATE TABLE events(id INTEGER PRIMARY KEY, ts REAL, session_id TEXT, event TEXT, detail TEXT);
    CREATE TABLE requests(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, session_id TEXT,
      done_at REAL, result TEXT);
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    INSERT INTO sessions(session_id, state) VALUES ('h1', 'hibernated');
    INSERT INTO evictions(session_id, ts, outcome) VALUES ('h1', 100.0, 'term');
    PRAGMA user_version=2;"""


# ------------------------------------------------------------------ #35 schema

class SchemaV3(TempEnv):
    def v2(self):
        path = os.path.join(self.tmp, "v2.db")
        c = sqlite3.connect(path)
        c.executescript(V2_LEDGER)
        c.close()
        return path

    def test_v2_ledger_gains_hook_at_and_old_evictions_count_as_run(self):
        path = self.v2()
        conn = ledger.connect(path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT hook_at FROM evictions").fetchone()[0], 100.0)
        conn.close()
        ledger.connect(path, readonly=True).close()

    def test_readers_keep_answering_on_a_ledger_not_yet_migrated(self):
        # the old daemon is still running after `make install`: a SessionEnd handoff that
        # reads "unknown" here would release a hibernating session's claims
        path = self.v2()
        conn = ledger.connect(path, readonly=True)
        self.assertEqual(ledger.state_of(conn, "h1"), "hibernated")
        conn.close()
        self.assertEqual(sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0], 2)
        self.cfg["db"] = path
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_state(self.cfg, argparse.Namespace(sid="h1"))
        self.assertEqual((rc, buf.getvalue().strip()), (0, "hibernated"))

    def test_readers_still_refuse_v1_and_newer(self):
        for v in (1, 4):
            path = os.path.join(self.tmp, "v%d.db" % v)
            c = sqlite3.connect(path)
            c.execute("PRAGMA user_version=%d" % v)
            c.close()
            with self.assertRaises(ledger.LedgerError):
                ledger.connect(path, readonly=True)


# ------------------------------------------------------------------ #35 stamping + replay

class HookBase(TempEnv):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.hooks = []
        self.cfg["on_evict"] = "/bin/true"
        ledger.begin_hook_tracking(self.conn, 0)  # this ledger tracks hooks from the start

    def evictor(self, tabs, identity=None):
        dead = []
        return D.Evictor(self.conn, self.cfg, tabs, kill=lambda p, s: dead.append(1),
                         identity=identity or (lambda r: "gone" if dead else "alive"),
                         clock=self.clock, sleep=self.clock.sleep,
                         on_evict=lambda exe, env: self.hooks.append(env),
                         children=lambda r: [], child_probe=lambda p, s: "gone")

    def row(self, sid):
        return self.insert(session_id=sid, state="idle", last_event_at=NOW - 3600,
                           iterm_guid="G-" + sid, pid=300, pid_start=1000, tty="ttys001",
                           title="Title " + sid)

    def unstamped(self, sid, state, ts=NOW - 1000, outcome="term"):
        self.insert(session_id=sid, state=state, pid=301, pid_start=1000, tty="ttys001", title="T")
        with ledger.tx(self.conn):
            cur = self.conn.execute("INSERT INTO evictions(session_id, ts, outcome) VALUES (?,?,?)",
                                    (sid, ts, outcome))
        return cur.lastrowid

    def ts_of(self, sid):
        return self.conn.execute("SELECT ts FROM evictions WHERE session_id=?", (sid,)).fetchone()[0]


class HookReplay(HookBase):
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

    def test_cancelled_while_preparing_the_tab_is_replayed(self):
        class Cancelled(Tabs):
            async def job_name(self, guid):
                raise asyncio.CancelledError()
        r = self.row("c1")
        ev = self.evictor(Cancelled())
        with self.assertRaises(asyncio.CancelledError):
            arun(ev.evict(r, 10, 1))
        self.assertEqual(ledger.state_of(self.conn, "c1"), "hibernated")
        self.assertEqual((self.hooks, hook_at(self.conn, "c1")), ([], None))
        self.assertEqual(ev.active, set())
        ts = self.ts_of("c1")
        self.assertEqual(ev.sweep_hooks(ts + 10), 0)  # a previous daemon's hook may still run
        self.assertEqual(ev.sweep_hooks(ts + D.HOOK_SWEEP_MIN_AGE_S + 1), 1)
        env = self.hooks[0]
        self.assertEqual((env["CC_SESSION_ID"], env["CC_SESSION_TITLE"], env["CC_EVICT_OUTCOME"]),
                         ("c1", "Title c1", "term"))
        self.assertIsNotNone(hook_at(self.conn, "c1"))
        self.assertEqual(ev.sweep_hooks(ts + 10_000), 0)
        self.assertEqual(len(self.hooks), 1)

    def test_cancelled_between_signal_and_hook_is_replayed_after_reconcile(self):
        # cut off in the SIGTERM wait: the eviction row is still 'started', the session
        # still evicting; reconcile later settles it as gone
        killed = []

        def identity(r):
            if killed:
                raise asyncio.CancelledError()
            return "alive"
        r = self.row("c2")
        ev = self.evictor(Tabs(), identity=identity)
        ev.kill = lambda p, s: killed.append(s)
        with self.assertRaises(asyncio.CancelledError):
            arun(ev.evict(r, 10, 1))
        self.assertEqual(outcome_of(self.conn, "c2"), "started")
        self.assertEqual(ledger.state_of(self.conn, "c2"), "evicting")
        later = self.ts_of("c2") + 10_000
        self.assertEqual(ev.sweep_hooks(later), 0)  # reconcile decides first
        self.assertIsNone(hook_at(self.conn, "c2"))
        fixed = D.reconcile(self.conn, {"c2": procs.GONE}, later)
        self.assertEqual(fixed[0][2], "hibernated")
        self.assertEqual(ev.sweep_hooks(later), 1)
        self.assertEqual(self.hooks[0]["CC_EVICT_OUTCOME"], "interrupted")

    def test_an_eviction_still_running_here_is_never_replayed(self):
        ev = self.evictor(Tabs())
        eid = self.unstamped("live", "hibernated")
        ev.active.add(eid)
        self.assertEqual(ev.sweep_hooks(NOW), 0)
        ev.active.discard(eid)
        ev.hooks_running.add(eid)
        self.assertEqual(ev.sweep_hooks(NOW), 0)
        ev.hooks_running.discard(eid)
        self.assertEqual(ev.sweep_hooks(NOW), 1)

    def test_a_replay_whose_stamp_fails_runs_once_per_daemon(self):
        ev = self.evictor(Tabs())
        self.unstamped("sticky", "hibernated")
        with mock.patch.object(ledger, "hook_done", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(ev.sweep_hooks(NOW), 1)
            self.assertEqual(ev.sweep_hooks(NOW + 60), 0)
        self.assertEqual(len(self.hooks), 1)
        self.assertIsNone(hook_at(self.conn, "sticky"))
        # the next daemon (a fresh Evictor) tries again, once
        self.assertEqual(self.evictor(Tabs()).sweep_hooks(NOW + 120), 1)
        self.assertIsNotNone(hook_at(self.conn, "sticky"))

    def test_sweep_only_replays_sessions_still_hibernated(self):
        ev = self.evictor(Tabs())
        self.unstamped("resumed", "idle")
        self.unstamped("pending", "evicting", outcome="unknown")
        self.unstamped("resuming", "resuming")
        self.unstamped("aborted", "hibernated", outcome="aborted")
        self.assertEqual(ev.sweep_hooks(NOW), 0)
        self.assertEqual(self.hooks, [])
        self.assertIsNotNone(hook_at(self.conn, "resumed"))  # nothing left to park
        self.assertIsNone(hook_at(self.conn, "pending"))     # reconcile decides first
        self.assertIsNone(hook_at(self.conn, "resuming"))    # the resume may still fail
        self.assertIsNone(hook_at(self.conn, "aborted"))     # never signalled
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET state='hibernated' "
                              "WHERE session_id IN ('pending', 'resuming')")
        self.assertEqual(ev.sweep_hooks(NOW), 2)
        self.assertEqual(sorted(h["CC_EVICT_OUTCOME"] for h in self.hooks), ["term", "unknown"])

    def test_only_the_newest_eviction_of_a_session_is_replayed(self):
        ev = self.evictor(Tabs())
        self.unstamped("twice", "hibernated", ts=NOW - 5000)
        with ledger.tx(self.conn):
            self.conn.execute("INSERT INTO evictions(session_id, ts, outcome) VALUES ('twice', ?, 'killed')",
                              (NOW - 1000,))
        self.assertEqual(ev.sweep_hooks(NOW), 1)
        self.assertEqual([h["CC_EVICT_OUTCOME"] for h in self.hooks], ["killed"])
        self.assertEqual(ev.sweep_hooks(NOW), 0)


class HookTrackingStart(TempEnv):
    def test_the_first_tracking_daemon_counts_earlier_evictions_as_run(self):
        # a pre-v3 daemon kept evicting after a hook process migrated the ledger: it ran
        # those hooks, so the first v3 daemon must not replay them
        self.cfg["on_evict"] = "/bin/true"
        hooks = []
        ev = D.Evictor(self.conn, self.cfg, Tabs(), on_evict=lambda exe, env: hooks.append(env),
                       clock=Clock())
        self.insert(session_id="old", state="hibernated", pid=1, pid_start=1, tty="ttys001")
        with ledger.tx(self.conn):
            self.conn.execute("INSERT INTO evictions(session_id, ts, outcome) VALUES ('old', ?, 'term')",
                              (NOW - 1000,))
        self.assertEqual(ev.sweep_hooks(NOW), 0)
        self.assertEqual(hooks, [])
        self.assertEqual(hook_at(self.conn, "old"), NOW - 1000)
        self.assertEqual(ledger.begin_hook_tracking(self.conn, NOW + 1), 0)  # once per ledger


class SweepWiring(IdleBase):
    def tick_daemon(self):
        d = self.daemon([])
        d.last_prune = d.last_reap = time.time()

        async def off(fn, *a):
            if fn is procs.free_pct:
                return 90
            if fn is procs.swap_pct:
                return None
            return fn(*a)
        d.off = off
        return d

    def test_tick_runs_the_sweep(self):
        self.cfg["idle_hibernate_min"] = 0
        d = self.tick_daemon()
        seen = []
        d.evictor.sweep_hooks = lambda now=None: seen.append(now)
        arun(d.tick())
        self.assertEqual(len(seen), 1)

    def test_a_failing_sweep_does_not_fail_the_tick(self):
        self.cfg["idle_hibernate_min"] = 0
        d = self.tick_daemon()

        def boom(now=None):
            raise sqlite3.OperationalError("database is locked")
        d.evictor.sweep_hooks = boom
        with self.assertLogs(D.log, "ERROR"):
            arun(d.tick())
        self.assertGreater(float(ledger.get_meta(self.conn, "heartbeat")), 0)

    def test_dry_run_never_sweeps(self):
        self.cfg["idle_hibernate_min"] = 0
        d = self.tick_daemon()
        d.dry_run = True
        seen = []
        d.evictor.sweep_hooks = lambda now=None: seen.append(now)
        arun(d.tick())
        self.assertEqual(seen, [])


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

    def test_a_timed_out_hook_counts_as_run(self):
        # otherwise a hook that always hangs would be replayed by every new daemon
        seen = []
        with mock.patch.object(D, "ON_EVICT_TIMEOUT_S", 0.2), self.assertLogs(D.log, "WARNING"):
            arun(D._run_hook(self.script("sleep 2"), dict(os.environ), lambda: seen.append(1)))
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
        out = "43984 43984 44024 /usr/bin/login\n44024 44024 44024 -zsh\n"
        self.assertEqual(procs.parse_foreground(out), "-zsh")

    def test_claude_group_holds_the_terminal(self):
        out = ("17565 17565 17565 claude\n17823 17565 17565 node\n"
               "43264 43264 17565 /usr/bin/login\n43265 43265 17565 -zsh\n")
        self.assertEqual(procs.parse_foreground(out), "claude")

    def test_a_shell_listed_first_in_a_mixed_group_does_not_win(self):
        self.assertEqual(procs.parse_foreground("10 10 10 -zsh\n11 10 10 vim\n"), "vim")

    def test_no_foreground_group(self):
        self.assertIsNone(procs.parse_foreground("100 100 -1 -zsh\n"))
        self.assertIsNone(procs.parse_foreground(""))
        self.assertIsNone(procs.parse_foreground(None))

    def test_full_paths_and_spaces(self):
        out = "5 5 5 /opt/claude/versions/2.1.296\n6 5 5 npm exec obsidian-mcp /a\n"
        self.assertEqual(procs.parse_foreground(out), "2.1.296")


class ForegroundJobProbe(TempEnv):
    def test_asks_ps_for_the_tty_and_parses_it(self):
        seen = []

        def run(args, timeout=10):
            seen.append(args)
            return 0, "1 1 1 -zsh\n"
        with mock.patch.object(procs, "_run_rc", run):
            self.assertEqual(REAL_FOREGROUND_JOB("/dev/ttys004"), "-zsh")
        self.assertEqual(seen, [["ps", "-t", "ttys004", "-o", "pid=,pgid=,tpgid=,comm="]])

    def test_failures_mean_cannot_tell(self):
        with mock.patch.object(procs, "_run_rc", lambda a, timeout=10: (1, "")):
            self.assertIsNone(REAL_FOREGROUND_JOB("ttys004"))

        def broken(a, timeout=10):
            raise procs.ProcsError("ps timed out")
        with mock.patch.object(procs, "_run_rc", broken):
            self.assertIsNone(REAL_FOREGROUND_JOB("ttys004"))
        self.assertIsNone(REAL_FOREGROUND_JOB(None))


class SettledJob(TempEnv):
    async def run(self, fn, *a):
        return fn(*a)

    def test_the_foreground_group_wins_when_ps_can_tell(self):
        self.foreground["ttys001"] = "-zsh"
        self.assertEqual(arun(D.settled_job("2.1.296", "ttys001", self.run)), "-zsh")
        self.foreground["ttys001"] = "claude"
        self.assertEqual(arun(D.settled_job("2.1.296", "ttys001", self.run)), "claude")
        # a stale shell name does not hide a program that just took the terminal
        self.foreground["ttys001"] = "vim"
        self.assertEqual(arun(D.settled_job("-zsh", "ttys001", self.run)), "vim")

    def test_jobname_when_ps_cannot_tell_and_none_stays_none(self):
        self.assertEqual(arun(D.settled_job("node", "ttys001", self.run)), "node")
        self.assertEqual(arun(D.settled_job("-zsh", "ttys001", self.run)), "-zsh")
        self.foreground["ttys001"] = "-zsh"
        self.assertIsNone(arun(D.settled_job(None, "ttys001", self.run)))
        self.assertEqual(arun(D.settled_job("node", None, self.run)), "node")

    def test_a_failing_probe_keeps_the_job(self):
        async def run(fn, *a):
            raise RuntimeError("ps gone")
        self.assertEqual(arun(D.settled_job("node", "ttys001", run)), "node")


class ForegroundInPrepareTab(HookBase):
    def test_jobname_stuck_on_the_dead_binary_still_gets_banner_and_command(self):
        self.foreground["ttys001"] = "-zsh"
        t = Tabs(job="2.1.296")
        r = self.row("f1")
        self.assertEqual(arun(self.evictor(t).evict(r, 10, 1)), "term")
        self.assertEqual(t.calls, ["name", "inject", "send"])
        self.assertEqual(t.args["name"], "[zz] Title f1")

    def test_the_shell_takes_the_terminal_back_during_the_wait(self):
        fg = self.foreground
        fg["ttys001"] = "claude"

        class Lagging(Tabs):
            async def job_name(self, guid):
                job = await Tabs.job_name(self, guid)
                if self.job_polls >= 4:
                    fg["ttys001"] = "-zsh"  # zsh reaped claude and took the terminal back
                return job
        t = Lagging(job="2.1.296")
        r = self.row("f3")
        arun(self.evictor(t).evict(r, 10, 1))
        self.assertEqual(t.calls, ["name", "inject", "send"])
        self.assertLess(t.job_polls, 10)

    def test_claude_still_in_the_foreground_types_nothing(self):
        self.foreground["ttys001"] = "claude"
        t = Tabs(job="2.1.296")
        r = self.row("f2")
        arun(self.evictor(t).evict(r, 10, 1))
        self.assertEqual(t.calls, ["name"])


class ForegroundDaemonPaths(TempEnv):
    def daemon(self):
        d = D.Daemon(self.cfg, self.conn, FakeApp([]), None, False)

        async def ident(r):
            return D.procs.GONE
        d.identity = ident

        async def off(fn, *a):
            return fn(*a)
        d.off = off
        return d

    def hib(self):
        self.insert(session_id="hib", tty="ttys002", state="hibernated", iterm_guid="G2",
                    evicted_at=time.time() - 600, pid=222, cwd="/w/x", title="T")

    def test_focus_resumes_a_tab_whose_jobname_lags(self):
        self.hib()
        self.foreground["ttys002"] = "-zsh"
        s = FakeSession("G2", "ttys002", job="node")
        arun(self.daemon().on_focus(s))
        self.assertEqual(len(s.sent), 1)
        self.assertEqual(ledger.state_of(self.conn, "hib"), "resuming")

    def test_focus_refuses_when_the_foreground_is_not_a_shell(self):
        self.hib()
        self.foreground["ttys002"] = "vim"
        s = FakeSession("G2", "ttys002", job="-zsh")
        arun(self.daemon().on_focus(s))
        self.assertEqual(s.sent, [])
        self.assertEqual(ledger.state_of(self.conn, "hib"), "hibernated")

    def test_wake_resumes_a_tab_whose_jobname_lags(self):
        self.hib()
        d = self.daemon()
        d.tabs = FakeTabs(["node"], tty="ttys002")
        self.foreground["ttys002"] = "-zsh"
        self.assertEqual(arun(d.wake(ledger.get(self.conn, "hib"))), "resumed")
        sent = [c for c in d.tabs.calls if c[0] == "send"]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][2].startswith("\x15") and sent[0][2].endswith("\r"))

    def test_wake_refuses_a_stale_shell_name_over_a_running_program(self):
        self.hib()
        d = self.daemon()
        d.tabs = FakeTabs(["-zsh"], tty="ttys002")
        self.foreground["ttys002"] = "vim"
        self.assertEqual(arun(d.wake(ledger.get(self.conn, "hib"))), "tab is running vim")
        self.assertFalse([c for c in d.tabs.calls if c[0] == "send"])
        self.assertEqual(ledger.state_of(self.conn, "hib"), "hibernated")
