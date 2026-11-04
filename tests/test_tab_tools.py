"""Tab tools: went-down-alone marker, login retry, dismiss, the activity key, sort order, the
Working bucket against guard_reason, groups file, close-idle refusals, find, open and colors.
Nothing here talks to iTerm2: bin/cc-tabs runs against fake windows, tabs and sessions."""
import ast
import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import time
from unittest import mock

from helpers import REPO, TempEnv, assistant, load_daemon, load_path, user

from ccsessions import activity, cli, ledger, procs, restore, tabgroups
from ccsessions import tabs as tabfile

D = load_daemon()
T = load_path("cc_tabs", os.path.join(REPO, "bin", "cc-tabs"))

INSTANCE = {"pid": 500, "start": 1000}
OLD = {"pid": 400, "start": 900}
GUI = {"loginwindow": {"pid": 1, "start": 10}, "WindowServer": {"pid": 2, "start": 20}}
NOW = 2_000_000_000.0
LONG_AGO = NOW - 30 * 86400


def transcript_at(folder, name, when):
    """A transcript whose only work is one reply at `when`."""
    path = os.path.join(folder, name)
    with open(path, "w") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(when)),
                             "message": {"model": "m", "content": [{"type": "text", "text": "done"}]}}) + "\n")
    return path


def arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Profile(object):
    def __init__(self, session):
        self.session = session

    async def async_set_tab_color(self, color):
        self.session.colors.append(color)

    async def async_set_use_tab_color(self, used):
        self.session.colors.append(("use", used))


class Session(object):
    def __init__(self, guid, tty):
        self.session_id, self.tty = guid, tty
        self.closed = self.activated = False
        self.colors = []
        self.color_error = None

    async def async_get_variable(self, name):
        return self.tty if name == "tty" else None

    async def async_get_profile(self):
        if self.color_error:
            raise self.color_error
        return Profile(self)

    async def async_close(self, force=False):
        self.closed = force

    async def async_activate(self, select_tab=True, order_window_front=True):
        self.activated = (select_tab, order_window_front)


class Tab(object):
    def __init__(self, tab_id, sessions):
        self.tab_id, self.sessions = tab_id, sessions
        self.current_session = sessions[0] if sessions else None


class Window(object):
    def __init__(self, tabs, current=0):
        self.tabs = tabs
        self.selected = tabs[current] if current is not None else None
        self.set_tabs_calls = []

    @property
    def current_tab(self):
        return self.selected

    async def async_set_tabs(self, tabs):
        self.set_tabs_calls.append(list(tabs))
        self.tabs = list(tabs)


class App(object):
    def __init__(self, windows):
        self.terminal_windows = windows
        self.activated = None
        self.refreshes = 0
        self.on_refresh = None

    async def async_refresh(self):
        self.refreshes += 1
        if self.on_refresh:
            self.on_refresh(self.refreshes)

    @property
    def current_terminal_window(self):
        return self.terminal_windows[0] if self.terminal_windows else None

    async def async_activate(self, raise_all_windows=True, ignoring_other_apps=False):
        self.activated = (raise_all_windows, ignoring_other_apps)


def tab(n):
    """Tab n: a tab whose one pane has guid G<n> and tty ttys<n>."""
    return Tab("T%d" % n, [Session("G%d" % n, "ttys%03d" % n)])


class Base(TempEnv):
    extra_config = {"resume_command": "echo-resume"}

    def setUp(self):
        super().setUp()
        p = mock.patch.object(tabfile, "iterm_instance", lambda: self.current)
        p.start()
        self.addCleanup(p.stop)
        self.current = INSTANCE

    def snapshot_file(self, sids=(None,), instance=OLD, taken_at=None, gui=None):
        taken_at = NOW if taken_at is None else taken_at
        tabs = [{"tab_id": "T%d" % i, "sessions": [{"guid": "old-%d" % i, "tty": "ttys%03d" % i, "session_id": s,
                                                    "state": "idle", "title": "t", "cwd": None}]}
                for i, s in enumerate(sids)]
        snap = {"v": 1, "iterm_pid": instance["pid"], "iterm_start": instance["start"], "taken_at": taken_at,
                "windows": [{"window_id": "W1", "tabs": tabs}]}
        if gui:
            snap["gui_session"] = gui
        d = tabfile.snapshots_dir(self.cfg)
        os.makedirs(d, exist_ok=True)
        path = tabfile.snapshot_path(self.cfg, instance)
        with open(path, "w") as fh:
            json.dump(snap, fh)
        return path

    def event(self, sid, name, ts):
        with ledger.tx(self.conn):
            ledger.add_event(self.conn, sid, name, {}, ts)


# ------------------------------------------------------------------ A1

class AloneCheck(Base):
    def check(self, gui_now, sleeps=None, snapshot_gui=GUI, current=None):
        path = self.snapshot_file(instance=OLD, gui=snapshot_gui)
        self.current = current
        sleeps = [] if sleeps is None else sleeps
        with mock.patch.object(tabfile, "gui_session", lambda: gui_now):
            word = D.alone_check(self.cfg, OLD, sleep=sleeps.append)
        return word, tabfile.flagged(path, tabfile.ENDED_ALONE_SUFFIX), sleeps

    def test_unchanged_session_writes_the_marker_after_the_wait(self):
        word, marked, sleeps = self.check(dict(GUI))
        self.assertEqual((word, marked, sleeps), ("went down alone", True, [30.0]))

    def test_marker_comes_after_the_wait_not_before(self):
        order = []
        path = self.snapshot_file(instance=OLD, gui=GUI)
        self.current = None

        def sleep(s):
            order.append(tabfile.flagged(path, tabfile.ENDED_ALONE_SUFFIX))
        with mock.patch.object(tabfile, "gui_session", lambda: dict(GUI)):
            D.alone_check(self.cfg, OLD, sleep=sleep)
        self.assertEqual(order, [False])
        self.assertTrue(tabfile.flagged(path, tabfile.ENDED_ALONE_SUFFIX))

    def test_wait_is_configurable(self):
        self.cfg["iterm_alone_check_s"] = 7
        self.assertEqual(self.check(dict(GUI))[2], [7.0])

    def test_changed_session_writes_nothing(self):
        for name in ("loginwindow", "WindowServer"):
            changed = json.loads(json.dumps(GUI))
            changed[name]["start"] += 1
            word, marked, _ = self.check(changed)
            self.assertEqual((word, marked), ("GUI session changed", False), name)
            changed = json.loads(json.dumps(GUI))
            changed[name]["pid"] += 1
            self.assertFalse(self.check(changed)[1], name)

    def test_unreadable_session_writes_nothing(self):
        word, marked, _ = self.check(None)
        self.assertEqual((word, marked), ("GUI session unreadable", False))

    def test_missing_gui_session_writes_nothing_and_does_not_wait(self):
        word, marked, sleeps = self.check(dict(GUI), snapshot_gui=None)
        self.assertEqual((word, marked, sleeps), ("snapshot has no gui_session", False, []))

    def test_no_snapshot_writes_nothing(self):
        sleeps = []
        self.assertEqual(D.alone_check(self.cfg, None, sleep=sleeps.append), "no snapshot yet")
        self.assertEqual(sleeps, [])

    def test_iterm2_still_up_is_not_alone(self):
        word, marked, _ = self.check(dict(GUI), current=OLD)
        self.assertEqual((word, marked), ("iTerm2 still running", False))

    def test_iterm2_relaunched_blank_during_the_wait_still_counts(self):
        self.assertTrue(self.check(dict(GUI), current=INSTANCE)[1])

    def test_a_sigterm_during_the_wait_writes_nothing(self):
        path = self.snapshot_file(instance=OLD, gui=GUI)

        def killed(_):
            raise SystemExit(143)
        with self.assertRaises(SystemExit):
            D.alone_check(self.cfg, OLD, sleep=killed)
        self.assertFalse(tabfile.flagged(path, tabfile.ENDED_ALONE_SUFFIX))

    def test_no_sigterm_handler_anywhere(self):
        for rel in (("daemon", "cc_sessions_daemon.py"), ("bin", "cc-tabs")):
            with open(os.path.join(REPO, *rel)) as fh:
                src = fh.read()
            self.assertNotIn("signal.signal", src)
            self.assertNotIn("add_signal_handler", src)

    def test_gui_session_is_not_part_of_write_on_change(self):
        snap = tabfile.build_snapshot(INSTANCE, [{"window_id": "W", "tabs": [
            {"tab_id": "T", "sessions": [{"guid": "G", "tty": "ttys001"}]}]}], [], now=1.0, gui=GUI)
        self.assertEqual(snap["gui_session"], GUI)
        self.assertEqual(tabfile.write_snapshot(self.cfg, snap), "written")
        other = dict(snap, taken_at=2.0, gui_session={"loginwindow": {"pid": 9, "start": 9}})
        self.assertEqual(tabfile.write_snapshot(self.cfg, other), "unchanged")

    def test_file_from_the_older_daemon_gets_the_field_once(self):
        snap = tabfile.build_snapshot(INSTANCE, [{"window_id": "W", "tabs": [
            {"tab_id": "T", "sessions": [{"guid": "G", "tty": "ttys001"}]}]}], [], now=1.0)
        self.assertNotIn("gui_session", snap)
        tabfile.write_snapshot(self.cfg, snap)
        self.assertEqual(tabfile.write_snapshot(self.cfg, dict(snap, gui_session=GUI)), "written")
        self.assertEqual(tabfile.read_snapshot(tabfile.snapshot_path(self.cfg, INSTANCE))["gui_session"], GUI)
        self.assertEqual(tabfile.write_snapshot(self.cfg, dict(snap, gui_session=GUI)), "unchanged")

    def test_gui_session_reads_both_or_none(self):
        found = {"loginwindow": [(10, 1)], "WindowServer": [(20, 2), (30, 3)]}
        with mock.patch.object(tabfile, "_processes", lambda n: found[n]):
            self.assertEqual(tabfile.gui_session(), {"loginwindow": {"pid": 1, "start": 10},
                                                     "WindowServer": {"pid": 3, "start": 30}})
        found["WindowServer"] = []
        with mock.patch.object(tabfile, "_processes", lambda n: found[n]):
            self.assertIsNone(tabfile.gui_session())

    def test_markers_are_pruned_with_their_snapshot(self):
        d = tabfile.snapshots_dir(self.cfg)
        os.makedirs(d)
        for i in range(3):
            with open(os.path.join(d, "%d-1.json" % i), "w") as fh:
                fh.write("{}")
            os.utime(os.path.join(d, "%d-1.json" % i), (1000 + i, 1000 + i))
        suffixes = (".restored", ".ended_alone", ".dismissed")
        for suffix in suffixes:
            with open(os.path.join(d, "0-1.json" + suffix), "w") as fh:
                fh.write("x")
            with open(os.path.join(d, "2-1.json" + suffix), "w") as fh:
                fh.write("x")
        tabfile.prune(self.cfg, keep=2)
        left = sorted(os.listdir(d))
        self.assertEqual([n for n in left if n.startswith("0-1")], [])
        for suffix in suffixes:
            self.assertIn("2-1.json" + suffix, left)


class DaemonWritesGuiSession(Base):
    def test_tick_snapshot_carries_the_identities(self):
        app = App([Window([tab(1)])])
        d = D.Daemon(self.cfg, self.conn, app, None, dry_run=False)
        for t in app.terminal_windows[0].tabs:
            t.sessions[0].session_id = "G1"
        app.terminal_windows[0].window_id = "W1"
        focus = {"tty_guid": {"ttys001": "G1"}, "panes": 1, "skipped": 0}
        with mock.patch.object(tabfile, "gui_session", lambda: GUI):
            self.assertEqual(arun(d.write_tab_snapshot(focus, now=5.0)), "written")
        self.assertEqual(tabfile.read_snapshot(tabfile.snapshot_path(self.cfg, INSTANCE))["gui_session"], GUI)

    def test_run_connected_hands_the_daemon_to_main(self):
        failure = {}

        class Stop(Exception):
            pass

        async def get_app(conn):
            return mock.Mock(connection=conn)
        conn_obj = object()
        with mock.patch.object(D.Daemon, "run_loops", side_effect=Stop()):
            with self.assertRaises(Stop):
                arun(D.run_connected(conn_obj, self.cfg, self.conn, True, get_app, failure))
        self.assertIsInstance(failure["daemon"], D.Daemon)


class AutoSkipsEndedAlone(Base):
    def setUp(self):
        super().setUp()
        self.insert(session_id="dead", pid=None, cwd=self.tmp)
        p = mock.patch.object(restore, "iterm_guids", lambda: set())
        p.start()
        self.addCleanup(p.stop)
        self.path = self.snapshot_file(["dead"])
        tabfile.set_flag(self.path, tabfile.ENDED_ALONE_SUFFIX)
        self.lines = []
        self.opened = []

    def do_run(self, **kw):
        self.lines = []
        with mock.patch.object(restore, "open_tabs", lambda tabs, progress: self.opened.append(tabs)):
            return restore.run(self.cfg, self.conn, out=self.lines.append, **kw)

    def test_auto_prints_a_line_consumes_nothing_exits_zero(self):
        self.assertEqual(self.do_run(auto=True), 0)
        self.assertEqual(self.opened, [])
        self.assertIn("went down alone", self.lines[-1])
        self.assertFalse(tabfile.consumed(self.path))

    def test_manual_restore_is_unaffected(self):
        self.assertEqual(self.do_run(), 0)
        self.assertEqual(len(self.opened), 1)

    def test_auto_without_the_marker_opens(self):
        os.unlink(self.path + tabfile.ENDED_ALONE_SUFFIX)
        self.assertEqual(self.do_run(auto=True), 0)
        self.assertEqual(len(self.opened), 1)

    def test_pending_still_counts_it_for_the_click(self):
        self.assertEqual(cli.tabs_data(self.cfg, self.conn)["pending"], 1)


