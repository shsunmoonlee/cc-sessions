"""The session ledger: one SQLite file (WAL) written by hooks and the daemon.

One writer per fact: hooks write session state; the daemon writes focus, evictions,
requests and the heartbeat. Every write is a short BEGIN IMMEDIATE transaction.
"""
import contextlib
import glob
import json
import os
import re
import sqlite3
import time

from . import (BUSY, ENDED, EVICTING, HIBERNATED, IDLE, PARKED_STATES, RESUMING,
               SCHEMA_VERSION, SUPERSEDED, WAITING)

V1_COLUMNS = ("session_id, pid, interactive, tty, cwd, launch_cwd, transcript, title, state, "
              "state_since, last_event, last_event_at, subagents, started_at, ended_at, "
              "end_reason, source, iterm_guid, last_focus_at, resumed_at, evicted_at")

SESSIONS_TABLE = """
CREATE TABLE sessions(
  session_id TEXT PRIMARY KEY, pid INTEGER, pid_start INTEGER, interactive INTEGER DEFAULT 1,
  tty TEXT, cwd TEXT, launch_cwd TEXT, transcript TEXT, title TEXT,
  state TEXT, state_since REAL, last_event TEXT, last_event_at REAL,
  subagents INTEGER DEFAULT 0, started_at REAL, ended_at REAL, end_reason TEXT,
  source TEXT, iterm_guid TEXT, last_focus_at REAL, resumed_at REAL, evicted_at REAL,
  held_state TEXT, restored_at REAL)"""

SESSIONS_INDEX = "CREATE INDEX IF NOT EXISTS sessions_pid ON sessions(pid, pid_start)"

