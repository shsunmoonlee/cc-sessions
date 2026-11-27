"""Event-driven wake: the sort loop on a fake clock (a waiting outcome re-arms at its exact
deadline, the min interval is per window, an incomplete pass backs off and resets), the
ledger's -wal kqueue watch on a real temp ledger (a write wakes it, a read does not, a
failing watch falls back to the safety poll without spinning), and the focus stamp taken
when the event arrives."""
import asyncio
import os
import sys
import time
import types
from unittest import mock

from helpers import TempEnv, load_daemon

from ccsessions import ledger

import test_activity_sort as tas

D = load_daemon()
arun = tas.arun

WRITER = ("import sqlite3,sys;c=sqlite3.connect(sys.argv[1],isolation_level=None);"
          "c.execute('PRAGMA busy_timeout=3000');"
          "c.execute('CREATE TABLE IF NOT EXISTS wake_probe(x)');"
          "c.execute('INSERT INTO wake_probe VALUES (1)')")
READER = ("import sqlite3,sys;c=sqlite3.connect('file:%s?mode=ro'%sys.argv[1],uri=True);"
          "c.execute('SELECT count(*) FROM sessions').fetchone()")


class FakeClock(object):
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class ClockedWindow(tas.Window):
    """Records the fake time of every set_tabs."""

    def __init__(self, clock, *a, **kw):
        tas.Window.__init__(self, *a, **kw)
        self.clock, self.set_at = clock, []

    async def async_set_tabs(self, tabs):
        self.set_at.append(self.clock.now)
        await tas.Window.async_set_tabs(self, tabs)


class LoopBase(tas.SortBase):
    extra_config = dict(tas.SortBase.extra_config, tab_sort_quiet_s=3, tab_sort_min_interval_s=2,
                        wake_safety_s=30)

    def setUp(self):
        super().setUp()
        self.clock = FakeClock(self.t)

    def window(self, wid, nums, current=None):
        return ClockedWindow(self.clock, wid, [tas.tab(n) for n in nums], current=current)

    def drive(self, d, until, script):
        """sort_loop on the fake clock: each wait jumps to the next scripted action (which
        wakes the loop) or to its own timeout, whichever comes first. -> [(seconds since
        start, trigger, word)] for every poll before `until`."""
        d.clock = self.clock
        script = sorted(script, key=lambda a: a[0])
        polls, real = [], d.sort_poll

        async def poll(now=None, trigger=None):
            if self.clock.now - self.t >= until:
                raise D.ConnectionLost("done")
            word = await real(now, trigger)
            polls.append((round(self.clock.now - self.t, 3), trigger, word))
            return word

        async def wait(timeout):
            if script and self.t + script[0][0] <= self.clock.now + timeout:
                at, act = script.pop(0)
                self.clock.now = max(self.clock.now, self.t + at)
                act()
                why, d.sort_wake.why = d.sort_wake.why, None
                return why
            self.clock.now += timeout
            return None
        d.sort_poll = poll
        d.sort_wake.wait = wait
        with self.assertRaises(D.ConnectionLost):
            arun(d.sort_loop())
        return polls