class EndedAloneBootGrace(Base):
    """A marker from a chosen restart's quit phase must not stop the login restore."""
    BOOT = 1_900_000_000

    def setUp(self):
        super().setUp()
        self.insert(session_id="dead", pid=None, cwd=self.tmp)
        self.boot = self.BOOT
        self.opened = []
        self.lines = []
        for obj, name, value in ((restore, "iterm_guids", lambda: set()),
                                 (procs, "boot_time", lambda: self.boot)):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.path = self.snapshot_file(["dead"])

    def mark(self, before_boot):
        tabfile.set_flag(self.path, tabfile.ENDED_ALONE_SUFFIX)
        t = self.BOOT - before_boot
        os.utime(self.path + tabfile.ENDED_ALONE_SUFFIX, (t, t))

    def auto(self):
        with mock.patch.object(restore, "open_tabs", lambda tabs, progress: self.opened.append(tabs)):
            self.assertEqual(restore.run(self.cfg, self.conn, auto=True, out=self.lines.append), 0)
        return len(self.opened)

    def test_marker_shortly_before_boot_is_ignored(self):
        self.mark(45)
        self.assertEqual(self.auto(), 1)
        self.assertTrue(tabfile.consumed(self.path))
        self.assertFalse(any("went down alone" in l for l in self.lines))

    def test_the_marker_file_is_left_in_place(self):
        self.mark(45)
        self.auto()
        self.assertTrue(tabfile.flagged(self.path, tabfile.ENDED_ALONE_SUFFIX))

    def test_grace_edges(self):
        self.assertEqual(restore.ENDED_ALONE_BOOT_GRACE_S, 600)
        self.mark(0)
        self.assertFalse(restore.went_down_alone(self.path))
        self.mark(600)
        self.assertFalse(restore.went_down_alone(self.path))
        self.mark(601)
        self.assertTrue(restore.went_down_alone(self.path))

    def test_marker_older_than_the_grace_is_honoured(self):
        self.mark(601)
        self.assertEqual(self.auto(), 0)
        self.assertIn("went down alone", self.lines[-1])
        self.assertFalse(tabfile.consumed(self.path))

    def test_marker_after_boot_is_honoured(self):
        self.mark(-1)
        self.assertEqual(self.auto(), 0)
        self.assertIn("went down alone", self.lines[-1])

    def test_unreadable_boot_time_restores(self):
        self.mark(100000)
        self.boot = None
        self.assertEqual(self.auto(), 1)

    def test_no_marker_needs_no_boot_time(self):
        with mock.patch.object(procs, "boot_time", side_effect=AssertionError("asked")):
            self.assertFalse(restore.went_down_alone(self.path))

    def test_login_wait_treats_an_ignored_marker_as_no_marker(self):
        self.insert(session_id="dying", pid=7, pid_start=1, cwd=self.tmp, state="idle")
        path = self.snapshot_file(["dying"])
        with mock.patch.object(procs, "identity_alive", lambda pid, start, **kw: bool(pid)):
            self.mark(45)
            self.assertEqual(path, self.path)
            self.assertFalse(restore.login_settled(self.cfg, self.conn))
            self.mark(601)
            self.assertTrue(restore.login_settled(self.cfg, self.conn))


class AfterConnectionLoss(Base):
    def setUp(self):
        super().setUp()
        self.clock = {"t": 5000.0}
        self.seen = []
        self.current = None
        for obj, name, value in ((time, "time", lambda: self.clock["t"]), (time, "sleep", self.sleep),
                                 (tabfile, "gui_session", lambda: dict(GUI))):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.path = self.snapshot_file(instance=OLD, gui=GUI)

    def meta(self, key):
        return float(ledger.get_meta(self.conn, key))

    def sleep(self, s):
        self.seen.append((s, self.meta("liveness"), self.meta("last_exit_at")))
        self.clock["t"] += s

    def test_liveness_is_written_through_the_wait(self):
        D.after_connection_loss(self.cfg, self.conn, OLD, "lost")
        self.assertEqual([x[0] for x in self.seen], [10, 10, 10])
        self.assertEqual([x[1] for x in self.seen], [5000.0, 5010.0, 5020.0])
        self.assertTrue(tabfile.flagged(self.path, tabfile.ENDED_ALONE_SUFFIX))

    def test_last_exit_at_is_the_real_exit_and_is_there_during_the_wait(self):
        D.after_connection_loss(self.cfg, self.conn, OLD, "lost")
        self.assertEqual(self.seen[0][2], 5000.0)
        self.assertEqual(self.meta("last_exit_at"), 5030.0)
        self.assertEqual(self.meta("liveness"), 5030.0)
        self.assertEqual(ledger.get_meta(self.conn, "last_error"), "lost")

    def test_a_sigterm_in_the_wait_leaves_the_reason_and_no_marker(self):
        with mock.patch.object(time, "sleep", side_effect=SystemExit(143)):
            with self.assertRaises(SystemExit):
                D.after_connection_loss(self.cfg, self.conn, OLD, "lost")
        self.assertEqual(ledger.get_meta(self.conn, "last_error"), "lost")
        self.assertEqual(self.meta("last_exit_at"), 5000.0)
        self.assertFalse(tabfile.flagged(self.path, tabfile.ENDED_ALONE_SUFFIX))

    def test_a_failing_check_still_records_the_exit(self):
        with mock.patch.object(D, "alone_check", side_effect=RuntimeError("no")):
            D.after_connection_loss(self.cfg, self.conn, OLD, "lost")
        self.assertEqual(self.meta("last_exit_at"), 5000.0)


# ------------------------------------------------------------------ A2

class LoginRetry(Base):
    """A fake clock that only moves when slept on: no real sleeps."""

    def setUp(self):
        super().setUp()
        self.t = 100.0
        self.sleeps = []
        self.guids = set()
        self.lines = []
        self.alive = lambda pid, start: False
        for obj, name, value in ((restore, "iterm_guids", lambda: self.guids),
                                 (procs, "identity_alive", lambda pid, start, **kw: self.alive(pid, start)),
                                 (restore, "open_tabs", self.fake_open)):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.opened = []
        self.lock_during_wait = []
        self.insert(session_id="dead", pid=77, pid_start=5, cwd=self.tmp)
        self.path = self.snapshot_file(["dead"])

    def fake_open(self, tabs, progress):
        self.opened.append(tabs)

    def sleep(self, s):
        self.sleeps.append(s)
        self.lock_during_wait.append(tabfile.restore_running(self.cfg))
        self.t += s
        if self.on_sleep:
            self.on_sleep()

    on_sleep = None

    def do_run(self, **kw):
        kw.setdefault("auto", True)
        return restore.run_snapshot(self.cfg, self.conn, out=self.lines.append, sleep=self.sleep,
                                    clock=lambda: self.t, **kw)

    def test_settled_at_once_does_not_wait(self):
        self.do_run()
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(self.opened), 1)

    def test_a_dying_process_is_waited_for_and_then_restored(self):
        self.alive = lambda pid, start: True

        def die():
            if self.t >= 130:
                self.alive = lambda pid, start: False
        self.on_sleep = die
        self.do_run()
        self.assertEqual(self.sleeps, [15, 15])
        self.assertEqual(len(self.opened), 1)
        self.assertTrue(tabfile.consumed(self.path))

    def test_gives_up_after_180_s_and_goes_on_as_today(self):
        self.alive = lambda pid, start: True
        self.do_run()
        self.assertEqual(sum(self.sleeps), 180)
        self.assertEqual(self.sleeps, [15] * 12)
        self.assertEqual(self.opened, [])
        self.assertFalse(tabfile.consumed(self.path))
        self.assertTrue(any("running without a tab" in l for l in self.lines))

    def test_iterm2_not_answering_is_waited_for_and_aborts_as_today(self):
        self.guids = None
        self.assertEqual(self.do_run(), 1)
        self.assertEqual(sum(self.sleeps), 180)
        self.assertFalse(tabfile.consumed(self.path))

    def test_iterm2_answering_late_goes_ahead(self):
        self.guids = None

        def answer():
            self.guids = set()
        self.on_sleep = answer
        self.assertEqual(self.do_run(), 0)
        self.assertEqual(self.sleeps, [15])
        self.assertEqual(len(self.opened), 1)

    def test_iterm2_not_running_is_settled(self):
        self.current = None
        self.guids = None
        self.do_run()
        self.assertEqual(self.sleeps, [])

    def test_no_lock_held_while_waiting(self):
        self.alive = lambda pid, start: True
        self.do_run()
        self.assertEqual(set(self.lock_during_wait), {False})

    def test_only_auto_waits(self):
        self.alive = lambda pid, start: True
        self.do_run(auto=False)
        self.assertEqual(self.sleeps, [])
        self.do_run(dry_run=True)
        self.assertEqual(self.sleeps, [])

    def test_ended_alone_and_consumed_and_dismissed_do_not_wait(self):
        self.alive = lambda pid, start: True
        for suffix in tabfile.MARKER_SUFFIXES:
            tabfile.set_flag(self.path, suffix)
            self.do_run()
            self.assertEqual(self.sleeps, [], suffix)
            os.unlink(self.path + suffix)

    def test_a_click_during_the_wait_is_not_opened_twice(self):
        self.alive = lambda pid, start: True

        def click():
            with ledger.tx(self.conn):
                self.conn.execute("UPDATE sessions SET restored_at=? WHERE session_id='dead'", (time.time() + NOW,))
            self.alive = lambda pid, start: False
        self.on_sleep = click
        self.do_run()
        self.assertEqual(self.opened, [])

    def test_cli_turns_the_wait_on(self):
        a = cli.build_parser().parse_args(["restore", "--auto"])
        with mock.patch.object(restore, "run", return_value=0) as run:
            cli.cmd_restore(self.cfg, a)
        self.assertIs(run.call_args.kwargs["sleep"], time.sleep)
        self.assertIs(run.call_args.kwargs["after_open"], restore.spawn_recolor)


# ------------------------------------------------------------------ A3

class Dismiss(Base):
    def setUp(self):
        super().setUp()
        self.insert(session_id="dead", pid=None, cwd=self.tmp)
        p = mock.patch.object(restore, "iterm_guids", lambda: set())
        p.start()
        self.addCleanup(p.stop)
        self.path = self.snapshot_file(["dead"])
        self.lines = []
        self.opened = []

    def do_run(self, **kw):
        self.lines = []
        with mock.patch.object(restore, "open_tabs", lambda tabs, progress: self.opened.append(tabs)):
            return restore.run(self.cfg, self.conn, out=self.lines.append, **kw)

    def test_dismiss_marks_the_newest_previous_only(self):
        newer = self.snapshot_file(["dead"], instance={"pid": 450, "start": 950}, taken_at=NOW + 10)
        cur = self.snapshot_file(["dead"], instance=INSTANCE, taken_at=NOW + 20)
        self.assertEqual(restore.dismiss(self.cfg, out=self.lines.append), 0)
        self.assertTrue(tabfile.flagged(newer, tabfile.DISMISSED_SUFFIX))
        self.assertFalse(tabfile.flagged(self.path, tabfile.DISMISSED_SUFFIX))
        self.assertFalse(tabfile.flagged(cur, tabfile.DISMISSED_SUFFIX))

    def test_tabs_reports_zero_and_known(self):
        self.assertEqual(cli.tabs_data(self.cfg, self.conn)["pending"], 1)
        restore.dismiss(self.cfg, out=self.lines.append)
        d = cli.tabs_data(self.cfg, self.conn)
        self.assertEqual((d["pending"], d["pending_known"], d["pending_dismissed"]), (0, True, True))
        self.assertEqual(d["pending_path"], self.path)

    def test_plain_restore_says_nothing_to_restore(self):
        restore.dismiss(self.cfg, out=self.lines.append)
        self.assertEqual(self.do_run(), 0)
        self.assertEqual(self.lines, ["nothing to restore"])
        self.assertEqual(self.opened, [])
        self.assertEqual(self.do_run(dry_run=True), 0)
        self.assertEqual(self.lines, ["nothing to restore"])

    def test_auto_leaves_it_alone(self):
        restore.dismiss(self.cfg, out=self.lines.append)
        self.do_run(auto=True)
        self.assertEqual(self.opened, [])

    def test_snapshot_path_still_works(self):
        restore.dismiss(self.cfg, out=self.lines.append)
        self.assertEqual(self.do_run(snapshot=self.path), 0)
        self.assertEqual(len(self.opened), 1)

    def test_nothing_to_dismiss(self):
        os.unlink(self.path)
        restore.dismiss(self.cfg, out=self.lines.append)
        self.assertEqual(self.lines, ["nothing to dismiss"])

    def test_cli_flag(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["restore", "--dismiss"]), 0)
        self.assertTrue(tabfile.flagged(self.path, tabfile.DISMISSED_SUFFIX))
        self.assertIn("dismissed", buf.getvalue())


class RecolorIsBestEffort(Base):
    def setUp(self):
        super().setUp()
        self.insert(session_id="dead", pid=None, cwd=self.tmp)
        p = mock.patch.object(restore, "iterm_guids", lambda: set())
        p.start()
        self.addCleanup(p.stop)
        self.snapshot_file(["dead"])

    def opener(self, tabs, progress):
        progress["window"] = 1
        progress["opened"].extend([("guid-%d-xxxx" % i, "ttys100") for i in range(len(tabs))])

    def test_after_open_failure_never_fails_the_restore(self):
        lines = []

        def boom():
            raise RuntimeError("no")
        with mock.patch.object(restore, "open_tabs", self.opener), mock.patch.object(restore, "select_first_tab"):
            rc = restore.run(self.cfg, self.conn, out=lines.append, after_open=boom)
        self.assertEqual(rc, 0)
        self.assertTrue(lines[-1].startswith("opened 1 tabs"))

    def test_after_open_runs_only_when_tabs_opened(self):
        calls = []
        with mock.patch.object(restore, "open_tabs", self.opener), mock.patch.object(restore, "select_first_tab"):
            restore.run(self.cfg, self.conn, out=lambda s: None, after_open=lambda: calls.append(1))
            self.assertEqual(calls, [1])
            restore.run(self.cfg, self.conn, out=lambda s: None, dry_run=True, after_open=lambda: calls.append(2))
        self.assertEqual(calls, [1])

    def test_spawn_failure_is_swallowed(self):
        lines = []
        with mock.patch.object(subprocess, "Popen", side_effect=OSError("nope")):
            restore.spawn_recolor(out=lines.append)
        self.assertIn("could not recolor", lines[0])

    def test_spawn_starts_the_recolor_command(self):
        with mock.patch.object(subprocess, "Popen") as popen:
            restore.spawn_recolor()
        self.assertEqual(popen.call_args.args[0][1:], ["recolor"])
        self.assertTrue(popen.call_args.args[0][0].endswith("cc-tabs"))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


