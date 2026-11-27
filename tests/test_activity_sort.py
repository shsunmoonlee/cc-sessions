"""dotclaude#159 + #144: the tty cache (no per-snapshot re-read of every pane), the
activity-driven tab sort (signature, min interval, current tab held, quiet period, model
lag), the fresh tty read before a signal, and ghost non-interactive rows ending. Fakes
only: no iTerm2, no real ledger."""
import asyncio
import os
import signal
import time
from unittest import mock

from helpers import TempEnv, assistant, load_daemon, user

from ccsessions import activity, ledger, procs, restore, tabgroups, tabsort

from test_idle_hibernate import Clock, IdleBase

D = load_daemon()


def arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Session(object):
    def __init__(self, guid, tty, fail=False, slow=0.0):
        self.session_id, self.tty, self.fail, self.slow = guid, tty, fail, slow
        self.reads = 0

    async def async_get_variable(self, name):
        if name != "tty":
            return None
        self.reads += 1
        if self.slow:
            await asyncio.sleep(self.slow)
        if self.fail:
            raise D._rpc_exception()("SESSION_NOT_FOUND")
        return "/dev/" + self.tty


class Tab(object):
    def __init__(self, tab_id, sessions):
        self.tab_id, self.sessions = tab_id, sessions


class Window(object):
    """Records async_set_tabs; lag=True keeps the old tab list (the model has not caught up)."""

    def __init__(self, window_id, tabs, current=None, lag=False):
        self.window_id, self.tabs, self.lag = window_id, list(tabs), lag
        self.current_tab = tabs[current] if current is not None else None
        self.set_tabs_calls = []

    async def async_set_tabs(self, tabs):
        self.set_tabs_calls.append([t.tab_id for t in tabs])
        if not self.lag:
            self.tabs = list(tabs)


class App(object):
    def __init__(self, windows):
        self.terminal_windows = windows

    def get_session_by_id(self, guid):
        for w in self.terminal_windows:
            for t in w.tabs:
                for s in t.sessions:
                    if s.session_id == guid:
                        return s
        return None


def tab(n, fail=False):
    return Tab("T%d" % n, [Session("G%d" % n, "ttys%03d" % n, fail=fail)])


def ids(w):
    return [t.tab_id for t in w.tabs]


class FakeFacts(object):
    kids = {}

    def __init__(self, cfg, now):
        pass

    def analysis(self, row):
        return {}

    def children(self, row):
        return FakeFacts.kids.get(row["session_id"], [])


# ------------------------------------------------------------------ tty cache

