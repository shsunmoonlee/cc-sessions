"""Tab snapshots: the file the daemon writes, what restore picks from it, how restore opens
and binds tabs, the daemon's guards against restore's burst of focus events, the title
request, and the `tabs` CLI. Nothing here talks to iTerm2: osascript is faked."""
import asyncio
import contextlib
import io
import json
import os
import time
import types
from unittest import mock

from helpers import REPO, TempEnv, load_daemon

from ccsessions import cli, config, ledger, procs, restore
from ccsessions import tabs as tabfile

D = load_daemon()

INSTANCE = {"pid": 500, "start": 1000}
OLD = {"pid": 400, "start": 900}
SID_A, SID_B, SID_C = "a" * 8, "b" * 8, "c" * 8


def arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def windows_of(*tabs):
    """Each argument is one tab: (guid, tty)."""
    return [{"window_id": "W1", "tabs": [{"tab_id": "T%d" % i, "sessions": [{"guid": g, "tty": t}]}
                                         for i, (g, t) in enumerate(tabs)]}]


def snap_of(sids, taken_at=2000.0, instance=OLD):
    """A snapshot with one tab per session id (None = a tab with no session)."""
    tabs = []
    for i, sid in enumerate(sids):
        tabs.append({"tab_id": "T%d" % i, "sessions": [
            {"guid": "old-guid-%d" % i, "tty": "ttys%03d" % i, "session_id": sid, "state": "idle",
             "title": "t", "cwd": None}]})
    return {"v": 1, "iterm_pid": instance["pid"], "iterm_start": instance["start"], "taken_at": taken_at,
            "windows": [{"window_id": "W1", "tabs": tabs}]}


def not_alive(r):
    return False


class SnapshotFile(TempEnv):
    def build(self, tabs, rows=(), now=2000.0):
        return tabfile.build_snapshot(INSTANCE, windows_of(*tabs), list(rows), now)

    def path(self, instance=INSTANCE):
        return os.path.join(tabfile.snapshots_dir(self.cfg), "%d-%d.json" % (instance["start"], instance["pid"]))

    def pane(self, snap, i=0):
        return snap["windows"][0]["tabs"][i]["sessions"][0]

    def test_default_dir_is_beside_the_ledger_and_config_overrides(self):
        self.assertEqual(tabfile.snapshots_dir(self.cfg), os.path.join(self.tmp, "state", "cc-sessions-tabs"))
        cfg = config.Config(self.cfg)
        cfg["tab_snapshots_dir"] = "~/elsewhere"
        self.assertEqual(tabfile.snapshots_dir(cfg), os.path.expanduser("~/elsewhere"))
        self.assertIn("tab_snapshots_dir", config.PATH_KEYS)
        cfg = config.Config(config.DEFAULTS)
        self.assertEqual(tabfile.snapshots_dir(cfg), os.path.expanduser("~/.claude/state/cc-sessions-tabs"))

    def test_live_row_on_the_tty_wins_over_a_stale_hibernated_guid(self):
        stale = self.insert(session_id="stale", state="hibernated", iterm_guid="G1", tty="ttys009",
                            last_event_at=3000.0)
        live = self.insert(session_id="live", state="idle", iterm_guid="OTHER", tty="ttys001",
                           last_event_at=1000.0)
        snap = self.build([("G1", "ttys001")], [stale, live])
        self.assertEqual(self.pane(snap)["session_id"], "live")

    def test_hibernated_row_by_guid_when_no_live_row_holds_the_tty(self):
        hib = self.insert(session_id="hib", state="hibernated", iterm_guid="G1", tty="ttys009",
                          title="named", cwd="/w")
        p = self.pane(self.build([("G1", "ttys001")], [hib]))
        self.assertEqual((p["session_id"], p["state"], p["title"], p["cwd"]), ("hib", "hibernated", "named", "/w"))

    def test_newest_live_row_wins_on_one_tty(self):
        a = self.insert(session_id="older", tty="ttys001", last_event_at=100.0)
        b = self.insert(session_id="newer", tty="ttys001", last_event_at=200.0)
        self.assertEqual(self.pane(self.build([("G1", "ttys001")], [b, a]))["session_id"], "newer")

    def test_non_interactive_superseded_and_ended_rows_never_map(self):
        rows = [self.insert(session_id="ni", tty="ttys001", interactive=0),
                self.insert(session_id="sup", tty="ttys001", state="superseded"),
                self.insert(session_id="ended", tty="ttys001", state="idle", ended_at=5.0)]
        p = self.pane(self.build([("G1", "ttys001")], rows))
        self.assertIsNone(p["session_id"])
        self.assertEqual(p["guid"], "G1")
        self.assertEqual(p["tty"], "ttys001")

    def test_a_pane_with_no_tty_read_still_maps_by_guid(self):
        hib = self.insert(session_id="hib", state="hibernated", iterm_guid="G1")
        self.assertEqual(self.pane(self.build([("G1", None)], [hib]))["session_id"], "hib")

    def test_write_only_on_change_ignores_taken_at(self):
        tabs = [("G1", "ttys001")]
        self.assertEqual(tabfile.write_snapshot(self.cfg, self.build(tabs, now=1.0)), "written")
        self.assertEqual(tabfile.write_snapshot(self.cfg, self.build(tabs, now=2.0)), "unchanged")
        self.assertEqual(tabfile.read_snapshot(self.path())["taken_at"], 1.0)
        self.assertEqual(tabfile.write_snapshot(self.cfg, self.build([("G2", "ttys002")], now=3.0)), "written")
        self.assertEqual(tabfile.read_snapshot(self.path())["taken_at"], 3.0)

    def test_write_is_atomic_and_a_failed_rename_keeps_the_old_file(self):
        tabfile.write_snapshot(self.cfg, self.build([("G1", "ttys001")], now=1.0))
        synced = []
        real = os.fsync
        with mock.patch.object(tabfile.os, "fsync", lambda fd: (synced.append(fd), real(fd))[1]):
            tabfile.write_snapshot(self.cfg, self.build([("G2", "ttys002")], now=2.0))
        self.assertGreaterEqual(len(synced), 2)  # the file and the directory
        with mock.patch.object(tabfile.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                tabfile.write_snapshot(self.cfg, self.build([("G3", "ttys003")], now=3.0))
        self.assertEqual(tabfile.read_snapshot(self.path())["taken_at"], 2.0)
        self.assertEqual([n for n in os.listdir(tabfile.snapshots_dir(self.cfg)) if n.startswith(".tmp-")], [])

    def test_never_overwritten_with_zero_tabs(self):
        tabfile.write_snapshot(self.cfg, self.build([("G1", "ttys001")], now=1.0))
        self.assertEqual(tabfile.write_snapshot(self.cfg, self.build([], now=2.0)), "empty")
        snap = tabfile.read_snapshot(self.path())
        self.assertEqual((snap["taken_at"], tabfile.tab_count(snap)), (1.0, 1))

    def test_zero_tabs_creates_no_file(self):
        self.assertEqual(tabfile.write_snapshot(self.cfg, self.build([], now=2.0)), "empty")
        self.assertEqual(tabfile.list_snapshots(self.cfg), [])

    def many(self, n):
        return [("G%d" % i, "ttys%03d" % i) for i in range(n)]

    def siblings(self):
        return [e for e in tabfile.list_snapshots(self.cfg) if e["sibling"]]

    def test_shrink_by_more_than_half_keeps_a_sibling(self):
        tabfile.write_snapshot(self.cfg, self.build(self.many(10), now=1000.0))
        tabfile.write_snapshot(self.cfg, self.build(self.many(4), now=2000.0))
        sib = self.siblings()
        self.assertEqual(len(sib), 1)
        self.assertTrue(sib[0]["path"].endswith(".before-shrink-1000.json"), sib[0]["path"])
        self.assertEqual(tabfile.tab_count(sib[0]["snap"]), 10)
        self.assertEqual(tabfile.tab_count(tabfile.read_snapshot(self.path())), 4)

    def test_shrink_of_exactly_half_keeps_none(self):
        tabfile.write_snapshot(self.cfg, self.build(self.many(10), now=1000.0))
        tabfile.write_snapshot(self.cfg, self.build(self.many(5), now=2000.0))
        self.assertEqual(self.siblings(), [])

    def test_prune_keeps_the_newest_files_and_drops_their_markers(self):
        d = tabfile.snapshots_dir(self.cfg)
        os.makedirs(d)
        names = ["%d-1.json" % (100 + i) for i in range(25)]
        for i, n in enumerate(names):
            p = os.path.join(d, n)
            with open(p, "w") as fh:
                json.dump(snap_of(["x"], taken_at=float(i)), fh)
            os.utime(p, (1000 + i, 1000 + i))
        tabfile.mark_consumed(os.path.join(d, names[0]))
        tabfile.mark_consumed(os.path.join(d, names[-1]))
        tabfile.prune(self.cfg)
        left = sorted(n for n in os.listdir(d) if n.endswith(".json"))
        self.assertEqual(left, sorted(names[5:]))
        self.assertFalse(tabfile.consumed(os.path.join(d, names[0])))
        self.assertTrue(tabfile.consumed(os.path.join(d, names[-1])))

    def test_prune_runs_on_write_and_removes_only_old_temp_files(self):
        d = tabfile.snapshots_dir(self.cfg)
        os.makedirs(d)
        old, young = os.path.join(d, ".tmp-old"), os.path.join(d, ".tmp-young")
        for p in (old, young):
            open(p, "w").close()
        os.utime(old, (1, 1))
        tabfile.write_snapshot(self.cfg, self.build([("G1", "ttys001")]))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(young))

    def test_unreadable_files_are_not_snapshots(self):
        d = tabfile.snapshots_dir(self.cfg)
        os.makedirs(d)
        with open(os.path.join(d, "1-1.json"), "w") as fh:
            fh.write("{torn")
        with open(os.path.join(d, "2-2.json"), "w") as fh:
            json.dump({"v": 99, "windows": [], "taken_at": 1}, fh)
        self.assertEqual(tabfile.list_snapshots(self.cfg), [])

    def write_raw(self, snap):
        d = tabfile.snapshots_dir(self.cfg)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "%d-%d.json" % (snap["iterm_start"], snap["iterm_pid"]))
        with open(p, "w") as fh:
            json.dump(snap, fh)
        return p

    def test_previous_ignores_the_current_instance(self):
        self.write_raw(snap_of(["x"], taken_at=5000.0, instance=INSTANCE))
        older = self.write_raw(snap_of(["x"], taken_at=2000.0, instance=OLD))
        self.assertEqual(tabfile.previous(self.cfg, INSTANCE)["path"], older)
        self.assertEqual(tabfile.previous(self.cfg, None)["snap"]["taken_at"], 5000.0)
        self.assertIsNone(tabfile.previous(self.cfg, INSTANCE, []))

    def test_previous_is_none_with_only_the_current_instance(self):
        self.write_raw(snap_of(["x"], instance=INSTANCE))
        self.assertIsNone(tabfile.previous(self.cfg, INSTANCE))

    def test_previous_never_picks_a_shrink_sibling(self):
        tabfile.write_snapshot(self.cfg, tabfile.build_snapshot(OLD, windows_of(*self.many(10)), [], 1000.0))
        tabfile.write_snapshot(self.cfg, tabfile.build_snapshot(OLD, windows_of(*self.many(2)), [], 2000.0))
        self.assertEqual(tabfile.tab_count(tabfile.previous(self.cfg, INSTANCE)["snap"]), 2)

    def test_next_instance_start(self):
        self.write_raw(snap_of(["x"], instance={"pid": 1, "start": 100}))
        self.write_raw(snap_of(["x"], instance={"pid": 2, "start": 200}))
        s = snap_of(["x"], instance={"pid": 1, "start": 100})
        self.assertEqual(tabfile.next_instance_start(self.cfg, s, {"pid": 3, "start": 300}), 200)
        s = snap_of(["x"], instance={"pid": 2, "start": 200})
        self.assertEqual(tabfile.next_instance_start(self.cfg, s, {"pid": 3, "start": 300}), 300)
        self.assertIsNone(tabfile.next_instance_start(self.cfg, s, None))

    def test_iterm_instance_picks_the_newest_of_two_pids(self):
        starts = {11: (500.0, "iTerm2"), 22: (900.0, "iTerm2")}
        with mock.patch.object(tabfile.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="11\n22\n")):
            with mock.patch.object(tabfile.procs, "lstart_comm", starts.get):
                self.assertEqual(tabfile.iterm_instance(), {"pid": 22, "start": 900})

    def test_iterm_instance_none_when_not_running(self):
        calls = []

        def fake(args, **kw):
            calls.append(args)
            return types.SimpleNamespace(stdout="")
        with mock.patch.object(tabfile.subprocess, "run", fake):
            self.assertIsNone(tabfile.iterm_instance())
        self.assertEqual(calls[0], ["pgrep", "-ax", "iTerm2"])

    def test_clean_title_strips_non_printables(self):
        self.assertEqual(tabfile.clean_title("a\x1b]0;evil\x07b\nc\td"), "a]0;evilbcd")
        self.assertEqual(tabfile.clean_title(None), "")


