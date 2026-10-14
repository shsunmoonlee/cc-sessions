#!/usr/bin/env python3
"""cc-sessions daemon: pressure-driven, LRU-by-focus hibernation of idle Claude Code
sessions in iTerm2, with resume-on-focus.

    cc_sessions_daemon.py                    run forever (launchd KeepAlive supervises it)
    cc_sessions_daemon.py --once --dry-run   connect, print the candidate table, exit

Needs a python with the `iterm2` package. The policy (`choose_candidates`) and the
eviction sequence (`Evictor`) are plain Python, importable and testable without iterm2.

Loop: every tick_s read free memory; below low_free_pct evict the best candidate, let it
settle, re-read, and continue until high_free_pct or no candidate (swap at or above
swap_high_pct also starts this pass). With idle_hibernate_min > 0 each tick then also
hibernates up to idle_hibernate_per_tick sessions idle that long. Eviction is recorded
first (compare-and-set to 'evicting'), then SIGTERM, then SIGKILL after term_grace_s.
Focusing a hibernated tab types the resume command into it.
"""
import asyncio
import collections
import contextlib
import fcntl
import functools
import glob
import json
import logging
import logging.handlers
import os
import signal
import stat
import subprocess
import sys
import threading
import time


def _libdir():
    here = os.path.dirname(os.path.realpath(__file__))
    for c in (here, os.path.join(here, "..", "lib")):
        if os.path.isdir(os.path.join(c, "ccsessions")):
            return os.path.normpath(c)
    return None


_LIB = _libdir()
if _LIB and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from ccsessions import (BUSY, EVICTING, HIBERNATED, IDLE, LIVE_STATES,  # noqa: E402
                        RESUMING, config, ledger, pending, procs, resumecmd)

log = logging.getLogger("cc-sessions")

RESUME_DEBOUNCE_S = 20
SESSION_END_WAIT_S = 5
TAB_SHELL_WAIT_S = 20
REQUEST_POLL_S = 5
REAP_EVERY_S = 3600
PRUNE_EVERY_S = 3600
ON_EVICT_TIMEOUT_S = 30
# an eviction finishes its tab + hook well inside this (term grace 15 + SessionEnd 5 +
# tab wait 20 + hook 30 + slack); younger unstamped evictions may still be in flight
HOOK_SWEEP_MIN_AGE_S = 180
MAX_ABORTS_PER_PASS = 3
ITERM_CALL_TIMEOUT_S = 5
APP_CONNECT_TIMEOUT_S = ITERM_CALL_TIMEOUT_S * 3
CANCEL_WAIT_S = 5
OUT_LOG_CAP = 5 * 1024 * 1024
RECENT_STARTS = 10
TITLE_CACHE_MAX = 256
TTY_RESET = ("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l\x1b[?1015l"
             "\x1b[?1004l\x1b[?2004l\x1b[?1049l\x1b[?25h\x1b[<u\x1b[?7h\x1b[0m\x1b>")


# ------------------------------------------------------------------ pure policy

def _mins(s):
    return int(max(0, s) / 60)


def _pct(v):
    """A percentage for the log; "?" when it could not be read."""
    return "?" if v is None else "%s%%" % v


def _as_analysis(v):
    if isinstance(v, dict):
        return v
    return {"pending": list(v or []), "has_user": True}


def _projects(cfg):
    if hasattr(cfg, "path"):
        return cfg.path("claude_projects")
    return config.expand(cfg.get("claude_projects"))


def row_transcript(row, cfg):
    """The row's transcript file (ledger.resolve_transcript), or None. When the resolver
    finds a different file than the recorded one, row["transcript"] is replaced in this
    dict; the event loop writes it back to the ledger (heal_transcripts)."""
    rec = row.get("transcript")
    tr = ledger.resolve_transcript(_projects(cfg), row.get("session_id"), rec,
                                   stale_s=float(cfg.get("idle_min") or 10) * 60)
    if tr and tr != rec:
        row["transcript"] = tr
    return tr


def heal_transcripts(conn, rows, before):
    """Write back the transcript paths the resolver corrected in `rows` (before: {sid: the
    recorded path}). Best effort, skipped inside a transaction. -> [(sid, old, new)]."""
    healed = []
    if conn.in_transaction:
        return healed
    for r in rows:
        sid = r["session_id"]
        if sid not in before or r.get("transcript") == before[sid] or not r.get("transcript"):
            continue
        if ledger.heal_transcript(conn, sid, before[sid], r["transcript"]):
            healed.append((sid, before[sid], r["transcript"]))
            log.info("transcript of %s resolved to %s (recorded %s)", sid[:8], r["transcript"],
                     before[sid])
    return healed


def heal_titles(conn, rows, before):
    """Write back the transcript titles title_for put in `rows` (before: {sid: the
    recorded title}), compare-and-set on the old value. Best effort, skipped inside a
    transaction. -> [(sid, old, new)]."""
    healed = []
    if conn.in_transaction:
        return healed
    for r in rows:
        sid = r["session_id"]
        if sid not in before or r.get("title") == before[sid] or not r.get("title"):
            continue
        if ledger.heal_title(conn, sid, before[sid], r["title"]):
            healed.append((sid, before[sid], r["title"]))
            log.info("title of %s is %r (recorded %r)", sid[:8], r["title"], before[sid])
    return healed


class TitleCache(object):
    """pending.transcript_title per transcript path, rescanned only when the file's
    (size, mtime) changes; at most `cap` paths, least recently used dropped first.
    Thread safe: the policy pass reads it in an executor thread."""

    def __init__(self, cap=TITLE_CACHE_MAX):
        self.cap = cap
        self.lock = threading.Lock()
        self.items = collections.OrderedDict()

    def get(self, path):
        if not path:
            return None
        try:
            st = os.stat(path)
        except OSError:
            return None
        sig = (st.st_size, st.st_mtime_ns)
        with self.lock:
            hit = self.items.get(path)
            if hit is not None and hit[0] == sig:
                self.items.move_to_end(path)
                return hit[1]
        try:
            title = pending.transcript_title(path)
        except OSError:
            return None
        with self.lock:
            self.items[path] = (sig, title)
            self.items.move_to_end(path)
            while len(self.items) > self.cap:
                self.items.popitem(last=False)
        return title


TITLES = TitleCache()
TITLE_ERRORS = set()  # transcript paths whose title read raised, so each is logged once


def guard_reason(row, now, cfg, focus_state, pending_fn, children_fn, identity_fn=None,
                 activity_fn=None, evictions_today=None, hook_failed=None, explicit=False,
                 status_fn=None):
    """Why this row must be kept, or None when it may be evicted. Cheap checks first; the
    transcript, file-activity and process-tree checks run only for rows that got that far."""
    sid = row["session_id"]
    idle_s_min = float(cfg["idle_min"]) * 60
    if row.get("source") != "hook":
        return "source=%s" % row.get("source")
    if row.get("interactive") == 0:
        return "non-interactive"
    if hook_failed and sid in hook_failed:
        return "a hook failed to record (marker)"
    st = row.get("state")
    in_state = now - (row.get("state_since") or now)
    stale_busy = False
    if st == BUSY and in_state > float(cfg["stale_busy_min"]) * 60:
        stale_busy = True  # confirmed below from the transcript (Esc-interrupt, no Stop)
    elif st != IDLE:
        return "state=%s" % st
    if not explicit and not stale_busy and in_state < idle_s_min:
        return "idle %dm" % _mins(in_state)
    if (row.get("subagents") or 0) > 0:
        return "subagents=%d" % row["subagents"]
    tty = row.get("tty")
    if not tty or tty not in (focus_state.get("tty_guid") or {}):
        return "no iTerm2 tab for %s" % (tty or "no tty")
    if tty in (focus_state.get("visible_ttys") or set()):
        return "visible tab"
    lf = row.get("last_focus_at")
    if lf and now - lf < idle_s_min:
        return "focused %dm ago" % _mins(now - lf)
    ra = row.get("resumed_at")
    if ra and now - ra < float(cfg["cooldown_min"]) * 60:
        return "cooldown (resumed %dm ago)" % _mins(now - ra)
    n = (evictions_today or {}).get(sid, 0)
    if n >= int(cfg["max_evictions_per_day"]):
        return "evicted %dx today" % n
    if identity_fn is not None:
        why = identity_fn(row)
        if why:
            return why
    if status_fn is not None:
        why = status_fn(row)
        if why:
            return why
    if not row_transcript(row, cfg):
        return "no transcript"
    if activity_fn is not None:
        newest = activity_fn(row)
        if newest is None:
            return "activity unreadable"
        if now - newest < idle_s_min:
            return "active %dm ago (files)" % _mins(now - newest)
    a = _as_analysis(pending_fn(row))
    if not a.get("has_user"):
        return "no user message"
    if a.get("pending"):
        return "pending: " + ", ".join(a["pending"])[:60]
    if stale_busy and not pending.interrupted_last(a):
        return "state=busy"
    kids = children_fn(row)
    if kids:
        return "running: " + ", ".join(sorted(set(kids))[:3])
    return None


def order_key(row, rss):
    lf = row.get("last_focus_at")
    return (0 if lf is None else 1, lf or 0, -float((rss or {}).get(row["session_id"], 0)))