class TtyCache(TempEnv):
    def daemon(self, app):
        return D.Daemon(self.cfg, self.conn, app, None, False)

    def reads(self, app):
        return [s.reads for w in app.terminal_windows for t in w.tabs for s in t.sessions]

    def test_known_guids_are_not_read_again(self):
        app = App([Window("W1", [tab(1), tab(2), tab(3)])])
        d = self.daemon(app)
        f1 = arun(d.snapshot())
        f2 = arun(d.snapshot())
        self.assertEqual(self.reads(app), [1, 1, 1])
        self.assertEqual((f1["reads"], f2["reads"]), (3, 0))
        self.assertEqual(f2["tty_guid"], {"ttys001": "G1", "ttys002": "G2", "ttys003": "G3"})

    def test_a_new_tab_is_read_once_and_a_closed_one_dropped(self):
        w = Window("W1", [tab(1), tab(2)])
        app = App([w])
        d = self.daemon(app)
        arun(d.snapshot())
        t3 = tab(3)
        w.tabs = [w.tabs[0], t3]
        f = arun(d.snapshot())
        arun(d.snapshot())
        self.assertEqual(t3.sessions[0].reads, 1)
        self.assertEqual(f["reads"], 1)
        self.assertNotIn("G2", d.tty_cache)
        self.assertEqual(set(d.tty_cache), {"G1", "G3"})

    def test_a_failed_read_is_skipped_never_cached_and_retried(self):
        bad = tab(2, fail=True)
        app = App([Window("W1", [tab(1), bad])])
        d = self.daemon(app)
        f1 = arun(d.snapshot())
        f2 = arun(d.snapshot())
        self.assertEqual((f1["skipped"], f2["skipped"]), (1, 1))
        self.assertEqual(bad.sessions[0].reads, 2)
        self.assertNotIn("G2", d.tty_cache)
        bad.sessions[0].fail = False
        f3 = arun(d.snapshot())
        self.assertEqual(f3["skipped"], 0)
        self.assertEqual(d.tty_cache["G2"], "ttys002")

    def test_fresh_rereads_every_pane(self):
        app = App([Window("W1", [tab(1), tab(2)])])
        d = self.daemon(app)
        arun(d.snapshot())
        f = arun(d.snapshot(fresh=True))
        self.assertEqual(self.reads(app), [2, 2])
        self.assertEqual(f["reads"], 2)

    def test_the_periodic_full_walk_heals_a_changed_tty(self):
        self.cfg["tty_refresh_s"] = 600
        t1 = tab(1)
        app = App([Window("W1", [t1, tab(2)])])
        d = self.daemon(app)
        arun(d.snapshot())
        t1.sessions[0].tty = "ttys009"
        self.assertEqual(arun(d.snapshot())["tty_guid"].get("ttys001"), "G1")  # cached
        d.tty_full_at -= 601
        f = arun(d.snapshot())
        self.assertEqual(f["tty_guid"].get("ttys009"), "G1")
        self.assertNotIn("ttys001", f["tty_guid"])

    def test_two_guids_on_one_cached_tty_are_both_reread(self):
        a, b = tab(1), tab(2)
        app = App([Window("W1", [a, b])])
        d = self.daemon(app)
        arun(d.snapshot())
        d.tty_cache["G2"] = "ttys001"  # stale: both claim ttys001
        f = arun(d.snapshot())
        self.assertEqual(f["reads"], 2)
        self.assertEqual(d.tty_cache, {"G1": "ttys001", "G2": "ttys002"})

    def test_blindness_still_fires_on_cached_walks(self):
        app = App([Window("W1", [tab(1, fail=True), tab(2, fail=True)])])
        d = self.daemon(app)
        arun(d.snapshot())
        arun(d.snapshot())
        self.assertEqual(ledger.get_meta(self.conn, "skipped_sessions"), "2")
        self.assertTrue(float(ledger.get_meta(self.conn, "blind_since")) > 0)

    def test_concurrent_first_snapshots_walk_once(self):
        tabs = [Tab("T%d" % n, [Session("G%d" % n, "ttys%03d" % n, slow=0.02)]) for n in (1, 2, 3)]
        app = App([Window("W1", tabs)])
        d = self.daemon(app)

        async def three():
            return await asyncio.gather(d.snapshot(), d.snapshot(fresh=True), d.snapshot())
        out = arun(three())
        self.assertEqual(self.reads(app), [1, 1, 1])
        self.assertEqual(sorted(f["reads"] for f in out), [0, 0, 3])
        for f in out:
            self.assertEqual((len(f["tty_guid"]), f["skipped"]), (3, 0))
        self.assertIsNone(d.walking)

    def test_a_failed_full_walk_leaves_no_waiter_stuck(self):
        app = App([Window("W1", [tab(1)])])
        d = self.daemon(app)

        async def boom(*a):
            raise RuntimeError("walk")
        with mock.patch.object(D, "focus_snapshot", boom):
            with self.assertRaises(RuntimeError):
                arun(d.snapshot())
        self.assertIsNone(d.walking)
        self.assertEqual(arun(d.snapshot())["reads"], 1)  # still owes a full walk

    def test_a_cached_walk_is_not_torn_by_a_walk_that_ends_during_it(self):
        t1, t2 = tab(1), tab(2)
        t3 = Tab("T3", [Session("G3", "ttys003", slow=0.05)])
        w = Window("W1", [t1, t2])
        d = self.daemon(App([w]))
        arun(d.snapshot())

        async def race():
            w.tabs = [t1, t2, t3]
            slow = asyncio.ensure_future(d.snapshot())  # reads T3 slowly, T1 and T2 cached
            await asyncio.sleep(0.01)
            w.tabs = [t1, t3]  # T2 closes; this walk drops it from the cache meanwhile
            t3.sessions[0].slow = 0
            await d.snapshot()
            return await slow
        f = arun(race())
        self.assertEqual(f["tty_guid"].get("ttys002"), "G2")
        self.assertEqual(f["skipped"], 0)

    def test_the_module_function_without_a_cache_reads_everything(self):
        app = App([Window("W1", [tab(1), tab(2)])])
        arun(D.focus_snapshot(app))
        arun(D.focus_snapshot(app))
        self.assertEqual(self.reads(app), [2, 2])


# ------------------------------------------------------------------ recheck