class SelectSnapshot(TempEnv):
    def row(self, sid, **kw):
        r = {"session_id": sid, "state": "idle", "interactive": 1, "ended_at": None, "end_reason": None,
             "cwd": self.tmp, "pid": 1, "pid_start": 1, "iterm_guid": None, "started_at": 1000.0,
             "resumed_at": None, "restored_at": None, "last_event_at": 1000.0}
        r.update(kw)
        return r

    def sel(self, snap, rows, alive=not_alive, live=(), **kw):
        return restore.select_snapshot(snap, rows, self.cfg, alive, set(live), **kw)

    def why(self, snap, rows, **kw):
        return {r["session_id"]: w for r, w in self.sel(snap, rows, **kw)[2]}

    def test_snapshot_order_is_kept_across_run_and_typed(self):
        rows = [self.row("s1"), self.row("s2", state="hibernated"), self.row("s3"),
                self.row("s4", state="hibernated")]
        run, typed, skipped, _ = self.sel(snap_of(["s3", "s2", "s1", "s4"]), rows)
        self.assertEqual([r["session_id"] for r in run], ["s3", "s1"])
        self.assertEqual([r["session_id"] for r in typed], ["s2", "s4"])
        self.assertEqual(skipped, [])
        plan = restore.tab_plan(run, typed)
        self.assertEqual([(r["session_id"], it) for r, it in plan],
                         [("s3", True), ("s2", False), ("s1", True), ("s4", False)])

    def test_deduped_by_session_and_non_claude_reported(self):
        run, _, _, non_claude = self.sel(snap_of(["s1", None, "s1"]), [self.row("s1")])
        self.assertEqual([r["session_id"] for r in run], ["s1"])
        self.assertEqual(len(non_claude), 1)

    def test_unknown_process_in_a_live_tab_is_not_resumed_a_second_time(self):
        rows = [self.row("s1", iterm_guid="LIVE")]
        unsure = lambda r: True
        run, _, skipped, _ = self.sel(snap_of(["s1"]), rows, live={"LIVE"}, unsure_fn=unsure)
        self.assertEqual(run, [])
        self.assertIn("process state unknown", skipped[0][1])
        self.assertNotIn(skipped[0][1], restore.QUIET_SKIPS)

    def test_unknown_process_without_a_live_tab_is_still_restored(self):
        rows = [self.row("s1", iterm_guid="dead")]
        run, _, _, _ = self.sel(snap_of(["s1"]), rows, live={"LIVE"}, unsure_fn=lambda r: True)
        self.assertEqual([r["session_id"] for r in run], ["s1"])

    def test_dead_process_in_a_reused_tab_is_still_restored(self):
        rows = [self.row("s1", iterm_guid="LIVE")]
        run, _, _, _ = self.sel(snap_of(["s1"]), rows, live={"LIVE"}, unsure_fn=lambda r: False)
        self.assertEqual([r["session_id"] for r in run], ["s1"])

    def test_every_skip_reason(self):
        rows = [self.row("sup", state="superseded"), self.row("ni", interactive=0),
                self.row("up"), self.row("open", state="hibernated", iterm_guid="LIVE"),
                self.row("quit", state="ended", ended_at=1500.0, end_reason="prompt_input_exit"),
                self.row("cleared", state="ended", ended_at=1500.0, end_reason="clear"),
                self.row("out", state="ended", ended_at=1500.0, end_reason="logout"),
                self.row("nocwd", cwd=os.path.join(self.tmp, "gone")), self.row("empty", cwd=None)]
        ids = ["missing"] + [r["session_id"] for r in rows]
        why = self.why(snap_of(ids), rows, alive=lambda r: r["session_id"] == "up", live={"LIVE"})
        self.assertEqual(why["missing"], "no ledger row")
        self.assertEqual(why["sup"], "superseded")
        self.assertEqual(why["ni"], "non-interactive")
        self.assertEqual(why["up"], "already running")
        self.assertEqual(why["open"], "tab still open")
        self.assertEqual(why["quit"], "exited (prompt_input_exit)")
        self.assertEqual(why["cleared"], "exited (clear)")
        self.assertEqual(why["out"], "exited (logout)")
        self.assertTrue(why["nocwd"].startswith("cwd gone"))
        self.assertTrue(why["empty"].startswith("cwd gone"))
        self.assertEqual(len(why), len(ids))

    def test_exited_sessions_never_reopened_whenever_they_ended(self):
        for ended in (1.0, 1999.0, 2001.0, time.time()):
            rows = [self.row("quit", state="ended", ended_at=ended, end_reason="prompt_input_exit")]
            run, typed, skipped, _ = self.sel(snap_of(["quit"], taken_at=2000.0), rows)
            self.assertEqual((run, typed), ([], []), ended)
            self.assertEqual(skipped[0][1], "exited (prompt_input_exit)")

    def test_exited_after_taken_at_is_not_appended_either(self):
        rows = [self.row("quit", state="ended", started_at=3000.0, ended_at=3100.0, end_reason="prompt_input_exit")]
        run, typed, skipped, _ = self.sel(snap_of([], taken_at=2000.0), rows)
        self.assertEqual((run, typed, skipped), ([], [], []))

    def test_killed_session_reason_other_runs_whenever_it_ended(self):
        rows = [self.row("k", state="ended", ended_at=1.0, end_reason="other")]
        self.assertEqual([r["session_id"] for r in self.sel(snap_of(["k"]), rows)[0]], ["k"])

    def test_no_age_or_count_limit(self):
        ancient = time.time() - 3 * 365 * 86400
        rows = [self.row("s%d" % i, last_event_at=ancient, started_at=ancient) for i in range(300)]
        cfg = config.Config(self.cfg)
        cfg["restore_limit"] = 1
        cfg["restore_max_age_days"] = 1
        run, _, skipped, _ = restore.select_snapshot(snap_of(["s%d" % i for i in range(300)]), rows, cfg,
                                                     not_alive, set())
        self.assertEqual((len(run), skipped), (300, []))

    def test_rows_started_after_taken_at_appended_newest_first(self):
        rows = [self.row("tab", started_at=1000.0),
                self.row("late1", started_at=3000.0), self.row("late2", started_at=4000.0),
                self.row("resumed", started_at=100.0, resumed_at=3500.0),
                self.row("late_other", state="ended", started_at=2500.0, ended_at=2600.0, end_reason="other"),
                self.row("before", started_at=1500.0)]
        run, _, _, _ = self.sel(snap_of(["tab"], taken_at=2000.0), rows)
        self.assertEqual([r["session_id"] for r in run], ["tab", "late2", "resumed", "late1", "late_other"])

    def test_appended_rows_skip_alive_non_interactive_superseded_and_live_tabs(self):
        rows = [self.row("alive", started_at=3000.0), self.row("ni", started_at=3000.0, interactive=0),
                self.row("sup", started_at=3000.0, state="superseded"),
                self.row("open", started_at=3000.0, state="hibernated", iterm_guid="LIVE")]
        run, typed, _, _ = self.sel(snap_of([], taken_at=2000.0), rows,
                                    alive=lambda r: r["session_id"] == "alive", live={"LIVE"})
        self.assertEqual((run, typed), ([], []))

    def test_until_bounds_appended_rows_to_the_snapshots_instance(self):
        rows = [self.row("inside", started_at=3000.0),
                self.row("later_instance", started_at=6000.0),
                self.row("closed_later", started_at=6000.0, state="ended", ended_at=6100.0, end_reason="other")]
        run, _, _, _ = self.sel(snap_of([], taken_at=2000.0), rows, until=5000.0)
        self.assertEqual([r["session_id"] for r in run], ["inside"])

    def test_ended_after_the_next_instance_started_stays_closed(self):
        rows = [self.row("closed", state="ended", ended_at=9000.0, end_reason="other"),
                self.row("killed", state="ended", ended_at=4000.0, end_reason="other"),
                self.row("hook_late", state="ended", ended_at=5030.0, end_reason="other")]
        run, _, skipped, _ = self.sel(snap_of(["closed", "killed", "hook_late"]), rows, until=5000.0)
        self.assertEqual([r["session_id"] for r in run], ["killed", "hook_late"])
        self.assertEqual(skipped[0][1], "ended after that iTerm2 instance")

    def test_restored_from_this_snapshot_then_closed_stays_closed(self):
        rows = [self.row("was_typed", state="hibernated", iterm_guid="closed-tab", restored_at=3000.0),
                self.row("earlier", state="hibernated", restored_at=1000.0)]
        _, typed, skipped, _ = self.sel(snap_of(["was_typed", "earlier"], taken_at=2000.0), rows)
        self.assertEqual([r["session_id"] for r in typed], ["earlier"])
        self.assertEqual(skipped[0][1], "already restored; its tab was closed since")

    def test_rebuildable_worktree_allowed_for_run_and_typed(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        wt = os.path.join(repo, ".claude", "worktrees", "gone")
        rows = [self.row("r", cwd=wt), self.row("t", state="hibernated", cwd=wt)]
        run, typed, skipped, _ = self.sel(snap_of(["r", "t"]), rows)
        self.assertEqual(([r["session_id"] for r in run], [r["session_id"] for r in typed], skipped), (["r"], ["t"], []))

    def test_cheap_checks_come_before_any_process_probe(self):
        probed = []
        rows = [self.row("parked", state="hibernated", iterm_guid="LIVE"),
                self.row("bound", iterm_guid="LIVE", restored_at=3000.0),
                self.row("quit", state="ended", ended_at=5.0, end_reason="clear")]
        self.sel(snap_of(["parked", "bound", "quit"]), rows, alive=lambda r: probed.append(r) or False,
                 live={"LIVE"})
        self.assertEqual(probed, [])

    def test_a_live_guid_alone_does_not_prove_a_live_state_row_is_open(self):
        rows = [self.row("open", iterm_guid="LIVE")]
        run, _, skipped, _ = self.sel(snap_of(["open"]), rows, live={"LIVE"})
        self.assertEqual(([r["session_id"] for r in run], skipped), (["open"], []))

    def test_a_live_guid_with_a_running_process_is_open(self):
        rows = [self.row("open", iterm_guid="LIVE")]
        self.assertEqual(self.why(snap_of(["open"]), rows, alive=lambda r: True, live={"LIVE"})["open"],
                         "tab still open")

    def test_a_live_guid_on_a_row_restore_bound_is_open(self):
        rows = [self.row("bound", state="ended", ended_at=2500.0, end_reason="other", iterm_guid="LIVE",
                         restored_at=3000.0)]
        self.assertEqual(self.why(snap_of(["bound"], taken_at=2000.0), rows, live={"LIVE"})["bound"],
                         "tab still open")

    def test_a_restored_row_from_before_the_snapshot_proves_nothing(self):
        rows = [self.row("old", iterm_guid="LIVE", restored_at=1000.0)]
        run, _, _, _ = self.sel(snap_of(["old"], taken_at=2000.0), rows, live={"LIVE"})
        self.assertEqual([r["session_id"] for r in run], ["old"])


class FakeITerm(object):
    """Stands in for osascript: parses what batch_script produced and answers like iTerm2."""

    def __init__(self, test, fail_at=None, malformed_at=None):
        self.test, self.fail_at, self.malformed_at = test, fail_at, malformed_at
        self.scripts, self.guids, self.selected = [], [], []
        self.bound_at_call = []
        self.window = 77

    def __call__(self, script, timeout=None):
        self.scripts.append(script)
        self.bound_at_call.append(self.test.bound_rows())
        if "to select" in script:
            self.selected.append(script)
            return ""
        out = ["window %d" % self.window]
        for line in script.splitlines():
            if not line.startswith("tell s to write text"):
                continue
            n = len(self.guids)
            if self.fail_at == n:
                out.append("error tab refused")
                break
            guid = "bad" if self.malformed_at == n else "w0t%dp0:GUID%04d" % (n, n)
            self.guids.append(guid)
            out.append("%s ttys%03d" % (guid, n + 100))
        return "\n".join(out) + "\n"


class RestoreEnv(TempEnv):
    extra_config = {"resume_command": "echo-resume"}

    def setUp(self):
        super().setUp()
        self.lines = []
        self.current = INSTANCE
        self.live = set()
        self.guid_calls = 0
        for obj, name, value in ((tabfile, "iterm_instance", lambda: self.current),
                                 (restore, "iterm_guids", self.fake_guids)):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def fake_guids(self):
        self.guid_calls += 1
        return self.live

    def bound_rows(self):
        return self.conn.execute("SELECT COUNT(*) FROM sessions WHERE restored_at IS NOT NULL").fetchone()[0]

    def snapshot_file(self, sids, instance=OLD, taken_at=None):
        taken_at = time.time() - 600 if taken_at is None else taken_at
        d = tabfile.snapshots_dir(self.cfg)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "%d-%d.json" % (instance["start"], instance["pid"]))
        with open(p, "w") as fh:
            json.dump(snap_of(sids, taken_at, instance), fh)
        return p

    def dead_row(self, sid, **kw):
        kw.setdefault("pid", None)
        kw.setdefault("started_at", 1.0)
        return self.insert(session_id=sid, **kw)

    def restore_run(self, iterm=None, **kw):
        self.lines = []
        iterm = iterm or FakeITerm(self)
        with mock.patch.object(restore, "_osascript", iterm):
            rc = restore.run(self.cfg, self.conn, out=self.lines.append, **kw)
        return rc, iterm

    def requests(self):
        return [(r[0], r[1]) for r in self.conn.execute("SELECT kind, session_id FROM requests ORDER BY id")]


