"""Fix round 1: one or more tests per review item."""
import asyncio
import json
import os
import signal
import threading
import time

from helpers import REPO, TempEnv, assistant, iso, load_daemon, load_path, user

from ccsessions import ledger, pending, procs, restore

D = load_daemon()
NOW = 2_000_000_000.0
PROC = {"pid": 4242, "pid_start": 1000, "tty": "ttys004", "interactive": True}


def arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Clock(object):
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


class Tabs(object):
    def __init__(self, job="-zsh", tty="ttys001", visible=False, boom=False):
        self.job, self.tty_now, self.visible_now, self.boom = job, tty, visible, boom
        self.calls, self.args, self.job_polls = [], {}, 0

    async def job_name(self, guid):
        if self.boom:
            raise RuntimeError("iTerm2 went away")
        self.job_polls += 1
        if self.job_polls > 200:  # a wait loop that stopped sleeping or lost its deadline
            raise AssertionError("prepare_tab polled jobName more than 200 times")
        return self.job

    async def tty(self, guid):
        return self.tty_now

    async def is_visible(self, guid):
        return self.visible_now

    async def inject(self, guid, data):
        self.calls.append("inject")

    async def set_name(self, guid, name):
        self.calls.append("name")
        self.args["name"] = name

    async def send_text(self, guid, text):
        self.calls.append("send")
        self.args["send"] = text


class FakeWS(object):
    def __init__(self, closed):
        self.closed = closed


class FakeConn(object):
    def __init__(self, closed=False):
        self.websocket = FakeWS(closed)


class FakeApp(object):
    terminal_windows = []


# ------------------------------------------------------------------ 1. hang after iTerm2 quits

class Fix1Loops(TempEnv):
    def daemon(self, conn=None):
        return D.Daemon(self.cfg, self.conn, FakeApp(), conn, dry_run=True)

    def test_first_failing_loop_cancels_the_others_and_raises(self):
        d = self.daemon()
        cancelled = []

        async def forever():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(1)
                raise

        async def dies():
            await asyncio.sleep(0)
            raise D.ConnectionLost("gone")
        with self.assertRaises(D.ConnectionLost):
            arun(d.run_loops([forever(), dies(), forever()]))
        self.assertEqual(len(cancelled), 2)

    def test_tick_on_closed_websocket_raises_and_writes_no_heartbeat(self):
        d = self.daemon(FakeConn(closed=True))
        with self.assertRaises(D.ConnectionLost):
            arun(d.tick())
        self.assertIsNone(ledger.get_meta(self.conn, "heartbeat"))

    def test_tick_loop_gives_up_after_consecutive_failures(self):
        d = self.daemon()
        self.cfg["tick_s"] = 0
        n = []

        async def bad_tick():
            n.append(1)
            raise RuntimeError("boom")
        d.tick = bad_tick
        with self.assertRaises(D.ConnectionLost):
            arun(d.tick_loop())
        self.assertEqual(len(n), D.Daemon.MAX_TICK_FAILURES)

    def test_snapshot_timeout_is_connection_lost(self):
        d = self.daemon()
        d.SNAPSHOT_TIMEOUT_S = 0.01

        class SlowSession(object):
            session_id = "G"

            async def async_get_variable(self, name):
                await asyncio.sleep(5)

        class T(object):
            tab_id = "T"
            sessions = [SlowSession()]

        class W(object):
            tabs = [T()]
            current_tab = tabs[0]
        d.app = type("A", (), {"terminal_windows": [W()]})()
        with self.assertRaises(D.ConnectionLost):
            arun(d.snapshot())

    def test_heartbeat_written_after_a_good_tick(self):
        d = self.daemon(FakeConn(closed=False))

        async def off(fn, *a):
            return None if fn is procs.free_pct else fn(*a)
        d.off = off
        arun(d.tick())
        self.assertIsNotNone(ledger.get_meta(self.conn, "heartbeat"))


# ------------------------------------------------------------------ 2. last_focus_at = last SEEN