class RecheckReadsTheTabTty(IdleBase):
    class Tabs(object):
        def __init__(self, tty):
            self.now_tty, self.tty_calls = tty, 0

        async def job_name(self, guid):
            return "zsh"

        async def tty(self, guid):
            self.tty_calls += 1
            return "/dev/" + self.now_tty

        async def is_visible(self, guid):
            return False

        async def inject(self, guid, data):
            pass

        async def set_name(self, guid, name):
            pass

        async def send_text(self, guid, text):
            pass

    def run_pick(self, tty_now, hang=False, raises=None):
        self.add("a", "ttys001", idle_min=4, pid=4242)
        d = self.daemon(["ttys001"])
        snaps = []
        real = d.snapshot

        async def snap():
            snaps.append(1)
            return await real()
        d.snapshot = snap
        clock, kills, alive = Clock(), [], [True]

        def kill(pid, sig):
            kills.append((pid, sig))
            alive[0] = False
        tabs = self.Tabs(tty_now)
        if hang or raises:
            async def stuck(guid):
                tabs.tty_calls += 1
                if raises:
                    raise raises
                await asyncio.sleep(3600)
            tabs.tty = stuck
        d.evictor = D.Evictor(self.conn, self.cfg, tabs, kill=kill, identity=lambda r: alive[0],
                              clock=clock, sleep=clock.sleep, children=lambda r: [])
        out = arun(asyncio.wait_for(d.idle_pass(27), 5))
        return out, kills, tabs, snaps

    def test_a_tab_whose_tty_changed_aborts_before_the_signal(self):
        out, kills, tabs, snaps = self.run_pick("ttys009")
        self.assertNotIn("term", out)
        self.assertEqual(kills, [])
        self.assertEqual(tabs.tty_calls, 1)
        # the pick, its re-check, and the next pick that finds nothing: no extra walk for the tty
        self.assertEqual(len(snaps), 3)
        self.assertEqual(ledger.state_of(self.conn, "a"), "idle")

    def test_a_hanging_tty_read_aborts_the_eviction_and_raises_nothing(self):
        with mock.patch.object(D, "ITERM_CALL_TIMEOUT_S", 0.05):
            out, kills, tabs, _ = self.run_pick("ttys001", hang=True)
        self.assertNotIn("term", out)
        self.assertEqual(kills, [])
        self.assertEqual(tabs.tty_calls, 1)
        self.assertEqual(ledger.state_of(self.conn, "a"), "idle")

    def test_a_lost_connection_at_the_tty_read_aborts_and_is_not_raised(self):
        out, kills, tabs, _ = self.run_pick("ttys001", raises=D.ConnectionLost("tty"))
        self.assertNotIn("term", out)
        self.assertEqual((kills, tabs.tty_calls), ([], 1))
        self.assertEqual(ledger.state_of(self.conn, "a"), "idle")

    def test_a_tab_that_became_visible_aborts(self):
        self.add("a", "ttys001", idle_min=4, pid=4242)
        d = self.daemon(["ttys001"])
        tabs = self.Tabs("ttys001")

        async def shown(guid):
            return True
        tabs.is_visible = shown
        kills = []
        d.evictor = D.Evictor(self.conn, self.cfg, tabs, kill=lambda p, s: kills.append(p),
                              identity=lambda r: True, children=lambda r: [])
        out = arun(d.idle_pass(27))
        self.assertNotIn("term", out)
        self.assertEqual(kills, [])

    def test_the_same_tty_goes_ahead(self):
        out, kills, tabs, _ = self.run_pick("ttys001")
        self.assertEqual(out, ["term"])
        self.assertEqual(kills, [(4242, signal.SIGTERM)])


# ------------------------------------------------------------------ the sort

class SortBase(TempEnv):
    # no wall-clock recheck slot: a poll crossing a 600 s boundary would change the signature;
    # quiet and min interval of 10 s so the +5/+20/+31 steps below stay meaningful
    extra_config = {"tab_sort": "recent", "tab_sort_bucket_s": 0, "tab_sort_recheck_s": 0,
                    "tab_sort_quiet_s": 10, "tab_sort_min_interval_s": 10}

    def setUp(self):
        super().setUp()
        p = mock.patch.object(D, "Facts", FakeFacts)
        p.start()
        self.addCleanup(p.stop)
        FakeFacts.kids = {}
        self.t = time.time()

    def rows(self, n=3, newest_first=False):
        base = self.t - 5000
        for i in range(1, n + 1):
            self.insert(session_id="s%d" % i, tty="ttys%03d" % i, pid=100 + i, last_event_at=base)
            # s1 oldest ... sN newest, so the policy order is reversed
            self.event("s%d" % i, base + i * 100)

    def event(self, sid, ts, name="Stop"):
        with ledger.tx(self.conn):
            ledger.add_event(self.conn, sid, name, {}, ts)

    def daemon(self, windows, dry_run=False):
        d = D.Daemon(self.cfg, self.conn, App(windows), None, dry_run)
        p = mock.patch.object(D.tab_snapshots, "iterm_instance", lambda: None)
        p.start()
        self.addCleanup(p.stop)
        self.passes = []
        real = d._tab_orders

        def counted(*a):
            self.passes.append(1)
            if self.during_pass:
                self.during_pass()
            return real(*a)
        self.during_pass = None
        d._tab_orders = counted
        return d

    def poll(self, d, dt=0.0):
        return arun(d.sort_poll(self.t + dt))


