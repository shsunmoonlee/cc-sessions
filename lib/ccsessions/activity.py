"""What a session is doing, shared by the daemon and `cc-tabs`: the in-flight checks behind
guard_reason (so the Working bucket and hibernation cannot disagree), the activity key, the
tab sort order, close-idle's refusals and the picker's rows. Stdlib only."""
import calendar
import json
import os
import time

from . import BUSY, HIBERNATED, IDLE, LIVE_STATES, WAITING, ledger, pending, procs
from . import tabs as tabfile

# A subagent finishing counts: it did the work. SessionStart and SessionEnd do not: a tab
# opened or torn down says nothing about the session being used.
ACTIVITY_EVENTS = ("UserPromptSubmit", "Stop", "StopFailure", "SubagentStop")
# user records nobody typed
NOT_TYPED_PREFIXES = ("<task-notification", "<system-reminder", "<local-command", "<command-", "<ide_", "Caveat:",
                      "This session is being continued", "WorktreeCreate hook failed")
TAIL_CHUNK = 1 << 20
TAIL_LIMIT = 64 << 20

PINNED, NEEDS_YOU, WORKING, REST, NON_CLAUDE = range(5)
BUCKET_NAMES = {PINNED: "pinned", NEEDS_YOU: "needs you", WORKING: "working", REST: "rest", NON_CLAUDE: "other"}
DAY_S = 86400


# ------------------------------------------------------------------ in-flight facts

def as_analysis(v):
    if isinstance(v, dict):
        return v
    return {"pending": list(v or []), "has_user": True}


def _projects(cfg):
    if hasattr(cfg, "path"):
        return cfg.path("claude_projects")
    from . import config
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


class Facts(object):
    """One snapshot of the slow facts (process table, transcripts) for a policy pass."""

    def __init__(self, cfg, now):
        self.cfg = cfg
        self.now = now
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


def stale_busy(row, now, cfg):
    """Busy for so long that the transcript decides: an Esc-interrupted turn fires no Stop."""
    return row.get("state") == BUSY and now - (row.get("state_since") or now) > float(cfg["stale_busy_min"]) * 60


def subagent_reason(row):
    n = row.get("subagents") or 0
    return "subagents=%d" % n if n > 0 else None


def background_reason(analysis, stale):
    if analysis.get("pending"):
        return "pending: " + ", ".join(analysis["pending"])[:60]
    if stale and not pending.interrupted_last(analysis):
        return "state=busy"
    return None


def children_reason(kids):
    return "running: " + ", ".join(sorted(set(kids))[:3]) if kids else None


def work_reason(row, now, cfg, analysis_fn, children_fn):
    """Why this row has work in flight, None when it has none: the part of guard_reason that is
    about work, not about recent focus or idle time. Every answer here is also a reason
    guard_reason keeps the row. A hibernated row has no process and no running transcript."""
    st = row.get("state")
    if st == HIBERNATED:
        return None
    stale = stale_busy(row, now, cfg)
    if st != IDLE and not stale:
        return "state=%s" % st
    why = subagent_reason(row)
    if why:
        return why
    # no transcript yet is a session nobody has typed in; guard_reason keeps that too, not as work
    if row_transcript(row, cfg):
        why = background_reason(as_analysis(analysis_fn(row)), stale)
    else:
        why = "state=busy" if stale else None
    return why or children_reason(children_fn(row))


# ------------------------------------------------------------------ activity

def newest_events(conn, session_id=None):
    """{session id: time of its newest activity event}, one grouped query."""
    marks = ",".join("?" * len(ACTIVITY_EVENTS))
    sql = "SELECT session_id, MAX(ts) FROM events WHERE event IN (%s)" % marks
    args = list(ACTIVITY_EVENTS)
    if session_id is not None:
        sql += " AND session_id=?"
        args.append(session_id)
    return {r[0]: r[1] for r in conn.execute(sql + " GROUP BY session_id", args) if r[0]}