class RunSnapshot(RestoreEnv):
    def test_opens_in_snapshot_order_and_reports_on_one_last_line(self):
        self.dead_row(SID_A)
        self.dead_row(SID_B, state="hibernated", iterm_guid="dead")
        self.dead_row(SID_C)
        self.snapshot_file([SID_C, SID_B, SID_A, None])
        rc, iterm = self.restore_run()
        self.assertEqual(rc, 0)
        writes = [l for l in iterm.scripts[0].splitlines() if l.startswith("tell s to write text")]
        self.assertEqual(len(writes), 3)
        self.assertIn(SID_C, writes[0])
        self.assertIn(SID_B, writes[1])
        self.assertTrue(writes[1].endswith("newline NO"))
        self.assertFalse(writes[0].endswith("newline NO"))
        self.assertIn(SID_A, writes[2])
        self.assertTrue(self.lines[-1].startswith("opened 3 tabs (2 running, 1 typed) from "), self.lines)
        self.assertTrue(any("without a session" in l for l in self.lines), self.lines)

    def test_delay_differs_for_run_and_typed(self):
        script = restore.batch_script([("a", True), ("b", False)])
        self.assertIn("delay 3\n", script)
        self.assertIn("delay 0.4\n", script)

    def test_nothing_to_restore_is_exit_0(self):
        self.snapshot_file([None])
        rc, iterm = self.restore_run()
        self.assertEqual((rc, self.lines[-1], iterm.scripts), (0, "nothing to restore", []))

    def test_only_the_current_instance_has_a_file(self):
        self.dead_row(SID_A)
        self.snapshot_file([SID_A], instance=INSTANCE)
        rc, iterm = self.restore_run()
        self.assertEqual((rc, self.lines[-1], iterm.scripts), (0, "nothing to restore", []))

    def test_legacy_path_when_no_snapshot_exists(self):
        self.insert(session_id=SID_A, pid=None, last_event_at=time.time() - 60)
        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            return types.SimpleNamespace(returncode=0, stdout="w0t0p0:ABCDEF12 ttys001\n", stderr="")
        with mock.patch.object(restore.subprocess, "run", fake_run):
            restore.run(self.cfg, self.conn, dry_run=False, out=self.lines.append)
        self.assertEqual(self.guid_calls, 0)
        self.assertEqual(self.lines[-1], "opened 1 tabs.")
        self.assertNotIn("-", calls[0])

    def test_batches_of_15_reuse_the_window_id(self):
        sids = ["%08d" % i for i in range(32)]
        for s in sids:
            self.dead_row(s)
        self.snapshot_file(sids)
        rc, iterm = self.restore_run()
        batches = [s for s in iterm.scripts if "write text" in s]
        self.assertEqual(len(batches), 3)
        self.assertEqual([b.count("write text") for b in batches], [15, 15, 2])
        self.assertIn("create window with default profile", batches[0])
        for b in batches[1:]:
            self.assertNotIn("create window", b)
            self.assertIn("set w to window id 77", b)
        self.assertIn("window id 77", iterm.selected[0])
        self.assertTrue(self.lines[-1].startswith("opened 32 tabs (32 running, 0 typed)"))

    def test_script_goes_through_stdin(self):
        calls = []

        def fake_run(args, **kw):
            calls.append((args, kw))
            return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(restore.subprocess, "run", fake_run):
            self.assertEqual(restore._osascript("SCRIPT", 5), "ok")
        self.assertEqual(calls[0][0], ["osascript", "-"])
        self.assertEqual(calls[0][1]["input"], "SCRIPT")

    def test_osascript_failure_raises(self):
        with mock.patch.object(restore.subprocess, "run",
                               lambda a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="nope")):
            with self.assertRaises(RuntimeError):
                restore._osascript("SCRIPT")

    def test_parse_batch(self):
        w, opened, err = restore.parse_batch("window 12\nw0t0p0:ABCDEF12 /dev/ttys005\nw0t1p0:ABCDEF13 \nerror boom\n")
        self.assertEqual((w, err), (12, "boom"))
        self.assertEqual(opened, [("w0t0p0:ABCDEF12", "ttys005"), ("w0t1p0:ABCDEF13", None)])
        self.assertEqual(restore.parse_batch("!!! ttys001\n")[1], [(None, "ttys001")])

    def test_binding_happens_only_after_every_tab_exists_and_first_tab_is_selected(self):
        sids = ["%08d" % i for i in range(20)]
        for s in sids:
            self.dead_row(s, state="hibernated", iterm_guid="dead-%s" % s)
        self.snapshot_file(sids)
        _, iterm = self.restore_run()
        self.assertEqual(iterm.bound_at_call, [0, 0, 0])  # two batches and the select
        self.assertEqual(self.bound_rows(), 20)
        self.assertIn("to select", iterm.scripts[-1])
        row = ledger.get(self.conn, sids[0])
        self.assertEqual((row["iterm_guid"], row["tty"]), (iterm.guids[0], "ttys100"))
        self.assertIsNotNone(row["restored_at"])

    def test_run_rows_are_bound_too_and_title_requested_only_for_typed(self):
        self.dead_row(SID_A)
        self.dead_row(SID_B, state="hibernated", iterm_guid="dead")
        self.snapshot_file([SID_A, SID_B])
        self.restore_run()
        self.assertIsNotNone(ledger.get(self.conn, SID_A)["restored_at"])
        self.assertEqual(self.requests(), [("title", SID_B)])

    def test_row_that_moved_since_selection_is_not_rebound(self):
        self.dead_row(SID_A, state="hibernated", iterm_guid="dead")
        self.snapshot_file([SID_A])

        class Moving(FakeITerm):
            def __call__(s, script, timeout=None):
                out = FakeITerm.__call__(s, script, timeout)
                if "write text" in script:
                    with ledger.tx(self.conn):
                        self.conn.execute("UPDATE sessions SET state='idle', state_since=999")
                return out
        self.restore_run(Moving(self))
        r = ledger.get(self.conn, SID_A)
        self.assertEqual((r["iterm_guid"], r["restored_at"]), ("dead", None))
        self.assertEqual(self.requests(), [])

    def test_failure_binds_what_opened_and_selects_the_first_tab(self):
        sids = ["%08d" % i for i in range(20)]
        for s in sids:
            self.dead_row(s, state="hibernated", iterm_guid="dead-%s" % s)
        self.snapshot_file(sids)
        rc, iterm = self.restore_run(FakeITerm(self, fail_at=17), auto=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.bound_rows(), 17)
        self.assertEqual(len(iterm.selected), 1)
        self.assertEqual(len(self.requests()), 17)
        self.assertTrue(any("3 of 20 tabs did not open: tab refused" in l for l in self.lines), self.lines)
        self.assertTrue(self.lines[-1].startswith("opened 17 tabs (0 running, 17 typed)"), self.lines)
        self.assertFalse(tabfile.consumed(tabfile.previous(self.cfg, self.current)["path"]))

    def test_exception_mid_batch_still_binds_earlier_batches(self):
        sids = ["%08d" % i for i in range(20)]
        for s in sids:
            self.dead_row(s)
        self.snapshot_file(sids)

        class Dies(FakeITerm):
            def __call__(s, script, timeout=None):
                if len(s.scripts) == 1:
                    s.scripts.append(script)
                    raise OSError("osascript vanished")
                return FakeITerm.__call__(s, script, timeout)
        self.restore_run(Dies(self))
        self.assertEqual(self.bound_rows(), 15)
        self.assertTrue(any("osascript vanished" in l for l in self.lines), self.lines)

    def test_a_malformed_id_keeps_later_tabs_aligned(self):
        self.dead_row(SID_A)
        self.dead_row(SID_B)
        self.snapshot_file([SID_A, SID_B])
        _, iterm = self.restore_run(FakeITerm(self, malformed_at=0))
        self.assertIsNone(ledger.get(self.conn, SID_A)["restored_at"])
        self.assertEqual(ledger.get(self.conn, SID_B)["iterm_guid"], iterm.guids[1])

    def test_second_run_opens_nothing_via_live_guids(self):
        self.dead_row(SID_A)
        self.dead_row(SID_B, state="hibernated", iterm_guid="dead")
        self.snapshot_file([SID_A, SID_B])
        _, iterm = self.restore_run()
        self.live = set(iterm.guids)
        rc, again = self.restore_run()
        self.assertEqual((rc, self.lines[-1], again.scripts), (0, "nothing to restore", []))

    def relaunched(self):
        """iTerm2 A died; B is up with one empty tab on a pty one of A's Claude tabs held."""
        now = time.time()
        self.current = {"pid": 500, "start": int(now - 28)}
        self.snapshot_file([SID_A], taken_at=now - 60)
        self.live = {"EMPTY-TAB"}
        ledger.stamp_visible(self.conn, {"ttys001": "EMPTY-TAB"}, now)
        self.assertEqual(ledger.get(self.conn, SID_A)["iterm_guid"], "EMPTY-TAB")
        return now

    def test_crash_tty_reused_by_the_new_empty_tab_hook_never_landed(self):
        self.dead_row(SID_A, tty="ttys001", state="idle", iterm_guid="dead")
        self.relaunched()
        rc, iterm = self.restore_run(auto=True)
        self.assertIn(SID_A, iterm.scripts[0])
        self.assertTrue(self.lines[-1].startswith("opened 1 tabs (1 running, 0 typed)"), self.lines)

    def test_crash_tty_reused_by_the_new_empty_tab_late_session_end(self):
        self.dead_row(SID_A, tty="ttys001", state="idle", iterm_guid="dead")
        now = self.relaunched()
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET state='ended', ended_at=?, end_reason='other'", (now - 5,))
        rc, iterm = self.restore_run(auto=True)
        self.assertIn(SID_A, iterm.scripts[0])
        self.assertTrue(self.lines[-1].startswith("opened 1 tabs (1 running, 0 typed)"), self.lines)

    def test_second_click_after_the_tty_reuse_trace_opens_nothing(self):
        self.dead_row(SID_A, tty="ttys001", state="idle", iterm_guid="dead")
        self.relaunched()
        _, iterm = self.restore_run()
        self.live = {"EMPTY-TAB"} | set(iterm.guids)
        rc, again = self.restore_run()
        self.assertEqual((rc, again.scripts, self.lines[-1]), (0, [], "nothing to restore"))

    def test_second_click_while_a_run_row_lost_the_bind_race_but_is_alive(self):
        self.dead_row(SID_A, pid=111, pid_start=1, iterm_guid="dead")
        self.snapshot_file([SID_A])
        with mock.patch.object(procs, "identity_alive", lambda pid, start: True):
            rc, iterm = self.restore_run()
        self.assertEqual((rc, iterm.scripts, self.lines[-1]), (0, [], "nothing to restore"))

    def alive_111(self):
        return mock.patch.object(procs, "identity_alive", lambda pid, start: pid == 111)

    def test_auto_keeps_the_snapshot_for_a_process_still_dying(self):
        self.dead_row(SID_A, pid=111, pid_start=1, iterm_guid="dead")
        self.dead_row(SID_B)
        p = self.snapshot_file([SID_A, SID_B])
        with self.alive_111():
            _, iterm = self.restore_run(auto=True)
        self.assertNotIn(SID_A, iterm.scripts[0])
        self.assertIn(SID_B, iterm.scripts[0])
        self.assertFalse(tabfile.consumed(p))
        self.assertTrue(any("not marked restored" in l for l in self.lines), self.lines)
        # it died since: the next --auto opens it, and only it, then consumes the file
        self.live = set(iterm.guids)
        _, again = self.restore_run(auto=True)
        self.assertIn(SID_A, again.scripts[0])
        self.assertNotIn(SID_B, again.scripts[0])
        self.assertTrue(tabfile.consumed(p))

    def test_auto_with_only_a_dying_process_does_not_consume(self):
        self.dead_row(SID_A, pid=111, pid_start=1, iterm_guid="dead")
        p = self.snapshot_file([SID_A])
        with self.alive_111():
            _, iterm = self.restore_run(auto=True)
        self.assertEqual((iterm.scripts, self.lines[-1]), ([], "nothing to restore"))
        self.assertFalse(tabfile.consumed(p))

    def test_auto_consumes_when_the_running_process_has_a_live_tab(self):
        self.dead_row(SID_A, pid=111, pid_start=1, iterm_guid="LIVE")
        p = self.snapshot_file([SID_A])
        self.live = {"LIVE"}
        with self.alive_111():
            self.restore_run(auto=True)
        self.assertTrue(tabfile.consumed(p))

    def test_lock_loser_prints_and_exits_0(self):
        self.dead_row(SID_A)
        self.snapshot_file([SID_A])
        lock = tabfile.restore_lock(self.cfg)
        self.addCleanup(lock.close)
        rc, iterm = self.restore_run()
        self.assertEqual((rc, self.lines, iterm.scripts), (0, ["restore already running"], []))
        self.assertTrue(tabfile.restore_running(self.cfg))
        lock.close()
        self.assertFalse(tabfile.restore_running(self.cfg))
        rc, iterm = self.restore_run()
        self.assertTrue(self.lines[-1].startswith("opened 1 tabs"), self.lines)

    def test_lock_is_released_when_the_run_raises(self):
        self.snapshot_file([SID_A])
        with mock.patch.object(restore, "_run_snapshot", side_effect=ValueError("x")):
            with self.assertRaises(ValueError):
                restore.run_snapshot(self.cfg, self.conn, out=self.lines.append)
        self.assertFalse(tabfile.restore_running(self.cfg))

    def test_dry_run_takes_no_lock_and_opens_nothing(self):
        self.dead_row(SID_A)
        self.dead_row(SID_B, state="hibernated", iterm_guid="dead")
        p = self.snapshot_file([SID_A, SID_B])
        lock = tabfile.restore_lock(self.cfg)
        self.addCleanup(lock.close)
        rc, iterm = self.restore_run(auto=True, dry_run=True)
        self.assertEqual((rc, iterm.scripts), (0, []))
        self.assertTrue(self.lines[-1].startswith("dry-run: would open 2 tabs (1 running, 1 typed) from "), self.lines)
        self.assertEqual(self.bound_rows(), 0)
        self.assertEqual(self.requests(), [])
        self.assertFalse(tabfile.consumed(p))

    def test_iterm_guids_none_aborts_without_consuming(self):
        self.dead_row(SID_A)
        p = self.snapshot_file([SID_A])
        self.live = None
        rc, iterm = self.restore_run(auto=True)
        self.assertEqual((rc, iterm.scripts), (1, []))
        self.assertEqual(self.lines[-1], "iTerm2 could not be asked; retry")
        self.assertFalse(tabfile.consumed(p))
        self.assertEqual(self.bound_rows(), 0)
        self.live = set()
        rc, iterm = self.restore_run(auto=True)  # the retry still works
        self.assertTrue(self.lines[-1].startswith("opened 1 tabs"), self.lines)

    def test_iterm2_not_running_is_not_blind_and_never_asks_it(self):
        self.dead_row(SID_A)
        self.snapshot_file([SID_A])
        self.current = None
        self.live = None
        rc, _ = self.restore_run(auto=True)
        self.assertEqual(self.guid_calls, 0)
        self.assertTrue(self.lines[-1].startswith("opened 1 tabs"), self.lines)

    def test_auto_consumes_a_snapshot_once(self):
        self.dead_row(SID_A)
        p = self.snapshot_file([SID_A])
        self.restore_run(auto=True)
        self.assertTrue(tabfile.consumed(p))
        # the tab died again, and a later login must not reopen it from the same file
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET restored_at=NULL")
        rc, iterm = self.restore_run(auto=True)
        self.assertEqual((rc, iterm.scripts, self.lines[-1]), (0, [], "nothing to restore"))

    def test_auto_with_nothing_to_open_consumes_too(self):
        p = self.snapshot_file([None])
        self.restore_run(auto=True)
        self.assertTrue(tabfile.consumed(p))

    def test_auto_does_not_need_the_bootmark(self):
        self.dead_row(SID_A)
        self.snapshot_file([SID_A])
        with open(self.cfg.path("bootmark"), "w") as fh:
            fh.write(str(procs.boot_time()))
        self.restore_run(auto=True)
        self.assertTrue(self.lines[-1].startswith("opened 1 tabs"), self.lines)

    def test_manual_run_ignores_the_marker(self):
        self.dead_row(SID_A)
        p = self.snapshot_file([SID_A])
        tabfile.mark_consumed(p)
        self.restore_run()
        self.assertTrue(self.lines[-1].startswith("opened 1 tabs"), self.lines)

    def test_snapshot_flag_picks_that_file(self):
        self.dead_row(SID_A)
        self.dead_row(SID_B)
        older = self.snapshot_file([SID_A], instance={"pid": 7, "start": 700}, taken_at=time.time() - 9000)
        self.snapshot_file([SID_B], instance=OLD, taken_at=time.time() - 60)
        _, iterm = self.restore_run(snapshot=older)
        self.assertIn(SID_A, iterm.scripts[0])
        self.assertNotIn(SID_B, iterm.scripts[0])

    def test_older_snapshot_does_not_pull_in_every_later_session(self):
        self.dead_row(SID_A)
        self.dead_row(SID_B, started_at=time.time() - 30)
        older = self.snapshot_file([SID_A], instance={"pid": 7, "start": 700}, taken_at=time.time() - 9000)
        self.snapshot_file([], instance={"pid": 8, "start": int(time.time() - 8000)})
        _, iterm = self.restore_run(snapshot=older)
        self.assertNotIn(SID_B, iterm.scripts[0])

    def test_unreadable_snapshot_flag_is_exit_1(self):
        rc, _ = self.restore_run(snapshot=os.path.join(self.tmp, "nope.json"))
        self.assertEqual(rc, 1)

    def test_cwd_gone_is_reported(self):
        self.dead_row(SID_A, cwd=os.path.join(self.tmp, "gone"))
        self.snapshot_file([SID_A])
        self.restore_run()
        self.assertTrue(any(l.startswith("  skip %s: cwd gone" % SID_A[:8]) for l in self.lines), self.lines)