class Fix2Focus(TempEnv):
    def test_visible_rows_stamped_each_pass_and_on_losing_visibility(self):
        self.insert(session_id="a", tty="ttys001", last_focus_at=None)
        self.insert(session_id="b", tty="ttys002", last_focus_at=None)
        self.insert(session_id="c", tty="ttys003", last_focus_at=None)
        d = D.Daemon(self.cfg, self.conn, FakeApp(), None, True)
        f1 = {"tty_guid": {"ttys001": "G1", "ttys002": "G2", "ttys003": "G3"},
              "visible_ttys": {"ttys001", "ttys002"}, "visible_guids": set()}
        d.stamp_visibility(f1, now=1000.0)
        self.assertEqual(ledger.get(self.conn, "a")["last_focus_at"], 1000.0)
        self.assertEqual(ledger.get(self.conn, "b")["last_focus_at"], 1000.0)
        self.assertIsNone(ledger.get(self.conn, "c")["last_focus_at"])
        # 25 minutes later the user switches away from a/b to c: a/b were seen until NOW
        f2 = dict(f1, visible_ttys={"ttys003"})
        d.stamp_visibility(f2, now=2500.0)
        for sid in ("a", "b", "c"):
            self.assertEqual(ledger.get(self.conn, sid)["last_focus_at"], 2500.0, sid)
        d.stamp_visibility(f2, now=2600.0)
        self.assertEqual(ledger.get(self.conn, "a")["last_focus_at"], 2500.0)
        self.assertEqual(ledger.get(self.conn, "c")["last_focus_at"], 2600.0)


# ------------------------------------------------------------------ 3. CAS first, then every check

class Fix3Cas(TempEnv):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.row = self.insert(session_id="s1", state="idle", last_event_at=NOW - 3600,
                               iterm_guid="G1", pid=111, pid_start=1000)
        self.kills = []

    def evictor(self, identity=lambda r: True):
        return D.Evictor(self.conn, self.cfg, Tabs(), kill=lambda p, s: self.kills.append((p, s)),
                         identity=identity, clock=self.clock, sleep=self.clock.sleep,
                         children=lambda r: [], child_probe=lambda p, s: "gone")

    def test_recheck_runs_after_the_cas(self):
        seen = []

        def recheck(r):
            seen.append(ledger.state_of(self.conn, "s1"))
            return "stop"
        self.assertEqual(arun(self.evictor().evict(self.row, 10, 1, recheck=recheck)), "aborted-recheck")
        self.assertEqual(seen, ["evicting"])
        self.assertEqual(ledger.state_of(self.conn, "s1"), "idle")

    def test_registry_changes_between_cas_and_signal_means_no_signal(self):
        sd = self.cfg.path("claude_sessions")
        os.makedirs(sd)
        chk = D.default_identity(self.cfg)

        def recheck(r):
            with open(os.path.join(sd, "111.json"), "w") as fh:
                json.dump({"sessionId": "someone-else"}, fh)  # in-app /resume happened
            return chk(r)
        orig = D.procs.identity_probe
        D.procs.identity_probe = lambda pid, start, **kw: D.procs.ALIVE
        try:
            out = arun(self.evictor().evict(self.row, 10, 1, recheck=recheck))
        finally:
            D.procs.identity_probe = orig
        self.assertEqual(out, "aborted-recheck")
        self.assertEqual(self.kills, [])
        self.assertEqual(ledger.state_of(self.conn, "s1"), "idle")
        self.assertIn("registry names another session",
                      self.conn.execute("SELECT detail FROM evictions").fetchone()[0])

    def test_hook_during_eviction_is_held_and_restored_on_rollback(self):
        ev = self.evictor()

        def recheck(r):
            ledger.apply_event(self.conn, "UserPromptSubmit", {"session_id": "s1"}, None, now=NOW)
            self.assertEqual(ledger.state_of(self.conn, "s1"), "evicting")
            fresh = ledger.get(self.conn, "s1")
            return "hook activity" if fresh["last_event_at"] != r["last_event_at"] else None
        self.assertEqual(arun(ev.evict(self.row, 10, 1, recheck=recheck)), "aborted-recheck")
        r = ledger.get(self.conn, "s1")
        self.assertEqual((r["state"], r["held_state"], r["evicted_at"]), ("busy", None, None))

    def test_session_start_supersedes_evicting_rows_of_the_same_process(self):
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET state='evicting' WHERE session_id='s1'")
        ledger.apply_event(self.conn, "SessionStart", {"session_id": "s2", "source": "clear"},
                           {"pid": 111, "pid_start": 1000, "tty": "ttys001", "interactive": True})
        self.assertEqual(ledger.state_of(self.conn, "s1"), "superseded")