# ------------------------------------------------------------------ B1 activity + order

class ActivityKey(Base):
    def test_every_activity_event_counts_subagent_stop_included(self):
        for i, name in enumerate(("UserPromptSubmit", "Stop", "StopFailure", "SubagentStop")):
            sid = "s%d" % i
            r = self.insert(session_id=sid, started_at=1.0)
            self.event(sid, name, 500.0 + i)
            self.assertEqual(activity.activity_time(r, activity.newest_events(self.conn)), 500.0 + i, name)

    def test_newest_of_several_wins(self):
        self.insert(session_id="a")
        self.event("a", "Stop", 100.0)
        self.event("a", "SubagentStop", 300.0)
        self.event("a", "UserPromptSubmit", 200.0)
        self.assertEqual(activity.newest_events(self.conn)["a"], 300.0)

    def test_session_start_and_end_never_count(self):
        r = self.insert(session_id="a", started_at=10.0, resumed_at=None)
        for name in ("SessionStart", "SessionEnd", "SubagentStart", "Notification"):
            self.event("a", name, 900.0)
        ev = activity.newest_events(self.conn)
        self.assertNotIn("a", ev)
        self.assertEqual(activity.activity_time(r, ev), 10.0)

    def test_real_hook_events_land_where_the_key_reads_them(self):
        for event, ts in (("SessionStart", 1000.0), ("UserPromptSubmit", 1001.0), ("SubagentStart", 1002.0),
                          ("SubagentStop", 1003.0), ("SessionEnd", 1004.0)):
            ledger.apply_event(self.conn, event, {"session_id": "h1", "reason": "other"}, None, now=ts)
        self.assertEqual(activity.newest_events(self.conn)["h1"], 1003.0)

    def test_waking_a_session_never_makes_it_recent(self):
        r = {"session_id": "x", "started_at": 5.0, "resumed_at": 9.0}
        self.assertEqual(activity.activity_time(r, {}), 5.0)
        r = {"session_id": "x", "started_at": None, "resumed_at": 9.0}
        self.assertEqual(activity.activity_time(r, {}), 0)
        r = {"session_id": "x", "started_at": 5.0, "resumed_at": None}
        self.assertEqual(activity.activity_time(r, {}), 5.0)
        self.assertEqual(activity.activity_time({"session_id": "x"}, {}), 0)

    def test_an_event_beats_a_later_resume(self):
        r = {"session_id": "x", "started_at": 5.0, "resumed_at": 900.0}
        self.assertEqual(activity.activity_time(r, {"x": 100.0}), 100.0)

    def test_one_session_query(self):
        self.insert(session_id="a")
        self.insert(session_id="b", tty="ttys002")
        self.event("a", "Stop", 100.0)
        self.event("b", "Stop", 200.0)
        self.assertEqual(activity.newest_events(self.conn, "a"), {"a": 100.0})


def info(bucket, act, group=None):
    return {"bucket": bucket, "activity": act, "group": group}


class SortOrder(TempEnv):
    def test_every_bucket_in_order(self):
        infos = [info(activity.NON_CLAUDE, 0), info(activity.REST, 50), info(activity.WORKING, 10),
                 info(activity.NEEDS_YOU, 5), info(activity.PINNED, 1), info(activity.NON_CLAUDE, 0)]
        self.assertEqual(activity.sort_order(infos), [4, 3, 2, 1, 0, 5])

    def test_newest_first_inside_a_section(self):
        for bucket in (activity.PINNED, activity.NEEDS_YOU, activity.WORKING, activity.REST):
            infos = [info(bucket, 1), info(bucket, 3), info(bucket, 2)]
            self.assertEqual(activity.sort_order(infos), [1, 2, 0])

    def test_non_claude_keep_their_relative_order(self):
        infos = [info(activity.NON_CLAUDE, 0), info(activity.REST, 1), info(activity.NON_CLAUDE, 0)]
        self.assertEqual(activity.sort_order(infos), [1, 0, 2])

    def test_grouped_members_sit_together_ranked_by_newest_member(self):
        infos = [info(activity.REST, 10, "g"), info(activity.REST, 50), info(activity.REST, 90, "g"),
                 info(activity.REST, 60), info(activity.REST, 20, "h")]
        self.assertEqual(activity.sort_order(infos), [2, 0, 3, 1, 4])

    def test_group_stays_whole_when_an_ungrouped_tab_ties_its_rank(self):
        infos = [info(activity.REST, 90), info(activity.REST, 90, "g"), info(activity.REST, 10, "g")]
        self.assertEqual(activity.sort_order(infos), [1, 2, 0])

    def test_two_groups_tied_stay_whole(self):
        infos = [info(activity.REST, 90, "a"), info(activity.REST, 90, "b"), info(activity.REST, 10, "a"),
                 info(activity.REST, 10, "b")]
        order = activity.sort_order(infos)
        self.assertEqual(order, [0, 2, 1, 3])

    def test_grouped_tab_in_an_urgent_section_stays_there_and_does_not_rank_the_group(self):
        infos = [info(activity.REST, 10, "g"), info(activity.WORKING, 99, "g"), info(activity.REST, 50)]
        self.assertEqual(activity.sort_order(infos), [1, 2, 0])

    def test_hibernated_and_idle_mix_in_the_rest(self):
        hib = {"session_id": "h", "state": "hibernated", "started_at": 80.0}
        idle = {"session_id": "i", "state": "idle", "started_at": 40.0}
        none = lambda r: None
        no = lambda s: None
        infos = [activity.tab_info([idle], {}, no, no, none), activity.tab_info([hib], {}, no, no, none)]
        self.assertEqual([i["bucket"] for i in infos], [activity.REST, activity.REST])
        self.assertEqual(activity.sort_order(infos), [1, 0])

    def test_split_tab_takes_the_most_urgent_pane_and_newest_activity(self):
        a = {"session_id": "a", "state": "idle", "started_at": 5.0}
        b = {"session_id": "b", "state": "waiting", "started_at": 7.0}
        t = activity.tab_info([a, b], {}, lambda s: None, lambda s: False, lambda r: None)
        self.assertEqual((t["bucket"], t["activity"]), (activity.NEEDS_YOU, 7.0))

    def test_pinned_beats_waiting_and_work_is_not_asked(self):
        r = {"session_id": "a", "state": "waiting", "started_at": 1.0}

        def work(_):
            raise AssertionError("no transcript read for a pinned row")
        self.assertEqual(activity.bucket_of(r, True, work), activity.PINNED)


class WorkingBucket(TempEnv):
    """guard_reason and work_reason on the same facts."""

    def setUp(self):
        super().setUp()
        self.tr = self.transcript("s1", [user("hello"), assistant("done")])

    def row(self, **kw):
        r = {"session_id": "s1", "source": "hook", "interactive": 1, "state": "idle",
             "state_since": NOW - 3600, "subagents": 0, "tty": "ttys001", "last_focus_at": None,
             "resumed_at": None, "pid": 10, "pid_start": "x", "transcript": self.tr, "last_event_at": NOW - 3600}
        r.update(kw)
        return r

    def both(self, row, pending=(), kids=()):
        analysis = {"pending": list(pending), "has_user": True, "last_kind": "assistant"}
        work = activity.work_reason(row, NOW, self.cfg, lambda r: analysis, lambda r: list(kids))
        guard = D.guard_reason(row, NOW, self.cfg, {"tty_guid": {"ttys001": "G"}, "visible_ttys": set()},
                               lambda r: analysis, lambda r: list(kids), identity_fn=lambda r: None,
                               activity_fn=lambda r: NOW - 3600, evictions_today={}, hook_failed=set())
        return work, guard

    def test_idle_row_with_a_subagent(self):
        w, g = self.both(self.row(subagents=2))
        self.assertEqual((w, g), ("subagents=2", "subagents=2"))

    def test_idle_row_with_pending_background_work(self):
        w, g = self.both(self.row(), pending=["monitor"])
        self.assertEqual((w, g), ("pending: monitor", "pending: monitor"))

    def test_working_child(self):
        w, g = self.both(self.row(), kids=["node", "shell task"])
        self.assertEqual((w, g), ("running: node, shell task", "running: node, shell task"))

    def test_busy(self):
        w, g = self.both(self.row(state="busy", state_since=NOW - 60))
        self.assertEqual((w, g), ("state=busy", "state=busy"))

    def test_stale_busy_not_interrupted_is_work_for_both(self):
        w, g = self.both(self.row(state="busy", state_since=NOW - 7200))
        self.assertEqual((w, g), ("state=busy", "state=busy"))

    def test_stale_busy_interrupted_is_idle_for_both(self):
        row = self.row(state="busy", state_since=NOW - 7200)
        analysis = {"pending": [], "has_user": True, "last_kind": "user",
                    "last_user_text": "[Request interrupted by user]"}
        w = activity.work_reason(row, NOW, self.cfg, lambda r: analysis, lambda r: [])
        g = D.guard_reason(row, NOW, self.cfg, {"tty_guid": {"ttys001": "G"}, "visible_ttys": set()},
                           lambda r: analysis, lambda r: [], identity_fn=lambda r: None,
                           activity_fn=lambda r: NOW - 3600, evictions_today={}, hook_failed=set())
        self.assertEqual((w, g), (None, None))

    def test_nothing_in_flight_is_free_for_both(self):
        self.assertEqual(self.both(self.row()), (None, None))

    def test_every_work_answer_is_also_kept_by_guard(self):
        for kw in ({"subagents": 1}, {"state": "busy", "state_since": NOW - 5}):
            w, g = self.both(self.row(**kw))
            self.assertTrue(w and g, kw)

    def test_a_real_monitor_in_the_transcript(self):
        tool = {"type": "tool_use", "id": "m1", "name": "Monitor", "input": {"command": "tail -f x", "timeout_ms": 3600000}}
        t = time.time()
        tr = self.transcript("s2", [user("x", t=t - 60), assistant("watching", t=t - 50, tools=[tool])])
        row = self.row(session_id="s2", transcript=tr)
        facts = activity.Facts(self.cfg, t)
        facts.cmds = {}
        why = activity.work_reason(row, t, self.cfg, facts.analysis, facts.children)
        self.assertTrue(why and why.startswith("pending:"), why)

    def test_hibernated_needs_no_transcript_or_process_check(self):
        def boom(_):
            raise AssertionError("not for a hibernated row")
        self.assertIsNone(activity.work_reason(self.row(state="hibernated", subagents=3), NOW, self.cfg, boom, boom))

    def test_no_transcript_yet_is_not_work_but_a_child_is(self):
        r = self.row(transcript=None, session_id="s9")
        self.assertIsNone(activity.work_reason(r, NOW, self.cfg, lambda _: {"pending": ["unreadable"]}, lambda _: []))
        self.assertEqual(activity.work_reason(r, NOW, self.cfg, lambda _: {}, lambda _: ["node"]), "running: node")

    def test_daemon_facts_still_build(self):
        with mock.patch.object(procs, "ps_commands", lambda: {}):
            f = D.Facts(self.cfg)
        self.assertIsInstance(f, activity.Facts)
        self.assertAlmostEqual(f.now, time.time(), delta=5)


# ------------------------------------------------------------------ mapping

class Mapping(TempEnv):
    def rows(self):
        return [{"session_id": "live", "state": "idle", "tty": "ttys001", "pid": 1, "pid_start": 1},
                {"session_id": "parked", "state": "hibernated", "tty": "ttys001", "iterm_guid": "G1"},
                {"session_id": "dead", "state": "idle", "tty": "ttys002", "pid": 2, "pid_start": 2},
                {"session_id": "ended", "state": "idle", "tty": "ttys003", "ended_at": 5.0}]

    def test_live_on_tty_beats_stale_guid_when_running(self):
        m = tabfile.match_panes([{"guid": "G1", "tty": "ttys001"}], self.rows(), lambda r: True)
        self.assertEqual(m["G1"]["session_id"], "live")

    def test_a_dead_live_row_does_not_claim_a_reused_tty(self):
        m = tabfile.match_panes([{"guid": "G9", "tty": "ttys002"}], self.rows(), lambda r: r["session_id"] != "dead")
        self.assertIsNone(m["G9"])

    def test_parked_by_guid_when_the_live_row_is_dead(self):
        m = tabfile.match_panes([{"guid": "G1", "tty": "ttys001"}], self.rows(), lambda r: False)
        self.assertEqual(m["G1"]["session_id"], "parked")

    def test_parked_never_by_tty(self):
        rows = [r for r in self.rows() if r["session_id"] == "parked"]
        m = tabfile.match_panes([{"guid": "OTHER", "tty": "ttys001"}], rows, lambda r: True)
        self.assertIsNone(m["OTHER"])

    def test_ended_row_is_nobody(self):
        m = tabfile.match_panes([{"guid": "G3", "tty": "ttys003"}], self.rows(), lambda r: True)
        self.assertIsNone(m["G3"])


# ------------------------------------------------------------------ groups file