class CliSide(RestoreEnv):
    def data(self):
        return cli.tabs_data(self.cfg, self.conn)

    def test_tabs_data(self):
        self.dead_row(SID_A)
        self.dead_row(SID_B, state="hibernated", iterm_guid="dead")
        self.dead_row(SID_C, state="ended", ended_at=5.0, end_reason="prompt_input_exit")
        prev = self.snapshot_file([SID_A, SID_B, SID_C, None])
        cur = self.snapshot_file([SID_C], instance=INSTANCE, taken_at=time.time())
        older = self.snapshot_file([SID_A], instance={"pid": 7, "start": 700}, taken_at=time.time() - 9000)
        d = self.data()
        self.assertEqual((d["pending"], d["pending_known"], d["pending_path"]), (2, True, prev))
        self.assertFalse(d["restoring"])
        self.assertEqual([s["path"] for s in d["snapshots"]], [cur, prev, older])
        self.assertEqual([s["is_current"] for s in d["snapshots"]], [True, False, False])
        self.assertEqual([s.get("pending") for s in d["snapshots"]], [None, 2, None])
        self.assertEqual((d["snapshots"][1]["tabs"], d["snapshots"][1]["sessions"]), (4, 3))

    def test_tabs_reports_a_held_lock(self):
        self.snapshot_file([SID_A])
        lock = tabfile.restore_lock(self.cfg)
        self.addCleanup(lock.close)
        self.assertTrue(self.data()["restoring"])

    def test_tabs_pending_unknown_when_iterm2_cannot_be_asked(self):
        self.dead_row(SID_A)
        self.snapshot_file([SID_A])
        self.live = None
        d = self.data()
        self.assertEqual((d["pending"], d["pending_known"]), (0, False))
        json.dumps(d)

    def test_tabs_with_no_snapshots(self):
        d = self.data()
        self.assertEqual((d["pending"], d["pending_known"], d["snapshots"], d["pending_path"]), (0, True, [], ""))

    def test_main_prints_json_and_table(self):
        self.snapshot_file([None])
        for argv in (["tabs", "--json"], ["tabs"]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli.main(argv), 0)
            if "--json" in argv:
                self.assertEqual(len(json.loads(buf.getvalue())["snapshots"]), 1)
            else:
                self.assertIn("PENDING", buf.getvalue())

    def test_restore_parser_takes_snapshot(self):
        a = cli.build_parser().parse_args(["restore", "--snapshot", "/x.json", "--dry-run"])
        self.assertEqual((a.snapshot, a.dry_run), ("/x.json", True))

    def test_doctor_line(self):
        self.current = None
        self.assertEqual(cli.tab_snapshot_check(self.cfg)[0], "PASS")
        self.current = INSTANCE
        self.assertEqual(cli.tab_snapshot_check(self.cfg)[0], "WARN")
        self.snapshot_file([None], instance=INSTANCE)
        st, name, detail = cli.tab_snapshot_check(self.cfg)
        self.assertEqual((st, name), ("PASS", "tab snapshots"))
        self.assertIn("pid 500", detail)