def choose_candidates(rows, now, cfg, focus_state, rss, pending_fn, children_fn,
                      identity_fn=None, activity_fn=None, evictions_today=None,
                      hook_failed=None, explicit=False, status_fn=None):
    """-> (candidates in eviction order, [(row, keep_reason)])."""
    cands, kept = [], []
    for r in rows:
        why = guard_reason(r, now, cfg, focus_state, pending_fn, children_fn, identity_fn,
                           activity_fn, evictions_today, hook_failed, explicit, status_fn)
        if why:
            kept.append((r, why))
        else:
            cands.append(r)
    cands.sort(key=lambda r: order_key(r, rss))
    return cands, kept


def resume_decision(row, guid, tty, now, last_fire, job, identity_alive):
    """Why a focused tab must NOT resume this hibernated row, or None to go ahead.
    Only a row whose recorded tab GUID is this tab resumes on focus; a row without a GUID
    (imported, or its tab was never seen) resumes only through `cc-sessions wake`."""
    if row.get("state") != HIBERNATED:
        return "state=%s" % row.get("state")
    if row.get("interactive") == 0:
        return "non-interactive"
    if now - last_fire.get(guid, 0) < RESUME_DEBOUNCE_S:
        return "debounce"
    if not row.get("iterm_guid"):
        return "no tab GUID recorded; use cc-sessions wake"
    if row["iterm_guid"] != guid:
        return "tab GUID changed (tty reused by another tab)"
    if job is not None and job not in procs.SHELL_JOBS:
        return "tab is running %s" % job
    if identity_alive(row):
        return "already running"
    return None


def is_shell_job(job):
    return job in procs.SHELL_JOBS


async def settled_job(job, tty, run):
    """iTerm2's jobName, corrected by the kernel: after a process exits, jobName can keep
    naming it (the claude binary is named after its version; MCP `node` children linger)
    while the shell already holds the terminal. A non-shell jobName is replaced by the
    tty's foreground group when that is a shell. `run(fn, *args)` runs ps off the loop.
    Never turns a shell into a non-shell, and keeps jobName when ps cannot tell."""
    if job is None or is_shell_job(job) or not tty:
        return job
    try:
        fg = await run(procs.foreground_job, tty)
    except Exception:
        return job
    return fg if fg is not None and is_shell_job(fg) else job


class ConnectionLost(Exception):
    """The iTerm2 connection is gone or unresponsive: leave the loops and exit (launchd restarts)."""


