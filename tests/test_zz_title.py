"""dotclaude#166: a hibernated tab's "[zz] <title>" also goes through the tty as OSC 0. On
iTerm2 3.7 an empty OSC title (the exiting Claude clearing its title) hides the name
async_set_name gave, and only an OSC title written after it wins. Eviction and restore build
the name with the same helper. Fakes and pipes only: no real tty, no iTerm2."""
import asyncio
import time
from unittest import mock

from helpers import TempEnv, load_daemon

from ccsessions import ledger, procs
from ccsessions import tabs as tabfile

from test_daemon import Clock, FakeTabs
from test_tty_reset import Pipe

D = load_daemon()
NOW = 2_000_000_000.0
DIRTY = "Fix \x1b]0;evil\x07 bug\nnext\tline\x9b31m\x7fend"


def arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def osc(name):
    return ("\x1b]0;%s\x07" % name).encode()


class Base(TempEnv):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.alive = True
        self.pipe = Pipe(self)
        self.writes = []
        self.wrote = None  # None: every byte gets out
        for name, fn in (("tty_open", lambda tty: self.pipe.w), ("tty_write_fd", self.writer)):
            p = mock.patch.object(procs, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def writer(self, fd, data, timeout_s):
        self.writes.append(data)
        return len(data) if self.wrote is None else self.wrote

    def evict(self, jobs, title="Title"):
        row = self.insert(session_id="s1", state="idle", last_event_at=NOW - 3600, iterm_guid="G1",
                          cwd="/w", title=title)
        tabs = FakeTabs(jobs)

        def kill(pid, sig):
            self.alive = False
        ev = D.Evictor(self.conn, self.cfg, tabs, kill=kill, identity=lambda r: self.alive,
                       clock=self.clock, sleep=self.clock.sleep, children=lambda r: [],
                       child_probe=lambda p, s: "gone")
        arun(ev.evict(row, 12, 1.0, title=title))
        return tabs

    def names(self, tabs):
        return [c[2] for c in tabs.calls if c[0] == "name"]

    def injected(self, tabs):
        return [c[2] for c in tabs.calls if c[0] == "inject"]


class AtAShell(Base):
    def test_the_title_follows_the_banner_in_the_one_write(self):
        tabs = self.evict(["zsh"])
        self.assertEqual(len(self.writes), 1)
        data = self.writes[0]
        self.assertTrue(data.startswith((procs.TTY_ABORT + D.TTY_RESET).encode()))
        self.assertTrue(data.endswith(osc("[zz] Title")), data[-40:])
        self.assertLess(data.index(b"hibernated to free memory"), data.rindex(b"\x1b]0;"))
        self.assertEqual(self.names(tabs), ["[zz] Title"])
        self.assertEqual(self.injected(tabs), [])
        self.assertTrue(self.pipe.write_end_closed())

    def test_zero_bytes_out_injects_the_same_title(self):
        self.wrote = 0
        tabs = self.evict(["zsh"])
        inj = self.injected(tabs)
        self.assertEqual(len(inj), 1)
        self.assertTrue(inj[0].endswith(osc("[zz] Title")))
        self.assertEqual(inj[0], self.writes[0][len(procs.TTY_ABORT):])
        self.assertEqual(self.names(tabs), ["[zz] Title"])


class NotAtAShell(Base):
    def test_only_the_title_goes_through_the_tty(self):
        tabs = self.evict(["vim"])
        self.assertEqual(self.writes, [procs.TTY_ABORT.encode() + osc("[zz] Title")])
        self.assertNotIn(D.TTY_RESET.encode(), self.writes[0])
        self.assertEqual(self.names(tabs), ["[zz] Title"])
        self.assertEqual([c[0] for c in tabs.calls if c[0] != "job"], ["name"])
        self.assertTrue(self.pipe.write_end_closed())

    def test_zero_bytes_out_puts_nothing_in_the_tab(self):
        self.wrote = 0
        with self.assertLogs("cc-sessions", "WARNING") as logs:
            tabs = self.evict(["vim"])
        self.assertEqual(self.injected(tabs), [])
        self.assertTrue(any("title not written" in m for m in logs.output), logs.output)

    def test_no_tty_writes_nothing_and_still_names(self):
        with mock.patch.object(procs, "tty_open", lambda tty: None):
            tabs = self.evict(["vim"])
        self.assertEqual((self.writes, self.injected(tabs)), ([], []))
        self.assertEqual(self.names(tabs), ["[zz] Title"])


class DirtyTitles(Base):
    def assert_clean(self, payload):
        start = payload.rindex(b"\x1b]0;") + 4
        self.assertTrue(payload.endswith(b"\x07"))
        body = payload[start:-1].decode("utf-8")
        self.assertFalse([c for c in body if ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f], body)
        return body

    def clean_case(self, jobs):
        tabs = self.evict(jobs, title=DIRTY)
        body = self.assert_clean(self.writes[0])
        self.assertEqual(body, "[zz] Fix ]0;evil bugnextline31mend")
        self.assertEqual(self.names(tabs), [body])

    def test_control_characters_never_reach_the_osc_payload_at_a_shell(self):
        self.clean_case(["zsh"])

    def test_control_characters_never_reach_the_osc_payload_not_at_a_shell(self):
        self.clean_case(["vim"])

    def test_the_banner_text_is_cleaned_too(self):
        self.evict(["zsh"], title=DIRTY)
        banner = self.writes[0][:self.writes[0].rindex(b"\x1b]0;")]
        self.assertNotIn(b"\x1b]0;evil", banner)
        self.assertIn(b"Fix ]0;evil bugnextline31mend: hibernated", banner)

    def test_clean_title_is_bounded(self):
        self.assertEqual(len(tabfile.clean_title("x" * 5000)), tabfile.TITLE_MAX)
        self.assertEqual(len(D.osc_title("y" * 5000)), len("\x1b]0;\x07") + tabfile.TITLE_MAX)


class SameNameAsRestore(Base):
    def test_title_tab_and_eviction_build_the_same_name(self):
        tabs = self.evict(["zsh"], title=DIRTY)
        evicted = self.names(tabs)[0]
        row = ledger.get(self.conn, "s1")
        d = D.Daemon(self.cfg, self.conn, None, None, False)
        rt = FakeTabs(["zsh"])
        rt.tty_now = "ttys001"
        d.tabs = rt

        async def title(r):
            return DIRTY
        d.resume_title = title
        self.assertEqual(arun(d.title_tab(row, time.time())), "titled")
        self.assertEqual(self.names(rt), [evicted])
        self.assertEqual(self.injected(rt), [osc(evicted)])
        self.assertEqual(evicted, D.zz_title(DIRTY))