def _timestamp(text):
    try:
        return float(calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")))
    except (TypeError, ValueError):
        return None


def _record_is_work(d):
    """A prompt someone typed, or a real reply. A resume appends meta and synthetic records with
    fresh timestamps, so a woken session must not look used; the replies to a background task's
    notification still count through the assistant branch."""
    if d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
        return False
    msg = d.get("message") or {}
    content = msg.get("content")
    if d.get("type") == "assistant":
        if msg.get("model") == "<synthetic>" or d.get("isApiErrorMessage") or not isinstance(content, list):
            return False
        return any(isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
                   for b in content)
    if d.get("type") != "user":
        return False
    if isinstance(content, list):
        content = " ".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")
    text = (content or "").strip() if isinstance(content, str) else ""
    return bool(text) and not text.startswith(NOT_TYPED_PREFIXES)


def last_work_time(path, chunk=TAIL_CHUNK, limit=TAIL_LIMIT):
    """When this transcript last held real work, read from the end. None when none turns up
    within `limit` bytes or the file can't be read."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            floor = max(0, pos - limit)
            tail = b""
            while pos > floor:
                step = min(chunk, pos - floor)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + tail
                if pos > 0 and b"\n" not in data:
                    tail = data
                    continue
                lines = data.split(b"\n")
                # the first piece may be the end of a line that starts in an earlier chunk
                tail = lines.pop(0) if pos > 0 else b""
                for line in reversed(lines):
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(d, dict) and _record_is_work(d):
                        ts = _timestamp(d.get("timestamp"))
                        if ts:
                            return ts
    except OSError:
        pass
    return None


def activity_times(conn, cfg, rows, session_id=None):
    """{session id: newest activity}. Events are pruned at 30 days, so a session with none left
    gets the time from its transcript. `session_id`: that one session only."""
    if session_id is not None:
        rows = [r for r in rows if r["session_id"] == session_id]
    times = newest_events(conn, session_id)
    for r in rows:
        if r["session_id"] not in times:
            path = row_transcript(r, cfg)
            ts = last_work_time(path) if path else None
            if ts:
                times[r["session_id"]] = ts
    return times


def activity_time(row, events):
    """Newest activity, else when the session started, 0 when unknown. Never the resume time:
    opening or waking a session doesn't make it recent."""
    return events.get(row["session_id"]) or row.get("started_at") or 0


# ------------------------------------------------------------------ sort order

def bucket_of(row, pinned, work_fn):
    if pinned:
        return PINNED
    if row.get("state") == WAITING:
        return NEEDS_YOU
    if work_fn(row):
        return WORKING
    return REST


def tab_info(rows, events, group_of, pinned_of, work_fn):
    """One tab's place: rows are its panes' sessions (several in a split tab, where the most
    urgent pane and the newest activity decide). No rows = not a Claude tab."""
    if not rows:
        return {"bucket": NON_CLAUDE, "activity": 0, "group": None}
    return {"bucket": min(bucket_of(r, pinned_of(r["session_id"]), work_fn) for r in rows),
            "activity": max(activity_time(r, events) for r in rows),
            "group": next((group_of(r["session_id"]) for r in rows if group_of(r["session_id"])), None)}


def sort_order(infos):
    """Indexes of `infos` (tab_info dicts) in tab order: pinned, needs you, working, then the
    rest, where a group and an ungrouped session rank alike by newest activity and a group's
    members sit together, then non-Claude tabs as they were."""
    def by(bucket):
        return [i for i, t in enumerate(infos) if t["bucket"] == bucket]

    def newest_first(idx):
        return sorted(idx, key=lambda i: -infos[i]["activity"])

    rest = by(REST)
    rank = {}
    for i in rest:
        g = infos[i]["group"]
        if g:
            rank[g] = max(rank.get(g, 0), infos[i]["activity"])

    def rest_key(i):
        g = infos[i]["group"]
        # (0, name) before (1, i) keeps a group whole when an ungrouped tab ties its rank
        tie = (0, g) if g else (1, str(i).zfill(6))
        return (-(rank[g] if g else infos[i]["activity"]), tie, -infos[i]["activity"])

    return (newest_first(by(PINNED)) + newest_first(by(NEEDS_YOU)) + newest_first(by(WORKING))
            + sorted(rest, key=rest_key) + by(NON_CLAUDE))


# ------------------------------------------------------------------ close-idle

def close_refusal(row, panes_in_tab, is_current, now, days, entry, activity, probe, job_fn):
    """Why this tab must stay open, None when every condition holds. Cheap checks first; the
    process and tty are asked only of a tab that got that far. `entry`: its groups-file entry."""
    if is_current:
        return "current tab"
    if panes_in_tab != 1:
        return "split tab"
    if not row:
        return "no session"
    why = row_refusal(row, now, days, entry, activity, probe)
    if why:
        return why
    job = job_fn()
    if job not in procs.SHELL_JOBS:
        return "foreground job %s" % (job or "unknown")
    return None


def row_refusal(row, now, days, entry, activity, probe):
    """The part of close_refusal that the ledger, the groups file and the process table can
    answer, with no iTerm2 API and no tty."""
    if row.get("state") != HIBERNATED:
        return "state=%s" % row.get("state")
    if not activity or now - activity <= days * DAY_S:
        return "active within %g days" % days
    if entry.get("pinned"):
        return "pinned"
    if entry.get("group"):
        return "in group %s" % entry["group"]
    if probe(row.get("pid"), row.get("pid_start")) != procs.GONE:
        return "process alive or unknown"
    return None


def idle_estimate(conn, doc, days, now, guids_fn, cfg=None):
    """About how many tabs close-idle would close, for the menu's minute refresh: no cookie, no
    websocket, no iterm2 import. It cannot see the selected tab, a split or the foreground job,
    so it can read high; a click gets the exact number from a dry run. guids_fn runs only when
    a row qualifies, and returns the live tab GUIDs or None. -> count, None when not asked."""
    from . import tabgroups
    rows = resumable_rows(conn)
    events = activity_times(conn, cfg, rows) if cfg is not None else newest_events(conn)
    parked = tabfile.session_index(rows)[1]
    hits = [guid for guid, r in parked.items()
            if row_refusal(r, now, days, tabgroups.entry(doc, r["session_id"]), activity_time(r, events),
                           procs.identity_probe) is None]
    if not hits:
        return 0
    guids = guids_fn()
    return None if guids is None else sum(1 for g in hits if g in guids)


# ------------------------------------------------------------------ picker

def display_title(row):
    return row.get("title") or os.path.basename(row.get("cwd") or "") or row["session_id"][:8]


def resumable_rows(conn):
    states = LIVE_STATES + (HIBERNATED,)
    rows = ledger.all_rows(conn, "ended_at IS NULL AND state IN (%s)" % ",".join("?" * len(states)), states)
    return [r for r in rows if r.get("interactive") != 0]


def find_rows(rows, events, group_of, pinned_of, live_guids):
    """The picker's rows, newest first. No group is "", not null: the menu decodes this."""
    out = []
    for r in rows:
        sid = r["session_id"]
        out.append({"session_id": sid, "title": display_title(r), "activity": activity_time(r, events),
                    "state": r.get("state"), "group": group_of(sid) or "", "pinned": bool(pinned_of(sid)),
                    "folder": os.path.basename((r.get("cwd") or "").rstrip("/")),
                    "tab_open": bool(live_guids) and r.get("iterm_guid") in live_guids})
    out.sort(key=lambda d: -d["activity"])
    return out
