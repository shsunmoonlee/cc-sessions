"""Fix round 2: one or more tests per review item."""
import asyncio
import json
import os
import sqlite3
import time

from helpers import TempEnv, load_daemon

from ccsessions import ledger, procs, restore

D = load_daemon()
NOW = 2_000_000_000.0
BOOT = int(time.time()) - 1000

V1_SESSIONS = """CREATE TABLE sessions(
  session_id TEXT PRIMARY KEY, pid INTEGER, pid_start TEXT, interactive INTEGER DEFAULT 1,
  tty TEXT, cwd TEXT, launch_cwd TEXT, transcript TEXT, title TEXT,
  state TEXT, state_since REAL, last_event TEXT, last_event_at REAL,
  subagents INTEGER DEFAULT 0, started_at REAL, ended_at REAL, end_reason TEXT,
  source TEXT, iterm_guid TEXT, last_focus_at REAL, resumed_at REAL, evicted_at REAL)"""


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


class Patch(object):
    def __init__(self, test, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        test.addCleanup(setattr, obj, name, old)


class HangingSession(object):
    session_id = "G1"

    async def async_get_variable(self, name):
        await asyncio.sleep(3600)

    async def async_send_text(self, text):
        await asyncio.sleep(3600)


# ------------------------------------------------------------------ 1. bounded iTerm2 awaits

class Item1Timeouts(TempEnv):
    def test_run_loops_does_not_wait_forever_on_a_task_that_ignores_cancel(self):
        Patch(self, D, "CANCEL_WAIT_S", 0.1)
        d = D.Daemon(self.cfg, self.conn, None, None, True)

        async def stubborn():
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    continue  # like an unsubscribe stuck on a dead dispatcher

        async def dies():
            await asyncio.sleep(0)
            raise D.ConnectionLost("gone")

        async def go():
            t0 = time.time()
            try:
                await d.run_loops([stubborn(), dies()])
            except D.ConnectionLost:
                pass
            return time.time() - t0
        loop = asyncio.new_event_loop()
        try:
            self.assertLess(loop.run_until_complete(go()), 2)
        finally:
            for t in asyncio.all_tasks(loop):
                t.cancel()
            loop.close()

    def test_itermtabs_call_that_never_answers_is_connection_lost(self):
        Patch(self, D, "ITERM_CALL_TIMEOUT_S", 0.05)
        app = type("A", (), {"get_session_by_id": lambda self, g: HangingSession()})()
        tabs = D.ItermTabs(app, None)
        with self.assertRaises(D.ConnectionLost):
            arun(tabs.job_name("G1"))
        with self.assertRaises(D.ConnectionLost):
            arun(tabs.send_text("G1", "x"))

    def test_on_focus_variable_read_is_bounded(self):
        Patch(self, D, "ITERM_CALL_TIMEOUT_S", 0.05)
        d = D.Daemon(self.cfg, self.conn, None, None, True)
        with self.assertRaises(D.ConnectionLost):
            arun(d.on_focus(HangingSession()))

    def test_prepare_tab_connection_loss_still_runs_on_evict_then_propagates(self):
        row = self.insert(session_id="s1", state="idle", last_event_at=NOW - 3600, iterm_guid="G1",
                          pid=111, pid_start=1000)
        hooks, dead = [], []
        self.cfg["on_evict"] = "/bin/true"

        class LostTabs(object):
            async def job_name(self, guid):
                raise D.ConnectionLost("timeout")
        clock = Clock()
        ev = D.Evictor(self.conn, self.cfg, LostTabs(), kill=lambda p, s: dead.append(1),
                       identity=lambda r: "gone" if dead else "alive", clock=clock, sleep=clock.sleep,
                       on_evict=lambda exe, env: hooks.append(env), children=lambda r: [],
                       child_probe=lambda p, s: "gone")
        with self.assertRaises(D.ConnectionLost):
            arun(ev.evict(row, 10, 1))
        self.assertEqual(len(hooks), 1)
        self.assertEqual(ledger.state_of(self.conn, "s1"), "hibernated")

    def test_evict_lock_released_after_connection_loss(self):
        d = D.Daemon(self.cfg, self.conn, None, None, False)

        async def boom(*a):
            raise D.ConnectionLost("x")
        d._pick_and_evict = boom

        async def go():
            with self.assertRaises(D.ConnectionLost):
                await d.pick_and_evict(10)
            return d.evict_lock.locked()
        self.assertFalse(arun(go()))


# ------------------------------------------------------------------ 2. final re-read before SIGTERM

class Item2FinalCheck(TempEnv):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.row = self.insert(session_id="s1", state="idle", last_event_at=NOW - 3600, iterm_guid="G1",
                               pid=111, pid_start=1000)
        self.kills = []

    def evictor(self, identity, children=lambda r: []):
        return D.Evictor(self.conn, self.cfg, None, kill=lambda p, s: self.kills.append(s),
                         identity=identity, clock=self.clock, sleep=self.clock.sleep,
                         children=children, child_probe=lambda p, s: "gone")

    def test_hook_write_during_awaited_checks_means_no_signal(self):
        def identity(r):
            # the last awaited check passes, but a Stop hook lands while it runs
            ledger.apply_event(self.conn, "Stop", {"session_id": "s1"}, None, now=NOW)
            return "alive"
        out = arun(self.evictor(identity).evict(self.row, 10, 1, recheck=lambda r: None))
        self.assertEqual(out, "aborted-final")
        self.assertEqual(self.kills, [])
        r = ledger.get(self.conn, "s1")
        self.assertEqual((r["state"], r["evicted_at"], r["held_state"]), ("idle", None, None))

    def test_registry_switch_during_child_capture_means_no_signal(self):
        sd = self.cfg.path("claude_sessions")
        os.makedirs(sd)

        def children(r):
            with open(os.path.join(sd, "111.json"), "w") as fh:
                json.dump({"sessionId": "other"}, fh)
            return []
        out = arun(self.evictor(lambda r: "alive", children).evict(self.row, 10, 1))
        self.assertEqual(out, "aborted-final")
        self.assertEqual(self.kills, [])

    def test_superseded_during_checks_means_no_signal(self):
        def identity(r):
            ledger.apply_event(self.conn, "SessionStart", {"session_id": "s2", "source": "clear"},
                               {"pid": 111, "pid_start": 1000, "tty": "ttys001", "interactive": True})
            return "alive"
        out = arun(self.evictor(identity).evict(self.row, 10, 1))
        self.assertEqual(out, "aborted-final")
        self.assertEqual(self.kills, [])
        self.assertEqual(ledger.state_of(self.conn, "s1"), "superseded")


# ------------------------------------------------------------------ 3. schema v2 migration

class Item3Migration(TempEnv):
    def test_v1_db_upgrades_in_place(self):
        path = os.path.join(self.tmp, "v1.db")
        c = sqlite3.connect(path)
        c.executescript(V1_SESSIONS + ";" + """
            CREATE INDEX sessions_pid ON sessions(pid, pid_start);
            CREATE TABLE events(id INTEGER PRIMARY KEY, ts REAL, session_id TEXT, event TEXT, detail TEXT);
            CREATE TABLE evictions(id INTEGER PRIMARY KEY, session_id TEXT, ts REAL, pid INTEGER,
              free_pct REAL, rss_mb REAL, signal TEXT, outcome TEXT, detail TEXT);
            CREATE TABLE requests(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, session_id TEXT,
              done_at REAL, result TEXT);
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO sessions(session_id, pid, pid_start, state, cwd, source)
              VALUES ('text', 5, 'Mon Sep 28 12:00:16 2026', 'idle', '/w', 'hook'),
                     ('num', 6, '1790596816', 'hibernated', '/x', 'hook'),
                     ('none', 7, NULL, 'ended', '/y', 'legacy');
            INSERT INTO meta VALUES ('heartbeat', '1');
            PRAGMA user_version=1;""")
        c.close()
        conn = ledger.connect(path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
        cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(sessions)")}
        self.assertEqual(cols["pid_start"], "INTEGER")
        self.assertIn("held_state", cols)
        self.assertIn("restored_at", cols)
        ecols = {r[1] for r in conn.execute("PRAGMA table_info(evictions)")}
        self.assertTrue({"hook_at", "hook_runs"} <= ecols, ecols)
        got = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT session_id, pid_start, typeof(pid_start) FROM sessions")}
        self.assertEqual(got, {"text": (None, "null"), "num": (1790596816, "integer"), "none": (None, "null")})
        self.assertEqual(ledger.get(conn, "num")["state"], "hibernated")
        self.assertEqual(ledger.get_meta(conn, "heartbeat"), "1")
        self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE name='sessions_pid'").fetchone()[0],
                         "sessions_pid")
        # an unidentifiable row is never alive, and never an eviction candidate
        self.assertEqual(procs.identity_probe(5, None), procs.UNKNOWN)
        conn.close()
        # idempotent reopen, and readers accept it
        ledger.connect(path).close()
        ledger.connect(path, readonly=True).close()

    def test_newer_schema_is_refused(self):
        path = os.path.join(self.tmp, "v9.db")
        c = sqlite3.connect(path)
        c.execute("PRAGMA user_version=9")
        c.close()
        with self.assertRaises(ledger.LedgerError):
            ledger.connect(path)


# ------------------------------------------------------------------ 4. restored_at + GUID capture

class FakeRun(object):
    def __init__(self, stdout):
        self.stdout = stdout
        self.calls = 0

    def run(self, args, **kw):
        self.calls += 1
        return type("R", (), {"stdout": self.stdout, "returncode": 0})()


class Item4Restored(TempEnv):
    def test_cleared_by_eviction_and_session_start(self):
        r = self.insert(session_id="a", state="idle", restored_at=5.0)
        ledger.begin_eviction(self.conn, r, 10, 1)
        self.assertIsNone(ledger.get(self.conn, "a")["restored_at"])
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET restored_at=5 WHERE session_id='a'")
        ledger.apply_event(self.conn, "SessionStart", {"session_id": "a", "source": "resume"}, None)
        self.assertIsNone(ledger.get(self.conn, "a")["restored_at"])

    def test_valid_guids_requires_one_distinct_id_per_tab(self):
        good = "w0t1p0:0A1B2C3D-0000-4000-8000-000000000001"
        self.assertEqual(restore.valid_guids(good + "\n", 1), [good])
        self.assertIsNone(restore.valid_guids("", 1))
        self.assertIsNone(restore.valid_guids(good + "\n" + good + "\n", 2))
        self.assertIsNone(restore.valid_guids("missing value\n", 1))
        self.assertIsNone(restore.valid_guids(good + "\n", 2))

    def run_restore(self, stdout):
        fake = FakeRun(stdout)
        Patch(self, restore, "subprocess", fake)
        Patch(self, restore, "iterm_guids", lambda: {"some-other-live-tab"})
        Patch(self, restore.procs, "boot_time", lambda: BOOT)
        out = []
        restore.run(self.cfg, self.conn, auto=True, out=out.append)
        return fake, out

    def test_bad_guid_output_leaves_rows_and_marker_retryable(self):
        self.insert(session_id="h", state="hibernated", iterm_guid="G-old-000001", pid=None,
                    last_event_at=BOOT - 100)
        fake, out = self.run_restore("")
        self.assertEqual(fake.calls, 1)
        self.assertIsNone(ledger.get(self.conn, "h")["restored_at"])
        self.assertFalse(os.path.exists(self.cfg.path("bootmark")))

    def test_good_guid_output_marks_row_and_boot(self):
        self.insert(session_id="h", state="hibernated", iterm_guid="G-old-000001", pid=None,
                    last_event_at=BOOT - 100)
        fake, out = self.run_restore("w0t0p0:NEW-GUID-0001\n")
        r = ledger.get(self.conn, "h")
        self.assertEqual(r["iterm_guid"], "w0t0p0:NEW-GUID-0001")
        self.assertIsNotNone(r["restored_at"])
        self.assertTrue(os.path.exists(self.cfg.path("bootmark")))


# ------------------------------------------------------------------ 5. reconcile scope + evicted_at

class Item5Reconcile(TempEnv):
    def test_reconcile_leaves_resuming_rows_to_expire_resuming(self):
        self.insert(session_id="r", state="resuming", state_since=0)
        self.assertEqual(D.reconcile(self.conn, {"r": "gone"}, now=100.0), [])
        self.assertEqual(ledger.state_of(self.conn, "r"), "resuming")

    def test_live_reconcile_clears_evicted_at_so_a_later_exit_is_an_exit(self):
        now = time.time()
        self.insert(session_id="e", state="evicting", state_since=now - 200, evicted_at=now - 200)
        D.reconcile(self.conn, {"e": "alive"}, now=now - 30)
        r = ledger.get(self.conn, "e")
        self.assertEqual((r["state"], r["evicted_at"]), ("idle", None))
        r = ledger.apply_event(self.conn, "SessionEnd", {"session_id": "e", "reason": "other"}, None, now=now)
        self.assertEqual(r["state"], "ended")

    def test_mark_hibernated_clears_held_state(self):
        self.insert(session_id="h", state="evicting", held_state="busy")
        ledger.mark_hibernated(self.conn, "h")
        r = ledger.get(self.conn, "h")
        self.assertEqual((r["state"], r["held_state"]), ("hibernated", None))


# ------------------------------------------------------------------ 6. README

class Item6Readme(TempEnv):
    def test_readme_documents_conservative_visibility(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")) as fh:
            text = fh.read()
        self.assertIn("minimized", text)
        self.assertIn("other Spaces", text)
