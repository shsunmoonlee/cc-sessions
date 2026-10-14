import time

from helpers import TempEnv

from ccsessions import ledger

PROC = {"pid": 4242, "pid_start": "Mon Sep 28 10:00:00 2026", "tty": "ttys004", "interactive": True}


class LedgerTransitions(TempEnv):
    def ev(self, event, sid="a1", proc=PROC, now=None, **payload):
        payload.setdefault("session_id", sid)
        payload.setdefault("cwd", "/w/repo")
        payload.setdefault("transcript_path", "/w/t.jsonl")
        return ledger.apply_event(self.conn, event, payload, dict(proc) if proc else None, now)

    def test_schema_and_wal(self):
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_unknown_session_creates_row_on_any_event(self):
        r = self.ev("Stop", sid="new1")
        self.assertEqual(r["state"], "idle")
        self.assertEqual(r["source"], "hook")
        self.assertEqual(r["pid"], 4242)
        self.assertEqual(r["launch_cwd"], "/w/repo")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_start_prompt_stop(self):
        r = self.ev("SessionStart", source="startup")
        self.assertEqual(r["state"], "idle")
        self.assertIsNone(r["ended_at"])
        r = self.ev("UserPromptSubmit")
        self.assertEqual(r["state"], "busy")
        r = self.ev("Stop")
        self.assertEqual(r["state"], "idle")
        self.assertEqual(r["last_event"], "Stop")

    def test_state_since_only_moves_on_change(self):
        r1 = self.ev("Stop", now=1000.0)
        r2 = self.ev("Stop", now=2000.0)
        self.assertEqual(r1["state_since"], r2["state_since"])
        self.assertEqual(r2["last_event_at"], 2000.0)

    def test_subagent_counter_floor(self):
        self.ev("SubagentStart")
        r = self.ev("SubagentStart")
        self.assertEqual(r["subagents"], 2)
        self.ev("SubagentStop")
        self.ev("SubagentStop")
        r = self.ev("SubagentStop")
        self.assertEqual(r["subagents"], 0)

    def test_R6_subagents_reset_on_start_clear_resume(self):
        for src in ("startup", "clear", "resume"):
            self.ev("SubagentStart")
            r = self.ev("SessionStart", source=src)
            self.assertEqual(r["subagents"], 0, src)

    def test_notification_waiting_and_other_types(self):
        self.ev("UserPromptSubmit")
        r = self.ev("Notification", notification_type="permission_prompt")
        self.assertEqual(r["state"], "waiting")
        r = self.ev("Notification", notification_type="auth_success")
        self.assertEqual(r["state"], "waiting")
        r = self.ev("Notification", notification_type="elicitation_dialog")
        self.assertEqual(r["state"], "waiting")

    def test_R6_idle_prompt_goes_idle_only_without_subagents(self):
        self.ev("UserPromptSubmit")
        self.ev("SubagentStart")
        r = self.ev("Notification", notification_type="idle_prompt")
        self.assertEqual(r["state"], "busy")
        self.ev("SubagentStop")
        r = self.ev("Notification", notification_type="idle_prompt")
        self.assertEqual(r["state"], "idle")

    def test_R6_stop_failure_goes_idle(self):
        self.ev("UserPromptSubmit")
        self.assertEqual(self.ev("StopFailure")["state"], "idle")

    def test_session_end_normal(self):
        self.ev("SessionStart", source="startup")
        r = self.ev("SessionEnd", reason="prompt_input_exit")
        self.assertEqual(r["state"], "ended")
        self.assertIsNotNone(r["ended_at"])
        self.assertEqual(r["end_reason"], "prompt_input_exit")

    def test_session_end_while_evicting_is_hibernation(self):
        self.ev("Stop")
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET state='evicting', evicted_at=? WHERE session_id='a1'",
                              (time.time(),))
        r = self.ev("SessionEnd", reason="other")
        self.assertEqual(r["state"], "hibernated")
        self.assertIsNone(r["ended_at"])
        self.assertEqual(r["end_reason"], "other")

    def test_R2_session_end_within_60s_of_evicted_at_is_hibernation_whatever_the_state(self):
        now = time.time()
        self.ev("Stop", now=now - 100)
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET state='idle', evicted_at=? WHERE session_id='a1'", (now - 30,))
        self.assertEqual(self.ev("SessionEnd", reason="other", now=now)["state"], "hibernated")
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET state='idle', evicted_at=? WHERE session_id='a1'", (now - 600,))
        self.assertEqual(self.ev("SessionEnd", reason="other", now=now)["state"], "ended")

    def test_resume_sets_resumed_at_only_from_parked_states(self):
        self.ev("Stop")
        r = self.ev("SessionStart", source="resume")
        self.assertIsNone(r["resumed_at"])
        for st in ("hibernated", "evicting", "resuming"):
            with ledger.tx(self.conn):
                self.conn.execute("UPDATE sessions SET state=?, resumed_at=NULL WHERE session_id='a1'", (st,))
            r = self.ev("SessionStart", source="resume")
            self.assertEqual(r["state"], "idle")
            self.assertIsNotNone(r["resumed_at"], st)

    def test_compact_keeps_state(self):
        self.ev("UserPromptSubmit")
        r = self.ev("SessionStart", source="compact", proc=dict(PROC, pid=5555))
        self.assertEqual(r["state"], "busy")
        self.assertEqual(r["pid"], 5555)

    def test_R1_session_start_supersedes_same_process_rows(self):
        self.ev("Stop", sid="old")
        r = self.ev("SessionStart", sid="new", source="clear")
        old = ledger.get(self.conn, "old")
        self.assertEqual(old["state"], "superseded")
        self.assertIsNotNone(old["ended_at"])
        self.assertEqual(r["state"], "idle")
        # a different process start time is a different process: not superseded
        self.ev("Stop", sid="other", proc=dict(PROC, pid_start="Sun Sep 27 09:00:00 2026"))
        self.ev("SessionStart", sid="new", source="startup")
        self.assertEqual(ledger.get(self.conn, "other")["state"], "idle")

    def test_R4_launch_cwd_never_overwritten(self):
        self.ev("Stop", cwd="/w/first")
        r = self.ev("Stop", cwd="/w/second")
        self.assertEqual(r["launch_cwd"], "/w/first")
        self.assertEqual(r["cwd"], "/w/second")

    def test_R14_non_interactive_recorded(self):
        r = self.ev("Stop", proc=dict(PROC, interactive=False))
        self.assertEqual(r["interactive"], 0)

    def test_missing_session_id_raises(self):
        with self.assertRaises(ValueError):
            ledger.apply_event(self.conn, "Stop", {}, None)

    def test_prune(self):
        now = time.time()
        self.ev("Stop", now=now - 40 * 86400)
        self.ev("Stop", now=now)
        ledger.prune(self.conn, now)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_R2_compare_and_set_eviction(self):
        r = self.ev("Stop", now=1000.0)
        eid = ledger.begin_eviction(self.conn, r, 15, 400, now=2000.0)
        self.assertIsNotNone(eid)
        self.assertEqual(ledger.state_of(self.conn, "a1"), "evicting")
        # a second attempt with the stale row loses
        self.assertIsNone(ledger.begin_eviction(self.conn, r, 15, 400, now=2001.0))
        # a row that moved on since the guard read loses too
        r2 = self.ev("Stop", sid="b2", now=1000.0)
        self.ev("UserPromptSubmit", sid="b2", now=1500.0)
        self.assertIsNone(ledger.begin_eviction(self.conn, r2, 15, 400))
        self.assertEqual(ledger.state_of(self.conn, "b2"), "busy")

    def test_evictions_today_counts(self):
        r = self.ev("Stop")
        eid = ledger.begin_eviction(self.conn, r, 10, 1)
        ledger.finish_eviction(self.conn, eid, "term", "SIGTERM")
        self.assertEqual(ledger.evictions_today(self.conn), {"a1": 1})
