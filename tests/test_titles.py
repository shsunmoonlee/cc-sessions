"""Session titles: the transcript's custom-title / ai-title (whole file, cached) beats the
ledger title (a v1 import carries the worktree slug), which beats basename(cwd)."""
import asyncio
import json
import os
import threading
import time

from helpers import TempEnv, assistant, load_daemon, user

from ccsessions import ledger, pending

D = load_daemon()

SID = "7a1b2c3d-1111-4222-8333-444455556666"


def arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def ai(t):
    return {"type": "ai-title", "aiTitle": t, "sessionId": SID}


def custom(t):
    return {"type": "custom-title", "customTitle": t, "sessionId": SID}


class Base(TempEnv):
    def setUp(self):
        super().setUp()
        saved = D.TITLES
        D.TITLES = D.TitleCache()
        self.addCleanup(setattr, D, "TITLES", saved)

    def row(self, tr, **kw):
        r = {"session_id": SID, "transcript": tr, "title": "crispy-spinning-leaf-d1",
             "cwd": "/w/crispy-spinning-leaf-d1"}
        r.update(kw)
        return r

    def count_scans(self):
        calls = []
        real = pending.transcript_title

        def counting(path):
            calls.append(path)
            return real(path)
        pending.transcript_title = counting
        self.addCleanup(setattr, pending, "transcript_title", real)
        return calls


