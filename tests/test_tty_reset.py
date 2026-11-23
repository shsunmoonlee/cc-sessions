"""Terminal modes a killed Claude leaves on: the eviction banner goes through the tty device
(ordered after the dead process's last output), a focused hibernated tab at a shell gets a
mouse/focus-reporting reset, and a SIGTERM hang is logged with what may explain it."""
import asyncio
import logging
import os
import re
import select
import signal
import threading
import time
import tty as ttymod
from unittest import mock

from helpers import REAL_PROC_STAT, REAL_TTY_OPEN, REAL_WRITE_TTY, TempEnv, load_daemon

from ccsessions import ledger, procs

from test_daemon import Clock, FakeApp, FakeSession, FakeTabs

D = load_daemon()
NOW = 2_000_000_000.0


def arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Pipe(object):
    """A write end standing in for a tty fd. Whether it was closed is read from the read end
    (EOF once every write end is closed), so a reused fd number cannot fake it."""

    def __init__(self, test):
        self.r, self.w = os.pipe()
        os.set_blocking(self.r, False)
        self.data = b""
        test.addCleanup(self.cleanup)

    def write_end_closed(self):
        while True:
            try:
                chunk = os.read(self.r, 65536)
            except BlockingIOError:
                return False
            if not chunk:
                return True
            self.data += chunk

    def cleanup(self):
        if not self.write_end_closed():
            os.close(self.w)
        os.close(self.r)


def real_pty(test):
    m, s = os.openpty()
    test.addCleanup(os.close, m)
    test.addCleanup(os.close, s)
    name = os.ttyname(s)
    if not re.match(r"^/dev/ttys\d+$", name):
        test.skipTest("pty slave is %s" % name)
    ttymod.setraw(s)
    return m, s, name