class GroupsFile(Base):
    def test_path_beside_the_ledger_and_configurable(self):
        self.assertEqual(os.path.dirname(tabgroups.path(self.cfg)), os.path.dirname(self.db))
        self.cfg["groups_file"] = os.path.join(self.tmp, "x.json")
        self.assertEqual(tabgroups.path(self.cfg), os.path.join(self.tmp, "x.json"))

    def test_missing_file_means_no_groups(self):
        doc, err = tabgroups.read(self.cfg)
        self.assertEqual((doc, err), (tabgroups.empty(), None))
        self.assertEqual(tabgroups.load(self.cfg), tabgroups.empty())

    def test_shape(self):
        tabgroups.add(self.cfg, "g", "s1", now=7.0)
        with open(tabgroups.path(self.cfg)) as fh:
            doc = json.load(fh)
        self.assertEqual(doc, {"groups": {"g": {"color": None}},
                               "sessions": {"s1": {"group": "g", "pinned": False, "at": 7.0}}})

    def test_write_is_atomic(self):
        tabgroups.add(self.cfg, "g", "s1")
        before = open(tabgroups.path(self.cfg)).read()
        with mock.patch.object(os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                tabgroups.add(self.cfg, "h", "s2")
        self.assertEqual(open(tabgroups.path(self.cfg)).read(), before)
        left = [n for n in os.listdir(os.path.dirname(tabgroups.path(self.cfg))) if n.startswith(".tmp-")]
        self.assertEqual(left, [])

    def test_unreadable_reads_as_no_groups_with_an_error(self):
        for raw in ("{not json", "[]", '{"groups": []}', '{"sessions": 3}'):
            with open(tabgroups.path(self.cfg), "w") as fh:
                fh.write(raw)
            doc, err = tabgroups.read(self.cfg)
            self.assertEqual(doc, tabgroups.empty(), raw)
            self.assertIn("unreadable", err)

    def test_unreadable_is_never_overwritten(self):
        raw = "{not json"
        with open(tabgroups.path(self.cfg), "w") as fh:
            fh.write(raw)
        for change in (lambda: tabgroups.add(self.cfg, "g", "s1"), lambda: tabgroups.remove(self.cfg, "s1"),
                       lambda: tabgroups.rename(self.cfg, "a", "b"), lambda: tabgroups.delete(self.cfg, "a"),
                       lambda: tabgroups.set_color(self.cfg, "a", None), lambda: tabgroups.set_pinned(self.cfg, "s", True),
                       lambda: tabgroups.toggle_pinned(self.cfg, "s")):
            with self.assertRaises(tabgroups.GroupsError):
                change()
        self.assertEqual(open(tabgroups.path(self.cfg)).read(), raw)

    def test_new_group_has_no_color(self):
        tabgroups.add(self.cfg, "g", "s1")
        doc = tabgroups.load(self.cfg)
        self.assertIsNone(doc["groups"]["g"]["color"])
        self.assertIsNone(tabgroups.color_of(doc, "s1"))

    def test_rename_keeps_members_and_color(self):
        tabgroups.add(self.cfg, "old", "s1")
        tabgroups.set_color(self.cfg, "old", "#112233")
        tabgroups.rename(self.cfg, "old", "new")
        doc = tabgroups.load(self.cfg)
        self.assertEqual(list(doc["groups"]), ["new"])
        self.assertEqual(tabgroups.group_of(doc, "s1"), "new")
        self.assertEqual(tabgroups.color_of(doc, "s1"), "#112233")

    def test_rename_refusals(self):
        tabgroups.add(self.cfg, "a", "s1")
        tabgroups.add(self.cfg, "b", "s2")
        with self.assertRaises(tabgroups.GroupsError):
            tabgroups.rename(self.cfg, "a", "b")
        with self.assertRaises(tabgroups.GroupsError):
            tabgroups.rename(self.cfg, "missing", "c")
        with self.assertRaises(tabgroups.GroupsError):
            tabgroups.rename(self.cfg, "a", "  ")
        doc = tabgroups.load(self.cfg)
        self.assertEqual(tabgroups.group_of(doc, "s1"), "a")

    def test_delete_ungroups_members_keeps_pins(self):
        tabgroups.add(self.cfg, "g", "s1")
        tabgroups.add(self.cfg, "g", "s2")
        tabgroups.add(self.cfg, "other", "s3")
        tabgroups.set_pinned(self.cfg, "s1", True)
        self.assertEqual(tabgroups.delete(self.cfg, "g"), ["s1", "s2"])
        doc = tabgroups.load(self.cfg)
        self.assertEqual(list(doc["groups"]), ["other"])
        self.assertIsNone(tabgroups.group_of(doc, "s1"))
        self.assertTrue(tabgroups.is_pinned(doc, "s1"))
        self.assertEqual(tabgroups.group_of(doc, "s3"), "other")
        with self.assertRaises(tabgroups.GroupsError):
            tabgroups.delete(self.cfg, "g")

    def test_color_values(self):
        self.assertEqual(tabgroups.parse_color("#AABBCC"), "#aabbcc")
        self.assertIsNone(tabgroups.parse_color("none"))
        for bad in ("red", "#abc", "#gggggg", ""):
            with self.assertRaises(tabgroups.GroupsError):
                tabgroups.parse_color(bad)
        self.assertEqual(tabgroups.rgb("#0a0b0c"), (10, 11, 12))

    def test_pins_and_toggle(self):
        tabgroups.set_pinned(self.cfg, "s", True)
        self.assertTrue(tabgroups.is_pinned(tabgroups.load(self.cfg), "s"))
        self.assertFalse(tabgroups.toggle_pinned(self.cfg, "s"))
        self.assertTrue(tabgroups.toggle_pinned(self.cfg, "s"))

    def test_pin_keeps_group_and_group_keeps_pin(self):
        tabgroups.add(self.cfg, "g", "s")
        tabgroups.set_pinned(self.cfg, "s", True)
        tabgroups.remove(self.cfg, "s")
        doc = tabgroups.load(self.cfg)
        self.assertTrue(tabgroups.is_pinned(doc, "s"))
        self.assertIsNone(tabgroups.group_of(doc, "s"))

    def test_listing_uses_no_nulls(self):
        tabgroups.add(self.cfg, "b", "s1")
        tabgroups.add(self.cfg, "a", "s2")
        tabgroups.set_color(self.cfg, "a", "#010203")
        self.assertEqual(tabgroups.listing(tabgroups.load(self.cfg)),
                         [{"name": "a", "color": "#010203", "members": 1}, {"name": "b", "color": "", "members": 1}])


# ------------------------------------------------------------------ sort on fake windows

class ToolEnv(Base):
    def setUp(self):
        super().setUp()
        self.proc = {}
        for name, value in (("identity_probe", self.fake_probe),):
            p = mock.patch.object(procs, name, value)
            p.start()
            self.addCleanup(p.stop)
        ps = mock.patch.object(procs, "ps_commands", lambda: {})
        ps.start()
        self.addCleanup(ps.stop)
        self.lines = []

    def fake_probe(self, pid, start, **kw):
        return self.proc.get(pid, procs.GONE)

    def live(self, sid, n, state="idle", **kw):
        pid = kw.pop("pid", 1000 + n)
        self.proc[pid] = procs.ALIVE
        kw.setdefault("state_since", time.time() - 3600)
        return self.insert(session_id=sid, tty="ttys%03d" % n, pid=pid, pid_start=1, state=state, **kw)

    def hibernated(self, sid, n, **kw):
        kw.setdefault("started_at", 1.0)
        kw.setdefault("last_event_at", 1.0)
        return self.insert(session_id=sid, tty="ttys%03d" % n, pid=None, pid_start=None, state="hibernated",
                           iterm_guid="G%d" % n, **kw)


class Sort(ToolEnv):
    def do_sort(self, win, dry_run=False):
        doc = tabgroups.load(self.cfg)
        return arun(T.sort_window(self.cfg, self.conn, win, doc, dry_run, self.lines.append))

    def names(self, win):
        return [t.tab_id for t in win.tabs]

    def test_full_order(self):
        win = Window([tab(i) for i in range(1, 9)])
        self.hibernated("old", 1, started_at=100.0)
        self.live("idle", 2, started_at=200.0)
        self.live("work", 3, subagents=1, started_at=30.0)  # older than the rest: only its bucket lifts it
        self.live("need", 4, state="waiting", started_at=50.0)
        self.live("pin", 5, started_at=10.0)
        tabgroups.set_pinned(self.cfg, "pin", True)
        self.live("g1", 6, started_at=150.0)
        self.hibernated("g2", 7, started_at=20.0)
        tabgroups.add(self.cfg, "grp", "g1")
        tabgroups.add(self.cfg, "grp", "g2")
        # tab 8 has no session
        self.do_sort(win)
        self.assertEqual(self.names(win), ["T5", "T4", "T3", "T2", "T6", "T7", "T1", "T8"])

    def test_a_woken_old_session_ranks_by_its_transcript_not_the_wake(self):
        win = Window([tab(1), tab(2)])
        old = transcript_at(self.tmp, "old.jsonl", time.time() - 50 * 86400)
        newer = transcript_at(self.tmp, "newer.jsonl", time.time() - 40 * 86400)
        self.hibernated("old", 1, transcript=old, resumed_at=time.time(), started_at=1.0)
        self.hibernated("newer", 2, transcript=newer, started_at=1.0)
        self.do_sort(win)
        self.assertEqual(self.names(win), ["T2", "T1"])

    def test_an_idle_row_with_a_subagent_goes_above_newer_idle_rows(self):
        win = Window([tab(1), tab(2)])
        self.live("newer", 1, started_at=900.0)
        self.live("sub", 2, subagents=1, started_at=10.0)
        self.do_sort(win)
        self.assertEqual(self.names(win), ["T2", "T1"])

    def test_an_idle_row_with_a_monitor_goes_above_newer_idle_rows(self):
        win = Window([tab(1), tab(2)])
        t = time.time()
        tool = {"type": "tool_use", "id": "m1", "name": "Monitor", "input": {"command": "tail -f x", "timeout_ms": 3600000}}
        tr = self.transcript("mon", [user("x", t=t - 60), assistant("watching", t=t - 50, tools=[tool])])
        quiet = self.transcript("newer", [user("x", t=t - 60), assistant("done", t=t - 50)])
        self.live("newer", 1, started_at=900.0, transcript=quiet)
        self.live("mon", 2, started_at=10.0, transcript=tr)
        self.do_sort(win)
        self.assertEqual(self.names(win), ["T2", "T1"])

    def test_subagent_finishing_counts_as_activity(self):
        win = Window([tab(1), tab(2)])
        self.live("a", 1, started_at=500.0)
        self.live("b", 2, started_at=100.0)
        self.event("b", "SubagentStop", 900.0)
        self.do_sort(win)
        self.assertEqual(self.names(win), ["T2", "T1"])

    def test_dry_run_prints_and_moves_nothing(self):
        win = Window([tab(1), tab(2)])
        self.live("a", 1, started_at=100.0, title="older")
        self.live("b", 2, started_at=500.0, title="newer")
        self.assertEqual(self.do_sort(win, dry_run=True), 2)
        self.assertEqual(win.set_tabs_calls, [])
        self.assertEqual(self.names(win), ["T1", "T2"])
        self.assertIn("newer", self.lines[0])
        self.assertIn("*", self.lines[0])

    def test_already_sorted_makes_no_call(self):
        win = Window([tab(1), tab(2)])
        self.live("a", 1, started_at=500.0)
        self.live("b", 2, started_at=100.0)
        self.assertEqual(self.do_sort(win), 0)
        self.assertEqual(win.set_tabs_calls, [])

    def test_sort_never_selects_a_tab(self):
        win = Window([tab(1), tab(2)], current=1)
        before = win.current_tab
        self.live("a", 1, started_at=100.0)
        self.live("b", 2, started_at=500.0)
        self.do_sort(win)
        self.assertIs(win.current_tab, before)
        self.assertFalse(any(s.activated for t in win.tabs for s in t.sessions))

    def test_a_dead_live_row_on_a_reused_tty_is_a_non_claude_tab(self):
        win = Window([tab(1), tab(2)])
        self.insert(session_id="dead", tty="ttys001", pid=99, pid_start=1, state="busy", started_at=900.0)
        self.live("real", 2, started_at=100.0)
        self.do_sort(win)
        self.assertEqual(self.names(win), ["T2", "T1"])

    def test_cmd_applies_colors_and_dry_run_does_not(self):
        win = Window([tab(1)])
        app = App([win])
        self.live("a", 1)
        tabgroups.add(self.cfg, "g", "a")
        tabgroups.set_color(self.cfg, "g", "#010203")
        s = win.tabs[0].sessions[0]
        with mock.patch.object(T, "make_color", lambda c: "C" + c):
            arun(T.cmd_sort_app(self.cfg, self.conn, app, mock.Mock(all=False, dry_run=True), self.lines.append))
            self.assertEqual(s.colors, [])
            self.assertEqual(self.lines[-1], "would move 0 tabs")
            arun(T.cmd_sort_app(self.cfg, self.conn, app, mock.Mock(all=False, dry_run=False), self.lines.append))
        self.assertEqual(s.colors, ["C#010203", ("use", True)])
        self.assertEqual(self.lines[-1], "moved 0 tabs")

    def test_all_sorts_every_window_default_only_the_current(self):
        w1, w2 = Window([tab(1), tab(2)]), Window([tab(3), tab(4)])
        app = App([w1, w2])
        self.live("a", 1, started_at=1.0)
        self.live("b", 2, started_at=9.0)
        self.live("c", 3, started_at=1.0)
        self.live("d", 4, started_at=9.0)
        arun(T.cmd_sort_app(self.cfg, self.conn, app, mock.Mock(all=False, dry_run=False), self.lines.append))
        self.assertEqual((self.names(w1), self.names(w2)), (["T2", "T1"], ["T3", "T4"]))
        arun(T.cmd_sort_app(self.cfg, self.conn, app, mock.Mock(all=True, dry_run=False), self.lines.append))
        self.assertEqual(self.names(w2), ["T4", "T3"])

    def test_not_running_never_asks_iterm2(self):
        self.current = None
        with mock.patch.object(T, "with_app") as app, mock.patch.object(T, "request_cookie") as cookie:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(T.cmd_sort(self.cfg, self.conn, mock.Mock()), 0)
        app.assert_not_called()
        cookie.assert_not_called()

    def test_each_process_probed_once_a_pass(self):
        calls = []
        self.live("a", 1)
        check = T.memo_running()
        with mock.patch.object(T, "running", lambda r: calls.append(r) or True):
            check({"pid": 1, "pid_start": 1})
            check({"pid": 1, "pid_start": 1})
            check({"pid": 2, "pid_start": 1})
        self.assertEqual(len(calls), 2)


# ------------------------------------------------------------------ B2 find

class Find(ToolEnv):
    def rows(self, live_guids=frozenset()):
        return T.find_data(self.cfg, self.conn, set(live_guids))

    def test_a_woken_old_session_shows_its_transcript_time(self):
        when = int(time.time() - 50 * 86400)
        self.hibernated("h", 1, transcript=transcript_at(self.tmp, "h.jsonl", when), resumed_at=time.time(),
                        started_at=50.0)
        self.assertEqual(self.rows()[0]["activity"], float(when))

    def test_fields(self):
        self.hibernated("h", 1, title="parked one", cwd="/some/path/proj", started_at=50.0)
        tabgroups.add(self.cfg, "g", "h")
        tabgroups.set_pinned(self.cfg, "h", True)
        self.assertEqual(self.rows({"G1"}), [{
            "session_id": "h", "title": "parked one", "activity": 50.0, "state": "hibernated", "group": "g",
            "pinned": True, "folder": "proj", "tab_open": True}])

    def test_tab_open_is_guid_in_live_tabs(self):
        self.hibernated("h", 1)
        self.assertFalse(self.rows()[0]["tab_open"])
        self.assertFalse(T.find_data(self.cfg, self.conn, None)[0]["tab_open"])

    def test_who_is_listed(self):
        self.hibernated("h", 1)
        self.live("l", 2)
        self.live("w", 3, state="waiting")
        self.live("b", 4, state="busy")
        self.insert(session_id="headless", tty="t", interactive=0)
        self.insert(session_id="sup", tty="t", state="superseded")
        self.insert(session_id="end", tty="t", state="ended", ended_at=1.0)
        self.insert(session_id="evicting", tty="t", state="evicting")
        self.assertEqual(sorted(d["session_id"] for d in self.rows()), ["b", "h", "l", "w"])

    def test_newest_first_by_activity(self):
        self.hibernated("a", 1, started_at=10.0)
        self.hibernated("b", 2, started_at=20.0)
        self.hibernated("c", 3, started_at=5.0)
        self.event("c", "Stop", 90.0)
        self.assertEqual([d["session_id"] for d in self.rows()], ["c", "b", "a"])

    def test_title_falls_back(self):
        self.hibernated("abcdefghijkl", 1, title=None, cwd=None)
        self.assertEqual(self.rows()[0]["title"], "abcdefgh")
        self.conn.execute("UPDATE sessions SET cwd='/x/repo'")
        self.assertEqual(self.rows()[0]["title"], "repo")

    def test_no_group_is_an_empty_string_json_has_no_null(self):
        self.hibernated("h", 1, title="t", cwd="/x/y")
        out = json.dumps(self.rows())
        self.assertNotIn("null", out)

    def test_cli_json_needs_no_iterm2_package(self):
        self.hibernated("h", 1)
        self.current = None
        buf = io.StringIO()
        with mock.patch.object(restore, "iterm_guids", lambda: None), mock.patch.object(T, "need_iterm2") as need, \
                contextlib.redirect_stdout(buf):
            self.assertEqual(T.main(["find", "--json"]), 0)
        need.assert_not_called()
        self.assertEqual(json.loads(buf.getvalue())[0]["session_id"], "h")

    def test_unreadable_groups_still_lists(self):
        self.hibernated("h", 1)
        with open(tabgroups.path(self.cfg), "w") as fh:
            fh.write("{bad")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.rows()[0]["group"], "")
        self.assertIn("unreadable", err.getvalue())