SCHEMA = SESSIONS_TABLE + ";\n" + SESSIONS_INDEX + """;
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, session_id TEXT,
  event TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS evictions(id INTEGER PRIMARY KEY, session_id TEXT, ts REAL,
  pid INTEGER, free_pct REAL, rss_mb REAL, signal TEXT, outcome TEXT, detail TEXT, hook_at REAL);
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, ts REAL, kind TEXT,
  session_id TEXT, done_at REAL, result TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

# readers (state, holds, cc-resume) only read sessions columns that v2 already has: they
# must keep answering on a ledger the running daemon has not migrated yet, or the
# SessionEnd handoff reads "unknown" for a hibernating session and releases its claims
READ_COMPATIBLE_FROM = 2

EVENTS_KEEP_S = 30 * 86400
EVICTIONS_KEEP_S = 90 * 86400
SESSION_END_HIBERNATE_WINDOW_S = 60


SID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class LedgerError(Exception):
    pass


def resolve_transcript(projects, sid, recorded, stale_s=600, now=None):
    """The transcript file of session `sid`: `recorded` when it is a regular file modified
    within `stale_s`, else the newest by mtime of `recorded` (when a file) and every
    <projects>/*/<sid>.jsonl (a session resumed from another cwd is reported under the new
    cwd's project dir but keeps appending in the one it was created in), else None.
    Only a UUID-shaped sid is searched for, and only one directory level deep."""
    best, best_m = None, None
    if recorded and os.path.isfile(recorded):
        try:
            best_m = os.path.getmtime(recorded)
        except OSError:
            best_m = None
        if best_m is not None:
            best = recorded
            now = time.time() if now is None else now
            if now - best_m < float(stale_s):
                return recorded
    if not projects or not isinstance(sid, str) or not SID_RE.match(sid):
        return best
    for p in glob.glob(os.path.join(glob.escape(projects), "*", sid + ".jsonl")):
        try:
            if not os.path.isfile(p):
                continue
            m = os.path.getmtime(p)
        except OSError:
            continue
        if best_m is None or m > best_m:
            best, best_m = p, m
    return best


def heal_transcript(conn, sid, old, new):
    """Best effort: point the row at the transcript the resolver found, unless a hook
    changed the column meanwhile. -> rows updated (0 on any SQLite error)."""
    try:
        with tx(conn):
            cur = conn.execute("UPDATE sessions SET transcript=? WHERE session_id=? AND "
                               "transcript IS ?", (new, sid, old))
            return cur.rowcount
    except sqlite3.Error:
        return 0


def heal_title(conn, sid, old, new):
    """Best effort: record the transcript's title, unless the column changed meanwhile.
    -> rows updated (0 on any SQLite error)."""
    try:
        with tx(conn):
            cur = conn.execute("UPDATE sessions SET title=? WHERE session_id=? AND title IS ?",
                               (new, sid, old))
            return cur.rowcount
    except sqlite3.Error:
        return 0


def connect(path, readonly=False):
    if readonly:
        if not os.path.exists(path):
            raise LedgerError("no ledger at %s" % path)
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=3.0,
                               isolation_level=None)
    else:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(path, timeout=3.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    if readonly:
        v = conn.execute("PRAGMA user_version").fetchone()[0]
        if not READ_COMPATIBLE_FROM <= v <= SCHEMA_VERSION:
            raise LedgerError("schema version %s, expected %s..%s"
                              % (v, READ_COMPATIBLE_FROM, SCHEMA_VERSION))
        return conn
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        conn.execute("PRAGMA journal_mode=WAL")
        with tx(conn):
            v = conn.execute("PRAGMA user_version").fetchone()[0]
            if v == 0:
                for stmt in SCHEMA.strip().split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
            elif v in (1, 2):
                if v == 1:
                    migrate_v1_to_v2(conn)
                migrate_v2_to_v3(conn)
            elif v != SCHEMA_VERSION:
                raise LedgerError("ledger schema %s is newer than this code (%s)" % (v, SCHEMA_VERSION))
            conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
    return conn


def migrate_v1_to_v2(conn):
    """v1 -> v2 inside the caller's transaction: add held_state + restored_at and give
    pid_start INTEGER affinity (a table rebuild: ALTER cannot change a column's type).
    A pid_start that is not a plain integer (v1 stored the ps lstart text) becomes NULL,
    which makes the row's identity 'unknown': never evicted, never treated as alive."""
    conn.execute("ALTER TABLE sessions RENAME TO sessions_v1")
    conn.execute(SESSIONS_TABLE)
    conn.execute("INSERT INTO sessions(%s, pid_start) SELECT %s, CASE "
                 "WHEN typeof(pid_start)='integer' THEN pid_start "
                 "WHEN typeof(pid_start)='text' AND pid_start<>'' AND pid_start NOT GLOB '*[^0-9]*' "
                 "THEN CAST(pid_start AS INTEGER) ELSE NULL END FROM sessions_v1"
                 % (V1_COLUMNS, V1_COLUMNS))
    conn.execute("DROP TABLE sessions_v1")
    conn.execute(SESSIONS_INDEX)


def migrate_v2_to_v3(conn):
    """v2 -> v3 inside the caller's transaction: evictions.hook_at, stamped when the
    on_evict hook has run for that eviction. Rows that predate it count as run, so the
    first v3 daemon replays nothing."""
    conn.execute("ALTER TABLE evictions ADD COLUMN hook_at REAL")
    conn.execute("UPDATE evictions SET hook_at=ts")


@contextlib.contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def row_dict(r):
    return dict(r) if r is not None else None


def get(conn, sid):
    return row_dict(conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone())


def all_rows(conn, where="1=1", args=()):
    return [dict(r) for r in conn.execute("SELECT * FROM sessions WHERE " + where, args)]


def add_event(conn, sid, event, detail, now):
    conn.execute("INSERT INTO events(ts, session_id, event, detail) VALUES (?,?,?,?)",
                 (now, sid, event, json.dumps(detail, separators=(",", ":")) if detail else None))


def _set_state(fields, row, new_state, now):
    if row is None or row.get("state") != new_state:
        fields["state"] = new_state
        fields["state_since"] = now


def apply_event(conn, event, payload, proc, now=None, projects=None):
    """Apply one hook event. `proc` is {pid, pid_start, tty, interactive} for the claude
    process that fired the hook, or None when it could not be found. `projects` is the
    Claude Code projects dir the transcript is looked up in when the payload's
    transcript_path does not exist (default: that path's grandparent). Returns the new row."""
    now = time.time() if now is None else now
    sid = payload.get("session_id")
    if not sid or not isinstance(sid, str):
        raise ValueError("hook payload has no session_id")
    tpath = payload.get("transcript_path")
    if tpath and isinstance(tpath, str):
        if projects is None:
            projects = os.path.dirname(os.path.dirname(tpath))
        # a brand-new session has no file yet: keep the payload path, later events re-resolve
        tpath = resolve_transcript(projects, sid, tpath) or tpath
    with tx(conn):
        row = get(conn, sid)
        fields = {"last_event": event, "last_event_at": now, "source": "hook"}
        if row is None:
            conn.execute("INSERT INTO sessions(session_id, state, state_since, started_at, "
                         "subagents, launch_cwd) VALUES (?,?,?,?,0,?)",
                         (sid, IDLE, now, now, payload.get("cwd")))
            row = get(conn, sid)
            new = True
        else:
            new = False
        if payload.get("cwd"):
            fields["cwd"] = payload["cwd"]
            if not row.get("launch_cwd"):
                fields["launch_cwd"] = payload["cwd"]
        if tpath:
            fields["transcript"] = tpath
        if proc:
            fields["pid"] = proc.get("pid")
            fields["pid_start"] = proc.get("pid_start")
            if proc.get("tty"):
                fields["tty"] = proc["tty"]
            if proc.get("interactive") is not None:
                fields["interactive"] = 1 if proc["interactive"] else 0
        state = row.get("state")
        detail = {}
        if event == "SessionStart":
            src = payload.get("source") or "startup"
            detail["source"] = src
            fields["ended_at"] = None
            fields["end_reason"] = None
            fields["restored_at"] = None
            if src in ("startup", "clear", "resume"):
                fields["subagents"] = 0
            if src in ("startup", "clear"):
                _set_state(fields, row, IDLE, now)
                if not new:
                    fields["started_at"] = now
            elif src == "resume":
                if state in PARKED_STATES:
                    fields["resumed_at"] = now
                _set_state(fields, row, IDLE, now)
            elif src == "compact":
                if state in (ENDED, SUPERSEDED, HIBERNATED, EVICTING, RESUMING, None):
                    _set_state(fields, row, IDLE, now)
            else:
                _set_state(fields, row, IDLE, now)
            if proc and proc.get("pid") and proc.get("pid_start") is not None:
                # every other live-looking row of this exact process, evicting included:
                # /clear and in-app /resume change the session id inside one process
                others = conn.execute(
                    "SELECT session_id FROM sessions WHERE pid=? AND pid_start=? AND "
                    "session_id<>? AND ended_at IS NULL AND state<>?",
                    (proc["pid"], proc["pid_start"], sid, SUPERSEDED)).fetchall()
                for o in others:
                    conn.execute("UPDATE sessions SET state=?, state_since=?, ended_at=?, "
                                 "end_reason='superseded' WHERE session_id=?",
                                 (SUPERSEDED, now, now, o[0]))
                    add_event(conn, o[0], "superseded", {"by": sid}, now)
        elif event == "UserPromptSubmit":
            _set_state(fields, row, BUSY, now)
        elif event in ("Stop", "StopFailure"):
            _set_state(fields, row, IDLE, now)
        elif event == "SubagentStart":
            fields["subagents"] = (row.get("subagents") or 0) + 1
        elif event == "SubagentStop":
            fields["subagents"] = max(0, (row.get("subagents") or 0) - 1)
        elif event == "Notification":
            nt = payload.get("notification_type") or ""
            detail["type"] = nt
            if nt in ("permission_prompt", "elicitation_dialog"):
                _set_state(fields, row, WAITING, now)
            elif nt == "idle_prompt":
                # only busy -> idle (Stop does not fire on Esc); waiting stays waiting
                subs = fields.get("subagents", row.get("subagents") or 0)
                if subs == 0 and state == BUSY:
                    _set_state(fields, row, IDLE, now)
        elif event == "SessionEnd":
            reason = payload.get("reason") or "other"
            detail["reason"] = reason
            fields["end_reason"] = reason
            if is_eviction_end(row, reason, now):
                _set_state(fields, row, HIBERNATED, now)
                fields["held_state"] = None
            else:
                _set_state(fields, row, ENDED, now)
                fields["ended_at"] = now
                fields["held_state"] = None
        if state == EVICTING and event not in ("SessionEnd", "SessionStart") and "state" in fields:
            # the daemon owns an evicting row's state; keep what the hook would have set so
            # a rolled-back eviction restores it, and last_event_at tells the daemon's
            # post-CAS re-check that the session moved
            fields["held_state"] = fields.pop("state")
            fields.pop("state_since", None)
        elif event == "SessionStart":
            fields["held_state"] = None
        cols = sorted(fields)
        conn.execute("UPDATE sessions SET %s WHERE session_id=?" % ", ".join("%s=?" % c for c in cols),
                     [fields[c] for c in cols] + [sid])
        add_event(conn, sid, event, detail, now)
        return get(conn, sid)


def is_eviction_end(row, reason, now):
    """A SessionEnd is the end of OUR eviction only with reason 'other' (what SIGTERM
    produces) while evicting, or right after one that no resume has followed."""
    if reason != "other":
        return False
    if row.get("state") == EVICTING:
        return True
    ev, ra = row.get("evicted_at"), row.get("resumed_at")
    return bool(ev and now - ev <= SESSION_END_HIBERNATE_WINDOW_S and (ra is None or ra < ev))


def state_of(conn, sid):
    r = conn.execute("SELECT state FROM sessions WHERE session_id=?", (sid,)).fetchone()
    return r[0] if r else None


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                 "value=excluded.value", (key, str(value)))


def get_meta(conn, key):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def prune(conn, now=None):
    now = time.time() if now is None else now
    with tx(conn):
        conn.execute("DELETE FROM events WHERE ts < ?", (now - EVENTS_KEEP_S,))
        conn.execute("DELETE FROM evictions WHERE ts < ?", (now - EVICTIONS_KEEP_S,))
        conn.execute("DELETE FROM requests WHERE done_at IS NOT NULL AND done_at < ?",
                     (now - EVENTS_KEEP_S,))


def add_request(conn, kind, sid, now=None):
    now = time.time() if now is None else now
    with tx(conn):
        cur = conn.execute("INSERT INTO requests(ts, kind, session_id) VALUES (?,?,?)",
                           (now, kind, sid))
        return cur.lastrowid


def evictions_today(conn, now=None):
    now = time.time() if now is None else now
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    out = {}
    for r in conn.execute("SELECT session_id, COUNT(*) FROM evictions WHERE ts >= ? AND "
                          "outcome NOT IN ('aborted', 'started') GROUP BY session_id", (midnight,)):
        out[r[0]] = r[1]
    return out


def begin_eviction(conn, row, free, rss, now=None):
    """Compare-and-set: flip the row to evicting only if its state and last_event_at are
    exactly what the guards saw. Returns the evictions row id, or None when the row moved."""
    now = time.time() if now is None else now
    with tx(conn):
        cur = conn.execute(
            "UPDATE sessions SET state=?, state_since=?, evicted_at=?, restored_at=NULL WHERE "
            "session_id=? AND state=? AND last_event_at IS ?",
            (EVICTING, now, now, row["session_id"], row["state"], row["last_event_at"]))
        if cur.rowcount != 1:
            return None
        cur = conn.execute("INSERT INTO evictions(session_id, ts, pid, free_pct, rss_mb, signal, "
                           "outcome) VALUES (?,?,?,?,?,?,?)",
                           (row["session_id"], now, row["pid"], free, rss, "SIGTERM", "started"))
        add_event(conn, row["session_id"], "evict", {"free": free}, now)
        return cur.lastrowid


def finish_eviction(conn, eviction_id, outcome, signal=None, detail=None):
    with tx(conn):
        if signal:
            conn.execute("UPDATE evictions SET outcome=?, signal=?, detail=? WHERE id=?",
                         (outcome, signal, detail, eviction_id))
        else:
            conn.execute("UPDATE evictions SET outcome=?, detail=? WHERE id=?",
                         (outcome, detail, eviction_id))


def hook_done(conn, eviction_id, now=None):
    """Stamp an eviction's on_evict hook as run (or as not needed)."""
    if eviction_id is None:
        return
    now = time.time() if now is None else now
    with tx(conn):
        conn.execute("UPDATE evictions SET hook_at=? WHERE id=? AND hook_at IS NULL",
                     (now, eviction_id))


def pending_hooks(conn, older_than):
    """Evictions whose on_evict hook never ran, begun before `older_than`, including ones
    still 'started' (cut off between the signal and the hook). Aborted ones never
    signalled. Only each session's newest eviction counts: a newer one means it resumed."""
    return [row_dict(r) for r in conn.execute(
        "SELECT e.id AS eviction_id, e.outcome, s.* FROM evictions e "
        "JOIN sessions s ON s.session_id = e.session_id "
        "WHERE e.hook_at IS NULL AND e.outcome <> 'aborted' AND e.ts < ? "
        "AND e.id = (SELECT MAX(id) FROM evictions WHERE session_id = e.session_id) "
        "ORDER BY e.id", (older_than,))]


def begin_hook_tracking(conn, now=None):
    """Once per ledger, the first daemon that stamps hooks: every eviction before it counts
    as run. A pre-v3 daemon still running after the migration wrote its evictions unstamped
    and did run their hooks. -> rows stamped."""
    now = time.time() if now is None else now
    with tx(conn):
        if conn.execute("SELECT 1 FROM meta WHERE key='hooks_tracked_since'").fetchone():
            return 0
        n = conn.execute("UPDATE evictions SET hook_at=ts WHERE hook_at IS NULL AND ts < ?",
                         (now,)).rowcount
        conn.execute("INSERT INTO meta(key, value) VALUES ('hooks_tracked_since', ?)", (repr(now),))
        return n


def rollback_eviction(conn, sid, prev_state, now=None, prev_since=None):
    """Undo an evicting flip when no signal was sent. A state a hook recorded meanwhile
    (held_state) wins over the state the guards saw."""
    now = time.time() if now is None else now
    with tx(conn):
        conn.execute("UPDATE sessions SET state=COALESCE(held_state, ?), state_since=CASE WHEN "
                     "held_state IS NULL THEN COALESCE(?, ?) ELSE ? END, held_state=NULL, "
                     "evicted_at=NULL WHERE session_id=? AND state=?",
                     (prev_state, prev_since, now, now, sid, EVICTING))
        add_event(conn, sid, "evict-rollback", None, now)


def mark_hibernated(conn, sid, now=None, only_from=(EVICTING,)):
    now = time.time() if now is None else now
    with tx(conn):
        q = ",".join("?" * len(only_from))
        cur = conn.execute("UPDATE sessions SET state=?, state_since=?, held_state=NULL WHERE "
                           "session_id=? AND state IN (%s)" % q, (HIBERNATED, now, sid) + tuple(only_from))
        return cur.rowcount


def mark_resuming(conn, sid, now=None):
    now = time.time() if now is None else now
    with tx(conn):
        cur = conn.execute("UPDATE sessions SET state=?, state_since=? WHERE session_id=? AND "
                           "state IN (?,?,?)", (RESUMING, now, sid, HIBERNATED, EVICTING, RESUMING))
        if cur.rowcount:
            add_event(conn, sid, "resuming", None, now)
        return cur.rowcount


def claim_resume(conn, sid, pid, pid_start, now=None):
    """cc-resume, just before it execs claude in its own process: the row takes this
    process's identity (exec keeps pid and start time), so SessionStart's supersede applies
    if the resume comes up under a new session id. Parked rows move to resuming."""
    now = time.time() if now is None else now
    with tx(conn):
        cur = conn.execute("UPDATE sessions SET pid=?, pid_start=?, state=CASE WHEN state IN (?,?,?) "
                           "THEN ? ELSE state END, state_since=? WHERE session_id=?",
                           (pid, pid_start, HIBERNATED, EVICTING, RESUMING, RESUMING, now, sid))
        if cur.rowcount:
            add_event(conn, sid, "resuming", {"pid": pid}, now)
        return cur.rowcount


def set_focus(conn, sid, guid, now=None):
    now = time.time() if now is None else now
    with tx(conn):
        conn.execute("UPDATE sessions SET iterm_guid=?, last_focus_at=? WHERE session_id=?",
                     (guid, now, sid))


def stamp_visible(conn, tty_guid, now=None):
    """last_focus_at = now for every live row whose tty is shown right now (all panes of
    each window's current tab), so it records the last time a session was SEEN. Evicting
    rows too: the daemon's last check before SIGTERM aborts when last_focus_at moved. An
    evicting row keeps its iterm_guid: a tty reused by another pane mid-eviction must not
    rebind the row to that pane's tab."""
    now = time.time() if now is None else now
    if not tty_guid:
        return 0
    n = 0
    with tx(conn):
        for tty, guid in tty_guid.items():
            cur = conn.execute("UPDATE sessions SET last_focus_at=?, iterm_guid=CASE WHEN "
                               "state=? THEN iterm_guid ELSE ? END WHERE tty=? AND "
                               "ended_at IS NULL AND state IN (?,?,?,?)",
                               (now, EVICTING, guid, tty, BUSY, IDLE, WAITING, EVICTING))
            n += cur.rowcount
    return n


def mark_restored(conn, sid, guid, now=None):
    now = time.time() if now is None else now
    with tx(conn):
        conn.execute("UPDATE sessions SET restored_at=?, iterm_guid=COALESCE(?, iterm_guid) WHERE "
                     "session_id=?", (now, guid, sid))


def set_guid(conn, sid, guid):
    with tx(conn):
        conn.execute("UPDATE sessions SET iterm_guid=? WHERE session_id=? AND "
                     "(iterm_guid IS NULL OR iterm_guid<>?)", (guid, sid, guid))