# ------------------------------------------------------------------ 4. SIGKILL errors, tri-state

class Fix4Kill(TempEnv):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.row = self.insert(session_id="s1", state="idle", last_event_at=NOW - 3600,
                               iterm_guid="G1", pid=111, pid_start=1000)
        self.hooks, self.tabs = [], Tabs()
        self.cfg["on_evict"] = "/bin/true"

    def evictor(self, kill, identity):
        return D.Evictor(self.conn, self.cfg, self.tabs, kill=kill, identity=identity, clock=self.clock,
                         sleep=self.clock.sleep, on_evict=lambda exe, env: self.hooks.append(env),
                         children=lambda r: [], child_probe=lambda p, s: "gone")

    def test_sigkill_eperm_leaves_row_evicting_and_touches_nothing(self):
        def kill(pid, sig):
            if sig == signal.SIGKILL:
                raise PermissionError(1, "Operation not permitted")
        out = arun(self.evictor(kill, lambda r: "alive").evict(self.row, 10, 1))
        self.assertEqual(out, "kill-failed")
        self.assertEqual(ledger.state_of(self.conn, "s1"), "evicting")
        self.assertEqual(self.tabs.calls, [])
        self.assertEqual(self.hooks, [])
        self.assertEqual(self.conn.execute("SELECT outcome FROM evictions").fetchone()[0], "kill-failed")

    def test_transient_ps_failure_after_sigterm_is_unknown_not_gone(self):
        states = iter(["alive", "unknown", "unknown", "unknown"])

        def ident(r):
            return next(states, "unknown")
        out = arun(self.evictor(lambda p, s: None, ident).evict(self.row, 10, 1))
        self.assertEqual(out, "unknown")
        self.assertEqual(ledger.state_of(self.conn, "s1"), "evicting")
        self.assertEqual((self.tabs.calls, self.hooks), ([], []))

    def test_sigterm_to_vanished_pid_is_fine(self):
        gone = []

        def kill(pid, sig):
            gone.append(1)
            raise ProcessLookupError()
        out = arun(self.evictor(kill, lambda r: "gone" if gone else "alive").evict(self.row, 10, 1))
        self.assertEqual(out, "term")
        self.assertEqual(ledger.state_of(self.conn, "s1"), "hibernated")

    def test_reconcile_is_tri_state(self):
        self.insert(session_id="a", state="evicting", state_since=0)
        self.insert(session_id="b", state="evicting", state_since=0)
        self.insert(session_id="c", state="evicting", state_since=0, held_state="busy")
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET state='evicting', state_since=0 WHERE session_id='s1'")
        D.reconcile(self.conn, {"a": "gone", "b": "unknown", "c": "alive", "s1": "unknown"}, now=100.0)
        self.assertEqual([ledger.state_of(self.conn, x) for x in ("a", "b", "c", "s1")],
                         ["hibernated", "evicting", "busy", "evicting"])

    def test_reconcile_min_age_spares_in_flight_evictions(self):
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET state='evicting', state_since=? WHERE session_id='s1'", (NOW,))
        D.reconcile(self.conn, {"s1": "gone"}, now=NOW + 10, min_age_s=75)
        self.assertEqual(ledger.state_of(self.conn, "s1"), "evicting")


# ------------------------------------------------------------------ 5. pending.py fails closed