# ------------------------------------------------------------------ B3 open

class Open(ToolEnv):
    def test_selects_the_tab_raises_the_window_and_activates(self):
        win = Window([tab(1), tab(2)])
        app = App([win])
        row = self.hibernated("h", 2)
        self.assertTrue(arun(T.open_in_app(app, row, T.open_rows(self.conn))))
        self.assertEqual(win.tabs[1].sessions[0].activated, (True, True))
        self.assertEqual(app.activated, (False, True))

    def test_live_row_found_by_tty_with_its_process(self):
        win = Window([tab(1)])
        row = self.live("l", 1)
        self.assertTrue(arun(T.open_in_app(App([win]), row, T.open_rows(self.conn))))

    def test_tab_gone_answers_false(self):
        row = self.hibernated("h", 7)
        self.assertFalse(arun(T.open_in_app(App([Window([tab(1)])]), row, T.open_rows(self.conn))))

    def wake_requests(self):
        return [(r[0], r[1]) for r in self.conn.execute("SELECT kind, session_id FROM requests")]

    def test_tab_gone_queues_the_wake_request(self):
        self.hibernated("h", 7)

        def tab_gone(cfg, fn):
            return arun(fn(App([Window([tab(1)])])))
        with mock.patch.object(T, "with_app", tab_gone), mock.patch.object(T, "need_iterm2"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(T.main(["open", "h"]), 0)
        self.assertEqual(self.wake_requests(), [("wake", "h")])

    def test_tab_open_queues_nothing(self):
        self.hibernated("h", 2)

        def has_tab(cfg, fn):
            return arun(fn(App([Window([tab(1), tab(2)], current=0)])))
        with mock.patch.object(T, "with_app", has_tab), mock.patch.object(T, "need_iterm2"):
            self.assertEqual(T.main(["open", "h"]), 0)
        self.assertEqual(self.wake_requests(), [])

    def open_with(self, app):
        def run_it(cfg, fn):
            return arun(fn(app))
        with mock.patch.object(T, "with_app", run_it), mock.patch.object(T, "need_iterm2"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return T.main(["open", self.sid])

    def test_hibernated_tab_already_selected_queues_the_wake_and_still_raises(self):
        self.sid = "h"
        self.hibernated("h", 1)
        win = Window([tab(1), tab(2)], current=0)
        app = App([win])
        self.assertEqual(self.open_with(app), 0)
        self.assertEqual(self.wake_requests(), [("wake", "h")])
        self.assertEqual(win.tabs[0].sessions[0].activated, (True, True))
        self.assertEqual(app.activated, (False, True))

    def test_selection_is_read_after_a_refresh(self):
        self.sid = "h"
        self.hibernated("h", 2)
        win = Window([tab(1), tab(2)], current=0)
        app = App([win])
        app.on_refresh = lambda n: setattr(win, "selected", win.tabs[1])
        self.assertEqual(self.open_with(app), 0)
        self.assertEqual(self.wake_requests(), [("wake", "h")])

    def test_live_row_already_selected_queues_nothing(self):
        self.sid = "l"
        self.live("l", 1)
        self.assertEqual(self.open_with(App([Window([tab(1)], current=0)])), 0)
        self.assertEqual(self.wake_requests(), [])

    def test_a_resuming_row_already_selected_queues_nothing(self):
        self.sid = "h"
        self.hibernated("h", 1)
        self.conn.execute("UPDATE sessions SET state='resuming'")
        self.assertEqual(self.open_with(App([Window([tab(1)], current=0)])), 0)
        self.assertEqual(self.wake_requests(), [])

    def test_selected_tab_with_another_pane_active_gets_a_focus_event_instead(self):
        self.sid = "h"
        self.hibernated("h", 1)
        t = tab(1)
        other = Session("G9", "ttys009")
        t.sessions.append(other)
        t.current_session = other
        self.assertEqual(self.open_with(App([Window([t], current=0)])), 0)
        self.assertEqual(self.wake_requests(), [])

    def test_iterm2_not_running_wakes_without_asking_it(self):
        self.hibernated("h", 7)
        self.current = None
        with mock.patch.object(T, "with_app") as app, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(T.main(["open", "h"]), 0)
        app.assert_not_called()
        self.assertEqual(self.wake_requests(), [("wake", "h")])

    def test_unknown_session(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(T, "need_iterm2"):
            self.assertEqual(T.main(["open", "nope"]), 1)
        self.assertIn("unknown", err.getvalue())


# ------------------------------------------------------------------ B4 close-idle

class CloseIdle(ToolEnv):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.foreground.update({"ttys001": "-zsh", "ttys002": "-zsh", "ttys003": "-zsh"})
        # tab 1 is selected and stays open throughout; tab 2 is the one under test
        self.win = Window([tab(1), tab(2)], current=0)
        self.app = App([self.win])
        self.target = self.win.tabs[1].sessions[0]
        self.old_ts = self.now - 30 * 86400

    def row(self, **kw):
        kw.setdefault("started_at", self.old_ts)
        return self.hibernated("h", 2, **kw)

    def run_close(self, days=7, dry_run=False):
        return arun(T.close_idle(self.cfg, self.conn, self.app, days, dry_run, self.lines.append))

    def assertCloses(self):
        self.assertEqual(self.run_close(), 1)
        self.assertEqual(self.target.closed, True)

    def assertRefused(self):
        self.assertEqual(self.run_close(), 0)
        self.assertFalse(self.target.closed)

    def test_recent_work_in_the_transcript_keeps_the_tab_open(self):
        self.row(transcript=transcript_at(self.tmp, "h.jsonl", self.now - 3600))
        self.assertEqual(self.run_close(dry_run=True), 0)
        self.assertRefused()

    def test_old_work_in_the_transcript_closes_even_when_just_woken(self):
        self.row(transcript=transcript_at(self.tmp, "h.jsonl", self.now - 30 * 86400), resumed_at=self.now - 60)
        self.assertCloses()

    def test_all_conditions_hold_closes_with_force_and_keeps_the_row(self):
        self.row()
        self.assertCloses()
        self.assertEqual(ledger.get(self.conn, "h")["state"], "hibernated")
        self.assertEqual(self.lines[-1], "closed 1 tabs")

    def test_dry_run_prints_titles_and_closes_nothing(self):
        self.row(title="old work")
        self.assertEqual(self.run_close(dry_run=True), 1)
        self.assertFalse(self.target.closed)
        self.assertEqual(self.lines, ["  old work", "would close 1 tabs"])

    def test_refuses_a_row_that_is_not_hibernated(self):
        for state in ("idle", "busy", "waiting", "resuming", "evicting"):
            with self.subTest(state=state):
                self.conn.execute("DELETE FROM sessions")
                self.insert(session_id="h", tty="ttys999", pid=None, state=state, iterm_guid="G2", started_at=self.old_ts)
                self.assertRefused()

    def test_refuses_a_live_row_on_the_tty_over_a_stale_parked_guid(self):
        self.row()
        self.insert(session_id="newer", tty="ttys002", pid=5, state="idle", started_at=self.old_ts)
        self.assertRefused()

    def test_refuses_an_alive_process(self):
        self.proc[77] = procs.ALIVE
        self.conn.execute("DELETE FROM sessions")
        self.insert(session_id="h", tty="ttys002", pid=77, pid_start=1, state="hibernated", iterm_guid="G2",
                    started_at=self.old_ts)
        self.assertRefused()

    def test_refuses_an_unknown_process(self):
        self.proc[77] = procs.UNKNOWN
        self.insert(session_id="h", tty="ttys002", pid=77, pid_start=None, state="hibernated", iterm_guid="G2",
                    started_at=self.old_ts)
        self.assertRefused()

    def test_refuses_recent_activity(self):
        self.row()
        self.event("h", "SubagentStop", self.now - 86400)
        self.assertRefused()

    def test_days_flag_moves_the_line(self):
        self.row(started_at=self.now - 3 * 86400)
        self.assertEqual(self.run_close(days=7, dry_run=True), 0)
        self.assertEqual(self.run_close(days=2, dry_run=True), 1)

    def test_refuses_no_activity_at_all(self):
        self.row(started_at=None)
        self.assertRefused()

    def test_refuses_pinned(self):
        self.row()
        tabgroups.set_pinned(self.cfg, "h", True)
        self.assertRefused()

    def test_refuses_grouped(self):
        self.row()
        tabgroups.add(self.cfg, "g", "h")
        self.assertRefused()

    def test_refuses_any_foreground_job_but_a_shell(self):
        self.row()
        for job in ("claude", "node", "vim", None):
            with self.subTest(job=job):
                self.foreground["ttys002"] = job
                self.assertRefused()

    def test_every_shell_name_closes(self):
        for job in ("zsh", "-zsh", "bash", "fish"):
            self.assertIn(job, procs.SHELL_JOBS)

    def test_refuses_the_current_tab(self):
        self.row()
        self.win.selected = self.win.tabs[1]
        self.assertRefused()

    def test_refuses_the_selected_tab_of_every_window(self):
        other = Window([tab(3)], current=0)
        self.app.terminal_windows.append(other)
        self.hibernated("x", 3, started_at=self.old_ts)
        self.assertEqual(self.run_close(), 0)
        self.assertFalse(other.tabs[0].sessions[0].closed)

    def test_refuses_a_window_with_no_known_selection(self):
        self.row()
        self.win.selected = None
        self.assertRefused()

    def test_refuses_a_split_tab(self):
        self.row()
        self.win.tabs[1].sessions.append(Session("G9", "ttys009"))
        self.assertRefused()

    def test_refuses_a_tab_with_no_session(self):
        self.assertRefused()

    def test_unreadable_groups_file_closes_nothing(self):
        self.row()
        with open(tabgroups.path(self.cfg), "w") as fh:
            fh.write("{bad")
        for dry_run in (True, False):
            with self.assertRaises(tabgroups.GroupsError):
                self.run_close(dry_run=dry_run)
        self.assertFalse(self.target.closed)

    def test_recheck_right_before_closing(self):
        """Each thing that can change between the listing and the close keeps the tab."""
        self.row()
        changes = {
            "state": lambda: self.conn.execute("UPDATE sessions SET state='resuming'"),
            "activity": lambda: self.event("h", "Stop", time.time()),
            "pinned": lambda: tabgroups.set_pinned(self.cfg, "h", True),
            "group": lambda: tabgroups.add(self.cfg, "g", "h"),
            "job": lambda: self.foreground.update({"ttys002": "claude"}),
            "selected": lambda: setattr(self.win, "selected", self.win.tabs[1]),
            "tty": lambda: (setattr(self.target, "tty", "ttys099"), self.foreground.update({"ttys099": "claude"})),
        }
        real = T.pane_tty
        for name, change in changes.items():
            with self.subTest(change=name):
                self.conn.execute("UPDATE sessions SET state='hibernated'")
                self.conn.execute("DELETE FROM events")
                if os.path.exists(tabgroups.path(self.cfg)):
                    os.unlink(tabgroups.path(self.cfg))
                self.foreground["ttys002"] = "-zsh"
                self.target.tty = "ttys002"
                self.win.selected = self.win.tabs[0]
                asked = []

                # the target's tty is read once listing and again in the recheck: change in between
                async def then_change(session, change=change, asked=asked):
                    if session is self.target:
                        asked.append(1)
                        if len(asked) == 2:
                            change()
                    return await real(session)
                with mock.patch.object(T, "pane_tty", then_change):
                    self.assertEqual(self.run_close(), 0, name)
                self.assertEqual(len(asked), 2, name)
                self.assertFalse(self.target.closed, name)
                self.assertIn("kept", self.lines[-2])

    def second_target(self):
        self.foreground["ttys003"] = "-zsh"
        self.win.tabs.append(tab(3))
        self.hibernated("h3", 3, started_at=self.old_ts)
        return self.win.tabs[2].sessions[0]

    def test_only_closes_at_most_those(self):
        self.row()
        extra = self.second_target()
        self.assertEqual(arun(T.close_idle(self.cfg, self.conn, self.app, 7, False, self.lines.append,
                                           only={"h"})), 1)
        self.assertTrue(self.target.closed)
        self.assertFalse(extra.closed)
        self.assertEqual(self.lines[-1], "closed 1 tabs")

    def test_only_with_no_ids_closes_nothing(self):
        self.row()
        self.assertEqual(arun(T.close_idle(self.cfg, self.conn, self.app, 7, False, self.lines.append,
                                           only=set())), 0)
        self.assertFalse(self.target.closed)

    def test_only_does_not_excuse_a_refusal(self):
        self.row()
        tabgroups.set_pinned(self.cfg, "h", True)
        self.assertEqual(arun(T.close_idle(self.cfg, self.conn, self.app, 7, False, self.lines.append,
                                           only={"h"})), 0)
        self.assertFalse(self.target.closed)

    def cli(self, *argv):
        def run_it(cfg, fn):
            return arun(fn(self.app))
        buf = io.StringIO()
        with mock.patch.object(T, "with_app", run_it), mock.patch.object(T, "need_iterm2"), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            return T.main(list(argv)), buf.getvalue()

    def test_cli_dry_run_json_names_the_sessions(self):
        self.row()
        self.second_target()
        rc, out = self.cli("close-idle", "--dry-run", "--json")
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual((d["count"], sorted(d["session_ids"])), (2, ["h", "h3"]))
        self.assertFalse(self.target.closed)

    def test_cli_a_tab_that_qualifies_after_the_dry_run_stays_open(self):
        self.row()
        ids = json.loads(self.cli("close-idle", "--dry-run", "--json")[1])["session_ids"]
        self.assertEqual(ids, ["h"])
        extra = self.second_target()
        rc, out = self.cli("close-idle", "--only", ",".join(ids))
        self.assertEqual((rc, out.strip().splitlines()[-1]), (0, "closed 1 tabs"))
        self.assertTrue(self.target.closed)
        self.assertFalse(extra.closed)

    def test_cli_json_needs_the_dry_run(self):
        self.row()
        self.assertEqual(self.cli("close-idle", "--json")[0], 2)
        self.assertFalse(self.target.closed)

    def test_cli_json_with_iterm2_stopped(self):
        self.current = None
        rc, out = self.cli("close-idle", "--dry-run", "--json")
        self.assertEqual((rc, json.loads(out)), (0, {"count": 0, "session_ids": []}))

    def test_recheck_refuses_while_a_restore_holds_the_lock(self):
        self.row()
        real, asked, locks = T.pane_tty, [], []

        async def lock_in_between(session):
            if session is self.target:
                asked.append(1)
                if len(asked) == 2:
                    locks.append(tabfile.restore_lock(self.cfg))
            return await real(session)
        with mock.patch.object(T, "pane_tty", lock_in_between):
            self.assertEqual(self.run_close(), 0)
        self.addCleanup(locks[0].close)
        self.assertFalse(self.target.closed)
        self.assertIn("a restore is running", self.lines[-2])

    def test_listing_reads_the_model_after_a_refresh(self):
        self.row()
        self.app.on_refresh = lambda n: setattr(self.win, "selected", self.win.tabs[1])
        self.assertEqual(self.run_close(dry_run=True), 0)

    def test_each_recheck_reads_the_model_after_its_own_refresh(self):
        self.row()
        self.second_target()

        def select_later(n):
            if n == 3:  # listing, tab 2's recheck, then tab 3's recheck
                self.win.selected = self.win.tabs[2]
        self.app.on_refresh = select_later
        self.assertEqual(self.run_close(), 1)
        self.assertEqual(self.app.refreshes, 3)
        self.assertTrue(self.target.closed)
        self.assertFalse(self.win.tabs[2].sessions[0].closed)

    def test_a_refresh_that_fails_closes_nothing(self):
        self.row()

        def boom(n):
            if n == 2:
                raise RuntimeError("no answer")
        self.app.on_refresh = boom
        with self.assertRaises(RuntimeError):
            self.run_close()
        self.assertFalse(self.target.closed)

    def test_a_tab_dragged_to_another_window_is_judged_by_that_window(self):
        self.row()
        moved = self.win.tabs[1]
        other = Window([tab(4)], current=0)

        def drag(n):
            if n == 2:
                self.win.tabs.remove(moved)
                other.tabs.append(moved)
                other.selected = moved
                self.app.terminal_windows.append(other)
        self.app.on_refresh = drag
        self.assertEqual(self.run_close(), 0)
        self.assertFalse(self.target.closed)

    def test_a_tab_gone_from_the_model_is_not_closed(self):
        self.row()
        moved = self.win.tabs[1]
        self.app.on_refresh = lambda n: self.win.tabs.remove(moved) if n == 2 else None
        self.assertEqual(self.run_close(), 0)
        self.assertFalse(self.target.closed)
        self.assertIn("gone", self.lines[-2])

    def test_a_close_that_fails_is_reported_and_the_rest_go_on(self):
        self.foreground["ttys003"] = "-zsh"
        self.win.tabs.append(tab(3))
        self.row()
        self.hibernated("h3", 3, started_at=self.old_ts)
        self.target.async_close = mock.Mock(side_effect=RuntimeError("no"))
        self.assertEqual(self.run_close(), 1)
        self.assertTrue(self.win.tabs[2].sessions[0].closed)
        self.assertTrue(any("could not close" in l for l in self.lines))

    def test_cli_not_running_never_asks_iterm2(self):
        self.current = None
        buf = io.StringIO()
        with mock.patch.object(T, "with_app") as app, mock.patch.object(T, "need_iterm2") as need, \
                contextlib.redirect_stdout(buf):
            self.assertEqual(T.main(["close-idle", "--dry-run"]), 0)
        app.assert_not_called()
        need.assert_not_called()
        self.assertEqual(buf.getvalue().strip(), "would close 0 tabs")

    def test_cli_refuses_during_a_restore(self):
        lock = tabfile.restore_lock(self.cfg)
        self.addCleanup(lock.close)
        with mock.patch.object(T, "with_app") as app, mock.patch.object(T, "need_iterm2"), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(T.main(["close-idle"]), 1)
        app.assert_not_called()

    def test_refusal_fn_directly_for_each_reason(self):
        row = {"state": "hibernated", "pid": None, "pid_start": None}
        gone = lambda pid, start: procs.GONE
        shell = lambda: "zsh"

        def why(row=row, panes=1, current=False, entry=None, last=1.0, probe=gone, job=shell):
            return activity.close_refusal(row, panes, current, 100 * 86400, 7, entry or {}, last, probe, job)
        self.assertIsNone(why())
        self.assertEqual(why(current=True), "current tab")
        self.assertEqual(why(panes=2), "split tab")
        self.assertEqual(why(row=None), "no session")
        self.assertEqual(why(row=dict(row, state="idle")), "state=idle")
        self.assertIn("active", why(last=100 * 86400 - 5))
        self.assertIn("active", why(last=0))
        self.assertEqual(why(entry={"pinned": True}), "pinned")
        self.assertEqual(why(entry={"group": "g"}), "in group g")
        self.assertIn("process", why(probe=lambda p, s: procs.ALIVE))
        self.assertIn("process", why(probe=lambda p, s: procs.UNKNOWN))
        self.assertEqual(why(job=lambda: "node"), "foreground job node")
        self.assertEqual(why(job=lambda: None), "foreground job unknown")


class SelectedSession(ToolEnv):
    def test_the_selected_tab_is_read_after_a_refresh(self):
        self.hibernated("a", 1)
        self.hibernated("b", 2)
        win = Window([tab(1), tab(2)], current=0)
        app = App([win])
        app.on_refresh = lambda n: setattr(win, "selected", win.tabs[1])
        self.assertEqual(arun(T.selected_session(app, self.conn)), "b")


class IdleEstimate(ToolEnv):
    """The menu title's count: no API. Each qualifying condition, and what it asks."""

    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.old_ts = self.now - 30 * 86400
        self.guids = {"G2", "G3"}
        self.asked = 0
        p = mock.patch.object(restore, "iterm_guids", self.fake_guids)
        p.start()
        self.addCleanup(p.stop)

    def fake_guids(self):
        self.asked += 1
        return self.guids

    def row(self, sid="h", n=2, **kw):
        kw.setdefault("started_at", self.old_ts)
        return self.hibernated(sid, n, **kw)

    def data(self, **kw):
        kw.setdefault("idle_days", 7)
        return cli.tabs_data(self.cfg, self.conn, **kw)

    def count(self):
        d = self.data()
        self.assertTrue(d["idle_tabs_known"])
        return d["idle_tabs"]

    def test_counts_what_qualifies(self):
        self.row()
        self.row("h3", 3)
        self.assertEqual(self.count(), 2)

    def test_tab_not_open(self):
        self.row("gone", 9)
        self.assertEqual(self.count(), 0)

    def test_not_hibernated(self):
        self.live("l", 2, started_at=self.old_ts, iterm_guid="G2")
        self.assertEqual(self.count(), 0)

    def test_live_pid(self):
        self.proc[77] = procs.ALIVE
        self.insert(session_id="h", tty="ttys002", pid=77, pid_start=1, state="hibernated", iterm_guid="G2",
                    started_at=self.old_ts)
        self.assertEqual(self.count(), 0)

    def test_recent_activity_and_the_days(self):
        self.row()
        self.event("h", "Stop", self.now - 86400)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.data(idle_days=0.5)["idle_tabs"], 1)

    def test_pinned_and_grouped(self):
        self.row()
        self.row("h3", 3)
        tabgroups.set_pinned(self.cfg, "h", True)
        tabgroups.add(self.cfg, "g", "h3")
        self.assertEqual(self.count(), 0)

    def test_unreadable_groups_file_counts_nothing(self):
        self.row()
        with open(tabgroups.path(self.cfg), "w") as fh:
            fh.write("{bad")
        self.assertEqual(self.count(), 0)

    def test_field_only_when_asked_for(self):
        self.assertNotIn("idle_tabs", cli.tabs_data(self.cfg, self.conn))

    def test_needs_no_tty_or_ps(self):
        self.row()
        with mock.patch.object(procs, "foreground_job", side_effect=AssertionError("ps")), \
                mock.patch.object(procs, "lstart_comm", side_effect=AssertionError("ps")):
            self.assertEqual(self.count(), 1)

    def test_no_candidate_asks_iterm2_nothing(self):
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.asked, 0)

    def test_one_applescript_call_for_both_numbers(self):
        self.row()
        self.snapshot_file(["h"])
        self.count()
        self.assertEqual(self.asked, 1)

    def test_no_ask_sends_nothing_and_says_so(self):
        self.row()
        self.snapshot_file(["h"])
        d = self.data(ask=False)
        self.assertEqual(self.asked, 0)
        self.assertEqual((d["idle_tabs"], d["idle_tabs_known"], d["pending_known"]), (0, False, False))

    def test_iterm2_stopped_needs_no_asking(self):
        self.row()
        self.current = None
        d = self.data(ask=False)
        self.assertEqual(self.asked, 0)
        self.assertEqual((d["idle_tabs"], d["idle_tabs_known"]), (0, True))

    def test_cli_flags(self):
        self.row()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["tabs", "--json", "--idle-days", "7", "--no-ask"])
        d = json.loads(buf.getvalue())
        self.assertEqual(d["idle_tabs_known"], False)
        self.assertEqual(self.asked, 0)