class Reader(Base):
    def test_custom_title_beats_any_ai_title(self):
        tr = self.transcript(SID, [ai("first ai"), custom("mine"), ai("later ai"), user("x")])
        self.assertEqual(pending.transcript_title(tr), "mine")

    def test_last_of_each_kind_wins(self):
        tr = self.transcript(SID, [ai("a1"), ai("a2"), user("x")])
        self.assertEqual(pending.transcript_title(tr), "a2")
        tr = self.transcript(SID, [custom("c1"), custom("c2")], cwd_dir="p2")
        self.assertEqual(pending.transcript_title(tr), "c2")

    def test_no_title_records(self):
        tr = self.transcript(SID, [user("talk about \"ai-title\" here"), assistant("ok")])
        self.assertIsNone(pending.transcript_title(tr))

    def test_title_only_in_the_head_of_a_large_transcript(self):
        filler = assistant("y" * 1000)
        recs = [user("hi"), ai("early title")] + [filler] * (pending.TAIL_BYTES // 1000 + 500)
        tr = self.transcript(SID, recs)
        self.assertGreater(os.path.getsize(tr), pending.TAIL_BYTES + 100000)
        self.assertIsNone(pending.analyze(tr)["title"])  # the tail alone misses it
        self.assertEqual(pending.transcript_title(tr), "early title")
        self.assertEqual(D.title_for(self.row(tr)), "early title")


class Priority(Base):
    def test_v1_slug_loses_to_the_transcript_ai_title(self):
        tr = self.transcript(SID, [user("hi"), ai("Fix session titles")])
        r = self.row(tr)
        self.assertEqual(D.title_for(r), "Fix session titles")
        self.assertEqual(r["title"], "Fix session titles")

    def test_custom_beats_ai_beats_ledger(self):
        tr = self.transcript(SID, [ai("ai one"), custom("renamed")])
        self.assertEqual(D.title_for(self.row(tr)), "renamed")

    def test_no_transcript_titles_uses_the_ledger_title(self):
        tr = self.transcript(SID, [user("hi"), assistant("ok")])
        r = self.row(tr, title="Ledger title")
        self.assertEqual(D.title_for(r), "Ledger title")
        self.assertEqual(r["title"], "Ledger title")

    def test_nothing_falls_back_to_cwd_then_sid(self):
        self.assertEqual(D.title_for(self.row(None, title=None)), "crispy-spinning-leaf-d1")
        self.assertEqual(D.title_for(self.row(None, title=None, cwd=None)), SID[:8])
        missing = os.path.join(self.tmp, "nope.jsonl")
        self.assertEqual(D.title_for(self.row(missing, title="", cwd="/a/b")), "b")


class Cache(Base):
    def test_unchanged_file_is_not_rescanned(self):
        tr = self.transcript(SID, [ai("t1")])
        calls = self.count_scans()
        for _ in range(5):
            self.assertEqual(D.title_for(self.row(tr)), "t1")
        self.assertEqual(len(calls), 1)
        with open(tr, "a") as fh:
            fh.write(json.dumps(custom("t2")) + "\n")
        os.utime(tr, (time.time() + 5, time.time() + 5))
        self.assertEqual(D.title_for(self.row(tr)), "t2")
        self.assertEqual(len(calls), 2)

    def test_cache_is_bounded(self):
        c = D.TitleCache(cap=3)
        paths = [self.transcript("s%d" % i, [ai("t%d" % i)]) for i in range(5)]
        for p in paths:
            c.get(p)
        self.assertEqual(len(c.items), 3)
        self.assertEqual(list(c.items), paths[2:])


class WriteBack(Base):
    def test_heal_titles_cas_writes_the_transcript_title(self):
        tr = self.transcript(SID, [ai("Real title")])
        self.insert(session_id=SID, transcript=tr, title="crispy-spinning-leaf-d1")
        rows = D.live_rows(self.conn)
        before = {r["session_id"]: r.get("title") for r in rows}
        D.title_for(rows[0])
        healed = D.heal_titles(self.conn, rows, before)
        self.assertEqual(healed, [(SID, "crispy-spinning-leaf-d1", "Real title")])
        self.assertEqual(ledger.get(self.conn, SID)["title"], "Real title")

    def test_heal_titles_does_not_clobber_a_concurrent_change(self):
        tr = self.transcript(SID, [ai("Real title")])
        self.insert(session_id=SID, transcript=tr, title="slug")
        rows = D.live_rows(self.conn)
        before = {r["session_id"]: r.get("title") for r in rows}
        D.title_for(rows[0])
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET title='hook wrote' WHERE session_id=?", (SID,))
        self.assertEqual(D.heal_titles(self.conn, rows, before), [])
        self.assertEqual(ledger.get(self.conn, SID)["title"], "hook wrote")

    def test_fallback_titles_are_never_written(self):
        self.insert(session_id=SID, transcript=None, title=None, cwd="/w/slug")
        rows = D.live_rows(self.conn)
        before = {r["session_id"]: r.get("title") for r in rows}
        self.assertEqual(D.title_for(rows[0]), "slug")
        self.assertEqual(D.heal_titles(self.conn, rows, before), [])
        self.assertIsNone(ledger.get(self.conn, SID)["title"])

    def test_daemon_policy_writes_back_on_the_loop_thread(self):
        tr = self.transcript(SID, [ai("Real title")])
        self.insert(session_id=SID, transcript=tr, title="slug")
        d = D.Daemon(self.cfg, self.conn, None, None, True)
        threads = {}
        main = threading.get_ident()

        def fake_compute(cfg, focus, rows, ev, explicit, now):
            threads["compute"] = threading.get_ident()
            titles = {r["session_id"]: D.title_for(r) for r in rows}
            return list(rows), [], {}, titles
        saved = D.compute_policy
        D.compute_policy = fake_compute
        self.addCleanup(setattr, D, "compute_policy", saved)
        real_heal = ledger.heal_title

        def heal(*a):
            threads["heal"] = threading.get_ident()
            return real_heal(*a)
        ledger.heal_title = heal
        self.addCleanup(setattr, ledger, "heal_title", real_heal)
        _, _, _, titles = arun(d.policy({"tty_guid": {}, "visible_ttys": set()}))
        self.assertEqual(titles[SID], "Real title")
        self.assertEqual(ledger.get(self.conn, SID)["title"], "Real title")
        self.assertNotEqual(threads["compute"], main)
        self.assertEqual(threads["heal"], main)


class FakeTabs(object):
    def __init__(self):
        self.calls = []

    async def job_name(self, guid):
        return "-zsh"

    async def tty(self, guid):
        return None

    async def send_text(self, guid, text):
        self.calls.append(("send", guid, text))

    async def set_name(self, guid, name):
        self.calls.append(("name", guid, name))


class FakeSession(object):
    def __init__(self, guid, tty):
        self.session_id, self.tty = guid, tty
        self.sent, self.names = [], []

    async def async_get_variable(self, name):
        return {"tty": "/dev/" + self.tty, "jobName": "-zsh"}.get(name)

    async def async_send_text(self, text):
        self.sent.append(text)

    async def async_set_name(self, name):
        self.names.append(name)


class ResumeNaming(Base):
    def daemon(self):
        d = D.Daemon(self.cfg, self.conn, None, None, False)
        d.tabs = FakeTabs()

        async def ident(r):
            return D.procs.GONE
        d.identity = ident
        return d

    def hibernated(self, records):
        tr = self.transcript(SID, records)
        return self.insert(session_id=SID, transcript=tr, title="crispy-spinning-leaf-d1",
                           state="hibernated", iterm_guid="G2", tty="ttys002",
                           evicted_at=time.time() - 600, cwd="/w/crispy-spinning-leaf-d1")

    def test_wake_names_the_tab_with_the_healed_title(self):
        row = self.hibernated([user("hi"), ai("Wake title")])
        d = self.daemon()
        self.assertEqual(arun(d.wake(row)), "resumed")
        self.assertIn(("name", "G2", "Wake title"), d.tabs.calls)
        self.assertEqual(ledger.get(self.conn, SID)["title"], "Wake title")

    def test_resume_on_focus_names_the_tab_with_the_healed_title(self):
        self.hibernated([user("hi"), custom("Focus title")])
        s = FakeSession("G2", "ttys002")
        arun(self.daemon().on_focus(s))
        self.assertEqual(len(s.sent), 1)
        self.assertEqual(s.names, ["Focus title"])
        self.assertEqual(ledger.get(self.conn, SID)["title"], "Focus title")

    def test_resume_without_transcript_titles_keeps_the_ledger_title(self):
        row = self.hibernated([user("hi"), assistant("ok")])
        d = self.daemon()
        arun(d.wake(row))
        self.assertIn(("name", "G2", "crispy-spinning-leaf-d1"), d.tabs.calls)