class Fix5Pending(TempEnv):
    def an(self, records, now=None):
        return pending.analyze(self.transcript("p", records), now)

    def test_tool_result_only_user_records_are_not_a_user_message(self):
        tr = {"type": "user", "timestamp": iso(time.time()), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}}
        self.assertFalse(self.an([tr])["has_user"])
        self.assertFalse(self.an([user("hi", isSidechain=True)])["has_user"])

    def test_truncated_tail_scans_the_head_for_a_real_user(self):
        orig = pending.TAIL_BYTES
        pending.TAIL_BYTES = 400
        try:
            filler = [assistant("x" * 150) for _ in range(10)]
            self.assertFalse(self.an(filler)["has_user"])
            self.assertTrue(self.an([user("the first message")] + filler)["has_user"])
        finally:
            pending.TAIL_BYTES = orig

    def test_bare_task_id_does_not_complete_a_launch(self):
        launch = {"type": "user", "timestamp": iso(time.time() - 60),
                  "toolUseResult": {"isAsync": True, "agentId": "ag1"}}
        quoted = assistant("see <task-notification><task-id>ag1</task-id></task-notification>")
        bare = user("the id was <task-id>ag1</task-id>")
        self.assertEqual(self.an([user("go"), launch, quoted])["pending"], ["1 agent running"])
        self.assertEqual(self.an([user("go"), launch, bare])["pending"], ["1 agent running"])
        env = user("<task-notification>\n<task-id>ag1</task-id>\n<status>completed</status></task-notification>")
        self.assertEqual(self.an([user("go"), launch, env])["pending"], [])

    def test_malformed_middle_record_is_unreadable_torn_last_is_not(self):
        self.assertEqual(self.an([user("a"), "{broken", assistant("b")])["pending"], ["unreadable"])
        self.assertEqual(self.an([user("a"), "[1, 2]", assistant("b")])["pending"], ["unreadable"])
        self.assertEqual(self.an([user("a"), assistant("b"), '{"type":"assis'])["pending"], [])

    def test_tool_in_flight(self):
        call = assistant("running", tools=[{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {}}])
        self.assertEqual(self.an([user("go"), call])["pending"], ["tool in flight"])
        res = {"type": "user", "timestamp": iso(time.time()), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "ok"}]}}
        self.assertEqual(self.an([user("go"), call, res, assistant("done")])["pending"], [])
        self.assertEqual(self.an([user("go"), call, res])["pending"], [])


# ------------------------------------------------------------------ 8, 9. SessionEnd + idle_prompt

class Fix8Ledger(TempEnv):
    def ev(self, event, now=None, **p):
        p.setdefault("session_id", "a1")
        return ledger.apply_event(self.conn, event, p, dict(PROC), now)

    def test_session_end_other_reason_only(self):
        self.ev("Stop")
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET state='evicting', evicted_at=? WHERE session_id='a1'", (time.time(),))
        self.assertEqual(self.ev("SessionEnd", reason="prompt_input_exit")["state"], "ended")

    def test_session_end_after_a_resume_is_an_exit(self):
        now = time.time()
        self.ev("Stop")
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET state='idle', evicted_at=?, resumed_at=? WHERE session_id='a1'",
                              (now - 30, now - 10))
        self.assertEqual(self.ev("SessionEnd", reason="other", now=now)["state"], "ended")

    def test_stop_does_not_overwrite_evicting_but_records_activity(self):
        self.ev("Stop", now=100.0)
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET state='evicting' WHERE session_id='a1'")
        r = self.ev("UserPromptSubmit", now=200.0)
        self.assertEqual((r["state"], r["held_state"], r["last_event_at"]), ("evicting", "busy", 200.0))

    def test_fix9_idle_prompt_never_ends_waiting(self):
        self.ev("Notification", notification_type="permission_prompt")
        self.assertEqual(self.ev("Notification", notification_type="idle_prompt")["state"], "waiting")


# ------------------------------------------------------------------ 10. prepare_tab safety