class Session(object):
    def __init__(self, guid, tty="ttys001", job="-zsh"):
        self.session_id, self.tty, self.job = guid, tty, job
        self.sent, self.names, self.injected = [], [], []

    async def async_get_variable(self, name):
        return {"tty": "/dev/" + self.tty, "jobName": self.job}[name]

    async def async_send_text(self, text):
        self.sent.append(text)

    async def async_set_name(self, name):
        self.names.append(name)

    async def async_inject(self, data):
        self.injected.append(data)


class Tab(object):
    def __init__(self, tab_id, sessions):
        self.tab_id, self.sessions = tab_id, sessions
        self.current_session = sessions[0]


class Window(object):
    def __init__(self, window_id, tabs, current=0):
        self.window_id, self.tabs = window_id, tabs
        self.current_tab = tabs[current]


class App(object):
    def __init__(self, windows):
        self.terminal_windows = windows
        self.current_terminal_window = windows[0] if windows else None

    def get_session_by_id(self, guid):
        for w in self.terminal_windows:
            for t in w.tabs:
                for s in t.sessions:
                    if s.session_id == guid:
                        return s
        return None


class DaemonSide(TempEnv):
    def daemon(self, app, dry_run=False):
        d = D.Daemon(self.cfg, self.conn, app, None, dry_run)

        async def ident(r):
            return procs.GONE
        d.identity = ident

        async def no_recheck():
            return None
        d.prompt_recheck = no_recheck
        return d

    def hib(self, sid="hib", guid="G1", **kw):
        kw.setdefault("tty", "ttys001")
        return self.insert(session_id=sid, state="hibernated", iterm_guid=guid,
                           evicted_at=time.time() - 600, cwd="/w/x", title="T", **kw)

    def two_tabs(self):
        a, b = Session("G1", "ttys001"), Session("G2", "ttys002")
        return a, b, App([Window(1, [Tab("T1", [a]), Tab("T2", [b])], current=1)])

    def test_on_focus_does_not_resume_a_no_longer_selected_tab(self):
        self.hib()
        a, _, app = self.two_tabs()
        d = self.daemon(app)
        arun(d.on_focus(a))
        self.assertEqual(a.sent, [])
        self.assertEqual(ledger.state_of(self.conn, "hib"), "hibernated")

    def test_on_focus_resumes_the_selected_tab(self):
        self.hib(guid="G2", tty="ttys002")
        _, b, app = self.two_tabs()
        arun(self.daemon(app).on_focus(b))
        self.assertEqual(len(b.sent), 1)
        self.assertEqual(ledger.state_of(self.conn, "hib"), "resuming")

    def test_a_stale_event_does_not_debounce_the_real_click(self):
        self.hib()
        a, _, app = self.two_tabs()
        d = self.daemon(app)
        arun(d.on_focus(a))
        self.assertEqual(a.sent, [])
        app.terminal_windows[0].current_tab = app.terminal_windows[0].tabs[0]
        arun(d.on_focus(a))
        self.assertEqual(len(a.sent), 1)

    def test_on_focus_refuses_a_row_restored_within_the_settle_time(self):
        self.hib(guid="G2", tty="ttys002", restored_at=time.time() - 5)
        _, b, app = self.two_tabs()
        arun(self.daemon(app).on_focus(b))
        self.assertEqual(b.sent, [])
        self.assertEqual(ledger.state_of(self.conn, "hib"), "hibernated")

    def test_resume_decision_refuses_within_settle_and_allows_after(self):
        now = 10000.0
        row = {"state": "hibernated", "iterm_guid": "G1", "tty": "ttys001", "restored_at": now - 59}
        self.assertIn("restored 59s ago", D.resume_decision(row, "G1", "ttys001", now, {}, "zsh", not_alive))
        row["restored_at"] = now - D.RESTORE_SETTLE_S
        self.assertIsNone(D.resume_decision(row, "G1", "ttys001", now, {}, "zsh", not_alive))
        del row["restored_at"]
        self.assertIsNone(D.resume_decision(row, "G1", "ttys001", now, {}, "zsh", not_alive))

    def test_restored_row_can_be_woken_explicitly_within_settle(self):
        self.hib(restored_at=time.time() - 1)
        a, _, app = self.two_tabs()
        d = self.daemon(app)
        self.assertEqual(arun(d.wake(ledger.get(self.conn, "hib"))), "resumed")
        self.assertEqual(len(a.sent), 1)

    # ------------------------------------------------------------ title request

    def do_title(self, app, sid="hib", ts=None, dry_run=False):
        d = self.daemon(app, dry_run)
        rid = ledger.add_request(self.conn, "title", sid, ts)
        arun(d.handle_requests())
        return self.conn.execute("SELECT done_at, result FROM requests WHERE id=?", (rid,)).fetchone()

    def test_title_request_injects_osc_and_sets_the_name(self):
        self.hib()
        a, _, app = self.two_tabs()
        done, result = self.do_title(app)
        self.assertEqual(result, "titled")
        self.assertIsNotNone(done)
        self.assertEqual(a.injected, [b"\x1b]0;[zz] T\x07"])
        self.assertEqual(a.names, ["[zz] T"])
        self.assertEqual(a.sent, [])

    def test_title_strips_non_printables(self):
        self.hib()
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET title=?", ("ev\x07il\x1b]0;x",))
        a, _, app = self.two_tabs()
        self.do_title(app)
        self.assertEqual(a.injected, [b"\x1b]0;[zz] evil]0;x\x07"])

    def test_title_only_while_hibernated(self):
        self.insert(session_id="hib", state="idle", iterm_guid="G1")
        a, _, app = self.two_tabs()
        self.assertEqual(self.do_title(app)[1], "state=idle")
        self.assertEqual((a.injected, a.names), ([], []))

    def test_title_of_a_gone_tab_or_no_guid(self):
        self.hib(guid="GONE")
        self.insert(session_id="noguid", state="hibernated", iterm_guid=None)
        a, _, app = self.two_tabs()
        self.assertEqual(self.do_title(app)[1], "tab is gone")
        self.assertEqual(self.do_title(app, sid="noguid")[1], "no tab GUID")

    def test_title_waits_for_the_shell_then_gives_up(self):
        self.hib()
        a, _, app = self.two_tabs()
        a.job = "vim"
        done, result = self.do_title(app, ts=time.time())
        self.assertEqual((done, result), (None, None))  # stays queued
        self.conn.execute("DELETE FROM requests")
        done, result = self.do_title(app, ts=time.time() - D.TITLE_RETRY_S - 1)
        self.assertEqual(result, "tab is running vim")
        self.assertIsNotNone(done)
        self.assertEqual(a.injected, [])

    def test_title_of_a_queued_request_lands_once_the_shell_is_up(self):
        self.hib()
        a, _, app = self.two_tabs()
        a.job = "login-helper"
        d = self.daemon(app)
        rid = ledger.add_request(self.conn, "title", "hib")
        arun(d.handle_requests())
        a.job = "-zsh"
        arun(d.handle_requests())
        row = self.conn.execute("SELECT result FROM requests WHERE id=?", (rid,)).fetchone()
        self.assertEqual(row[0], "titled")
        self.assertEqual(len(a.injected), 1)

    def test_title_dry_run_touches_nothing(self):
        self.hib()
        a, _, app = self.two_tabs()
        self.assertEqual(self.do_title(app, dry_run=True)[1], "dry-run")
        self.assertEqual((a.injected, a.names), ([], []))

    def test_title_does_not_read_memory(self):
        self.hib()
        _, _, app = self.two_tabs()
        with mock.patch.object(procs, "free_pct", side_effect=AssertionError("read memory")):
            self.assertEqual(self.do_title(app)[1], "titled")

    # ------------------------------------------------------------ tick

    def test_tab_layout_keeps_window_tab_and_pane_order(self):
        a, b, c = Session("G1", "ttys001"), Session("G2", "ttys002"), Session("G3", "ttys003")
        app = App([Window(5, [Tab("T9", [b, c]), Tab("T1", [a])])])
        tty_guid = {"ttys001": "G1", "ttys002": "G2"}
        self.assertEqual(D.tab_layout(app, tty_guid), [
            {"window_id": 5, "tabs": [
                {"tab_id": "T9", "sessions": [{"guid": "G2", "tty": "ttys002"}, {"guid": "G3", "tty": None}]},
                {"tab_id": "T1", "sessions": [{"guid": "G1", "tty": "ttys001"}]}]}])

    def write(self, app, focus):
        d = self.daemon(app)
        d.instance = INSTANCE
        return d, arun(d.write_tab_snapshot(focus, 5000.0))

    def test_tick_snapshot_maps_sessions_and_is_written_once(self):
        self.insert(session_id="live", tty="ttys001", title="L", cwd="/l")
        self.hib(sid="zz", guid="G2", tty="ttys002")
        _, _, app = self.two_tabs()
        focus = {"tty_guid": {"ttys001": "G1", "ttys002": "G2"}, "panes": 2, "skipped": 0}
        d, word = self.write(app, focus)
        self.assertEqual(word, "written")
        self.assertEqual(arun(d.write_tab_snapshot(focus, 5001.0)), "unchanged")
        snap = tabfile.previous(self.cfg, None)["snap"]
        self.assertEqual([p["session_id"] for p in tabfile.panes(snap)], ["live", "zz"])
        self.assertEqual((snap["iterm_pid"], snap["iterm_start"], snap["taken_at"]), (500, 1000, 5000.0))

    def test_a_blind_snapshot_never_replaces_a_good_file(self):
        self.insert(session_id="live", tty="ttys001")
        _, _, app = self.two_tabs()
        self.write(app, {"tty_guid": {"ttys001": "G1"}, "panes": 2, "skipped": 1})
        d, word = self.write(app, {"tty_guid": {}, "panes": 2, "skipped": 2})
        self.assertIsNone(word)
        snap = tabfile.previous(self.cfg, None)["snap"]
        self.assertEqual(tabfile.panes(snap)[0]["session_id"], "live")

    def test_a_snapshot_failure_never_fails_the_tick(self):
        _, _, app = self.two_tabs()
        with mock.patch.object(tabfile, "write_snapshot", side_effect=OSError("disk full")):
            _, word = self.write(app, {"tty_guid": {}, "panes": 2, "skipped": 0})
        self.assertIsNone(word)

    def test_instance_resolved_once(self):
        _, _, app = self.two_tabs()
        d = self.daemon(app)
        calls = []

        def inst():
            calls.append(1)
            return INSTANCE
        focus = {"tty_guid": {}, "panes": 2, "skipped": 0}
        with mock.patch.object(tabfile, "iterm_instance", inst):
            arun(d.write_tab_snapshot(focus))
            arun(d.write_tab_snapshot(focus))
        self.assertEqual(len(calls), 1)

    def test_no_window_list_never_asks_for_the_instance(self):
        d = self.daemon(App([]))
        with mock.patch.object(tabfile, "iterm_instance", side_effect=AssertionError("asked")):
            self.assertIsNone(arun(d.write_tab_snapshot({"tty_guid": {}})))

    def test_tick_calls_the_snapshot_right_after_the_focus_read(self):
        order = []
        d = self.daemon(App([]))

        async def snapshot():
            order.append("snapshot")
            return {"tty_guid": {}, "visible_ttys": set()}

        async def write(focus, now=None):
            order.append("write")
        d.snapshot = snapshot
        d.write_tab_snapshot = write
        d.stamp_visibility = lambda focus, now=None: order.append("stamp")
        with mock.patch.object(procs, "free_pct", lambda: None):
            arun(d.tick())
        self.assertEqual(order[:3], ["snapshot", "write", "stamp"])


class MenuFile(TempEnv):
    def test_menu_shape(self):
        with open(os.path.join(REPO, "contrib", "hammerspoon", "cc_sessions_menu.lua")) as fh:
            src = fh.read()
        self.assertIn('os.getenv("HOME")', src)
        self.assertIn('"/.claude/bin/cc-sessions"', src)
        self.assertIn("function M.start()", src)
        self.assertIn("M.tasks = {}", src)
        self.assertNotIn("hs.execute", src)  # the menu callback must not block
        self.assertNotIn("—", src)