class RefreshNeedsNoApi(ToolEnv):
    """The real commands the menu runs every minute, in a child process with iTerm2 "running"
    (stub pgrep) and osascript stubbed: importing iterm2 at all, even inside a try, ends it."""
    BLOCK = ("import os, runpy, sys\n"
             "class Block:\n"
             "    def find_spec(self, name, path=None, target=None):\n"
             "        if name.split('.')[0] == 'iterm2':\n"
             "            os._exit(99)\n"
             "sys.meta_path.insert(0, Block())\n"
             "sys.argv = sys.argv[1:]\n"
             "runpy.run_path(sys.argv[0], run_name='__main__')\n")

    def setUp(self):
        super().setUp()
        self.stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(self.stubs)
        self.calls = os.path.join(self.tmp, "calls")
        self.stub("osascript", 'echo "osascript $*" >> "%s"\necho G2\n' % self.calls)
        self.stub("pgrep", 'echo "pgrep $*" >> "%s"\ncase "$1" in *q*) exit 0;; esac\necho "$PPID"\n' % self.calls)
        self.hibernated("h", 2, started_at=1.0)
        self.snapshot_file(["h"])

    def stub(self, name, body):
        path = os.path.join(self.stubs, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)

    def run_cmd(self, tool, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("ITERM2_")}
        env["PATH"] = self.stubs + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin"
        return subprocess.run([sys.executable, "-c", self.BLOCK, os.path.join(REPO, "bin", tool)] + list(args),
                              env=env, capture_output=True, text=True, timeout=60)

    def recorded(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as fh:
            return fh.read().splitlines()

    def test_tabs_json_asks_for_no_cookie(self):
        r = self.run_cmd("cc-sessions", "tabs", "--json", "--idle-days", "7")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual((d["idle_tabs"], d["idle_tabs_known"]), (1, True))
        scripts = [l for l in self.recorded() if l.startswith("osascript")]
        self.assertEqual(len(scripts), 1)
        self.assertNotIn("cookie", "".join(scripts))

    def test_tabs_json_no_ask_sends_no_applescript(self):
        r = self.run_cmd("cc-sessions", "tabs", "--json", "--idle-days", "7", "--no-ask")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["idle_tabs_known"], False)
        self.assertEqual([l for l in self.recorded() if l.startswith("osascript")], [])

    def test_group_list_needs_no_iterm2_and_asks_nothing(self):
        tabgroups.add(self.cfg, "g", "h")
        r = self.run_cmd("cc-tabs", "group", "list", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [{"name": "g", "color": "", "members": 1}])
        self.assertEqual(self.recorded(), [])

    def test_the_block_does_catch_a_command_that_needs_iterm2(self):
        r = self.run_cmd("cc-tabs", "close-idle", "--dry-run")
        self.assertEqual(r.returncode, 99, r.stderr)