class Fix10Tab(TempEnv):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.row = self.insert(session_id="s1", state="idle", last_event_at=NOW - 3600,
                               iterm_guid="G1", pid=111, pid_start=1000, tty="ttys001")
        self.hooks = []
        self.cfg["on_evict"] = "/bin/true"

    def go(self, tabs):
        dead = []
        ev = D.Evictor(self.conn, self.cfg, tabs, kill=lambda p, s: dead.append(1),
                       identity=lambda r: "gone" if dead else "alive", clock=self.clock,
                       sleep=self.clock.sleep, on_evict=lambda exe, env: self.hooks.append(env),
                       children=lambda r: [], child_probe=lambda p, s: "gone")
        return arun(ev.evict(self.row, 10, 1))

    def test_tab_exception_still_runs_on_evict(self):
        self.assertEqual(self.go(Tabs(boom=True)), "term")
        self.assertEqual(len(self.hooks), 1)

    def test_tty_changed_or_visible_means_nothing_typed(self):
        t = Tabs(tty="ttys009")
        self.go(t)
        self.assertEqual(t.calls, [])
        self.row = self.insert(session_id="s2", state="idle", last_event_at=NOW - 3600,
                               iterm_guid="G2", pid=112, pid_start=1000, tty="ttys001")
        t = Tabs(visible=True)
        self.go(t)
        self.assertEqual(t.calls, [])
        t = Tabs()
        self.row = self.insert(session_id="s3", state="idle", last_event_at=NOW - 3600,
                               iterm_guid="G3", pid=113, pid_start=1000, tty="ttys001",
                               title="Fix the uploader", cwd="/w/s3")
        self.go(t)
        self.assertEqual(t.calls, ["name", "inject", "send"])
        self.assertEqual(t.args["name"], "[zz] Fix the uploader")
        self.assertIn("s3 --cwd /w/s3", t.args["send"])

    def test_tab_not_at_a_shell_is_still_named(self):
        # iTerm2 kept reporting the dead claude binary (named after its version) or a
        # lingering MCP child as the job: the tab must still get its [zz] title
        for job in ("2.1.296", "node"):
            t = Tabs(job=job)
            self.row = self.insert(session_id="s-" + job, state="idle", last_event_at=NOW - 3600,
                                   iterm_guid="G-" + job, pid=120, pid_start=1000, tty="ttys001",
                                   title="Title " + job)
            self.assertEqual(self.go(t), "term")
            self.assertEqual(t.calls, ["name"])
            self.assertEqual(t.args["name"], "[zz] Title " + job)
            self.assertEqual(len(self.hooks), 1)
            self.hooks.clear()

    def test_wait_is_bounded_by_the_clock_not_the_poll_count(self):
        clock = self.clock

        class Slow(Tabs):
            def __init__(self):
                super().__init__(job="2.1.296")
                self.polls = 0

            async def job_name(self, guid):
                self.polls += 1
                clock.t += 4.5  # each iTerm2 round trip answers just under its timeout
                return self.job
        t = Slow()
        start = clock.t
        with self.assertLogs(D.log, "WARNING") as logs:
            self.go(t)
        self.assertLessEqual(t.polls, 6)
        self.assertEqual(t.calls, ["name"])
        # the deadline, at most one sleep plus one round trip past it, and the SessionEnd
        # grace that nothing flips in this test
        self.assertLessEqual(clock.t - start,
                             D.TAB_SHELL_WAIT_S + 0.5 + 4.5 + D.SESSION_END_WAIT_S)
        # polls end at 4.5, 9.5, 14.5, 19.5, 24.5 s: the log reports the measured 24 s
        self.assertTrue(any("not at a shell after 24s" in m for m in logs.output), logs.output)

    def _flip_row_in(self, method, sid, guid, pid):
        conn = self.conn

        class Flip(Tabs):
            async def tty(self, g):
                if method == "tty":
                    conn.execute("UPDATE sessions SET state='resuming' WHERE session_id=?", (sid,))
                    conn.commit()
                return self.tty_now

            async def set_name(self, g, name):
                await Tabs.set_name(self, g, name)
                if method == "name":
                    conn.execute("UPDATE sessions SET state='resuming' WHERE session_id=?", (sid,))
                    conn.commit()

            async def inject(self, g, data):
                await Tabs.inject(self, g, data)
                if method == "inject":
                    conn.execute("UPDATE sessions SET state='resuming' WHERE session_id=?", (sid,))
                    conn.commit()
        self.row = self.insert(session_id=sid, state="idle", last_event_at=NOW - 3600,
                               iterm_guid=guid, pid=pid, pid_start=1000, tty="ttys001")
        return Flip()

    def test_resume_racing_the_tab_writes_stops_them(self):
        # wake can resume the session during any await after the wait: the row is re-read
        # right before the name and right before the typed command
        t = self._flip_row_in("tty", "w1", "GW1", 150)
        self.go(t)
        self.assertEqual(t.calls, [])
        t = self._flip_row_in("name", "w3", "GW3", 152)
        self.go(t)
        self.assertEqual(t.calls, ["name"])
        t = self._flip_row_in("inject", "w2", "GW2", 151)
        self.go(t)
        self.assertEqual(t.calls, ["name", "inject"])

    def test_job_settles_to_a_shell_within_the_wait(self):
        class Settling(Tabs):
            def __init__(self):
                super().__init__()
                self.seen = 0

            async def job_name(self, guid):
                self.seen += 1
                return "2.1.296" if self.seen < 12 else "-zsh"
        t = Settling()
        self.assertEqual(self.go(t), "term")
        self.assertEqual(t.calls, ["name", "inject", "send"])

    def _changes_mid_wait(self, **after):
        class Late(Tabs):
            def __init__(self):
                super().__init__(job="2.1.296")
                self.seen = 0

            async def job_name(self, guid):
                self.seen += 1
                return "2.1.296" if self.seen < 5 else "-zsh"

            async def tty(self, guid):
                return after.get("tty", "ttys001") if self.seen >= 3 else "ttys001"

            async def is_visible(self, guid):
                return after.get("visible", False) and self.seen >= 3
        return Late()

    def test_visible_or_tty_change_during_the_wait_leaves_the_tab_alone(self):
        for i, change in enumerate(({"visible": True}, {"tty": "ttys009"})):
            t = self._changes_mid_wait(**change)
            self.row = self.insert(session_id="m%d" % i, state="idle", last_event_at=NOW - 3600,
                                   iterm_guid="GM%d" % i, pid=130 + i, pid_start=1000, tty="ttys001")
            self.assertEqual(self.go(t), "term")
            self.assertEqual(t.calls, [], change)

    def test_tab_gone_types_nothing(self):
        class Gone(Tabs):
            async def job_name(self, guid):
                return None
        t = Gone()
        self.assertEqual(self.go(t), "term")
        self.assertEqual(t.calls, [])

    def test_resumed_during_the_wait_is_not_renamed(self):
        conn, sid = self.conn, "r1"

        class Resumed(Tabs):
            def __init__(self):
                super().__init__(job="2.1.296")
                self.seen = 0

            async def job_name(self, guid):
                self.seen += 1
                if self.seen == 3:
                    conn.execute("UPDATE sessions SET state='resuming' WHERE session_id=?", (sid,))
                    conn.commit()
                return "2.1.296"
        t = Resumed()
        self.row = self.insert(session_id=sid, state="idle", last_event_at=NOW - 3600,
                               iterm_guid="GR", pid=140, pid_start=1000, tty="ttys001")
        self.go(t)
        self.assertEqual(t.calls, [])