class TtyOpen(TempEnv):
    def test_rejects_anything_but_a_plain_ttys_name_without_opening(self):
        with mock.patch.object(procs.os, "open", side_effect=AssertionError("opened")) as op:
            for bad in (None, "", "console", "/dev/disk0", "../ttys1", "ttys1/../x", "ttys", "s1x"):
                self.assertIsNone(REAL_TTY_OPEN(bad), bad)
        op.assert_not_called()

    def test_opens_write_only_without_becoming_the_controlling_tty(self):
        seen = []

        def fake_open(path, flags, *a):
            seen.append((path, flags))
            raise OSError("no such device")
        with mock.patch.object(procs.os, "open", fake_open):
            self.assertIsNone(REAL_TTY_OPEN("/dev/ttys5"))
        self.assertEqual(seen[0][0], "/dev/ttys5")
        flags = seen[0][1]
        self.assertTrue(flags & os.O_NOCTTY and flags & os.O_NONBLOCK and flags & os.O_WRONLY)

    def test_a_node_that_is_not_a_character_device_is_refused_and_closed(self):
        p = Pipe(self)
        with mock.patch.object(procs.os, "open", lambda path, flags, *a: p.w):
            self.assertIsNone(REAL_TTY_OPEN("ttys5"))
        self.assertTrue(p.write_end_closed())

    def test_a_device_owned_by_another_user_is_refused_and_closed(self):
        p = Pipe(self)
        fake = os.stat_result((0o20620, 0, 0, 1, os.getuid() + 1, 0, 0, 0, 0, 0))
        with mock.patch.object(procs.os, "open", lambda path, flags, *a: p.w), \
                mock.patch.object(procs.os, "fstat", lambda fd: fake):
            self.assertIsNone(REAL_TTY_OPEN("ttys5"))
        self.assertTrue(p.write_end_closed())

    def test_an_owned_character_device_is_kept_open(self):
        p = Pipe(self)
        fake = os.stat_result((0o20620, 0, 0, 1, os.getuid(), 0, 0, 0, 0, 0))
        with mock.patch.object(procs.os, "open", lambda path, flags, *a: p.w), \
                mock.patch.object(procs.os, "fstat", lambda fd: fake):
            self.assertEqual(REAL_TTY_OPEN("ttys5"), p.w)
        self.assertFalse(p.write_end_closed())

    def test_round_trip_through_a_real_pty(self):
        m, s, name = real_pty(self)
        fd = REAL_TTY_OPEN(name)
        self.assertIsNotNone(fd)
        try:
            payload = (procs.TTY_ABORT + "\x1b[?1003lhello").encode()
            self.assertEqual(procs.tty_write_fd(fd, payload, 1.0), len(payload))
            got = b""
            deadline = time.time() + 2
            while len(got) < len(payload) and time.time() < deadline:
                got += os.read(m, 1024)
            self.assertEqual(got, payload)
            self.assertIsInstance(procs.tty_outq_fd(fd), int)
        finally:
            os.close(fd)

    def test_write_tty_on_a_real_pty(self):
        m, s, name = real_pty(self)
        with mock.patch.object(procs, "tty_open", REAL_TTY_OPEN):
            self.assertEqual(REAL_WRITE_TTY(name, "abc", 1.0), 3)
        self.assertEqual(os.read(m, 16), b"abc")

    def test_write_tty_closes_the_fd_it_opened(self):
        p = Pipe(self)
        with mock.patch.object(procs, "tty_open", lambda tty: p.w):
            self.assertEqual(REAL_WRITE_TTY("ttys5", "abc", 1.0), 3)
        self.assertTrue(p.write_end_closed())
        self.assertEqual(p.data, b"abc")

    def test_tty_outq_closes_the_fd_it_opened(self):
        p = Pipe(self)
        with mock.patch.object(procs, "tty_open", lambda tty: p.w):
            procs.tty_outq("ttys5")
        self.assertTrue(p.write_end_closed())

    def test_tty_write_close_closes_even_when_the_write_raises(self):
        p = Pipe(self)
        with mock.patch.object(procs, "tty_write_fd", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                procs.tty_write_close(p.w, b"x", 1.0)
        self.assertTrue(p.write_end_closed())

    def test_proc_stat_never_raises_and_is_bounded(self):
        seen = []

        def fake_run_rc(args, timeout=10):
            seen.append(timeout)
            raise procs.ProcsError("ps timed out")
        with mock.patch.object(procs, "_run_rc", fake_run_rc):
            self.assertIsNone(REAL_PROC_STAT(111))
        self.assertEqual(seen, [2])


class TtyWriteFd(TempEnv):
    def test_waits_out_a_full_queue_then_writes_everything(self):
        writes = [BlockingIOError(), 3, 3]

        def fake_write(fd, data):
            w = writes.pop(0)
            if isinstance(w, Exception):
                raise w
            return w
        with mock.patch.object(procs.os, "write", fake_write), \
                mock.patch.object(procs.select, "select", lambda r, w, x, t: (r, w, x)):
            self.assertEqual(procs.tty_write_fd(99, b"abcdef", 1.0), 6)
        self.assertEqual(writes, [])

    def test_the_deadline_returns_a_partial_count(self):
        calls = []

        def fake_write(fd, data):
            calls.append(1)
            if len(calls) == 1:
                return 2
            raise BlockingIOError()
        with mock.patch.object(procs.os, "write", fake_write), \
                mock.patch.object(procs.select, "select", lambda r, w, x, t: ([], [], [])):
            self.assertEqual(procs.tty_write_fd(99, b"abcdef", 0.05), 2)

    def test_a_zero_byte_write_waits_instead_of_spinning(self):
        calls, waits = [], []

        def fake_write(fd, data):
            calls.append(1)
            if len(calls) > 1000:
                raise AssertionError("spinning on zero-byte writes")
            return 0

        def fake_select(r, w, x, t):
            waits.append(t)
            time.sleep(0.01)
            return [], [], []
        with mock.patch.object(procs.os, "write", fake_write), \
                mock.patch.object(procs.select, "select", fake_select):
            self.assertEqual(procs.tty_write_fd(99, b"abc", 0.05), 0)
        self.assertTrue(waits)
        self.assertLess(len(calls), 100)

    def test_any_other_error_stops_without_raising(self):
        with mock.patch.object(procs.os, "write", side_effect=OSError(5, "EIO")):
            self.assertEqual(procs.tty_write_fd(99, "abc", 1.0), 0)

    def test_a_full_real_pty_queue_is_waited_out(self):
        m, s, name = real_pty(self)
        os.set_blocking(s, False)
        filled = 0
        while filled < 1 << 22:
            try:
                filled += os.write(s, b"f" * 4096)
            except BlockingIOError:
                break
        fd = REAL_TTY_OPEN(name)
        self.assertIsNotNone(fd)
        self.addCleanup(os.close, fd)
        stop = threading.Event()

        def drain():
            time.sleep(0.3)
            while not stop.is_set():
                if select.select([m], [], [], 0.05)[0]:
                    try:
                        os.read(m, 65536)
                    except OSError:
                        return
        t = threading.Thread(target=drain)
        t.start()
        try:
            payload = b"p" * 8192
            started = time.monotonic()
            self.assertEqual(procs.tty_write_fd(fd, payload, 3.0), len(payload))
            self.assertGreaterEqual(time.monotonic() - started, 0.25)
        finally:
            stop.set()
            t.join(5)


class BannerThroughTheTty(TempEnv):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.row = self.insert(session_id="s1", state="idle", last_event_at=NOW - 3600, iterm_guid="G1",
                               cwd="/w", title="Title")
        self.alive = True
        self.kills = []
        self.writes = []
        self.pipe = Pipe(self)
        self.opened = []

        def opener(tty):
            self.opened.append(tty)
            return self.pipe.w
        p = mock.patch.object(procs, "tty_open", opener)
        p.start()
        self.addCleanup(p.stop)
        self.wrote = None  # None: write everything

        def writer(fd, data, timeout_s):
            self.writes.append((fd, data, timeout_s))
            return len(data) if self.wrote is None else self.wrote
        p = mock.patch.object(procs, "tty_write_fd", writer)
        p.start()
        self.addCleanup(p.stop)

    def evictor(self, tabs, offload=None):
        def kill(pid, sig):
            self.kills.append((pid, sig))
            self.alive = False
        return D.Evictor(self.conn, self.cfg, tabs, kill=kill, identity=lambda r: self.alive,
                         clock=self.clock, sleep=self.clock.sleep,
                         children=lambda r: [], child_probe=lambda p, s: "gone", offload=offload)

    def kinds(self, tabs):
        return [c[0] for c in tabs.calls if c[0] != "job"]

    def test_full_write_injects_nothing(self):
        tabs = FakeTabs(["zsh"])
        self.assertEqual(arun(self.evictor(tabs).evict(self.row, 12, 1.0)), "term")
        self.assertEqual(self.kinds(tabs), ["name", "send"])
        self.assertEqual(len(self.writes), 1)
        data = self.writes[0][1]
        self.assertTrue(data.startswith((procs.TTY_ABORT + D.TTY_RESET).encode()), data[:40])
        self.assertIn(b"hibernated to free memory", data)
        self.assertEqual(self.opened, ["ttys001"])
        self.assertTrue(self.pipe.write_end_closed())

    def test_zero_bytes_out_falls_back_to_one_inject(self):
        self.wrote = 0
        tabs = FakeTabs(["zsh"])
        with self.assertLogs("cc-sessions", "WARNING") as logs:
            arun(self.evictor(tabs).evict(self.row, 12, 1.0))
        self.assertEqual(self.kinds(tabs), ["name", "inject", "send"])
        inj = [c[2] for c in tabs.calls if c[0] == "inject"][0]
        self.assertTrue(inj.startswith(D.TTY_RESET.encode()))
        self.assertEqual(inj, self.writes[0][1][len(procs.TTY_ABORT):])
        self.assertTrue(any("0 bytes out" in m and "injected through iTerm2" in m for m in logs.output))
        self.assertTrue(self.pipe.write_end_closed())

    def test_a_partial_write_is_not_repeated_through_inject(self):
        self.wrote = 7
        tabs = FakeTabs(["zsh"])
        with self.assertLogs("cc-sessions", "WARNING") as logs:
            arun(self.evictor(tabs).evict(self.row, 12, 1.0))
        self.assertEqual(self.kinds(tabs), ["name", "send"])
        self.assertTrue(any("got 7 of" in m and "reset incomplete" in m for m in logs.output), logs.output)
        self.assertTrue(self.pipe.write_end_closed())

    def test_a_partial_write_past_the_reset_does_not_say_incomplete(self):
        self.wrote = len((procs.TTY_ABORT + D.TTY_RESET).encode()) + 5
        tabs = FakeTabs(["zsh"])
        with self.assertLogs("cc-sessions", "WARNING") as logs:
            arun(self.evictor(tabs).evict(self.row, 12, 1.0))
        msgs = [m for m in logs.output if "banner left partial" in m]
        self.assertEqual(len(msgs), 1)
        self.assertNotIn("reset incomplete", msgs[0])

    def test_the_fd_is_closed_when_the_tab_tty_changed(self):
        tabs = FakeTabs(["zsh"], tty="ttys009")
        arun(self.evictor(tabs).evict(self.row, 12, 1.0))
        self.assertEqual(self.kinds(tabs), [])
        self.assertEqual(self.writes, [])
        self.assertTrue(self.pipe.write_end_closed())

    def test_the_fd_is_closed_when_the_tab_became_visible(self):
        tabs = FakeTabs(["zsh"], visible=True)
        arun(self.evictor(tabs).evict(self.row, 12, 1.0))
        self.assertEqual(self.writes, [])
        self.assertTrue(self.pipe.write_end_closed())

    def test_the_fd_is_closed_when_the_tab_is_not_at_a_shell(self):
        tabs = FakeTabs(["vim"])
        arun(self.evictor(tabs).evict(self.row, 12, 1.0))
        self.assertEqual(self.kinds(tabs), ["name"])
        self.assertEqual(self.writes, [])
        self.assertTrue(self.pipe.write_end_closed())

    def test_the_fd_is_closed_when_the_tab_call_raises(self):
        class Raising(FakeTabs):
            async def set_name(self, guid, name):
                raise RuntimeError("rpc")
        tabs = Raising(["zsh"])
        arun(self.evictor(tabs).evict(self.row, 12, 1.0))
        self.assertTrue(self.pipe.write_end_closed())

    def test_no_fd_injects_as_before(self):
        with mock.patch.object(procs, "tty_open", lambda tty: None):
            tabs = FakeTabs(["zsh"])
            arun(self.evictor(tabs).evict(self.row, 12, 1.0))
        self.assertEqual(self.kinds(tabs), ["name", "inject", "send"])
        self.assertEqual(self.writes, [])

    def test_a_cancelled_prepare_leaves_the_fd_to_the_writing_thread(self):
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET state='hibernated' WHERE session_id='s1'")
        started, go = threading.Event(), threading.Event()

        def slow_writer(fd, data, timeout_s):
            started.set()
            go.wait(5)
            return len(data)

        async def scenario():
            loop = asyncio.get_running_loop()
            ev = self.evictor(FakeTabs(["zsh"]),
                              offload=lambda fn, *a: loop.run_in_executor(None, fn, *a))
            task = loop.create_task(ev.prepare_tab(ledger.get(self.conn, "s1"), "Title"))
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            still_open = not self.pipe.write_end_closed()
            go.set()
            for _ in range(200):
                if self.pipe.write_end_closed():
                    break
                await asyncio.sleep(0.01)
            return still_open
        with mock.patch.object(procs, "tty_write_fd", slow_writer):
            self.assertTrue(arun(scenario()), "the outer finally closed an fd the thread still used")
        self.assertTrue(self.pipe.write_end_closed())


class OrderedSession(FakeSession):
    def __init__(self, guid, tty, events, job="-zsh"):
        FakeSession.__init__(self, guid, tty, job)
        self.events = events

    async def async_send_text(self, text):
        self.events.append(("send", text))
        await FakeSession.async_send_text(self, text)


class FocusReset(TempEnv):
    def setUp(self):
        super().setUp()
        self.events = []
        self.short = None

        def writer(tty, data, timeout_s):
            self.events.append(("write", tty, data))
            return len(data) if self.short is None else self.short
        p = mock.patch.object(procs, "write_tty", writer)
        p.start()
        self.addCleanup(p.stop)

    def daemon(self, dry_run=False):
        d = D.Daemon(self.cfg, self.conn, FakeApp([]), None, dry_run)

        async def ident(r):
            return procs.GONE
        d.identity = ident
        return d

    def hib(self, **kw):
        return self.insert(session_id="hib", tty="ttys002", state="hibernated", iterm_guid="G2",
                           evicted_at=time.time() - 600, pid=222, cwd="/w/x", title="T", **kw)

    def test_shell_tab_gets_the_reset_before_the_resume(self):
        self.hib()
        s = OrderedSession("G2", "ttys002", self.events)
        arun(self.daemon().on_focus(s))
        self.assertEqual([e[0] for e in self.events], ["write", "send"])
        self.assertEqual(self.events[0][1:], ("ttys002", (procs.TTY_ABORT + D.FOCUS_RESET).encode()))
        self.assertNotIn("\x1b[?2004l", D.FOCUS_RESET)
        self.assertNotIn("\x1b[?1049l", D.FOCUS_RESET)
        self.assertIn("\x1b[?1003l", D.FOCUS_RESET)
        self.assertEqual(ledger.state_of(self.conn, "hib"), "resuming")

    def test_a_full_reset_logs_at_debug_a_short_one_at_info(self):
        self.hib(restored_at=time.time() - 5)
        s = OrderedSession("G2", "ttys002", self.events)
        with self.assertLogs("cc-sessions", "DEBUG") as logs:
            arun(self.daemon().on_focus(s))
        full = [r for r in logs.records if "terminal mode reset" in r.getMessage()]
        self.assertEqual([r.levelno for r in full], [logging.DEBUG])
        self.short = 3
        with self.assertLogs("cc-sessions", "DEBUG") as logs:
            arun(self.daemon().on_focus(s))
        short = [r for r in logs.records if "terminal mode reset" in r.getMessage()]
        self.assertEqual([r.levelno for r in short], [logging.INFO])
        self.assertIn("3 of", short[0].getMessage())

    def test_a_failing_write_still_resumes(self):
        self.hib()

        def boom(tty, data, timeout_s):
            raise OSError("EIO")
        s = OrderedSession("G2", "ttys002", self.events)
        with mock.patch.object(procs, "write_tty", boom):
            arun(self.daemon().on_focus(s))
        self.assertEqual([e[0] for e in self.events], ["send"])
        self.assertEqual(ledger.state_of(self.conn, "hib"), "resuming")

    def test_a_tab_running_a_program_gets_nothing(self):
        self.hib()
        s = OrderedSession("G2", "ttys002", self.events, job="vim")
        arun(self.daemon().on_focus(s))
        self.assertEqual(self.events, [])

    def test_a_declined_resume_still_resets(self):
        self.hib(restored_at=time.time() - 5)
        s = OrderedSession("G2", "ttys002", self.events)
        arun(self.daemon().on_focus(s))
        self.assertEqual([e[0] for e in self.events], ["write"])
        self.assertEqual(ledger.state_of(self.conn, "hib"), "hibernated")

    def test_a_dry_run_writes_nothing(self):
        self.hib()
        s = OrderedSession("G2", "ttys002", self.events)
        arun(self.daemon(dry_run=True).on_focus(s))
        self.assertEqual(self.events, [])


class Recorder(logging.Handler):
    def __init__(self, events):
        logging.Handler.__init__(self, logging.DEBUG)
        self.events = events

    def emit(self, record):
        self.events.append(("log", record.getMessage()))


class HangDiagnostics(TempEnv):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.row = self.insert(session_id="s1", state="idle", last_event_at=NOW - 3600, iterm_guid="G1",
                               cwd="/w", title="Title")
        self.kills = []
        self.events = []
        self.alive = True

    def evictor(self, kids, kid_state):
        def kill(pid, sig):
            self.kills.append((pid, sig))
            self.events.append(("kill", pid, sig))
            if sig == signal.SIGKILL and pid == 111:
                self.alive = False
        return D.Evictor(self.conn, self.cfg, FakeTabs(["zsh"]), kill=kill, identity=lambda r: self.alive,
                         clock=self.clock, sleep=self.clock.sleep,
                         children=lambda r: list(kids), child_probe=lambda p, s: kid_state.get(p, "gone"))

    def test_incident_line_comes_before_the_sigkill_and_names_children(self):
        ev = self.evictor([(501, 9001, "node"), (502, 9002)], {501: "alive", 502: "alive"})
        lg = logging.getLogger("cc-sessions")
        h = Recorder(self.events)
        self.addCleanup(lg.removeHandler, h)
        with mock.patch.object(procs, "proc_stat", lambda pid: "S+"), \
                mock.patch.object(procs, "tty_outq", lambda tty: 1024), \
                self.assertLogs("cc-sessions", "WARNING") as logs:
            lg.addHandler(h)  # inside: assertLogs swaps the handler list and restores it on exit
            self.assertEqual(arun(ev.evict(self.row, 12, 1.0)), "killed")
        self.assertEqual(self.kills, [(111, signal.SIGTERM), (111, signal.SIGKILL),
                                      (501, signal.SIGKILL), (502, signal.SIGKILL)])
        out = "\n".join(logs.output)
        self.assertIn("INCIDENT evict s1: before SIGKILL stat=S+ tty_outq=1024 children=[501 node, 502 ?]", out)
        self.assertIn("SIGKILLed child 501 (node) of the killed session", out)
        self.assertIn("SIGKILLed child 502 (?) of the killed session", out)
        before = [i for i, e in enumerate(self.events) if e[0] == "log" and "before SIGKILL" in e[1]]
        sigkill = self.events.index(("kill", 111, signal.SIGKILL))
        self.assertEqual(len(before), 1)
        self.assertLess(before[0], sigkill)

    def test_failing_diagnostics_never_stop_the_kill(self):
        def boom(*a):
            raise RuntimeError("ps")
        ev = self.evictor([], {})
        with mock.patch.object(procs, "proc_stat", boom), mock.patch.object(procs, "tty_outq", boom), \
                self.assertLogs("cc-sessions", "ERROR") as logs:
            self.assertEqual(arun(ev.evict(self.row, 12, 1.0)), "killed")
        self.assertTrue(any("stat=None tty_outq=None children=[]" in m for m in logs.output))

    def test_child_identities_carry_the_command_basename(self):
        with mock.patch.object(procs, "_run", lambda args, timeout=10: "  501   111\n  502   999\n"), \
                mock.patch.object(procs, "lstart_comm", lambda pid: (9001, "/usr/local/bin/node")):
            self.assertEqual(procs.child_identities(111), [(501, 9001, "node")])