class LibraryCall(TempEnv):
    def test_async_refresh_exists_and_takes_no_required_argument(self):
        try:
            from iterm2 import app as iterm2_app
        except ImportError:
            self.skipTest("the iterm2 package is not on this python")
        import inspect
        params = inspect.signature(iterm2_app.App.async_refresh).parameters
        self.assertEqual([n for n, p in params.items() if p.default is p.empty], ["self"])


class ShrinkSiblingUndoesAClose(Base):
    """README claim: closing more than half the tabs keeps the older file, and restoring it
    reopens the closed tabs. Real write_snapshot, real select_snapshot."""

    def windows(self, n):
        return [{"window_id": "W", "tabs": [{"tab_id": "T%d" % i, "sessions": [{"guid": "G%d" % i, "tty": None}]}
                                            for i in range(n)]}]

    def setUp(self):
        super().setUp()
        for i in range(1, 6):
            self.insert(session_id="s%d" % i, tty=None, pid=None, state="hibernated", iterm_guid="G%d" % i,
                        cwd=self.tmp, source="hook")
        self.rows = ledger.all_rows(self.conn)

    def test_closing_over_half_keeps_a_sibling_that_reopens_them(self):
        tabfile.write_snapshot(self.cfg, tabfile.build_snapshot(INSTANCE, self.windows(6), self.rows, now=100.0))
        tabfile.write_snapshot(self.cfg, tabfile.build_snapshot(INSTANCE, self.windows(1), self.rows, now=200.0))
        entries = tabfile.list_snapshots(self.cfg)
        sibling = [e for e in entries if e["sibling"]]
        self.assertEqual(len(sibling), 1)
        self.assertEqual(tabfile.tab_count(sibling[0]["snap"]), 6)
        run, typed, skipped, _ = restore.select_snapshot(
            sibling[0]["snap"], self.rows, self.cfg, lambda r: False, {"G0"},
            until=tabfile.next_instance_start(self.cfg, sibling[0]["snap"], INSTANCE, entries))
        self.assertEqual(sorted(r["session_id"] for r in typed), ["s1", "s2", "s3", "s4", "s5"])
        self.assertEqual(run, [])

    def test_closing_under_half_keeps_no_sibling(self):
        tabfile.write_snapshot(self.cfg, tabfile.build_snapshot(INSTANCE, self.windows(6), self.rows, now=100.0))
        tabfile.write_snapshot(self.cfg, tabfile.build_snapshot(INSTANCE, self.windows(4), self.rows, now=200.0))
        self.assertEqual([e for e in tabfile.list_snapshots(self.cfg) if e["sibling"]], [])


# ------------------------------------------------------------------ B5 commands, colors

class GroupCommands(ToolEnv):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(T, "need_iterm2"), mock.patch.object(T, "make_color", lambda c: c), \
                mock.patch.object(T, "with_app", lambda cfg, fn: arun(fn(self.app))):
            rc = T.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def setUp(self):
        super().setUp()
        self.win = Window([tab(1), tab(2)], current=0)
        self.app = App([self.win])
        self.live("selected", 1)
        self.live("other", 2)

    def doc(self):
        return tabgroups.load(self.cfg)

    def colors(self, n):
        return self.win.tabs[n].sessions[0].colors

    def test_default_session_is_the_selected_tab(self):
        self.assertEqual(self.main("group", "add", "g")[0], 0)
        self.assertEqual(tabgroups.group_of(self.doc(), "selected"), "g")
        self.assertIsNone(tabgroups.group_of(self.doc(), "other"))

    def test_session_flag_wins_and_resolves_a_prefix(self):
        self.assertEqual(self.main("group", "add", "g", "--session", "oth")[0], 0)
        self.assertEqual(tabgroups.group_of(self.doc(), "other"), "g")

    def test_selected_tab_without_a_session_is_an_error(self):
        self.win.selected = tab(9)
        rc, _, err = self.main("group", "add", "g")
        self.assertEqual(rc, 1)
        self.assertIn("no Claude Code session", err)
        self.assertEqual(self.doc(), tabgroups.empty())

    def test_no_default_color(self):
        self.main("group", "add", "g")
        self.assertEqual(self.colors(0), [])

    def test_color_paints_members_and_none_clears(self):
        self.main("group", "add", "g")
        self.assertEqual(self.main("group", "color", "g", "#AABBCC")[0], 0)
        self.assertEqual(self.colors(0), ["#aabbcc", ("use", True)])
        self.assertEqual(self.colors(1), [])
        self.colors(0).clear()
        self.assertEqual(self.main("group", "color", "g", "none")[0], 0)
        self.assertEqual(self.colors(0), [("use", False)])
        self.assertIsNone(self.doc()["groups"]["g"]["color"])

    def test_add_to_a_colored_group_paints_remove_clears(self):
        self.main("group", "add", "g", "--session", "other")
        self.main("group", "color", "g", "#010203")
        self.main("group", "add", "g")
        self.assertEqual(self.colors(0), ["#010203", ("use", True)])
        self.colors(0).clear()
        self.main("group", "remove")
        self.assertEqual(self.colors(0), [("use", False)])

    def test_delete_ungroups_and_clears_the_color(self):
        self.main("group", "add", "g")
        self.main("group", "color", "g", "#010203")
        self.colors(0).clear()
        rc, out, _ = self.main("group", "delete", "g")
        self.assertEqual(rc, 0)
        self.assertEqual(self.colors(0), [("use", False)])
        self.assertIsNone(tabgroups.group_of(self.doc(), "selected"))

    def test_rename_and_list(self):
        self.main("group", "add", "g")
        self.main("group", "rename", "g", "h")
        rc, out, _ = self.main("group", "list", "--json")
        self.assertEqual(json.loads(out), [{"name": "h", "color": "", "members": 1}])
        self.assertIn("h", self.main("group", "list")[1])

    def test_list_reports_an_unreadable_file(self):
        with open(tabgroups.path(self.cfg), "w") as fh:
            fh.write("{bad")
        rc, out, err = self.main("group", "list", "--json")
        self.assertEqual((rc, json.loads(out)), (1, []))
        self.assertIn("unreadable", err)

    def test_bad_input_is_an_error_not_a_traceback(self):
        self.assertEqual(self.main("group", "color", "missing", "#010203")[0], 1)
        self.assertEqual(self.main("group", "color", "missing", "red")[0], 1)
        self.assertEqual(self.main("group", "rename", "missing", "x")[0], 1)
        self.assertEqual(self.main("group", "delete", "missing")[0], 1)
        self.assertEqual(self.main("group", "add", "g", "--session", "nope")[0], 1)

    def test_pin_unpin_toggle(self):
        self.assertEqual(self.main("pin")[0], 0)
        self.assertTrue(tabgroups.is_pinned(self.doc(), "selected"))
        self.assertEqual(self.main("unpin")[0], 0)
        self.assertFalse(tabgroups.is_pinned(self.doc(), "selected"))
        self.assertIn("pinned", self.main("pin", "--toggle")[1])
        self.assertTrue(tabgroups.is_pinned(self.doc(), "selected"))
        self.assertIn("unpinned", self.main("pin", "--toggle")[1])
        self.assertFalse(tabgroups.is_pinned(self.doc(), "selected"))

    def test_no_iterm2_session_flag_still_works_selected_does_not(self):
        self.current = None
        self.assertEqual(self.main("group", "add", "g", "--session", "other")[0], 0)
        self.assertEqual(tabgroups.group_of(self.doc(), "other"), "g")
        rc, _, err = self.main("group", "add", "g")
        self.assertEqual(rc, 1)
        self.assertIn("--session", err)

    def test_recolor_only_touches_colored_group_members(self):
        self.main("group", "add", "g")
        self.main("group", "color", "g", "#010203")
        self.main("group", "add", "plain", "--session", "other")
        self.colors(0).clear()
        rc, out, _ = self.main("recolor")
        self.assertEqual(rc, 0)
        self.assertIn("recolored 1 tabs", out)
        self.assertEqual(self.colors(0), ["#010203", ("use", True)])
        self.assertEqual(self.colors(1), [])

    def test_recolor_reaches_a_tab_restore_just_bound(self):
        win = Window([tab(5)])
        self.app = App([win])
        self.insert(session_id="restored", tty="ttys005", pid=None, state="idle", iterm_guid="G5")
        tabgroups.add(self.cfg, "g", "restored")
        tabgroups.set_color(self.cfg, "g", "#010203")
        self.assertEqual(self.main("recolor")[0], 0)
        self.assertEqual(win.tabs[0].sessions[0].colors, ["#010203", ("use", True)])

    def test_recolor_not_running_does_nothing(self):
        self.main("group", "add", "g")
        self.main("group", "color", "g", "#010203")
        self.current = None
        with mock.patch.object(T, "with_app") as app, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(T.main(["recolor"]), 0)
        app.assert_not_called()

    def test_one_failed_color_does_not_stop_the_rest(self):
        self.main("group", "add", "g")
        self.main("group", "add", "g", "--session", "other")
        self.win.tabs[0].sessions[0].color_error = RuntimeError("gone")
        with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(T, "make_color", lambda c: c):
            n = arun(T.paint(self.app, self.conn, {"selected": "#010203", "other": "#010203"}))
        self.assertEqual(n, 1)
        self.assertEqual(self.colors(1), ["#010203", ("use", True)])

    def test_color_goes_through_the_sessions_own_profile(self):
        calls = []

        class S(object):
            async def async_get_profile(self):
                calls.append("session profile")
                return Profile(mock.Mock(colors=calls))
        with mock.patch.object(T, "make_color", lambda c: c):
            arun(T.set_tab_color(S(), "#010203"))
        self.assertEqual(calls, ["session profile", "#010203", ("use", True)])


class Reexec(ToolEnv):
    def block_import(self):
        return mock.patch.dict(sys.modules, {"iterm2": None})

    def test_reexecs_once_on_the_daemon_python(self):
        py = os.path.join(self.tmp, "py")
        with open(py, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(py, 0o755)
        self.cfg["daemon_python"] = py
        with self.block_import(), mock.patch.dict(os.environ), mock.patch.object(os, "execv") as ex:
            os.environ.pop(T.REEXEC_ENV, None)
            T.need_iterm2(self.cfg)
            self.assertEqual(os.environ[T.REEXEC_ENV], "1")
        self.assertEqual(ex.call_args.args[0], py)

    def test_guard_stops_a_loop(self):
        with self.block_import(), mock.patch.dict(os.environ, {T.REEXEC_ENV: "1"}), mock.patch.object(os, "execv") as ex:
            with self.assertRaises(SystemExit):
                T.need_iterm2(self.cfg)
        ex.assert_not_called()

    def test_missing_python_is_a_message(self):
        self.cfg["daemon_python"] = os.path.join(self.tmp, "nope")
        with self.block_import(), mock.patch.dict(os.environ), mock.patch.object(os, "execv") as ex:
            os.environ.pop(T.REEXEC_ENV, None)
            with self.assertRaises(SystemExit):
                T.need_iterm2(self.cfg)
        ex.assert_not_called()

    def test_reexec_comes_before_any_change(self):
        """It replays the whole command: a replayed delete would fail, a replayed toggle undo."""
        tabgroups.set_pinned(self.cfg, "s", False)

        class Replayed(Exception):
            pass
        with mock.patch.object(T, "need_iterm2", side_effect=Replayed()):
            with self.assertRaises(Replayed):
                T.main(["pin", "--toggle", "--session", "s"])
            tabgroups.add(self.cfg, "g", "s")
            with self.assertRaises(Replayed):
                T.main(["group", "delete", "g"])
        doc = tabgroups.load(self.cfg)
        self.assertFalse(tabgroups.is_pinned(doc, "s"))
        self.assertIn("g", doc["groups"])

    def test_reads_stay_on_this_python(self):
        with mock.patch.object(T, "need_iterm2") as need, mock.patch.object(restore, "iterm_guids", lambda: None), \
                contextlib.redirect_stdout(io.StringIO()):
            T.main(["find", "--json"])
            T.main(["group", "list", "--json"])
        need.assert_not_called()

    def test_cookie_never_asked_of_a_stopped_iterm2_by_the_refresh_commands(self):
        self.current = None
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("osascript launches iTerm2")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(T.main(["close-idle", "--dry-run"]), 0)
            self.assertEqual(T.main(["recolor"]), 0)
            self.assertEqual(T.main(["sort"]), 0)

    def test_existing_cookie_is_not_asked_for_again(self):
        with mock.patch.dict(os.environ, {"ITERM2_COOKIE": "x"}), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("asked")):
            T.request_cookie()


# ------------------------------------------------------------------ files