# ------------------------------------------------------------------ 11, 12, 14, 16

class Fix11To16Daemon(TempEnv):
    def test_fix11_all_idle_never_repicks_an_aborted_session(self):
        d = D.Daemon(self.cfg, self.conn, FakeApp(), None, False)
        excluded_seen = []
        script = iter([("A", "aborted-cas"), ("B", "term"), (None, None)])

        async def pick(free, rows=None, explicit=False, exclude=()):
            excluded_seen.append(set(exclude))
            sid, out = next(script)
            return out, sid
        d.pick = pick
        res = arun(d.do_request({"kind": "hibernate-all-idle", "session_id": None}))
        self.assertEqual(res, "evicted 1")
        self.assertEqual(excluded_seen, [set(), {"A"}, {"A"}])

    def test_fix12_blocking_work_runs_off_the_loop_thread(self):
        d = D.Daemon(self.cfg, self.conn, FakeApp(), None, True)
        loop_thread = threading.get_ident()
        seen = []
        arun(d.off(lambda: seen.append(threading.get_ident())))
        self.assertNotEqual(seen[0], loop_thread)
        # the policy half that shells out takes no SQLite connection
        import inspect
        self.assertNotIn("conn", inspect.signature(D.compute_policy).parameters)

    def test_fix14_on_focus_ignores_non_interactive_rows(self):
        self.insert(session_id="h", tty="ttys002", state="hibernated", iterm_guid="G2", interactive=0,
                    evicted_at=time.time() - 600)
        d = D.Daemon(self.cfg, self.conn, FakeApp(), None, False)

        async def ident(r):
            return procs.GONE
        d.identity = ident

        class S(object):
            session_id = "G2"
            sent = []

            async def async_get_variable(self, n):
                return {"tty": "/dev/ttys002", "jobName": "zsh"}[n]

            async def async_send_text(self, t):
                self.sent.append(t)
        s = S()
        arun(d.on_focus(s))
        self.assertEqual(s.sent, [])
        self.assertEqual(ledger.state_of(self.conn, "h"), "hibernated")

    def test_fix16_wake_all_is_capped(self):
        for i in range(14):
            self.insert(session_id="h%02d" % i, state="hibernated", evicted_at=1000 + i, tty=None)
        d = D.Daemon(self.cfg, self.conn, FakeApp(), None, True)
        woke = []

        async def wake(r):
            woke.append(r["session_id"])
            return "resumed"
        d.wake = wake

        async def off(fn, *a):
            return 50 if fn is procs.free_pct else fn(*a)
        d.off = off
        res = arun(d.do_request({"kind": "wake-all", "session_id": None}))
        self.assertEqual(len(woke), 10)
        self.assertEqual(woke[0], "h13")  # most recently hibernated first
        self.assertIn("limit 10", res)