class SortLoopDeadlines(LoopBase):
    def test_a_quiet_outcome_rearms_and_the_pass_runs_at_quiet_end(self):
        self.rows()
        w = self.window("W1", [1, 2, 3], current=1)
        d = self.daemon([w])

        def switch():
            self.event("s2", self.t)  # T2 is newest, and you click T1
            w.current_tab = w.tabs[2]
            d.stamp_focus("T1")
        polls = self.drive(d, 20, [(5, switch)])
        self.assertEqual(polls[:3], [(0, "start", "sorted"), (5, "focus", "quiet"),
                                     (8, "deadline", "sorted")])
        self.assertEqual(tas.ids(w), ["T2", "T3", "T1"])
        self.assertEqual(w.set_at, [self.t, self.t + 8])

    def test_a_pass_over_one_window_does_not_hold_back_another(self):
        self.rows(n=6)
        w1 = self.window("W1", [1, 2, 3])
        w2 = self.window("W2", [4, 5, 6])
        d = self.daemon([w1, w2])

        def switch_w2():
            self.event("s4", self.t)
            w2.current_tab = w2.tabs[0]
            d.stamp_focus(w2.tabs[0].tab_id)  # W2 quiet until +3.5

        def activity_w1():
            self.event("s1", self.t + 1)
            d.on_ledger_write(self.clock.now)
        polls = self.drive(d, 10, [(0.5, switch_w2), (3, activity_w1)])
        self.assertIn((2, "deadline", "incomplete"), polls)  # W1 looked at, W2 still quiet
        self.assertIn((3, "event", "too soon"), polls)
        self.assertIn((3.5, "deadline", "sorted"), polls)
        # a global min interval would have held W2 until the pass at +2 plus 2 s
        self.assertEqual(w2.set_at[-1], self.t + 3.5)
        self.assertEqual(tas.ids(w1)[0], "T1")

    def test_an_incomplete_pass_backs_off_doubling_and_a_complete_one_resets(self):
        self.rows()
        w = self.window("W1", [1, 2, 3])
        d = self.daemon([w])
        restore = {"on": True}

        def restore_ends():
            restore["on"] = False

        def activity_with_restore():
            restore["on"] = True
            self.event("s1", self.t + 1)
            d.on_ledger_write(self.clock.now)
        with mock.patch.object(D.tab_snapshots, "restore_running", lambda cfg: restore["on"]):
            polls = self.drive(d, 150, [(130, restore_ends), (140, activity_with_restore)])
        passes = [(t, word) for t, _, word in polls if word in ("sorted", "incomplete")]
        self.assertEqual(passes, [(0, "incomplete"), (5, "incomplete"), (15, "incomplete"),
                                  (35, "incomplete"), (75, "incomplete"), (135, "sorted"),
                                  (140, "incomplete"), (145, "incomplete")])
        self.assertIn((65, "safety", "backoff"), polls)  # the safety poll respects it

    def test_a_safety_pass_is_logged_as_such(self):
        self.rows()
        w = self.window("W1", [1, 2, 3])
        d = self.daemon([w])
        with self.assertLogs("cc-sessions", "INFO") as logs:
            arun(d.sort_poll(self.t, trigger="safety"))
        self.assertTrue(any("trigger=safety" in m and "moved 2 tab(s)" in m
                            for m in logs.output), logs.output)


class MovedTabs(LoopBase):
    def test_the_fewest_tabs_that_changed_place(self):
        for order, n in (([0, 1, 2], 0), ([2, 0, 1], 1), ([1, 2, 3, 0], 1), ([2, 1, 0], 2),
                         ([0, 3, 1, 2, 4], 1), ([], 0)):
            got = D._moved_tabs(order)
            self.assertEqual(len(got), n, order)
            rest = [i for i in order if i not in got]
            self.assertEqual(rest, sorted(rest), order)  # the others kept their relative order
        self.assertEqual(D._moved_tabs([2, 0, 1]), [2])