class Signature(SortBase):
    def test_nothing_changed_means_no_pass(self):
        self.rows()
        w = Window("W1", [tab(1), tab(2), tab(3)])
        d = self.daemon([w])
        self.assertEqual(self.poll(d), "sorted")
        self.assertEqual(ids(w), ["T3", "T2", "T1"])
        for k in range(1, 6):
            self.assertEqual(self.poll(d, 20 * k), "unchanged")
        self.assertEqual(len(self.passes), 1)
        self.assertEqual(len(w.set_tabs_calls), 1)

    def test_a_new_event_brings_one_pass(self):
        self.rows()
        w = Window("W1", [tab(1), tab(2), tab(3)])
        d = self.daemon([w])
        self.poll(d)
        self.event("s1", self.t)
        self.assertEqual(self.poll(d, 20), "sorted")
        self.assertEqual(ids(w), ["T1", "T3", "T2"])
        self.assertEqual(self.poll(d, 40), "unchanged")
        self.assertEqual(len(self.passes), 2)

    def test_a_state_change_with_no_event_brings_a_pass(self):
        self.rows()
        d = self.daemon([Window("W1", [tab(1), tab(2), tab(3)])])
        self.poll(d)
        ledger.mark_hibernated(self.conn, "s1", only_from=("idle",))
        self.assertEqual(self.poll(d, 20), "sorted")
        self.assertEqual(len(self.passes), 2)

    def test_a_groups_file_change_brings_a_pass(self):
        self.rows()
        d = self.daemon([Window("W1", [tab(1), tab(2), tab(3)])])
        self.poll(d)
        tabgroups.add(self.cfg, "g", "s1")
        self.assertEqual(self.poll(d, 20), "sorted")
        self.assertEqual(len(self.passes), 2)

    def test_the_recheck_slot_brings_a_pass(self):
        self.cfg["tab_sort_recheck_s"] = 600
        self.rows()
        d = self.daemon([Window("W1", [tab(1), tab(2), tab(3)])])
        self.t = (int(self.t // 600) + 1) * 600 - 30  # 30 s before a slot boundary
        self.poll(d)
        self.assertEqual(self.poll(d, 15), "unchanged")
        self.assertEqual(self.poll(d, 45), "sorted")
        self.assertEqual(len(self.passes), 2)

    def test_a_tab_opened_or_closed_brings_a_pass(self):
        self.cfg["tab_sort_quiet_s"] = 0
        self.rows()
        w = Window("W1", [tab(1), tab(2), tab(3)])
        d = self.daemon([w])
        self.poll(d)
        w.tabs.append(tab(4))
        self.assertEqual(self.poll(d, 20), "sorted")
        w.tabs.pop()
        self.assertEqual(self.poll(d, 40), "sorted")
        self.assertEqual(len(self.passes), 3)

    def test_min_interval(self):
        self.rows()
        d = self.daemon([Window("W1", [tab(1), tab(2), tab(3)])])
        self.poll(d)
        self.event("s1", self.t)
        self.assertEqual(self.poll(d, 5), "too soon")
        self.assertEqual(self.poll(d, 11), "sorted")
        self.assertEqual(len(self.passes), 2)

    def test_an_event_during_the_pass_brings_another(self):
        self.rows()
        d = self.daemon([Window("W1", [tab(1), tab(2), tab(3)])])
        real, fired = d.snapshot, []

        async def snap(fresh=False):
            # after the signature was taken, before the pass reads its inputs
            if not fired:
                fired.append(1)
                self.event("s2", self.t + 1)
            return await real(fresh)
        d.snapshot = snap
        self.poll(d)
        self.assertEqual(self.poll(d, 20), "sorted")
        self.assertEqual(self.poll(d, 40), "unchanged")
        self.assertEqual(len(self.passes), 2)

    def test_an_incomplete_pass_is_retried(self):
        self.rows()
        w = Window("W1", [tab(1), tab(2), tab(3)])
        d = self.daemon([w])
        with mock.patch.object(D.tab_snapshots, "restore_running", lambda cfg: True):
            self.assertEqual(self.poll(d), "incomplete")
        self.assertEqual(w.set_tabs_calls, [])
        self.assertEqual(self.poll(d, 20), "sorted")
        self.assertEqual(ids(w), ["T3", "T2", "T1"])

    def test_skipped_panes_leave_the_pass_incomplete(self):
        self.rows()
        w = Window("W1", [tab(1), tab(2, fail=True), tab(3)])
        d = self.daemon([w])
        self.assertEqual(self.poll(d), "incomplete")
        self.assertEqual(w.set_tabs_calls, [])

    def test_bucket_zero_and_a_real_bucket_both_work(self):
        self.rows()
        w = Window("W1", [tab(1), tab(2), tab(3)])
        self.poll(self.daemon([w]))
        self.assertEqual(ids(w), ["T3", "T2", "T1"])
        self.cfg["tab_sort_bucket_s"] = 10 ** 9  # one bucket: the order stands
        w2 = Window("W2", [tab(1), tab(2), tab(3)])
        self.poll(self.daemon([w2]))
        self.assertEqual(w2.set_tabs_calls, [])

    def test_tab_sort_off_does_nothing(self):
        self.cfg["tab_sort"] = "off"
        self.rows()
        w = Window("W1", [tab(1), tab(2), tab(3)])
        self.assertEqual(self.poll(self.daemon([w])), "off")
        self.assertEqual((self.passes, w.set_tabs_calls), ([], []))

    def test_the_tick_no_longer_sorts(self):
        self.rows()
        w = Window("W1", [tab(1), tab(2), tab(3)])
        d = self.daemon([w])
        d.last_prune = d.last_reap = time.time()
        with mock.patch.object(procs, "free_pct", lambda: None):
            arun(d.tick())
        self.assertEqual((self.passes, w.set_tabs_calls), ([], []))

    def test_a_force_request_takes_a_fresh_snapshot(self):
        self.rows()
        d = self.daemon([Window("W1", [tab(1), tab(2), tab(3)])])
        real, asked = d.snapshot, []

        async def snap(fresh=False):
            asked.append(fresh)
            return await real(fresh)
        d.snapshot = snap
        ledger.add_request(self.conn, "tab-sort", None, time.time() + 1)
        arun(d.handle_requests())
        self.assertEqual(asked, [True])

    def test_a_second_force_sort_after_you_rearranged_moves_again(self):
        # tab_sort off: no poll ever observes the window, so only the pass itself can tell
        self.cfg["tab_sort"] = "off"
        self.rows()
        for arrange in ([1, 2, 0], [0, 1, 2]):  # some order of yours; exactly the one before
            w = Window("W1", [tab(1), tab(2), tab(3)], lag=True)
            d = self.daemon([w])
            self.assertEqual(arun(d.sort_tabs(arun(d.snapshot()), force=True)), "moved 2 tab(s)")
            self.assertEqual(w.set_tabs_calls, [["T3", "T2", "T1"]])
            by_id = {t.tab_id: t for t in w.tabs}
            w.tabs = [by_id["T%d" % (i + 1)] for i in arrange]  # the model, after you moved tabs
            arun(d.sort_tabs(arun(d.snapshot()), force=True))
            self.assertEqual(len(w.set_tabs_calls), 2, arrange)
            self.assertEqual(w.set_tabs_calls[1], ["T3", "T2", "T1"])

    def test_a_force_request_sorts_at_once_and_everything(self):
        self.cfg["tab_sort"] = "off"
        self.rows()
        w = Window("W1", [tab(1), tab(2), tab(3)], current=0)
        d = self.daemon([w])
        d.quiet_until["W1"] = time.time() + 100
        ledger.add_request(self.conn, "tab-sort", None, time.time() + 1)
        arun(d.handle_requests())
        self.assertEqual(ids(w), ["T3", "T2", "T1"])  # the current tab moved too

    def test_a_parked_row_is_never_working(self):
        self.rows()
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET state='evicting', iterm_guid='G1' WHERE session_id='s1'")
        FakeFacts.kids = {"s1": ["node"]}
        w = Window("W1", [tab(1), tab(2), tab(3)])
        self.poll(self.daemon([w]))
        self.assertEqual(ids(w)[0], "T3")
        # the same row idle with that child is Working and leads
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET state='idle' WHERE session_id='s1'")
        w2 = Window("W2", [tab(1), tab(2), tab(3)])
        self.poll(self.daemon([w2]))
        self.assertEqual(ids(w2)[0], "T1")


class SortLoop(SortBase):
    def test_a_sort_bug_is_logged_and_the_loop_goes_on(self):
        d = self.daemon([])
        calls = []

        async def poll(trigger=None):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("bug")
            raise D.ConnectionLost("gone")
        d.sort_poll = poll
        with mock.patch.object(D, "REQUEST_POLL_S", 0), \
                self.assertLogs("cc-sessions", "ERROR") as logs:
            with self.assertRaises(D.ConnectionLost):
                arun(d.sort_loop())
        self.assertEqual(len(calls), 3)
        self.assertEqual(len([m for m in logs.output if "tab sort failed" in m]), 2)

    def test_run_loops_starts_the_sort_loop(self):
        with ledger.tx(self.conn):
            ledger.set_meta(self.conn, "probe", 1)  # the -wal file exists from the first write
        d = self.daemon([])
        started, armed = [], []

        async def fake(name):
            started.append(name)
            armed.append(d.watch is not None and d.watch.fd is not None)
            await asyncio.sleep(10)

        async def dies():
            raise D.ConnectionLost("x")
        d.focus_loop = lambda: fake("focus")
        d.tick_loop = lambda: fake("tick")
        d.request_loop = lambda: dies()
        d.sort_loop = lambda: fake("sort")
        with self.assertRaises(D.ConnectionLost):
            arun(d.run_loops())
        self.assertIn("sort", started)
        self.assertTrue(armed and all(armed), armed)  # the ledger watch runs with the loops
        self.assertIsNone(d.watch.kq)  # and is stopped with them


class CurrentTab(SortBase):
    def test_hold_current_keeps_its_index(self):
        for order, cur, want in (([2, 1, 0], 0, [0, 2, 1]), ([2, 1, 0], 1, [2, 1, 0]),
                                 ([2, 1, 0], 2, [1, 0, 2]), ([3, 0, 1, 2], 1, [3, 1, 0, 2]),
                                 ([1, 0], None, [1, 0])):
            got = tabsort.hold_current(order, cur)
            self.assertEqual(got, want, (order, cur))
            if cur is not None:
                self.assertEqual(got.index(cur), cur)

    def test_the_current_tab_never_moves(self):
        self.rows()
        for cur in (0, 1, 2):
            w = Window("W%d" % cur, [tab(1), tab(2), tab(3)], current=cur)
            self.poll(self.daemon([w]))
            self.assertEqual(ids(w).index("T%d" % (cur + 1)), cur, ids(w))

    def test_no_call_when_only_the_current_tab_would_move(self):
        self.rows(n=2)
        w = Window("W1", [tab(1), tab(2)], current=0)
        self.assertEqual(self.poll(self.daemon([w])), "sorted")
        self.assertEqual(w.set_tabs_calls, [])

    def test_a_tab_switch_starts_the_quiet_period(self):
        self.rows()
        w = Window("W1", [tab(1), tab(2), tab(3)], current=1)
        d = self.daemon([w])
        self.poll(d)
        self.assertEqual(ids(w), ["T3", "T2", "T1"])
        self.event("s2", self.t)  # T2 is now newest, but it is the current tab
        w.current_tab = w.tabs[2]  # you click T1
        self.assertEqual(self.poll(d, 20), "quiet")
        self.assertEqual(self.poll(d, 25), "quiet")
        self.assertEqual(self.poll(d, 31), "sorted")
        self.assertEqual(ids(w), ["T2", "T3", "T1"])

    def test_a_user_drag_starts_the_quiet_period(self):
        self.rows()
        w = Window("W1", [tab(1), tab(2), tab(3)])
        d = self.daemon([w])
        self.poll(d)
        w.tabs = [w.tabs[2], w.tabs[0], w.tabs[1]]  # you drag T1 to the front
        self.assertEqual(self.poll(d, 20), "quiet")
        self.assertEqual(self.poll(d, 31), "sorted")
        self.assertEqual(ids(w), ["T3", "T2", "T1"])

    def test_a_switch_during_the_pass_aborts_that_window(self):
        self.rows()
        w = Window("W1", [tab(1), tab(2), tab(3)], current=0)
        d = self.daemon([w])
        self.during_pass = lambda: setattr(w, "current_tab", w.tabs[1])
        self.assertEqual(self.poll(d), "incomplete")
        self.assertEqual(w.set_tabs_calls, [])

    def test_a_lagging_model_gets_no_second_move(self):
        self.rows()
        w = Window("W1", [tab(1), tab(2), tab(3)], lag=True)
        d = self.daemon([w])
        self.assertEqual(self.poll(d), "sorted")
        self.assertEqual(self.poll(d, 20), "unchanged")
        self.event("s3", self.t + 1)  # new input while the model still shows the old order
        self.assertEqual(self.poll(d, 40), "sorted")
        self.assertEqual(len(w.set_tabs_calls), 1)
        w.tabs = [w.tabs[2], w.tabs[1], w.tabs[0]]  # the model catches up
        self.assertEqual(self.poll(d, 60), "unchanged")
        self.assertEqual(d.sort_expected, {})
        self.assertEqual(d.quiet_until.get("W1"), None)

    def test_dry_run_moves_nothing_and_says_so_once_per_change(self):
        self.rows()
        w = Window("W1", [tab(1), tab(2), tab(3)])
        d = self.daemon([w], dry_run=True)
        with self.assertLogs("cc-sessions", "INFO") as logs:
            self.assertEqual(self.poll(d), "sorted")
            self.assertEqual(self.poll(d, 20), "unchanged")
            self.assertEqual(self.poll(d, 40), "unchanged")
        self.assertEqual(w.set_tabs_calls, [])
        self.assertEqual(len([m for m in logs.output if "DRY RUN would move" in m]), 1)

    def test_you_restoring_the_old_order_is_not_model_lag(self):
        self.rows()
        w = Window("W1", [tab(1), tab(2), tab(3)])
        d = self.daemon([w])
        self.poll(d)
        w.tabs = [w.tabs[2], w.tabs[1], w.tabs[0]]  # back to T1, T2, T3: the order before
        self.assertEqual(self.poll(d, 20), "quiet")
        self.assertEqual(d.sort_expected, {})
        self.assertEqual(self.poll(d, 31), "sorted")
        self.assertEqual(ids(w), ["T3", "T2", "T1"])
        self.assertEqual(len(w.set_tabs_calls), 2)


# ------------------------------------------------------------------ ghost rows

class Ghosts(TempEnv):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.probes = {}
        p = mock.patch.object(D, "probe_rows", lambda rows: {r["session_id"]: self.probes.get(r["session_id"], procs.UNKNOWN)
                                                              for r in rows})
        p.start()
        self.addCleanup(p.stop)

    def ghost(self, sid, interactive=0, age=3600, state="busy", **kw):
        return self.insert(session_id=sid, interactive=interactive, state=state,
                           last_event_at=self.now - age, pid=4000, pid_start=1000, **kw)

    def reconcile(self, now=None):
        d = D.Daemon(self.cfg, self.conn, App([]), None, False)
        return d, arun(d.reconcile_ghosts(self.now if now is None else now))

    def test_a_gone_non_interactive_row_ends(self):
        self.ghost("g1")
        self.probes["g1"] = procs.GONE
        with self.assertLogs("cc-sessions", "INFO") as logs:
            _, ended = self.reconcile()
        self.assertEqual(ended, ["g1"])
        r = ledger.get(self.conn, "g1")
        self.assertEqual((r["state"], r["end_reason"]), ("ended", "gone"))
        self.assertIsNotNone(r["ended_at"])
        ev = self.conn.execute("SELECT event FROM events WHERE session_id='g1'").fetchall()
        self.assertEqual([e[0] for e in ev], ["ghost-ended"])
        self.assertTrue(any("reconcile g1: process gone, busy -> ended" in m for m in logs.output))

    def test_interactive_unknown_young_and_null_rows_are_left_alone(self):
        self.ghost("inter", interactive=1)
        self.ghost("unk")
        self.ghost("young", age=60)
        self.ghost("nullint", interactive=None)
        self.ghost("nopid")
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET pid_start=NULL WHERE session_id='nopid'")
        self.probes.update(inter=procs.GONE, unk=procs.UNKNOWN, young=procs.GONE, nullint=procs.GONE,
                           nopid=procs.GONE)
        _, ended = self.reconcile()
        self.assertEqual(ended, [])
        for sid in ("inter", "unk", "young", "nullint", "nopid"):
            self.assertIsNone(ledger.get(self.conn, sid)["ended_at"], sid)

    def test_candidates_are_old_non_interactive_rows_with_an_identity(self):
        self.ghost("g1")
        self.ghost("inter", interactive=1)
        self.ghost("nullint", interactive=None)
        self.ghost("young", age=60)
        self.ghost("hib", state="hibernated")
        got = [r["session_id"] for r in ledger.ghost_candidates(self.conn, self.now - 600)]
        self.assertEqual(got, ["g1"])

    def test_a_row_that_changed_loses_the_compare_and_set(self):
        r = self.ghost("g1")
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET last_event_at=? WHERE session_id='g1'", (self.now,))
        self.assertFalse(ledger.end_ghost(self.conn, r, self.now))
        r2 = ledger.get(self.conn, "g1")
        ledger.claim_resume(self.conn, "g1", 5000, 2000)  # new identity, same state
        self.assertFalse(ledger.end_ghost(self.conn, dict(r2, state=ledger.state_of(self.conn, "g1")), self.now))
        self.assertIsNone(ledger.get(self.conn, "g1")["ended_at"])

    def test_the_rate_limit(self):
        self.ghost("g1")
        self.probes["g1"] = procs.GONE
        d = D.Daemon(self.cfg, self.conn, App([]), None, False)
        d.last_ghosts = self.now - 10
        self.assertEqual(arun(d.reconcile_ghosts(self.now)), [])
        self.assertEqual(arun(d.reconcile_ghosts(self.now + 600)), ["g1"])

    def test_dry_run_ends_nothing(self):
        self.ghost("g1")
        self.probes["g1"] = procs.GONE
        d = D.Daemon(self.cfg, self.conn, App([]), None, True)
        self.assertEqual(arun(d.reconcile_ghosts(self.now)), [])
        self.assertIsNone(ledger.get(self.conn, "g1")["ended_at"])

    def test_restore_selection_is_unchanged(self):
        cwd = self.tmp
        self.ghost("g1", cwd=cwd)
        self.insert(session_id="i1", state="idle", cwd=cwd, last_event_at=self.now - 3600)
        self.probes["g1"] = procs.GONE

        def pick():
            rows = ledger.all_rows(self.conn)
            run, typed, _ = restore.select(rows, None, self.now, self.cfg, lambda r: False)
            snap = {"taken_at": self.now - 7200, "windows": [{"tabs": [
                {"sessions": [{"session_id": "g1"}]}, {"sessions": [{"session_id": "i1"}]}]}]}
            srun, styped, _, _ = restore.select_snapshot(snap, rows, self.cfg, lambda r: False, ())
            return ([r["session_id"] for r in run + typed], [r["session_id"] for r in srun + styped])
        before = pick()
        self.reconcile()
        self.assertEqual(ledger.state_of(self.conn, "g1"), "ended")
        self.assertEqual(pick(), before)

    def test_the_tick_runs_it(self):
        self.ghost("g1")
        self.probes["g1"] = procs.GONE
        d = D.Daemon(self.cfg, self.conn, App([]), None, False)
        d.last_prune = d.last_reap = time.time()
        with mock.patch.object(procs, "free_pct", lambda: None), \
                mock.patch.object(D.tab_snapshots, "iterm_instance", lambda: None):
            arun(d.tick())
        self.assertEqual(ledger.state_of(self.conn, "g1"), "ended")


# ------------------------------------------------------------------ last_work_time memo

class WorkTimeMemo(TempEnv):
    def test_a_changed_transcript_is_read_again(self):
        t0 = time.time() - 3600
        p = self.transcript("m1", [user("hi", t=t0), assistant("one", t=t0 + 10)])
        first = activity.last_work_time(p)
        with mock.patch.object(activity, "_read_last_work_time", side_effect=AssertionError("read")):
            self.assertEqual(activity.last_work_time(p), first)
        with open(p, "a") as fh:
            fh.write(__import__("json").dumps(assistant("two", t=t0 + 500)) + "\n")
        self.assertGreater(activity.last_work_time(p), first)

    def test_a_missing_file_is_none(self):
        self.assertIsNone(activity.last_work_time(os.path.join(self.tmp, "nope.jsonl")))


# ------------------------------------------------------------------ near a slot boundary

def _near_a_slot_boundary(cls):
    """`cls` again with self.t 10 s before a 600 s wall-clock boundary, so every poll at
    +20..+100 crosses it: green only if the sort tests do not depend on the recheck slot."""
    class Near(cls):
        def setUp(self):
            cls.setUp(self)
            self.t = (int(time.time() // 600) + 1) * 600 - 10
    Near.__name__ = Near.__qualname__ = cls.__name__ + "NearASlotBoundary"
    return Near


SignatureNearASlotBoundary = _near_a_slot_boundary(Signature)
CurrentTabNearASlotBoundary = _near_a_slot_boundary(CurrentTab)