# ------------------------------------------------------------------ 13, 15. restore marker + retype

class Fix13Restore(TempEnv):
    def patch(self, guids):
        saved = (restore.iterm_guids, restore.procs.boot_time)
        restore.iterm_guids = lambda: guids
        restore.procs.boot_time = lambda: 10 ** 9
        self.addCleanup(lambda: (setattr(restore, "iterm_guids", saved[0]),
                                 setattr(restore.procs, "boot_time", saved[1])))

    def test_no_boot_marker_when_tabs_could_not_be_listed(self):
        self.insert(session_id="h", state="hibernated", iterm_guid="G1", pid=None,
                    last_event_at=10 ** 9 - 100)
        self.patch(None)
        out = []
        restore.run(self.cfg, self.conn, auto=True, out=out.append)
        self.assertFalse(os.path.exists(self.cfg.path("bootmark")))
        self.assertTrue(any("not written" in l for l in out))
        self.patch({"G1"})
        restore.run(self.cfg, self.conn, auto=True, out=out.append)
        self.assertTrue(os.path.exists(self.cfg.path("bootmark")))

    def test_fix15_retyped_rows_are_not_retyped_next_boot(self):
        self.insert(session_id="h", state="hibernated", iterm_guid="G1", pid=None, last_event_at=900)
        ledger.mark_restored(self.conn, "h", "G-new", now=1050)
        r = ledger.get(self.conn, "h")
        self.assertEqual(r["iterm_guid"], "G-new")
        # restored this boot and the replacement tab is live: skip
        run, typed, skipped = restore.select([r], 1000, 1100, self.cfg, lambda r: False, auto=True,
                                             live_guids={"G-new"})
        self.assertEqual(typed, [])
        self.assertIn("already retyped", skipped[0][1])
        # round 2: replacement tab gone -> retype
        run, typed, _ = restore.select([r], 1000, 1100, self.cfg, lambda r: False, auto=True, live_guids=set())
        self.assertEqual([t["session_id"] for t in typed], ["h"])
        # round 2: restored during an earlier boot -> not a reason to skip
        run, typed, _ = restore.select([r], 1060, 1100, self.cfg, lambda r: False, auto=True, live_guids=set())
        self.assertEqual([t["session_id"] for t in typed], ["h"])

    def test_osascript_returns_session_ids(self):
        lines = restore.osascript_for([("a", True), ("b", False)])
        self.assertIn('set o to o & (id of s) & " " & y & linefeed', lines)
        # a tab without a tty yet must not fail the whole script
        i = lines.index("set y to tty of s")
        self.assertEqual(lines[i - 1], "try")
        # read after the tab settles: a tty read right after create can come back empty
        self.assertLess(lines.index("delay 0.4"), i)
        self.assertEqual(lines[i + 1:i + 3], ['if y is missing value then set y to ""', "end try"])
        self.assertEqual(lines[-1], "return o")


# ------------------------------------------------------------------ 18. cc-resume claims identity

class Fix18Resume(TempEnv):
    def test_claim_then_new_session_id_supersedes(self):
        mod = load_path("cc_resume_bin_f18", os.path.join(REPO, "bin", "cc-resume"))
        self.insert(session_id="old", state="hibernated", pid=None, pid_start=None)
        mod.mark_resuming(self.cfg, "old", pid=7777, pid_start=5555)
        r = ledger.get(self.conn, "old")
        self.assertEqual((r["state"], r["pid"], r["pid_start"]), ("resuming", 7777, 5555))
        ledger.apply_event(self.conn, "SessionStart", {"session_id": "new", "source": "resume"},
                           {"pid": 7777, "pid_start": 5555, "tty": "ttys001", "interactive": True})
        self.assertEqual(ledger.state_of(self.conn, "old"), "superseded")