class Pins(LoopBase):
    def test_with_no_multi_tab_window_passes_have_a_floor(self):
        self.rows()
        for windows in ([], [self.window("W1", [1])]):
            d = self.daemon(windows)
            self.assertEqual(arun(d.sort_poll(self.t)), "sorted")
            self.event("s1", self.t)
            self.assertEqual(arun(d.sort_poll(self.t + 0.5)), "too soon")
            self.assertEqual(d.sort_due, self.t + 2)
            self.assertEqual(arun(d.sort_poll(self.t + 2)), "sorted")
            self.assertEqual(len(self.passes), 0)  # no window to order: the pass is cheap

    def test_unchanged_schedules_the_recheck_slot_and_clears_the_backoff(self):
        self.cfg["tab_sort_recheck_s"] = 600
        self.rows()
        d = self.daemon([self.window("W1", [1, 2, 3])])
        t = (int(self.t // 600)) * 600 + 100
        self.assertEqual(arun(d.sort_poll(t)), "sorted")
        d.sort_backoff = 20
        self.assertEqual(arun(d.sort_poll(t + 50)), "unchanged")
        self.assertEqual(d.sort_due, (int(t // 600) + 1) * 600)
        self.assertEqual(d.sort_backoff, 0)

    def test_a_switch_back_to_a_tab_seen_stamped_is_quiet_again(self):
        self.rows()
        w = self.window("W1", [1, 2, 3], current=0)
        d = self.daemon([w])
        arun(d.sort_poll(self.t))
        tabs = {t.tab_id: t for t in w.tabs}
        d.stamp_focus("T2", now=self.t + 1)
        w.current_tab = tabs["T2"]
        arun(d.sort_poll(self.t + 2))
        self.assertEqual(d.focus_tab, {})  # seen, so consumed
        w.current_tab = tabs["T3"]  # its event lags behind
        arun(d.sort_poll(self.t + 20))
        w.current_tab = tabs["T2"]  # and back, still no event
        arun(d.sort_poll(self.t + 40))
        self.assertEqual(d.quiet_until["W1"], self.t + 43)


class UserChangedDuringThePass(LoopBase):
    def test_a_switch_during_the_pass_is_a_flat_retry_not_a_backoff(self):
        self.rows()
        w = self.window("W1", [1, 2, 3], current=0)
        d = self.daemon([w])
        d.clock = self.clock
        flips = []

        def switch():
            if len(flips) < 3:  # you keep switching during three passes
                flips.append(1)
                w.current_tab = w.tabs[(len(flips)) % 3]
        self.during_pass = switch
        polls = self.drive(d, 30, [])
        passes = [(t, word) for t, _, word in polls if word in ("sorted", "incomplete")]
        # 5 s flat, then the 3 s quiet the next poll stamps for the switch: even steps, no doubling
        self.assertEqual([t for t, _ in passes][:4], [0, 8, 16, 24])
        self.assertEqual([word for _, word in passes][:4], ["incomplete"] * 3 + ["sorted"])
        self.assertEqual(d.sort_backoff, 0)

    def test_a_switch_during_the_pass_retries_at_its_quiet_end(self):
        self.rows()
        w = self.window("W1", [1, 2, 3], current=0)
        d = self.daemon([w])
        d.clock = self.clock

        def switch():
            if self.during_pass:
                self.during_pass = None
                w.current_tab = w.tabs[1]
                d.stamp_focus("T2")  # quiet until +3
        self.during_pass = switch
        polls = self.drive(d, 5, [])
        self.assertEqual(polls[:2], [(0, "start", "incomplete"), (3, "deadline", "sorted")])


class FocusStamp(LoopBase):
    def test_quiet_runs_from_the_receipt_not_from_the_poll(self):
        self.rows()
        w = self.window("W1", [1, 2, 3], current=0)
        d = self.daemon([w])
        d.clock = self.clock
        self.assertEqual(arun(d.sort_poll(self.t)), "sorted")
        self.clock.now = self.t + 10
        self.assertEqual(d.stamp_focus(w.tabs[1].tab_id), "W1")
        self.assertEqual(d.quiet_until["W1"], self.t + 13)
        self.assertEqual(d.sort_wake.why, "focus")
        w.current_tab = w.tabs[1]  # the model shows the switch by the next poll
        self.assertEqual(arun(d.sort_poll(self.t + 12)), "quiet")
        self.assertEqual(d.quiet_until["W1"], self.t + 13)
        self.assertEqual(d.sort_due, self.t + 13)

    def test_focus_loop_stamps_before_the_slow_handling(self):
        self.rows()
        w = self.window("W1", [1, 2, 3], current=0)

        class App(tas.App):
            def get_tab_by_id(self, tab_id):
                found = [t for t in w.tabs if t.tab_id == tab_id]
                return types.SimpleNamespace(current_session=None) if found else None
        d = D.Daemon(self.cfg, self.conn, App([w]), None, False)
        d.clock = self.clock
        updates = [types.SimpleNamespace(selected_tab_changed=types.SimpleNamespace(tab_id="T2"),
                                         active_session_changed=None, window_changed=None)]

        class Monitor(object):
            def __init__(self, connection):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def async_get_next_update(self):
                if updates:
                    return updates.pop(0)
                raise D.ConnectionLost("done")
        seen = []

        async def slow(session):
            seen.append(d.quiet_until.get("W1"))
            self.clock.now += 5  # a full snapshot per event
        d.on_focus_change = slow
        fake = types.SimpleNamespace(FocusMonitor=Monitor, FocusUpdateWindowChanged=types.SimpleNamespace(
            Reason=types.SimpleNamespace(TERMINAL_WINDOW_BECAME_KEY=1)))
        with mock.patch.dict(sys.modules, {"iterm2": fake}):
            with self.assertRaises(D.ConnectionLost):
                arun(d.focus_loop())
        self.assertEqual(seen, [self.t + 3])
        self.assertEqual(d.quiet_until["W1"], self.t + 3)


class RequestLoop(LoopBase):
    def early_wakes(self, wake, until):
        """Every wait ends 0.1 s later with a ledger-write wake, long before any deadline."""
        async def wait(timeout):
            if self.clock.now - self.t >= until:
                raise D.ConnectionLost("done")
            self.clock.now += min(0.1, timeout)
            return "event"
        wake.wait = wait

    def test_a_retried_request_waits_its_turn_while_writes_wake_the_loop(self):
        d = self.daemon([])
        d.clock = self.clock
        tried = {"title": [], "wake": []}

        async def do(req):
            tried[req["kind"]].append(round(self.clock.now - self.t, 1))
            if req["kind"] == "title":
                if len(tried["title"]) == 1:
                    ledger.add_request(self.conn, "wake", "s2")  # new work while one is held
                return D.REQUEST_RETRY
            return "ok"
        d.do_request = do
        ledger.add_request(self.conn, "title", "s1")
        self.early_wakes(d.request_wake, 12)
        with self.assertRaises(D.ConnectionLost):
            arun(d.request_loop())
        self.assertEqual(len(tried["title"]), 3, tried)  # about 0, 5, 10: not on every wake
        self.assertEqual(tried["title"][0], 0)
        self.assertGreaterEqual(tried["title"][1], D.REQUEST_POLL_S)
        self.assertEqual(tried["wake"], [0.1])  # a new request runs on the next wake

    def quiet_waits(self, d, until, during=None):
        """No wakes at all: each wait runs to its timeout. -> the timeouts asked for."""
        waits = []

        async def wait(timeout):
            if len(waits) >= until:
                raise D.ConnectionLost("done")
            waits.append(timeout)
            self.clock.now += timeout
            if during:
                during(len(waits))
            return None
        d.request_wake.wait = wait
        return waits

    def test_a_retry_lands_at_its_deadline_and_a_done_one_leaves_no_hold(self):
        d = self.daemon([])
        d.clock = self.clock
        tried = []

        async def do(req):
            tried.append(round(self.clock.now - self.t, 2))
            return D.REQUEST_RETRY if len(tried) == 1 else "ok"
        d.do_request = do
        ledger.add_request(self.conn, "title", "s1")
        waits = self.quiet_waits(d, 2)
        with self.assertRaises(D.ConnectionLost):
            arun(d.request_loop())
        self.assertEqual(tried, [0, D.REQUEST_POLL_S])  # no wake came: the deadline did it
        self.assertEqual(d.request_hold, {})
        self.assertEqual(waits, [D.REQUEST_POLL_S, 30])

    def test_a_held_request_that_went_away_leaves_no_hold(self):
        d = self.daemon([])
        d.clock = self.clock

        async def do(req):
            return D.REQUEST_RETRY
        d.do_request = do
        ledger.add_request(self.conn, "title", "s1")

        def gone(n):
            if n == 1:
                with ledger.tx(self.conn):
                    self.conn.execute("DELETE FROM requests")
        waits = self.quiet_waits(d, 3, gone)
        with self.assertRaises(D.ConnectionLost):
            arun(d.request_loop())
        self.assertEqual(d.request_hold, {})
        self.assertEqual(waits, [D.REQUEST_POLL_S, 30, 30])  # never a spin on a stale hold

    def test_a_hold_beyond_one_retry_is_expired(self):
        d = self.daemon([])
        d.clock = self.clock
        tried = []

        async def do(req):
            tried.append(1)
            return "ok"
        d.do_request = do
        rid = ledger.add_request(self.conn, "title", "s1")
        hold = {rid: self.clock.now + 1000}  # the clock stepped back after it was set
        self.assertEqual(arun(d.handle_requests(hold)), (1, 0))
        self.assertEqual((tried, hold), ([1], {}))

    def test_a_failed_poll_sleeps_whatever_wakes_it(self):
        d = self.daemon([])
        d.clock = self.clock
        fails, slept = [], []

        async def boom(hold=None):
            fails.append(round(self.clock.now - self.t, 1))
            raise RuntimeError("bug")

        async def sleep(s):
            slept.append(s)
            self.clock.now += s
        d.handle_requests, d.sleep = boom, sleep
        self.early_wakes(d.request_wake, 100)
        with self.assertLogs("cc-sessions", "ERROR"):
            with self.assertRaises(D.ConnectionLost) as cm:
                arun(d.request_loop())
        self.assertIn("3 consecutive", str(cm.exception))
        self.assertEqual(fails, [0, 5, 10])
        self.assertEqual(slept, [D.REQUEST_POLL_S] * 2)

    def test_a_sort_bug_is_retried_after_a_sleep_not_on_every_write(self):
        d = self.daemon([])
        d.clock = self.clock
        polls, slept = [], []

        async def poll(now=None, trigger=None):
            polls.append((round(self.clock.now - self.t, 1), trigger))
            if len(polls) > 3:
                raise D.ConnectionLost("done")
            raise RuntimeError("bug")

        async def sleep(s):
            slept.append(s)
            self.clock.now += s
        d.sort_poll, d.sleep = poll, sleep
        self.early_wakes(d.sort_wake, 100)
        with self.assertLogs("cc-sessions", "ERROR"):
            with self.assertRaises(D.ConnectionLost):
                arun(d.sort_loop())
        self.assertEqual(polls, [(0, "start"), (5, "deadline"), (10, "deadline"), (15, "deadline")])
        self.assertEqual(slept, [D.REQUEST_POLL_S] * 3)

    def test_a_request_the_safety_poll_found_is_logged(self):
        d = self.daemon([])
        results = [(0, 0), (2, 0)]

        async def handle(hold=None):
            if not results:
                raise D.ConnectionLost("done")
            return results.pop(0)

        async def wait(timeout):
            return None
        d.handle_requests = handle
        d.request_wake.wait = wait
        with self.assertLogs("cc-sessions", "INFO") as logs:
            with self.assertRaises(D.ConnectionLost):
                arun(d.request_loop())
        self.assertEqual(len([m for m in logs.output if "trigger=safety handled 2" in m]), 1)

    def test_min_interval_is_at_least_a_second(self):
        self.cfg["tab_sort_min_interval_s"] = 0
        self.rows()
        d = self.daemon([])
        self.assertEqual(arun(d.sort_poll(self.t)), "sorted")
        self.event("s1", self.t)
        self.assertEqual(arun(d.sort_poll(self.t + 0.5)), "too soon")
        self.assertEqual(d.sort_due, self.t + 1)

    def test_wake_safety_s_is_clamped_and_a_bad_value_is_the_default(self):
        d = self.daemon([])
        for raw, want in ((1, 5), (1000, 300), (60, 60), ("60", 60)):
            self.cfg["wake_safety_s"] = raw
            self.assertEqual(d.safety_s(), want, raw)
        with self.assertLogs("cc-sessions", "WARNING") as logs:
            for raw in ("soon", None, float("nan"), "x"):
                self.cfg["wake_safety_s"] = raw
                self.assertEqual(d.safety_s(), 30, raw)
        self.assertEqual(len(logs.output), 1)

    def test_a_ledger_write_wakes_both_loops(self):
        d = self.daemon([])
        d.on_ledger_write(self.t)
        self.assertEqual((d.sort_wake.why, d.request_wake.why, d.sort_changed_at),
                         ("event", "event", self.t))

    def test_wake_wait_returns_on_set_and_on_timeout(self):
        wake = D.Wake()

        async def go():
            asyncio.get_running_loop().call_later(0.01, wake.set, "focus")
            first = await wake.wait(5)
            second = await wake.wait(0.01)
            return first, second
        self.assertEqual(arun(go()), ("focus", None))


class WalWatch(TempEnv):
    def setUp(self):
        super().setUp()
        with ledger.tx(self.conn):
            ledger.set_meta(self.conn, "probe", 1)  # the -wal file exists from the first write
        self.wal = self.db + "-wal"
        self.assertTrue(os.path.exists(self.wal))

    async def child(self, code):
        p = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", code, self.db)
        await p.wait()
        self.assertEqual(p.returncode, 0)

    async def until(self, cond, timeout):
        end = time.time() + timeout
        while not cond() and time.time() < end:
            await asyncio.sleep(0.02)
        return cond()

    def test_an_external_write_wakes_and_a_read_does_not(self):
        hits = []

        async def go():
            w = D.LedgerWatch(self.wal, hits.append)
            self.assertTrue(w.start(asyncio.get_running_loop()))
            try:
                await self.child(READER)
                await asyncio.sleep(0.6)
                self.assertEqual(hits, [])
                await self.child(WRITER)
                self.assertTrue(await self.until(lambda: hits, 1.0))
                await asyncio.sleep(0.8)
                # two commits: one wake, plus one more when the second landed mid-debounce
                n = len(hits)
                self.assertIn(n, (1, 2))
                await asyncio.sleep(0.5)
                self.assertEqual(len(hits), n)  # and then quiet
            finally:
                w.stop()
        arun(go())

    def test_a_missing_wal_is_armed_once_it_exists(self):
        hits = []
        path = os.path.join(self.tmp, "later.db-wal")

        async def go():
            w = D.LedgerWatch(path, hits.append)
            self.assertTrue(w.start(asyncio.get_running_loop()))
            try:
                self.assertIsNone(w.fd)
                with open(path, "w"):
                    pass
                w.ensure()
                self.assertIsNotNone(w.fd)
                with open(path, "a") as fh:
                    fh.write("x")
                self.assertTrue(await self.until(lambda: hits, 1.0))
            finally:
                w.stop()
        arun(go())

    def test_writes_during_the_debounce_get_one_more_wake(self):
        hits = []

        async def go():
            w = D.LedgerWatch(self.wal, hits.append)
            w.start(asyncio.get_running_loop())
            try:
                with open(self.wal, "ab") as fh:
                    fh.write(b"x")
                await asyncio.sleep(0.1)
                with open(self.wal, "ab") as fh:
                    fh.write(b"y")  # while the timer is pending
                await asyncio.sleep(1.0)
                self.assertEqual(len(hits), 2)
            finally:
                w.stop()
        arun(go())

    def test_a_replaced_wal_is_watched_again(self):
        hits = []

        async def go():
            w = D.LedgerWatch(self.wal, hits.append)
            w.start(asyncio.get_running_loop())
            try:
                old = os.fstat(w.fd).st_ino
                os.rename(self.wal, self.wal + ".old")
                with open(self.wal, "wb"):
                    pass
                new = os.stat(self.wal).st_ino
                self.assertTrue(await self.until(lambda: w.fd is not None and
                                                 os.fstat(w.fd).st_ino == new, 1.0))
                self.assertNotEqual(old, new)
                await asyncio.sleep(0.6)
                del hits[:]
                with open(self.wal, "ab") as fh:
                    fh.write(b"x")
                self.assertTrue(await self.until(lambda: hits, 1.0))
            finally:
                w.stop()
        arun(go())

    def test_stop_takes_the_reader_off_the_loop(self):
        async def go():
            loop = asyncio.get_running_loop()
            w = D.LedgerWatch(self.wal, lambda at: None)
            w.start(loop)
            fd = w.kq.fileno()
            with mock.patch.object(loop, "remove_reader", wraps=loop.remove_reader) as rm:
                w.stop()
            rm.assert_called_once_with(fd)
            self.assertIsNone(w.kq)
            self.assertIsNone(w.fd)
        arun(go())

    def test_wait_wake_arms_a_missing_wal_and_polls_often_until_then(self):
        path = os.path.join(self.tmp, "later.db-wal")
        d = D.Daemon(self.cfg, self.conn, tas.App([]), None, False)
        waits = []

        async def wait(timeout):
            waits.append(timeout)
            return None
        d.sort_wake.wait = wait

        async def go():
            d.watch = D.LedgerWatch(path, lambda at: None)
            d.watch.start(asyncio.get_running_loop())
            try:
                with self.assertLogs("cc-sessions", "WARNING") as logs:
                    self.assertEqual(await d.wait_wake(d.sort_wake, None), "safety")
                    with open(path, "w"):
                        pass
                    await d.wait_wake(d.sort_wake, None)  # its timeout arms the file
                self.assertIsNotNone(d.watch.fd)
                await d.wait_wake(d.sort_wake, None)
                d.watch.ensure = mock.Mock(side_effect=RuntimeError("boom"))
                with self.assertLogs("cc-sessions", "WARNING"):
                    self.assertEqual(await d.wait_wake(d.sort_wake, None), "safety")
            finally:
                d.watch.stop()
            self.assertEqual(len([m for m in logs.output if "not armed" in m]), 1)
        arun(go())
        self.assertEqual(waits[:3], [D.REQUEST_POLL_S, D.REQUEST_POLL_S, 30])

    def test_a_failing_watch_falls_back_without_spinning(self):
        hits, calls = [], []

        class Broken(object):
            def __init__(self, kq):
                self.kq = kq

            def fileno(self):
                return self.kq.fileno()

            def control(self, *a):
                if a[0] is None:
                    calls.append(1)
                    raise OSError("boom")
                return self.kq.control(*a)

            def close(self):
                self.kq.close()
        d = D.Daemon(self.cfg, self.conn, tas.App([]), None, False)
        p = mock.patch.object(D, "REQUEST_POLL_S", 0.05)  # a dead watch polls this often
        p.start()
        self.addCleanup(p.stop)

        async def go():
            w = D.LedgerWatch(self.wal, hits.append)
            w.start(asyncio.get_running_loop())
            w.kq = Broken(w.kq)
            d.watch = w
            with self.assertLogs("cc-sessions", "WARNING") as logs:
                await self.child(WRITER)
                await self.until(lambda: calls, 1.0)
                await self.child(WRITER)
                await asyncio.sleep(0.5)
            self.assertEqual(len(calls), 1)  # the reader is gone: no hot loop
            self.assertEqual(hits, [])
            self.assertTrue(w.dead)
            self.assertIsNone(w.kq)
            self.assertEqual(len([m for m in logs.output if "ledger watch failed" in m]), 1)
            self.assertEqual(await d.wait_wake(d.sort_wake, None), "safety")
            self.assertIsNone(w.fd)  # a dead watch is never armed again
        arun(go())