class Files(TempEnv):
    def read(self, *rel):
        with open(os.path.join(REPO, *rel)) as fh:
            return fh.read()

    def test_lib_side_is_stdlib_only_and_parses_as_39(self):
        allowed = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else None
        for name in ("activity.py", "tabgroups.py", "tabs.py", "restore.py", "cli.py"):
            tree = ast.parse(self.read("lib", "ccsessions", name), feature_version=(3, 9))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    mods = [node.module]
                for m in mods:
                    self.assertNotEqual(m.split(".")[0], "iterm2", name)
                    if allowed:
                        self.assertIn(m.split(".")[0], allowed, name)

    def test_cc_tabs_parses_as_39_and_imports_iterm2_only_inside_functions(self):
        tree = ast.parse(self.read("bin", "cc-tabs"), feature_version=(3, 9))
        for node in tree.body:
            if isinstance(node, ast.Import):
                self.assertNotIn("iterm2", [a.name for a in node.names])

    def test_cc_tabs_is_executable_and_runs_on_this_python(self):
        path = os.path.join(REPO, "bin", "cc-tabs")
        self.assertTrue(os.access(path, os.X_OK))
        r = subprocess.run([sys.executable, path, "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("close-idle", r.stdout)

    def test_makefile_installs_the_tool_and_the_shim(self):
        mk = self.read("Makefile")
        self.assertIn("BIN_FILES  = cc-sessions cc-resume cc-tabs", mk)
        self.assertIn("build/cc-tabsort $(PREFIX)/bin/cc-tabsort", mk)
        r = subprocess.run(["make", "-s", "-C", REPO, "render-shim", "PREFIX=/opt/x"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = self.read("build", "cc-tabsort").splitlines()
        self.assertEqual(lines, ["#!/bin/sh", 'exec "/opt/x/bin/cc-tabs" sort "$@"'])

    def test_sort_keeps_all_and_dry_run_drops_group_and_repo(self):
        a = T.build_parser().parse_args(["sort", "--all", "--dry-run"])
        self.assertTrue(a.all and a.dry_run)
        for gone in ("--group", "--repo"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                T.build_parser().parse_args(["sort", gone])

    def test_menu_shape(self):
        src = self.read("contrib", "hammerspoon", "cc_sessions_menu.lua")
        for piece in ('"/.claude/bin/cc-tabs"', "hs.chooser.new", "searchSubText(true)", "Find session", "Sort tabs",
                      '"Cancel", closeLabel', "hs.dialog.blockAlert", "hs.dialog.textPrompt", "New group...",
                      "Remove from group", '"Dismiss"', '"restore", "--dismiss"', "Group this tab", '"close-idle"',
                      "hs.hotkey.bind(HYPER, FIND_KEY", "hs.hotkey.bind(HYPER, SORT_KEY", '{ "group", "color", name, "none" }'):
            self.assertIn(piece, src)
        self.assertNotIn("hs.execute", src)
        self.assertNotIn("—", src)
        self.assertEqual(src.count('{ "Red", ') + src.count('{ "Orange", ') + src.count('{ "Yellow", ')
                         + src.count('{ "Green", ') + src.count('{ "Blue", ') + src.count('{ "Purple", ')
                         + src.count('{ "Pink", ') + src.count('{ "Gray", '), 8)

    def menu_function(self, name):
        src = self.read("contrib", "hammerspoon", "cc_sessions_menu.lua")
        start = src.index("local function %s(" % name)
        return src[start:src.index("\nend\n", start)]

    def test_menu_refresh_runs_only_the_two_commands_with_no_api(self):
        body = self.menu_function("refresh")
        self.assertEqual(body.count("run(") + body.count("runTabs("), 2)
        self.assertIn('{ "tabs", "--json", "--idle-days", tostring(CLOSE_DAYS) }', body)
        self.assertIn('runTabs({ "group", "list", "--json" }', body)
        self.assertNotIn("close-idle", body)
        self.assertIn('"--no-ask"', body)

    def test_menu_click_confirms_the_exact_dry_run_and_passes_its_ids(self):
        body = self.menu_function("closeIdle")
        self.assertIn('"close-idle", "--dry-run", "--json"', body)
        self.assertIn("#d.session_ids", body)
        self.assertIn('"--only", table.concat(d.session_ids, ",")', body)

    def test_menu_asks_iterm2_only_after_it_was_used(self):
        self.assertIn("allowAsking()", self.menu_function("buildMenu"))
        self.assertIn("allowAsking()", self.menu_function("openPicker"))
        self.assertIn("allowAsking()", self.menu_function("sortTabs"))
        self.assertNotIn("allowAsking", self.menu_function("refresh"))

    def test_user_visible_strings_have_no_em_dash(self):
        for rel in (("bin", "cc-tabs"), ("lib", "ccsessions", "activity.py"), ("lib", "ccsessions", "tabgroups.py")):
            self.assertNotIn("—", self.read(*rel), rel)

    def test_readme_says_the_close_is_undoable_only_over_half(self):
        text = self.read("README.md")
        self.assertIn("more than half", text)
        self.assertIn("Older snapshots", text)


class NewTabReopensAClosedSession(TempEnv):
    """A wake whose tab is gone opens a new tab. A tab fresh from async_create_tab reports no
    current session yet, which raised on the send and left a blank tab behind."""

    def test_text_goes_to_the_new_tabs_only_session_when_none_is_current_yet(self):
        sent = []

        class Session(object):
            session_id = "NEW-SESSION"

            async def async_send_text(self, text):
                sent.append(text)

        class Tab(object):
            current_session = None
            sessions = [Session()]

        class Window(object):
            async def async_create_tab(self):
                return Tab()

        class App(object):
            current_terminal_window = Window()

        guid = arun(D.ItermTabs(App(), None).new_tab("resume\r"))
        self.assertEqual((guid, sent), ("NEW-SESSION", ["resume\r"]))



class TranscriptActivity(TempEnv):
    """A session with no events left (pruned at 30 days) is ranked from its transcript."""

    def write(self, *records, name="t.jsonl"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        return path

    @staticmethod
    def stamp(day):
        return "2026-09-%02dT10:00:00.000Z" % day

    def typed(self, day, text="fix this"):
        return {"type": "user", "timestamp": self.stamp(day), "message": {"role": "user", "content": text}}

    def reply(self, day, text="done", **extra):
        return dict({"type": "assistant", "timestamp": self.stamp(day),
                     "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": text}]}}, **extra)

    def epoch(self, day):
        return activity._timestamp(self.stamp(day))

    def test_a_reply_after_the_prompt_counts(self):
        self.assertEqual(activity.last_work_time(self.write(self.typed(1), self.reply(2))), self.epoch(2))

    def test_a_typed_prompt_counts(self):
        self.assertEqual(activity.last_work_time(self.write(self.reply(1), self.typed(2))), self.epoch(2))

    def test_prompt_as_text_blocks_counts(self):
        rec = {"type": "user", "timestamp": self.stamp(2),
               "message": {"content": [{"type": "text", "text": "fix this"}]}}
        self.assertEqual(activity.last_work_time(self.write(self.reply(1), rec)), self.epoch(2))

    def test_what_a_resume_appends_does_not_count(self):
        noise = [
            {"type": "user", "isMeta": True, "timestamp": self.stamp(20), "message": {"content": "hook context"}},
            self.typed(21, "<local-command-stdout>x</local-command-stdout>"),
            self.typed(22, "<command-name>/resume</command-name>"),
            self.typed(23, "<system-reminder>x</system-reminder>"),
            self.typed(24, "Caveat: the messages below"),
            self.typed(25, "This session is being continued from a previous conversation"),
            self.typed(26, "<task-notification>done</task-notification>"),
            {"type": "system", "timestamp": self.stamp(27), "message": {"content": "x"}},
            {"type": "assistant", "timestamp": self.stamp(28),
             "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "No response requested."}]}},
            self.reply(29, isApiErrorMessage=True),
            {"type": "user", "isCompactSummary": True, "timestamp": self.stamp(30), "message": {"content": "summary"}},
        ]
        self.assertEqual(activity.last_work_time(self.write(self.typed(1), self.reply(2), *noise)), self.epoch(2))

    def test_a_subagents_lines_and_tool_results_do_not_count_here(self):
        side = dict(self.reply(20), isSidechain=True)
        tool = {"type": "user", "timestamp": self.stamp(21),
                "message": {"content": [{"type": "tool_result", "content": "x"}]}}
        only_tool = {"type": "assistant", "timestamp": self.stamp(22),
                     "message": {"model": "m", "content": [{"type": "tool_use", "name": "Bash"}]}}
        self.assertEqual(activity.last_work_time(self.write(self.reply(2), side, tool, only_tool)), self.epoch(2))

    def test_a_reply_to_a_background_notification_counts(self):
        note = self.typed(20, "<task-notification>done</task-notification>")
        self.assertEqual(activity.last_work_time(self.write(self.typed(1), note, self.reply(21))), self.epoch(21))

    def test_a_record_longer_than_a_chunk_is_read_whole(self):
        big = self.reply(5, text="x" * 5000)
        self.assertEqual(activity.last_work_time(self.write(self.reply(1), big), chunk=64), self.epoch(5))

    def test_a_chunk_boundary_finds_the_right_record(self):
        rows = [self.reply(1)] + [{"type": "system", "timestamp": self.stamp(9), "message": {"content": "p" * 300}}] * 40
        self.assertEqual(activity.last_work_time(self.write(*rows), chunk=100), self.epoch(1))

    def test_limit_gives_up_rather_than_reading_everything(self):
        rows = [self.reply(1)] + [{"type": "system", "timestamp": self.stamp(9), "message": {"content": "p" * 300}}] * 40
        self.assertIsNone(activity.last_work_time(self.write(*rows), chunk=100, limit=500))

    def test_garbage_and_unreadable_files_give_none(self):
        bad = os.path.join(self.tmp, "bad.jsonl")
        with open(bad, "wb") as fh:
            fh.write(b"not json\n\xff\xfe\n[1,2]\n")
        self.assertIsNone(activity.last_work_time(bad))
        self.assertIsNone(activity.last_work_time(os.path.join(self.tmp, "missing.jsonl")))

    def test_a_record_with_a_bad_timestamp_is_passed_over(self):
        rec = dict(self.reply(3), timestamp="soon")
        self.assertEqual(activity.last_work_time(self.write(self.reply(1), rec)), self.epoch(1))

    def insert(self, sid, **kw):
        ledger.apply_event(self.conn, "SessionStart", {"session_id": sid}, None, now=kw.pop("started", 1000.0))
        with ledger.tx(self.conn):
            for k, v in kw.items():
                self.conn.execute("UPDATE sessions SET %s=? WHERE session_id=?" % k, (v, sid))
        return ledger.get(self.conn, sid)

    def test_a_woken_session_with_no_events_ranks_by_its_transcript(self):
        path = self.write(self.typed(9), self.reply(9))
        r = self.insert("old", transcript=path, resumed_at=time.time())
        times = activity.activity_times(self.conn, self.cfg, [r])
        self.assertEqual(activity.activity_time(r, times), self.epoch(9))

    def test_a_used_session_outranks_a_woken_old_one(self):
        old = self.insert("old", transcript=self.write(self.reply(9), name="o.jsonl"), resumed_at=time.time())
        used = self.insert("used", transcript=self.write(self.reply(9), name="u.jsonl"))
        ledger.apply_event(self.conn, "Stop", {"session_id": "used"}, None, now=self.epoch(20))
        times = activity.activity_times(self.conn, self.cfg, [old, used])
        self.assertGreater(activity.activity_time(used, times), activity.activity_time(old, times))

    def test_an_event_wins_and_its_transcript_is_not_read(self):
        r = self.insert("s", transcript=self.write(self.reply(9)))
        ledger.apply_event(self.conn, "Stop", {"session_id": "s"}, None, now=5000.0)
        with mock.patch.object(activity, "last_work_time", side_effect=AssertionError("read")):
            self.assertEqual(activity.activity_times(self.conn, self.cfg, [r])["s"], 5000.0)

    def test_no_transcript_falls_back_to_the_start(self):
        r = self.insert("s", started_at=77.0, transcript=os.path.join(self.tmp, "none.jsonl"))
        self.assertEqual(activity.activity_time(r, activity.activity_times(self.conn, self.cfg, [r])), 77.0)

    def test_one_session_reads_only_that_transcript(self):
        a = self.insert("a", transcript=self.write(self.reply(1), name="a.jsonl"))
        b = self.insert("b", transcript=self.write(self.reply(2), name="b.jsonl"))
        self.assertEqual(activity.activity_times(self.conn, self.cfg, [a, b], "b"), {"b": self.epoch(2)})

    def parked(self, sid, day, guid, now):
        self.insert(sid, transcript=self.write(self.reply(day), name=sid + ".jsonl"), state="hibernated",
                    resumed_at=now - 60, evicted_at=now - 30, iterm_guid=guid, pid=None, pid_start=None)

    def test_the_idle_estimate_reads_the_transcript_time(self):
        now = self.epoch(30)
        doc = {"groups": {}, "sessions": {}}
        self.parked("recent", 29, "G1", now)
        self.assertEqual(activity.idle_estimate(self.conn, doc, 7, now, lambda: {"G1"}, self.cfg), 0)
        self.parked("old", 1, "G2", now)
        self.assertEqual(activity.idle_estimate(self.conn, doc, 7, now, lambda: {"G1", "G2"}, self.cfg), 1)

    def test_the_menus_idle_count_passes_the_config_through(self):
        now = time.time()
        recent = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 3600))
        rec = dict(self.reply(1), timestamp=recent)
        self.insert("recent", transcript=self.write(rec, name="r.jsonl"), state="hibernated", resumed_at=now - 60,
                    evicted_at=now - 30, iterm_guid="G1", pid=None, pid_start=None)
        with mock.patch.object(restore, "iterm_guids", return_value={"G1"}), \
                mock.patch.object(tabfile, "iterm_instance", return_value=dict(INSTANCE)):
            self.assertEqual(cli.tabs_data(self.cfg, self.conn, idle_days=7)["idle_tabs"], 0)

    def test_injected_ide_and_hook_lines_do_not_count(self):
        noise = [self.typed(20, "<ide_opened_file>x</ide_opened_file>"),
                 self.typed(21, "WorktreeCreate hook failed: x")]
        self.assertEqual(activity.last_work_time(self.write(self.reply(2), *noise)), self.epoch(2))