async def iterm_call(aw, what):
    """Every iTerm2 await outside the focus snapshot goes through here: an unanswered call
    is a dead connection, not something to wait on forever."""
    try:
        return await asyncio.wait_for(aw, ITERM_CALL_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise ConnectionLost("iTerm2 did not answer %s within %ss" % (what, ITERM_CALL_TIMEOUT_S))


def connection_alive(connection):
    ws = getattr(connection, "websocket", None)
    if ws is None:
        return False
    if getattr(ws, "closed", False) is True:
        return False
    state = getattr(ws, "state", None)
    if state is not None and getattr(state, "name", "") in ("CLOSING", "CLOSED"):
        return False
    return True


# ------------------------------------------------------------------ default fact sources

REGISTRY_UNREADABLE = object()


def read_registry(sessions_dir, pid):
    """Claude Code's own session file <sessions_dir>/<pid>.json: None when it does not
    exist, REGISTRY_UNREADABLE when it exists but is not a readable JSON object, else the
    dict (sessionId, status idle|busy|waiting|shell, statusUpdatedAt, ...)."""
    reg = os.path.join(sessions_dir, "%s.json" % pid)
    if not os.path.exists(reg):
        return None
    try:
        with open(reg) as fh:
            d = json.load(fh)
    except Exception:
        return REGISTRY_UNREADABLE
    return d if isinstance(d, dict) else REGISTRY_UNREADABLE


def status_reason(d):
    """Keep reason from Claude Code's own status (d: read_registry), or None. A missing
    file (older Claude Code) or a file without a status changes nothing; a file that exists
    but does not parse keeps the session (fail closed); any status but idle keeps it (busy,
    waiting on a prompt or dialog, shell)."""
    if d is None:
        return None
    if d is REGISTRY_UNREADABLE:
        return "claude status file unreadable"
    status = d.get("status")
    if status is None or status == IDLE:
        return None
    return "claude status %s" % str(status)[:20]


def default_status(cfg):
    sessions_dir = cfg.path("claude_sessions")

    def check(row):
        if row.get("pid") is None:
            return None
        return status_reason(read_registry(sessions_dir, row["pid"]))
    return check


def default_identity(cfg):
    sessions_dir = cfg.path("claude_sessions")

    def check(row):
        if row.get("pid_start") is None:
            return "no recorded process start time"
        st = procs.identity_probe(row.get("pid"), row["pid_start"])
        if st != procs.ALIVE:
            return "process gone or pid reused" if st == procs.GONE else "process identity unknown"
        d = read_registry(sessions_dir, row["pid"])
        if d is REGISTRY_UNREADABLE:
            return "registry file unreadable"
        if d is not None and d.get("sessionId") != row["session_id"]:
            return "registry names another session"
        return None
    return check


def default_activity(cfg):
    tmp = cfg.path("claude_tmp")

    def newest(row):
        sid, tr = row["session_id"], row_transcript(row, cfg)
        paths = []
        if tr:
            paths.append(tr)
            if tr.endswith(".jsonl"):
                paths += glob.glob(tr[:-6] + "/subagents/*.jsonl")
                paths += glob.glob(tr[:-6] + "/subagents/workflows/*/*.jsonl")
        paths += glob.glob(os.path.join(tmp, "*", sid, "tasks", "*"))
        best = None
        for p in paths:
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            best = m if best is None else max(best, m)
        return best
    return newest


def hook_failed_sids(cfg):
    try:
        return set(os.listdir(os.path.join(config.db_dir(cfg), "hook-failed")))
    except OSError:
        return set()


class Facts(object):
    """One snapshot of the slow facts (process table, transcripts) for a policy pass."""

    def __init__(self, cfg, now=None):
        self.cfg = cfg
        self.now = time.time() if now is None else now
        self.cmds = procs.ps_commands()
        self._analysis = {}

    def analysis(self, row):
        sid = row["session_id"]
        if sid not in self._analysis:
            tr = row_transcript(row, self.cfg) or row.get("transcript")
            self._analysis[sid] = pending.analyze(
                tr, self.now, float(self.cfg.get("cron_recurring_block_h", 24)) * 3600)
        return self._analysis[sid]

    def children(self, row):
        return procs.working_children(row["pid"], self.cmds, self.cfg["helper_patterns"])

    def rss(self, rows):
        return {r["session_id"]: procs.tree_rss_mb(r["pid"], self.cmds) for r in rows if r.get("pid")}


def compute_policy(cfg, focus_state, rows, evictions_today, explicit=False, now=None):
    """The slow half of a policy pass (ps, transcripts, registry, file stats). Touches no
    SQLite, so the daemon runs it in an executor thread. -> (cands, kept, rss, titles)."""
    now = time.time() if now is None else now
    for r in rows:
        row_transcript(r, cfg)  # heal every row, not only those that reach that guard
    facts = Facts(cfg, now)
    rss = facts.rss(rows)
    cands, kept = choose_candidates(
        rows, now, cfg, focus_state, rss, facts.analysis, facts.children,
        identity_fn=default_identity(cfg), activity_fn=default_activity(cfg),
        evictions_today=evictions_today, hook_failed=hook_failed_sids(cfg), explicit=explicit,
        status_fn=default_status(cfg))
    # only the first candidate is named this pass; the rest heal when they come first
    titles = {cands[0]["session_id"]: title_for(cands[0], facts)} if cands else {}
    return cands, kept, rss, titles


def live_rows(conn):
    return ledger.all_rows(conn, "ended_at IS NULL AND state IN (?,?,?)", LIVE_STATES)


def policy_pass(conn, cfg, focus_state, rows=None, explicit=False, now=None):
    now = time.time() if now is None else now
    rows = live_rows(conn) if rows is None else rows
    before = {r["session_id"]: r.get("transcript") for r in rows}
    before_titles = {r["session_id"]: r.get("title") for r in rows}
    out = compute_policy(cfg, focus_state, rows, ledger.evictions_today(conn, now), explicit, now)
    heal_transcripts(conn, rows, before)
    heal_titles(conn, rows, before_titles)
    return out


def title_for(row, facts=None):
    """custom-title > ai-title (whole transcript, row["transcript"]) > the ledger title >
    basename(cwd) > sid[:8]. A transcript title that differs from row["title"] replaces it
    in this dict; the event loop writes it back (heal_titles). Reads files: off the loop.
    A transcript whose title read raises counts as having none (logged once per path)."""
    path = row.get("transcript")
    try:
        t = TITLES.get(path)
    except Exception:
        t = None
        if path not in TITLE_ERRORS:
            if len(TITLE_ERRORS) >= TITLE_CACHE_MAX:
                TITLE_ERRORS.clear()
            TITLE_ERRORS.add(path)
            log.exception("title of %s unreadable: %s", row["session_id"][:8], path)
    if t and t != row.get("title"):
        row["title"] = t
    return t or row.get("title") or os.path.basename(row.get("cwd") or "") or row["session_id"][:8]


def resolve_title(row, cfg):
    """title_for after resolving the row's transcript (row_transcript)."""
    row_transcript(row, cfg)
    return title_for(row)


# ------------------------------------------------------------------ eviction sequence

def _as_state(v):
    if v is True:
        return procs.ALIVE
    if v is False or v is None:
        return procs.GONE
    return v


class Evictor(object):
    """Compare-and-set to evicting, repeat every safety check, SIGTERM, SIGKILL after the
    grace period, and only once the exact process identity is confirmed gone: hibernated,
    tab prepared, on_evict run. An identity that cannot be confirmed leaves the row
    evicting (INCIDENT) for reconcile.

    identity(row) -> ALIVE | GONE | UNKNOWN (bools accepted); recheck(row) -> reason|None
    runs AFTER the CAS; children(row) -> [(pid, start)] captured before any signal;
    child_probe(pid, start) -> ALIVE | GONE | UNKNOWN; offload(fn, *args) runs blocking
    probes off the event loop. `tabs`: async job_name, tty, is_visible, inject, set_name,
    send_text (by GUID)."""

    def __init__(self, conn, cfg, tabs, kill=os.kill, identity=None, clock=time.time,
                 sleep=asyncio.sleep, on_evict=None, children=None, child_probe=None, offload=None):
        self.conn, self.cfg, self.tabs = conn, cfg, tabs
        self.kill, self.clock, self.sleep = kill, clock, sleep
        self.identity = identity or (lambda r: procs.identity_probe(r.get("pid"), r.get("pid_start")))
        self.on_evict = on_evict
        self.children = children or (lambda r: procs.child_identities(r["pid"]))
        self.child_probe = child_probe or (lambda pid, start: procs.identity_probe(pid, start, want_claude=False))
        self.offload = offload

    async def _call(self, fn, *args):
        if self.offload is None:
            return fn(*args)
        return await self.offload(fn, *args)

    async def probe(self, row):
        try:
            return _as_state(await self._call(self.identity, row))
        except Exception:
            return procs.UNKNOWN

    def _abort(self, row, eid, why):
        ledger.rollback_eviction(self.conn, row["session_id"], row["state"], self.clock(),
                                 row.get("state_since"))
        ledger.finish_eviction(self.conn, eid, "aborted", detail=why[:200])
        log.info("keep %s: %s (eviction rolled back, no signal sent)", row["session_id"][:8], why)

    async def evict(self, row, free, rss_mb, recheck=None, title=None, reason=None):
        sid, pid = row["session_id"], int(row["pid"])
        eid = ledger.begin_eviction(self.conn, row, free, rss_mb, self.clock())
        if eid is None:
            log.info("keep %s: row changed since the guards ran (compare-and-set lost)", sid[:8])
            return "aborted-cas"
        if recheck is not None:
            try:
                why = recheck(row)
                if asyncio.iscoroutine(why):
                    why = await why
            except ConnectionLost as exc:
                self._abort(row, eid, "re-check: connection lost: %s" % exc)
                raise
            except Exception as exc:
                why = "re-check failed: %s" % exc
            if why:
                self._abort(row, eid, "re-check: %s" % why)
                return "aborted-recheck"
        p = await self.probe(row)
        if p != procs.ALIVE:
            self._abort(row, eid, "identity %s before signal" % p)
            return "aborted-identity"
        try:
            kids = list(await self._call(self.children, row) or [])
        except Exception:
            kids = []
        why = self.final_check(row)  # synchronous: nothing can interleave before the signal
        if why:
            self._abort(row, eid, why)
            return "aborted-final"
        try:
            self.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            self._abort(row, eid, "SIGTERM failed: %s" % exc)
            return "aborted-signal"
        deadline = self.clock() + float(self.cfg["term_grace_s"])
        p = await self.probe(row)
        while p == procs.ALIVE and self.clock() < deadline:
            await self.sleep(0.5)
            p = await self.probe(row)
        outcome, sig = "term", "SIGTERM"
        if p == procs.ALIVE:
            log.error("INCIDENT evict %s: still alive %ss after SIGTERM, sending SIGKILL "
                      "(SessionEnd hooks will not run)", sid[:8], self.cfg["term_grace_s"])
            try:
                self.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                log.error("INCIDENT evict %s: SIGKILL failed (%s); row left evicting for reconcile",
                          sid[:8], exc)
                ledger.finish_eviction(self.conn, eid, "kill-failed", "SIGKILL", str(exc)[:200])
                return "kill-failed"
            outcome, sig = "killed", "SIGKILL"
            await self.kill_children(sid, kids)
            waited = 0.0
            p = await self.probe(row)
            while p == procs.ALIVE and waited < SESSION_END_WAIT_S:
                await self.sleep(0.5)
                waited += 0.5
                p = await self.probe(row)
        if p != procs.GONE:
            log.error("INCIDENT evict %s: process identity %s after %s; row left evicting for "
                      "reconcile, tab untouched", sid[:8], p, sig)
            ledger.finish_eviction(self.conn, eid, "unknown", sig, "identity %s" % p)
            return "unknown"
        if outcome == "term":
            waited = 0.0
            while ledger.state_of(self.conn, sid) == EVICTING and waited < SESSION_END_WAIT_S:
                await self.sleep(0.5)
                waited += 0.5
        if ledger.mark_hibernated(self.conn, sid, self.clock()):
            log.info("evict %s: SessionEnd did not flip the row; daemon set hibernated", sid[:8])
        st = ledger.state_of(self.conn, sid)
        ledger.finish_eviction(self.conn, eid, outcome, signal=sig,
                               detail=None if st == HIBERNATED else "row ended as %s" % st)
        log.info("evicted %s (%s)%s free=%s rss=%.0fMB", sid[:8], outcome,
                 " reason=%s" % reason if reason else "", _pct(free), rss_mb or 0)
        title = title or row.get("title") or sid[:8]
        lost = None
        if st == HIBERNATED:
            try:
                await self.prepare_tab(row, title)
            except ConnectionLost as exc:
                lost = exc
                log.warning("evict %s: iTerm2 stopped answering while preparing the tab", sid[:8])
            except Exception:
                log.exception("evict %s: preparing the tab failed", sid[:8])
        self.run_on_evict(row, title, outcome, eid)
        if lost is not None:
            raise lost
        return outcome

    def final_check(self, row):
        """Re-read the row and the registry immediately before SIGTERM. A focus or
        visibility stamp only writes last_focus_at, so it is compared too: a row whose
        last_focus_at moved after the guards read it was seen in the meantime."""
        sid = row["session_id"]
        fresh = ledger.get(self.conn, sid)
        if not fresh or fresh.get("state") != EVICTING:
            return "row is %s before signal" % (fresh or {}).get("state")
        if fresh.get("last_event_at") != row.get("last_event_at"):
            return "hook activity during the checks"
        if fresh.get("last_focus_at") != row.get("last_focus_at"):
            return "focused during the checks"
        if fresh.get("pid") != row.get("pid") or fresh.get("pid_start") != row.get("pid_start"):
            return "row identity changed"
        d = read_registry(self.cfg.path("claude_sessions"), row["pid"])
        if d is REGISTRY_UNREADABLE:
            return "registry file unreadable before signal"
        if d is not None and d.get("sessionId") != sid:
            return "registry names another session before signal"
        why = status_reason(d)
        if why:
            return why + " before signal"
        return None

    async def kill_children(self, sid, kids):
        for cpid, cstart in kids:
            try:
                st = _as_state(await self._call(self.child_probe, cpid, cstart))
            except Exception:
                st = procs.UNKNOWN
            if st != procs.ALIVE:
                continue
            try:
                self.kill(int(cpid), signal.SIGKILL)
                log.warning("evict %s: SIGKILLed child %s of the killed session", sid[:8], cpid)
            except OSError as exc:
                log.warning("evict %s: could not SIGKILL child %s: %s", sid[:8], cpid, exc)

    async def prepare_tab(self, row, title):
        sid = row["session_id"][:8]
        guid = row.get("iterm_guid_now") or row.get("iterm_guid")
        if not guid:
            log.warning("evict %s: no tab GUID; nothing typed", sid)
            return False
        # wall-clock bound: each job_name is an iTerm2 round trip that can itself be slow
        start = self.clock()
        deadline = start + TAB_SHELL_WAIT_S
        job = await settled_job(await self.tabs.job_name(guid), row.get("tty"), self._call)
        while job is not None and not is_shell_job(job) and self.clock() < deadline:
            await self.sleep(0.5)
            job = await settled_job(await self.tabs.job_name(guid), row.get("tty"), self._call)
        waited = self.clock() - start
        if job is None:
            log.warning("evict %s: tab is gone; nothing typed", sid)
            return False
        # the tab may have been reused or brought forward while we waited
        tty = procs.norm_tty(await self.tabs.tty(guid))
        if tty != row.get("tty"):
            log.warning("evict %s: tab tty is now %s (was %s); tab untouched", sid, tty, row.get("tty"))
            return False
        if await self.tabs.is_visible(guid):
            log.warning("evict %s: tab became visible; tab untouched", sid)
            return False
        # wake and focus do not take evict_lock, and every await yields to them: re-read the
        # row right before each write to the tab
        if not self._still_hibernated(row, "before naming"):
            return False
        # the name goes on whatever the tab reports: iTerm2's jobName lags the dead process
        # (the claude binary is named after its version; MCP children linger), and a tab left
        # unnamed shows "-zsh" instead of the hibernated title. Focus resumes it later, once
        # jobName reads as a shell (resume_decision refuses any other job).
        await self.tabs.set_name(guid, "[zz] %s" % title)
        if not is_shell_job(job):
            log.warning("evict %s: tab is not at a shell after %.0fs (%s); named, nothing typed",
                        sid, waited, job)
            return False
        banner = "%s\r\n\x1b[2m[cc-sessions] %s: hibernated to free memory; transcript intact.\r\n" \
                 "  press Enter to resume here\x1b[0m\r\n" % (TTY_RESET, title)
        if not self._still_hibernated(row, "before the banner"):
            return False
        await self.tabs.inject(guid, banner.encode("utf-8"))
        if not self._still_hibernated(row, "before typing"):
            return False
        await self.tabs.send_text(guid, resumecmd.command(self.cfg, row["session_id"], row.get("cwd")))
        return True

    def _still_hibernated(self, row, when):
        st = ledger.state_of(self.conn, row["session_id"])
        if st != HIBERNATED:
            log.warning("evict %s: row is %s %s; tab left as is", row["session_id"][:8], st, when)
            return False
        return True

    def run_on_evict(self, row, title, outcome, eid=None):
        """Run the on_evict hook; the eviction row's hook_at is stamped once it has run (or
        when none is configured). An eviction cancelled before this point, or a daemon that
        died while the hook ran, leaves hook_at empty: sweep_hooks replays it."""
        def done():
            ledger.hook_done(self.conn, eid)
        exe = self.cfg.path("on_evict") if self.cfg.get("on_evict") else None
        if not exe:
            done()
            return None
        env = dict(os.environ, CC_SESSION_ID=row["session_id"], CC_SESSION_CWD=row.get("cwd") or "",
                   CC_SESSION_TITLE=title or "", CC_EVICT_OUTCOME=outcome)
        if self.on_evict is not None:
            out = self.on_evict(exe, env)
            done()
            return out
        try:
            return asyncio.ensure_future(_run_hook(exe, env, done))
        except RuntimeError:
            return None

    def sweep_hooks(self, now=None):
        """Replay on_evict for finished evictions that never ran it (a cancelled eviction
        pass, a daemon that exited mid-hook). Only a session still hibernated gets it; any
        other is stamped as not needed. -> number replayed."""
        now = time.time() if now is None else now
        n = 0
        for r in ledger.pending_hooks(self.conn, now - HOOK_SWEEP_MIN_AGE_S):
            eid = r.pop("eviction_id")
            outcome = r.pop("outcome")
            if r.get("state") == EVICTING:
                continue  # reconcile decides first: hibernated gets the hook, alive does not
            if r.get("state") != HIBERNATED:
                ledger.hook_done(self.conn, eid, now)
                continue
            log.warning("on_evict never ran for %s (eviction %s, %s); running it now",
                        r["session_id"][:8], eid, outcome)
            self.run_on_evict(r, r.get("title") or r["session_id"][:8], outcome, eid)
            n += 1
        return n


async def _run_hook(exe, env, done=None):
    try:
        p = await asyncio.create_subprocess_exec(exe, env=env, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(p.communicate(), ON_EVICT_TIMEOUT_S)
        except asyncio.TimeoutError:
            p.kill()
            log.warning("on_evict %s timed out after %ss", exe, ON_EVICT_TIMEOUT_S)
        else:
            log.info("on_evict %s exit %s: %s", exe, p.returncode,
                     (out or b"").decode("utf-8", "replace").strip()[:300])
    except Exception as exc:
        log.warning("on_evict %s failed: %s", exe, exc)
    # not on CancelledError (it propagates past this line): the next daemon replays it
    if done is not None:
        try:
            done()
        except Exception:
            log.exception("on_evict: could not stamp the eviction as run")


# ------------------------------------------------------------------ pressure loop (pure-ish)

async def _maybe_await(v):
    if asyncio.iscoroutine(v):
        return await v
    return v


class Pressure(object):
    """Hysteresis + global backoff. evict_fn(free) -> outcome or None (no candidate).
    free_fn and swap_fn may be sync or async. Two triggers: free < low_free_pct ("free"),
    else swap used >= swap_high_pct ("swap"). Every pass stops at high_free_pct; a
    swap-triggered pass also stops once swap is below swap_high_pct - 10. A pass also
    stops after max_evictions_per_pass evictions (0 = no cap); the next tick continues."""

    SWAP_HYSTERESIS = 10

    def __init__(self, cfg, free_fn, clock=time.time, sleep=asyncio.sleep, swap_fn=None):
        self.cfg, self.free_fn, self.clock, self.sleep = cfg, free_fn, clock, sleep
        self.swap_fn = swap_fn
        self.backoff_until = 0.0
        self.trigger = None

    def _trigger(self, free, swap):
        if free < float(self.cfg["low_free_pct"]):
            return "free"
        if free >= float(self.cfg["high_free_pct"]):
            return None  # every pass stops here: a swap pass must not start above it
        swap_high = float(self.cfg.get("swap_high_pct") or 0)
        if swap_high > 0 and swap is not None and swap >= swap_high:
            return "swap"
        return None

    async def relieve(self, free, evict_fn, swap=None):
        high = float(self.cfg["high_free_pct"])
        trigger = None if free is None else self._trigger(free, swap)
        if trigger is None:
            self.trigger = None
            return "comfortable"
        if self.clock() < self.backoff_until:
            return "backoff"
        if trigger != self.trigger:
            log.info("memory pressure: trigger=%s (free %s%%, evict below %s%%; swap %s%%, "
                     "evict at %s%%)", trigger, free, self.cfg["low_free_pct"],
                     "?" if swap is None else swap, self.cfg.get("swap_high_pct"))
        self.trigger = trigger
        swap_stop = float(self.cfg.get("swap_high_pct") or 0) - self.SWAP_HYSTERESIS
        cap = int(self.cfg.get("max_evictions_per_pass") or 0)
        misses = done = 0
        while True:
            outcome = await evict_fn(free)
            if outcome is None:
                return "no-candidate"
            if outcome not in ("term", "killed"):
                misses += 1
                if misses >= MAX_ABORTS_PER_PASS:
                    return "aborted"
                continue
            await self.sleep(float(self.cfg["settle_s"]))
            new = await _maybe_await(self.free_fn())
            if new is None:
                return "unreadable"
            new_swap = None
            if trigger == "swap":
                new_swap = await _maybe_await(self.swap_fn()) if self.swap_fn is not None else None
                if new_swap is None:
                    return "unreadable"
            gained = new - free >= 1 or (trigger == "swap" and swap - new_swap >= 1)
            if not gained:
                self.backoff_until = self.clock() + float(self.cfg["backoff_min"]) * 60
                if trigger == "swap":
                    log.warning("free %s -> %s%%, swap %s -> %s%% after an eviction: the memory "
                                "is elsewhere; no evictions for %s min", free, new, swap, new_swap,
                                self.cfg["backoff_min"])
                else:
                    log.warning("free memory rose %s -> %s%% after an eviction: the memory is "
                                "elsewhere; no evictions for %s min", free, new, self.cfg["backoff_min"])
                return "backoff"
            if new >= high:
                return "relieved"
            if trigger == "swap" and new_swap < swap_stop:
                return "relieved"
            done += 1
            if cap > 0 and done >= cap:
                log.info("memory pressure: %d evictions this pass (max_evictions_per_pass); "
                         "continuing next tick", done)
                return "capped"
            free, swap = new, new_swap


def reconcile(conn, probes, now=None, min_age_s=0.0):
    """Rows left evicting (daemon restart, crash, an eviction that could not confirm its
    kill). Resuming rows belong to expire_resuming. probes: {session_id: ALIVE|GONE|UNKNOWN}
    (bools accepted) or a callable(row). Alive -> the state a hook recorded (else idle) and
    evicted_at cleared, so a later SessionEnd(other) is an exit; gone -> hibernated;
    unknown -> left alone."""
    now = time.time() if now is None else now
    fixed = []
    for r in ledger.all_rows(conn, "state=? AND COALESCE(state_since, 0) <= ?",
                             (EVICTING, now - min_age_s)):
        st = _as_state(probes(r) if callable(probes) else probes.get(r["session_id"], procs.UNKNOWN))
        if st == procs.UNKNOWN:
            continue
        with ledger.tx(conn):
            if st == procs.ALIVE:
                conn.execute("UPDATE sessions SET state=COALESCE(held_state, ?), held_state=NULL, "
                             "evicted_at=NULL, state_since=? WHERE session_id=? AND state=?",
                             (IDLE, now, r["session_id"], r["state"]))
            else:
                conn.execute("UPDATE sessions SET state=?, held_state=NULL, state_since=? WHERE "
                             "session_id=? AND state=?", (HIBERNATED, now, r["session_id"], r["state"]))
        fixed.append((r["session_id"], r["state"], ledger.state_of(conn, r["session_id"])))
    return fixed


def expire_resuming(conn, cfg, now=None, probes=None):
    """R10: a resuming row with no SessionStart after resume_timeout_s. With probes, a row
    whose (cc-resume-claimed) identity is alive becomes idle; unknown is left alone."""
    now = time.time() if now is None else now
    cutoff = now - float(cfg["resume_timeout_s"])
    n = 0
    for r in ledger.all_rows(conn, "state=? AND state_since < ?", (RESUMING, cutoff)):
        st = _as_state(probes.get(r["session_id"], procs.GONE)) if probes is not None else procs.GONE
        if st == procs.UNKNOWN:
            continue
        with ledger.tx(conn):
            conn.execute("UPDATE sessions SET state=?, state_since=? WHERE session_id=? AND state=?",
                         (IDLE if st == procs.ALIVE else HIBERNATED, now, r["session_id"], RESUMING))
        n += 1
    return n


def probe_rows(rows):
    """{session_id: ALIVE|GONE|UNKNOWN} (blocking; run it in an executor)."""
    return {r["session_id"]: procs.identity_probe(r.get("pid"), r.get("pid_start")) for r in rows}


def backoff_delays(start=5, cap=60):
    d = start
    while True:
        yield d
        d = min(cap, d * 2)


# ------------------------------------------------------------------ iTerm2 side

def iterm_running():
    try:
        r = subprocess.run(["osascript", "-e", 'application id "com.googlecode.iterm2" is running'],
                           capture_output=True, text=True, timeout=10)
    except subprocess.SubprocessError:
        return False
    return r.stdout.strip() == "true"


API_DISABLED_RETRY_S = 600
API_DISABLED_CHUNK_S = 60  # liveness is written before each chunk (doctor: DAEMON_FRESH_S 120)


def api_disabled(exc):
    """True when a cookie request failed because iTerm2's Python API is switched off."""
    return "not enabled" in str(exc).lower()


def request_cookie():
    """ITERM2_COOKIE/ITERM2_KEY via AppleScript, 10 s timeout. Raises on failure."""
    r = subprocess.run(["osascript", "-e", 'tell application id "com.googlecode.iterm2" to '
                        'request cookie and key for app named "cc-sessions"'],
                       capture_output=True, text=True, timeout=10)
    parts = r.stdout.strip().split()
    if r.returncode != 0 or len(parts) != 2:
        raise RuntimeError("iTerm2 refused a cookie: %s" % (r.stderr.strip()[:160] or "empty reply"))
    os.environ["ITERM2_COOKIE"], os.environ["ITERM2_KEY"] = parts


class ItermTabs(object):
    def __init__(self, app, connection):
        self.app, self.connection = app, connection

    def _s(self, guid):
        return self.app.get_session_by_id(guid) if guid else None

    async def job_name(self, guid):
        s = self._s(guid)
        if s is None:
            return None
        return (await iterm_call(s.async_get_variable("jobName"), "jobName")) or ""

    async def tty(self, guid):
        s = self._s(guid)
        return (await iterm_call(s.async_get_variable("tty"), "tty")) if s is not None else None

    async def is_visible(self, guid):
        for w in self.app.terminal_windows:
            cur = w.current_tab
            if cur is not None and any(s.session_id == guid for s in cur.sessions):
                return True
        return False

    async def inject(self, guid, data):
        s = self._s(guid)
        if s is not None:
            await iterm_call(s.async_inject(data), "inject")

    async def set_name(self, guid, name):
        s = self._s(guid)
        if s is not None:
            await iterm_call(s.async_set_name(name), "set_name")

    async def send_text(self, guid, text):
        s = self._s(guid)
        if s is not None:
            await iterm_call(s.async_send_text(text), "send_text")

    async def new_tab(self, text):
        import iterm2
        w = self.app.current_terminal_window
        if w is None:
            w = await iterm_call(iterm2.Window.async_create(self.connection), "create window")
            s = w.current_tab.current_session
        else:
            t = await iterm_call(w.async_create_tab(), "create tab")
            s = t.current_session
        await iterm_call(s.async_send_text(text), "send_text")
        return s.session_id


class _NoRPCException(Exception):
    """Stands in for iterm2.rpc.RPCException when the iterm2 package is not loaded."""


def _rpc_exception():
    """From the already-loaded package (main() imports iterm2 before any snapshot); never
    imports it, so this module stays importable and testable without iterm2."""
    return getattr(sys.modules.get("iterm2.rpc"), "RPCException", None) or _NoRPCException


_last_skipped = frozenset()


async def _session_tty(s, rpc_exc, skipped=None):
    """A session closed between listing and asking answers with an RPCException: skip it
    (recorded in `skipped` as {guid: reason}; focus_snapshot logs the set when it changes)."""
    try:
        return procs.norm_tty(await s.async_get_variable("tty"))
    except rpc_exc as exc:
        if skipped is not None:
            skipped[getattr(s, "session_id", "?")] = str(exc)
        return None


def _log_skipped(skipped):
    global _last_skipped
    now = frozenset(skipped)
    if now != _last_skipped:
        if now:
            log.info("focus snapshot: skipping %d session(s): %s", len(now),
                     ", ".join("%s (%s)" % (g, skipped[g]) for g in sorted(now)))
        else:
            log.info("focus snapshot: no sessions skipped")
        _last_skipped = now


async def focus_snapshot(app):
    """{tty_guid: {tty: guid}, visible_ttys: set, visible_guids: set, panes: int,
    skipped: int}. Visible = every pane of each window's current tab. The tty reads run
    concurrently; `skipped` counts panes whose tty read raised an RPCException."""
    rpc_exc = _rpc_exception()
    panes = []
    for w in app.terminal_windows:
        cur = w.current_tab
        for t in w.tabs:
            shown = cur is not None and t.tab_id == cur.tab_id
            for s in t.sessions:
                panes.append((s, shown))
    # return_exceptions: every read finishes (none left running or unretrieved) before the
    # first non-RPCException failure, in pane order, propagates.
    skipped = {}
    ttys = await asyncio.gather(*[_session_tty(s, rpc_exc, skipped) for s, _ in panes],
                                return_exceptions=True)
    for tty in ttys:
        if isinstance(tty, BaseException) and not isinstance(tty, rpc_exc):
            raise tty
    _log_skipped(skipped)
    tty_guid, visible, vguids = {}, set(), set()
    for (s, shown), tty in zip(panes, ttys):
        if isinstance(tty, BaseException):
            tty = None
        if tty:
            tty_guid[tty] = s.session_id
        if shown:
            vguids.add(s.session_id)
            if tty:
                visible.add(tty)
    return {"tty_guid": tty_guid, "visible_ttys": visible, "visible_guids": vguids,
            "panes": len(panes), "skipped": len(skipped)}


class Daemon(object):
    MAX_TICK_FAILURES = 3
    SNAPSHOT_TIMEOUT_S = 15

    def __init__(self, cfg, conn, app, connection, dry_run):
        self.cfg, self.conn, self.app, self.connection = cfg, conn, app, connection
        self.dry_run = dry_run
        self.tabs = ItermTabs(app, connection)
        self.evictor = Evictor(conn, cfg, self.tabs, offload=self.off)
        self.pressure = Pressure(cfg, lambda: self.off(procs.free_pct),
                                 swap_fn=lambda: self.off(procs.swap_pct))
        self.evict_lock = asyncio.Lock()
        self.pass_lock = asyncio.Lock()  # one pressure or idle pass at a time
        self.last_fire = {}
        self.last_reap = self.last_prune = 0.0
        self.last_said = None
        self.busy_depth = 0
        self.pass_marked = False
        self.idle_disabled_said = False
        self.visible = {}  # tty -> guid shown right now
        self.blind_since = None
        self.last_blind_meta = None

    async def off(self, fn, *args):
        """Blocking work (ps, transcript reads, stats) in an executor; SQLite stays here."""
        return await asyncio.get_running_loop().run_in_executor(None, functools.partial(fn, *args))

    async def identity(self, row):
        return _as_state(await self.off(procs.identity_probe, row.get("pid"), row.get("pid_start")))

    async def snapshot(self):
        """A slow snapshot on a live connection is a plain TimeoutError, so tick_loop's
        consecutive-failure rule decides; only a closed websocket is ConnectionLost."""
        try:
            focus = await asyncio.wait_for(focus_snapshot(self.app), self.SNAPSHOT_TIMEOUT_S)
        except asyncio.TimeoutError:
            what = "iTerm2 did not answer a focus snapshot in %ss" % self.SNAPSHOT_TIMEOUT_S
            if not connection_alive(self.connection):
                raise ConnectionLost(what)
            raise TimeoutError(what)
        self.record_blindness(focus)
        return focus

    def record_blindness(self, focus, now=None):
        """meta `skipped_sessions` = panes the last snapshot could not read; `blind_since` =
        when every pane (of at least one) started being skipped, "" once any pane reads.
        Written only on change; doctor warns on a long blind spell."""
        panes, skipped = focus.get("panes", 0), focus.get("skipped", 0)
        blind = panes > 0 and skipped == panes
        if blind and self.blind_since is None:
            self.blind_since = time.time() if now is None else now
        elif not blind:
            self.blind_since = None
        state = (skipped, self.blind_since)
        if state == self.last_blind_meta:
            return
        values = {"skipped_sessions": skipped,
                  "blind_since": "" if self.blind_since is None else self.blind_since}
        try:
            if self.conn.in_transaction:
                for k, v in values.items():
                    ledger.set_meta(self.conn, k, v)
            else:
                with ledger.tx(self.conn):
                    for k, v in values.items():
                        ledger.set_meta(self.conn, k, v)
            self.last_blind_meta = state
        except Exception:
            log.exception("could not record snapshot blindness")

    def stamp_visibility(self, focus, now=None):
        """last_focus_at = now for rows shown now AND rows that were shown until now."""
        new = {t: focus["tty_guid"][t] for t in focus["visible_ttys"] if t in focus["tty_guid"]}
        both = dict(self.visible)
        both.update(new)
        ledger.stamp_visible(self.conn, both, now)
        self.visible = new

    def heartbeat(self):
        now = time.time()
        with ledger.tx(self.conn):
            ledger.set_meta(self.conn, "heartbeat", now)
            ledger.set_meta(self.conn, "dry_run", int(self.dry_run))
        p = os.path.join(config.db_dir(self.cfg), "cc-sessions.heartbeat")
        with open(p, "a"):
            os.utime(p, None)

    async def policy(self, focus, rows=None, explicit=False, exclude=(), cfg=None):
        now = time.time()
        rows = live_rows(self.conn) if rows is None else rows
        rows = [r for r in rows if r["session_id"] not in exclude]
        ev = ledger.evictions_today(self.conn, now)
        before = {r["session_id"]: r.get("transcript") for r in rows}
        before_titles = {r["session_id"]: r.get("title") for r in rows}
        out = await self.off(compute_policy, cfg or self.cfg, focus, rows, ev, explicit, now)
        heal_transcripts(self.conn, rows, before)
        heal_titles(self.conn, rows, before_titles)
        return out

    async def resume_title(self, row):
        """The tab name for a resumed session: resolve_title off the loop, then the healed
        transcript path and title written back here."""
        sid = row["session_id"]
        before, before_title = {sid: row.get("transcript")}, {sid: row.get("title")}
        try:
            title = await self.off(resolve_title, row, self.cfg)
        except Exception:
            log.exception("title of %s unreadable", sid[:8])
            return row.get("title") or sid[:8]
        heal_transcripts(self.conn, [row], before)
        heal_titles(self.conn, [row], before_title)
        return title

    async def pick(self, free, rows=None, explicit=False, exclude=(), policy_cfg=None, reason=None):
        """-> (outcome, session_id picked). Outcome None = nothing evicted (no candidate, or
        dry run); the session id is per call, so concurrent picks never mix them up.
        policy_cfg: the config the guards (and the post-CAS re-check) run with; the idle
        pass swaps idle_min. reason: str, or callable(row) -> str, for the eviction log."""
        async with self.evict_lock:
            return await self._pick_and_evict(free, rows, explicit, exclude, policy_cfg, reason)

    async def pick_and_evict(self, free, rows=None, explicit=False, exclude=(), policy_cfg=None,
                             reason=None):
        """pick() without the session id -> outcome."""
        out, _ = await self.pick(free, rows, explicit, exclude, policy_cfg, reason)
        return out

    async def _pick_and_evict(self, free, rows, explicit, exclude, policy_cfg=None, reason=None):
        focus = await self.snapshot()
        self.stamp_visibility(focus)
        cands, kept, rss, titles = await self.policy(focus, rows, explicit, exclude, cfg=policy_cfg)
        if not cands:
            said = ("none", tuple(sorted((r["session_id"][:8], w) for r, w in kept)))
            # the idle pass finds nothing on most ticks: only the pressure pass logs keeps
            if policy_cfg is None and said != self.last_said:
                log.info("free %s: no eviction candidate (%s)", _pct(free),
                         "; ".join("%s %s" % (r["session_id"][:8], w) for r, w in kept[:12]))
                self.last_said = said
            return None, None
        row = cands[0]
        row["iterm_guid_now"] = focus["tty_guid"].get(row["tty"])
        title = titles.get(row["session_id"]) or row["session_id"][:8]
        if self.dry_run:
            said = ("would", row["session_id"])
            if said != self.last_said:
                log.info("DRY RUN would evict %s %s (free %s, %.0f MB)", row["session_id"][:8],
                         title[:40], _pct(free), rss.get(row["session_id"], 0))
                self.last_said = said
            return None, row["session_id"]

        async def recheck(r):
            # runs AFTER the compare-and-set: the row is ours, re-verify everything
            fresh = ledger.get(self.conn, r["session_id"])
            if not fresh or fresh.get("state") != EVICTING:
                return "row is %s" % (fresh or {}).get("state")
            if fresh.get("last_event_at") != r.get("last_event_at"):
                return "hook activity after the guards ran"
            focus2 = await self.snapshot()
            if focus2["tty_guid"].get(fresh.get("tty")) != r["iterm_guid_now"]:
                return "tab changed"
            check = dict(fresh, state=r["state"], state_since=r.get("state_since"))
            _, k2, _, _ = await self.policy(focus2, rows=[check], explicit=explicit, cfg=policy_cfg)
            return k2[0][1] if k2 else None

        with ledger.tx(self.conn):
            if row["iterm_guid_now"]:
                self.conn.execute("UPDATE sessions SET iterm_guid=? WHERE session_id=?",
                                  (row["iterm_guid_now"], row["session_id"]))
        why = reason(row) if callable(reason) else reason
        self.mark_pass()
        out = await self.evictor.evict(row, free, rss.get(row["session_id"], 0), recheck=recheck,
                                       title=title, reason=why)
        return out, row["session_id"]

    async def evict_pass(self, free, rows=None, explicit=False, limit=50, reason=None):
        """Repeated picks that never re-pick a session that aborted in this pass."""
        aborted, outcomes = set(), []

        async def one(f):
            kw = {"reason": reason} if reason is not None else {}
            out, sid = await self.pick(f, rows=rows, explicit=explicit, exclude=aborted, **kw)
            if out is not None and out not in ("term", "killed") and sid:
                aborted.add(sid)
            if out is not None:
                outcomes.append(out)
                self.alive()
            return out
        return one, outcomes

    def alive(self):
        """liveness after each eviction attempt: a long pass delays the heartbeat (written
        once per completed tick), and doctor must not read that as a hung daemon."""
        record_meta(self.conn, liveness=time.time())

    def mark_pass(self):
        """meta pass_started_at (with liveness, same instant) on the first eviction attempt
        inside busy(), so ticks that evict nothing write no meta."""
        if self.busy_depth > 0 and not self.pass_marked:
            now = time.time()
            record_meta(self.conn, pass_started_at=now, liveness=now)
            self.pass_marked = True

    @contextlib.contextmanager
    def busy(self):
        """An eviction pass is running (the outermost entry starts it). pass_started_at is
        written lazily by mark_pass and cleared when the last pass ends, only if it was
        written; record_start clears a value a dead process left. doctor reads a stale
        heartbeat as busy, not hung, only while liveness is fresh and no older than it."""
        if self.busy_depth == 0:
            self.pass_marked = False
        self.busy_depth += 1
        try:
            yield
        finally:
            self.busy_depth -= 1
            if self.busy_depth == 0 and self.pass_marked:
                record_meta(self.conn, pass_started_at="")
                self.pass_marked = False

    async def tick(self):
        if self.connection is not None and not connection_alive(self.connection):
            raise ConnectionLost("iTerm2 websocket closed")
        now = time.time()
        if now - self.last_prune > PRUNE_EVERY_S:
            ledger.prune(self.conn, now)
            self.last_prune = now
        stuck = ledger.all_rows(self.conn, "state IN (?,?)", (EVICTING, RESUMING))
        if stuck:
            probes = await self.off(probe_rows, stuck)
            for sid, old, new in reconcile(self.conn, probes, now,
                                           min_age_s=float(self.cfg["term_grace_s"]) + 60):
                log.info("reconcile: %s %s -> %s", sid[:8], old, new)
            n = expire_resuming(self.conn, self.cfg, now, probes)
            if n:
                log.info("%d resuming row(s) timed out", n)
        if not self.dry_run:
            self.evictor.sweep_hooks(now)
        focus = await self.snapshot()
        self.stamp_visibility(focus, now)
        if now - self.last_reap > REAP_EVERY_S:
            from ccsessions import cli
            removed, _ = await self.off(functools.partial(cli.reap, self.cfg, dry_run=self.dry_run,
                                                          cmd_table={}, cwds={}))
            if removed:
                log.info("reaped %d dead sockets", len(removed))
            self.last_reap = now
        free = await self.off(procs.free_pct)
        with self.busy():
            if free is None:
                log.warning("memory_pressure unreadable; not evicting")
            else:
                swap = await self.off(procs.swap_pct)
                async with self.pass_lock:
                    one, _ = await self.evict_pass(free, reason=lambda r: self.pressure.trigger)
                    await self.pressure.relieve(free, one, swap=swap)
            await self.idle_pass(free)
        self.heartbeat()  # only after a tick that completed

    async def idle_pass(self, free):
        """Hibernate sessions in state idle for idle_hibernate_min, whatever the memory: the
        same guards (idle_hibernate_min standing in for idle_min, for idle time, focus and
        file activity) and the same Evictor as the pressure pass. At most
        idle_hibernate_per_tick evictions; not subject to the pressure backoff (that is
        about memory relief); serialized with the pressure pass. -> [outcome]."""
        mins = float(self.cfg.get("idle_hibernate_min") or 0)
        if mins <= 0:
            return []
        cap = int(self.cfg.get("idle_hibernate_per_tick") or 0)
        if cap <= 0:
            if not self.idle_disabled_said:
                log.info("idle pass disabled (per_tick %s)", self.cfg.get("idle_hibernate_per_tick"))
                self.idle_disabled_said = True
            return []
        pcfg = config.Config(self.cfg)
        pcfg["idle_min"] = mins
        aborted, outcomes, done, misses = set(), [], 0, 0

        def why(r):
            return "idle %dm" % _mins(time.time() - (r.get("state_since") or time.time()))
        async with self.pass_lock:
            while done < cap:
                if self.connection is not None and not connection_alive(self.connection):
                    raise ConnectionLost("iTerm2 websocket closed")
                rows = [r for r in live_rows(self.conn) if r.get("state") == IDLE]
                out, sid = await self.pick(free, rows=rows, exclude=aborted, policy_cfg=pcfg,
                                           reason=why)
                if out is None:
                    break
                outcomes.append(out)
                self.alive()
                if out in ("term", "killed"):
                    done += 1
                    continue
                if sid:
                    aborted.add(sid)
                misses += 1
                if misses >= MAX_ABORTS_PER_PASS:
                    break
        return outcomes

    async def tick_loop(self):
        failures = 0
        while True:
            try:
                await self.tick()
                failures = 0
            except ConnectionLost:
                raise
            except Exception:
                failures += 1
                log.exception("tick failed (%d in a row)", failures)
                if failures >= self.MAX_TICK_FAILURES:
                    raise ConnectionLost("%d consecutive tick failures" % failures)
            await asyncio.sleep(float(self.cfg["tick_s"]))

    async def handle_requests(self):
        rows = self.conn.execute("SELECT * FROM requests WHERE done_at IS NULL ORDER BY id").fetchall()
        for req in rows:
            req = dict(req)
            try:
                result = await self.do_request(req)
            except ConnectionLost:
                raise
            except Exception as exc:
                log.exception("request %s failed", req["id"])
                result = "error: %s" % exc
            with ledger.tx(self.conn):
                self.conn.execute("UPDATE requests SET done_at=?, result=? WHERE id=?",
                                  (time.time(), result, req["id"]))

    async def do_request(self, req):
        kind, sid = req["kind"], req.get("session_id")
        free = await self.off(procs.free_pct)
        if kind == "hibernate":
            row = ledger.get(self.conn, sid)
            if not row:
                return "unknown session"
            out = await self.pick_and_evict(free, rows=[row], explicit=True)
            return out or "kept (see log)"
        if kind == "hibernate-all-idle":
            one, outcomes = await self.evict_pass(free)
            with self.busy():
                for _ in range(50):
                    if await one(free) is None:
                        break
            return "evicted %d" % len([d for d in outcomes if d in ("term", "killed")])
        if kind == "wake":
            row = ledger.get(self.conn, sid)
            return await self.wake(row) if row else "unknown session"
        if kind == "wake-all":
            limit = int(self.cfg.get("wake_all_limit", 10))
            rows = ledger.all_rows(self.conn, "state=? AND COALESCE(interactive, 1)<>0 ORDER BY "
                                   "evicted_at DESC LIMIT ?", (HIBERNATED, limit))
            res = [await self.wake(r) for r in rows]
            return "woke %d (limit %d)" % (res.count("resumed"), limit)
        return "unknown request kind"

    async def wake(self, row):
        if row.get("state") != HIBERNATED:
            return "state=%s" % row.get("state")
        if row.get("interactive") == 0:
            return "non-interactive"
        if await self.identity(row) != procs.GONE:
            return "already running (or identity unknown)"
        cmd = resumecmd.command(self.cfg, row["session_id"], row.get("cwd"))
        if self.dry_run:
            log.info("DRY RUN would wake %s", row["session_id"][:8])
            return "dry-run"
        guid = row.get("iterm_guid")
        job = await self.tabs.job_name(guid) if guid else None
        if job is not None and not is_shell_job(job):
            job = await settled_job(job, procs.norm_tty(await self.tabs.tty(guid)), self.off)
        ledger.mark_resuming(self.conn, row["session_id"])
        if job is None:
            await self.tabs.new_tab(cmd + "\r")
            return "resumed"
        if not is_shell_job(job):
            ledger.mark_hibernated(self.conn, row["session_id"], only_from=(RESUMING,))
            return "tab is running %s" % job
        await self.tabs.send_text(guid, "\x15" + cmd + "\r")
        await self.tabs.set_name(guid, await self.resume_title(row))
        return "resumed"

    async def request_loop(self):
        failures = 0
        while True:
            try:
                await self.handle_requests()
                failures = 0
            except ConnectionLost:
                raise
            except Exception:
                failures += 1
                log.exception("request poll failed (%d in a row)", failures)
                if failures >= self.MAX_TICK_FAILURES:
                    raise ConnectionLost("%d consecutive request-poll failures" % failures)
            await asyncio.sleep(REQUEST_POLL_S)

    async def on_focus_change(self, session):
        focus = await self.snapshot()
        self.stamp_visibility(focus)
        await self.on_focus(session)

    async def on_focus(self, session):
        if session is None:
            return
        guid = session.session_id
        tty = procs.norm_tty(await iterm_call(session.async_get_variable("tty"), "tty"))
        if not tty:
            return
        now = time.time()
        rows = [r for r in ledger.all_rows(self.conn, "ended_at IS NULL AND (tty=? OR iterm_guid=?) "
                                           "ORDER BY last_event_at DESC", (tty, guid))
                if r.get("interactive") != 0]
        live = []
        for r in rows:
            if r["state"] in LIVE_STATES and r.get("tty") == tty and await self.identity(r) == procs.ALIVE:
                live.append(r)
        if live:
            ledger.set_focus(self.conn, live[0]["session_id"], guid, now)
            return
        hib = [r for r in rows if r["state"] == HIBERNATED and r.get("iterm_guid") == guid]
        if not hib:
            return
        hib.sort(key=lambda r: r.get("evicted_at") or 0, reverse=True)
        row = hib[0]
        job = (await iterm_call(session.async_get_variable("jobName"), "jobName")) or ""
        job = await settled_job(job, tty, self.off)
        gone = await self.identity(row) == procs.GONE
        why = resume_decision(row, guid, tty, now, self.last_fire, job, lambda r: not gone)
        if why:
            if why != "debounce":
                log.info("focus %s: not resuming %s: %s", tty, row["session_id"][:8], why)
            self.last_fire[guid] = now
            return
        self.last_fire[guid] = now
        if self.dry_run:
            log.info("DRY RUN would resume %s on focus", row["session_id"][:8])
            return
        ledger.mark_resuming(self.conn, row["session_id"], now)
        await iterm_call(session.async_send_text(
            "\x15" + resumecmd.command(self.cfg, row["session_id"], row.get("cwd")) + "\r"), "send_text")
        await iterm_call(session.async_set_name(await self.resume_title(row)), "set_name")
        log.info("resumed %s on focus (%s)", row["session_id"][:8], tty)

    async def focus_loop(self):
        import iterm2
        app = self.app
        async with iterm2.FocusMonitor(self.connection) as mon:
            while True:
                update = await mon.async_get_next_update()
                try:
                    s = None
                    if update.selected_tab_changed:
                        tab = app.get_tab_by_id(update.selected_tab_changed.tab_id)
                        s = tab.current_session if tab else None
                    elif update.active_session_changed:
                        s = app.get_session_by_id(update.active_session_changed.session_id)
                    elif update.window_changed and update.window_changed.event == \
                            iterm2.FocusUpdateWindowChanged.Reason.TERMINAL_WINDOW_BECAME_KEY:
                        w = app.get_window_by_id(update.window_changed.window_id)
                        s = w.current_tab.current_session if w and w.current_tab else None
                    await self.on_focus_change(s)
                except ConnectionLost:
                    raise
                except Exception:
                    log.exception("focus handler failed")

    async def run_loops(self, loops=None):
        """Run focus, tick and request loops; the first one to fail cancels the others and
        its exception propagates, so main() exits for a clean restart instead of hanging."""
        coros = loops or (self.focus_loop(), self.tick_loop(), self.request_loop())
        tasks = [asyncio.ensure_future(c) for c in coros]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            # bounded: FocusMonitor's unsubscribe can wait forever on a dead dispatcher
            await asyncio.wait(tasks, timeout=CANCEL_WAIT_S)
        for t in done:
            if not t.cancelled() and t.exception() is not None:
                raise t.exception()
        stuck = [t for t in tasks if not t.done()]
        if stuck:
            log.warning("%d loop(s) did not finish cancelling within %ss; exiting anyway",
                        len(stuck), CANCEL_WAIT_S)
        raise ConnectionLost("daemon loops ended")


# ------------------------------------------------------------------ entry points

def setup_logging(cfg, stderr=False):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    if stderr:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        log.addHandler(h)
        return
    path = cfg.path("log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    h = logging.handlers.RotatingFileHandler(path, maxBytes=1000000, backupCount=3)
    h.setFormatter(fmt)
    log.addHandler(h)


def trim_stdio(cap=OUT_LOG_CAP, fds=(1, 2)):
    """launchd points stdout/stderr at one never-rotated file, and the iterm2 package prints
    tracebacks there. Truncate it at startup when it is over `cap`. Works on the inherited
    descriptors (no path needed); pipes and ttys are left alone. -> bytes dropped."""
    seen, dropped = set(), 0
    for fd in fds:
        try:
            st = os.fstat(fd)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) in seen:
            continue
        seen.add((st.st_dev, st.st_ino))
        if st.st_size <= cap:
            continue
        try:
            os.ftruncate(fd, 0)
            os.write(fd, ("%s cc-sessions: truncated %d bytes of daemon output at startup\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), st.st_size)).encode())
            dropped += st.st_size
        except OSError:
            pass
    return dropped


def record_meta(conn, **values):
    """Best effort: a ledger write must never stop the daemon from starting or exiting."""
    try:
        with ledger.tx(conn):
            for k, v in values.items():
                ledger.set_meta(conn, k, v)
    except Exception:
        log.exception("could not record %s in the ledger", ", ".join(sorted(values)))


def sleep_alive(conn, total, chunk):
    """Sleep `total` seconds in `chunk`-second pieces, writing liveness before each, so a
    long pre-connect wait never reads as an unresponsive daemon."""
    left = total
    while left > 0:
        step = min(chunk, left)
        record_meta(conn, liveness=time.time())
        time.sleep(step)
        left -= step


def record_start(conn, now=None):
    """starts += 1, pid, recent_starts (last RECENT_STARTS start times, for doctor's
    restart-loop warning), liveness. Clears blind_since / skipped_sessions / pass_started_at:
    a value left by an earlier process must not drive doctor's blind-snapshot warning or
    read as a pass in progress."""
    now = time.time() if now is None else now
    try:
        n = int(ledger.get_meta(conn, "starts") or 0)
    except (ValueError, TypeError):
        n = 0
    try:
        recent = [float(t) for t in json.loads(ledger.get_meta(conn, "recent_starts") or "[]")]
    except (ValueError, TypeError):
        recent = []
    recent = (recent + [now])[-RECENT_STARTS:]
    record_meta(conn, starts=n + 1, pid=os.getpid(), recent_starts=json.dumps(recent), liveness=now,
                blind_since="", skipped_sessions=0, pass_started_at="")


async def run_connected(connection, cfg, conn, dry_run, get_app, failure=None):
    """The coroutine handed to iterm2.run_until_complete. A dead connection from an earlier
    run in this process can leave the library's App singleton (and its notification
    handlers) behind; never run on top of it. The library turns any exception into a
    printed traceback + sys.exit(1), so the reason is kept in `failure["error"]`.
    Getting the App is bounded: iTerm2 can accept the websocket and never answer.
    A good connect records `connected_at` and clears `last_error`: whatever the error was,
    it is resolved now, and any later failure records a fresh one on exit."""
    try:
        try:
            app = await asyncio.wait_for(get_app(connection), APP_CONNECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise ConnectionLost("iTerm2 did not answer the App setup within %ss" % APP_CONNECT_TIMEOUT_S)
        if getattr(app, "connection", None) is not connection:
            raise ConnectionLost("stale App singleton")
        d = Daemon(cfg, conn, app, connection, dry_run)
        log.info("connected to iTerm2")
        record_meta(conn, connected_at=time.time(), last_error="")
        await d.run_loops()
    except Exception as exc:
        if failure is not None:
            failure["error"] = "%s: %s" % (type(exc).__name__, exc)
        raise


def take_lock(cfg):
    d = config.db_dir(cfg)
    os.makedirs(d, exist_ok=True)
    fh = open(os.path.join(d, "cc-sessions.daemon.lock"), "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def once_table(cfg, conn, focus, dry_run, out=print):
    now = time.time()
    rows = ledger.all_rows(conn, "ended_at IS NULL AND state IN (?,?,?)", LIVE_STATES)
    cands, kept, rss, facts = policy_pass(conn, cfg, focus, rows=rows, now=now)
    free = procs.free_pct()
    out("free %s%% (evict below %s%%, stop at %s%%)   dry_run=%s   iTerm2 sessions seen: %d" % (
        free, cfg["low_free_pct"], cfg["high_free_pct"], "on" if dry_run else "off",
        len(focus["tty_guid"])))
    out("%-3s %-8s %-8s %-7s %6s  %s" % ("#", "SID", "TTY", "STATE", "RSS", "VERDICT"))
    for i, r in enumerate(cands, 1):
        out("%-3d %-8s %-8s %-7s %5.0fM  CANDIDATE" % (i, r["session_id"][:8], r.get("tty") or "-",
                                                      r["state"], rss.get(r["session_id"], 0)))
    for r, why in kept:
        out("%-3s %-8s %-8s %-7s %5.0fM  keep: %s" % ("-", r["session_id"][:8], r.get("tty") or "-",
                                                     r["state"], rss.get(r["session_id"], 0), why))
    if not rows:
        out("(no live sessions in the ledger at %s)" % cfg.path("db"))
    return cands, kept


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="cc-sessions daemon")
    ap.add_argument("--once", action="store_true", help="print the candidate table and exit")
    ap.add_argument("--dry-run", action="store_true", help="never signal, type or resume")
    a = ap.parse_args(argv)
    cfg = config.load()
    dry_run = bool(a.dry_run or cfg.get("dry_run"))
    setup_logging(cfg, stderr=a.once)
    if cfg.error:
        log.error("config error, dry_run forced: %s", cfg.error)
    try:
        import iterm2
    except ImportError:
        log.error("the iterm2 package is missing from %s (pip install iterm2)", sys.executable)
        if a.once:
            return 1
        time.sleep(60)  # launchd restarts us; do not spin
        return 1

    if a.once:
        if not iterm_running():
            print("iTerm2 is not running", file=sys.stderr)
            return 1
        if not os.environ.get("ITERM2_COOKIE"):
            request_cookie()
        conn = ledger.connect(cfg.path("db"))
        result = {}

        async def once(connection):
            app = await iterm2.async_get_app(connection)
            result["focus"] = await focus_snapshot(app)
        iterm2.run_until_complete(once, retry=False)
        once_table(cfg, conn, result["focus"], dry_run)
        return 0

    lock = take_lock(cfg)
    if lock is None:
        log.info("another daemon holds the lock; exiting")
        return 0
    dropped = trim_stdio()
    if dropped:
        log.warning("daemon output file was %d bytes; truncated", dropped)
    conn = ledger.connect(cfg.path("db"))
    record_start(conn)
    fixed = reconcile(conn, probe_rows(ledger.all_rows(conn, "state=?", (EVICTING,))))
    for sid, old, new in fixed:
        log.info("startup: %s %s -> %s", sid[:8], old, new)
    log.info("daemon start (dry_run=%s, pid %d)", dry_run, os.getpid())
    # Before the first connection attempt no iterm2 library state exists, so waiting here
    # for iTerm2 (or for its cookie) is safe. Once run_until_complete has been entered the
    # library keeps process-global state (the App singleton, notification handlers) that a
    # lost connection leaves dirty, so any end of the connection exits and launchd
    # (KeepAlive) starts a clean process.
    delays = backoff_delays()
    use_env_cookie = bool(os.environ.get("ITERM2_COOKIE"))
    while True:
        record_meta(conn, liveness=time.time())
        try:
            if not iterm_running():
                raise RuntimeError("iTerm2 is not running")
            if not use_env_cookie:
                os.environ.pop("ITERM2_COOKIE", None)
                os.environ.pop("ITERM2_KEY", None)
                request_cookie()
            break
        except Exception as exc:
            use_env_cookie = False
            if api_disabled(exc):
                # each cookie request pops iTerm2's enable-the-API alert: ask rarely
                log.warning("iTerm2's Python API is not enabled; retrying in %ds: %s",
                            API_DISABLED_RETRY_S, exc)
                record_meta(conn, last_error="iTerm2 Python API not enabled (Settings > General > "
                            "Magic > Enable Python API); retrying every %ds: %s" % (API_DISABLED_RETRY_S, exc))
                sleep_alive(conn, API_DISABLED_RETRY_S, API_DISABLED_CHUNK_S)
                continue
            log.warning("waiting for iTerm2: %s", exc)
            record_meta(conn, last_error="waiting for iTerm2: %s" % exc)
            time.sleep(next(delays))
    record_meta(conn, liveness=time.time())
    failure = {}
    try:
        iterm2.run_until_complete(
            functools.partial(run_connected, cfg=cfg, conn=conn, dry_run=dry_run,
                              get_app=iterm2.async_get_app, failure=failure), retry=False)
        reason = "iTerm2 connection ended"
    except (Exception, SystemExit) as exc:  # the iterm2 package sys.exit()s on errors
        if isinstance(exc, SystemExit):
            reason = "iTerm2 connection lost (library exit %s)" % exc.code
        else:
            reason = "iTerm2 connection lost: %s: %s" % (type(exc).__name__, exc)
        if failure.get("error"):
            reason += ": %s" % failure["error"]
    log.warning("%s; exiting so launchd starts a clean process", reason)
    record_meta(conn, last_error=reason[:500], last_exit_at=time.time(), liveness=time.time(),
                pass_started_at="")
    return 1


if __name__ == "__main__":
    sys.exit(main())
