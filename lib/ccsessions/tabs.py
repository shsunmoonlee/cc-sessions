"""Tab snapshots: the ordered iTerm2 tab list, one JSON file per iTerm2 process lifetime.

The daemon rewrites <iterm_start>-<iterm_pid>.json whenever the list changes, so the last
write of a dead iTerm2 instance IS the state at death, whatever killed it. `restore` reads
that file instead of inferring what was open from process ends. A plain file on purpose: it
survives a damaged ledger and needs no schema change.

A file holds {"v", "iterm_pid", "iterm_start", "taken_at", "windows": [{"window_id", "tabs":
[{"tab_id", "sessions": [{"guid", "tty", "session_id", "state", "title", "cwd"}]}]}],
"gui_session": {"loginwindow": {"pid", "start"}, "WindowServer": {"pid", "start"}}}, with
session_id null for a pane that runs no Claude Code session. Marker files sit beside a
snapshot: .restored (--auto consumed it), .ended_alone (its iTerm2 went down while the GUI
session lived on: restored on a click, not at login), .dismissed (the owner waved it off).
"""
import fcntl
import json
import logging
import os
import re
import subprocess
import tempfile
import time

from . import LIVE_STATES, PARKED_STATES, SUPERSEDED, config, procs

log = logging.getLogger("cc-sessions")

VERSION = 1
KEEP_FILES = 20
SHRINK_KEEP_RATIO = 0.5
TMP_MAX_AGE_S = 3600
# `tabs --json` probes the lock every minute; a real run must not lose to that probe
LOCK_TRIES = 3
LOCK_RETRY_S = 0.05
SNAP_RE = re.compile(r"^\d+-\d+(\.before-shrink-\d+)?\.json$")
MARKER_SUFFIX = ".restored"
ENDED_ALONE_SUFFIX = ".ended_alone"
DISMISSED_SUFFIX = ".dismissed"
MARKER_SUFFIXES = (MARKER_SUFFIX, ENDED_ALONE_SUFFIX, DISMISSED_SUFFIX)
GUI_PROCESSES = ("loginwindow", "WindowServer")


def snapshots_dir(cfg):
    """Config tab_snapshots_dir, else cc-sessions-tabs beside the ledger: a temp ledger
    (tests, a live rehearsal) can never read or write the real snapshots."""
    return cfg.path("tab_snapshots_dir") or os.path.join(config.db_dir(cfg), "cc-sessions-tabs")


def _processes(name):
    """[(start, pid)] of every process with exactly this name."""
    # -a: macOS pgrep skips its own ancestors by default, and iTerm2 is an ancestor of
    # every shell in an iTerm2 tab
    try:
        r = subprocess.run(["pgrep", "-ax", name], capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return []
    found = []
    for word in r.stdout.split():
        try:
            pid = int(word)
            ent = procs.lstart_comm(pid)
        except (ValueError, procs.ProcsError):
            continue
        if ent:
            found.append((int(ent[0]), pid))
    return found


def gui_session():
    """{name: {"pid", "start"}} for the processes that restart with the GUI session; None when
    any cannot be read, which callers must treat as "cannot tell"."""
    out = {}
    for name in GUI_PROCESSES:
        found = _processes(name)
        if not found:
            return None
        start, pid = max(found)
        out[name] = {"pid": pid, "start": start}
    return out


def iterm_instance():
    """{"pid", "start"} of the running iTerm2, None when not running. Two pids (a hung old
    one and its replacement): the newest start wins."""
    found = _processes("iTerm2")
    if not found:
        return None
    if len(found) > 1:
        log.warning("%d iTerm2 processes answer (%s); using the newest", len(found),
                    ", ".join(str(p) for _, p in found))
    start, pid = max(found)
    return {"pid": pid, "start": start}


def same_instance(snap, instance):
    return bool(instance) and snap.get("iterm_pid") == instance["pid"] \
        and snap.get("iterm_start") == instance["start"]


def session_index(rows):
    """-> (live row by tty, parked row by guid): newest row wins, interactive rows only."""
    live, parked = {}, {}
    for r in sorted(rows, key=lambda r: r.get("last_event_at") or 0):
        if r.get("interactive") == 0 or r.get("state") == SUPERSEDED or r.get("ended_at") is not None:
            continue
        if r.get("state") in LIVE_STATES and r.get("tty"):
            live[r["tty"]] = r
        elif r.get("state") in PARKED_STATES and r.get("iterm_guid"):
            parked[r["iterm_guid"]] = r
    return live, parked


def pane_row(pane, live, parked):
    """The session of one pane, from session_index's two maps: the live row on its tty; only
    when none holds that tty, a parked row bound to its GUID."""
    return live.get(pane.get("tty")) or parked.get(pane["guid"])


def match_panes(panes, rows, running):
    """{guid: row or None} for a tool that acts on a tab. A live row counts only when
    running(row): after a crash a dead row still names a tty some other tab holds now, so a
    tty alone proves nothing. A running process on that tty does, and it is all there is: a
    live row's iterm_guid is NULL until its tab is first seen, and stale after that."""
    live, parked = session_index([r for r in rows if r.get("state") not in LIVE_STATES or running(r)])
    return {p["guid"]: pane_row(p, live, parked) for p in panes}


def build_snapshot(instance, windows, rows, now=None, gui=None):
    """windows: [{"window_id", "tabs": [{"tab_id", "sessions": [{"guid", "tty"}]}]}].
    A pane's session is the live row on its tty; only when none holds that tty, a parked row
    bound to its guid. Guid first would lose a live session: a hibernated row keeps a stale
    guid when a new claude starts in its tab."""
    live, parked = session_index(rows)
    out = []
    for w in windows:
        tabs = []
        for t in w["tabs"]:
            panes = []
            for p in t["sessions"]:
                r = pane_row(p, live, parked) or {}
                panes.append({"guid": p["guid"], "tty": p.get("tty"), "session_id": r.get("session_id"),
                              "state": r.get("state"), "title": r.get("title"), "cwd": r.get("cwd")})
            tabs.append({"tab_id": t["tab_id"], "sessions": panes})
        out.append({"window_id": w["window_id"], "tabs": tabs})
    snap = {"v": VERSION, "iterm_pid": instance["pid"], "iterm_start": instance["start"],
            "taken_at": time.time() if now is None else now, "windows": out}
    if gui:
        snap["gui_session"] = gui
    return snap


def tab_count(snap):
    return sum(len(w.get("tabs") or []) for w in snap.get("windows") or [])


def panes(snap):
    """Every pane of the snapshot, in window, tab, pane order."""
    return [p for w in snap.get("windows") or [] for t in w.get("tabs") or []
            for p in t.get("sessions") or []]


def session_count(snap):
    return len({p["session_id"] for p in panes(snap) if p.get("session_id")})


def _fsync_dir(d):
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data):
    """Temp file in the same dir, fsync, rename, fsync the dir: a power cut leaves the old
    file or the new one, never a torn one."""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(d)


def read_snapshot(path):
    """The parsed snapshot, None when unreadable or not one of ours."""
    try:
        with open(path) as fh:
            snap = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(snap, dict) or snap.get("v") != VERSION or not isinstance(snap.get("windows"), list) \
            or not isinstance(snap.get("taken_at"), (int, float)):
        return None
    return snap


def _content(snap):
    return {k: v for k, v in snap.items() if k not in ("taken_at", "gui_session")}


def snapshot_path(cfg, instance):
    return os.path.join(snapshots_dir(cfg), "%d-%d.json" % (instance["start"], instance["pid"]))


def write_snapshot(cfg, snap):
    """-> "empty" | "unchanged" | "written". Never replaces a file with zero tabs: iTerm2's
    teardown looks like that. A write that more than halves the tab count keeps the current
    file as a sibling first, because a clean quit's teardown can land between ticks."""
    if tab_count(snap) == 0:
        return "empty"
    d = snapshots_dir(cfg)
    os.makedirs(d, exist_ok=True)
    stem = "%d-%d" % (snap["iterm_start"], snap["iterm_pid"])
    path = os.path.join(d, stem + ".json")
    old = read_snapshot(path)
    if old is not None:
        # a file an older daemon wrote gets the field once, or it would never have it
        if _content(old) == _content(snap) and (old.get("gui_session") or not snap.get("gui_session")):
            return "unchanged"
        if tab_count(snap) < tab_count(old) * SHRINK_KEEP_RATIO:
            with open(path, "rb") as fh:
                atomic_write(os.path.join(d, "%s.before-shrink-%d.json" % (stem, old["taken_at"])), fh.read())
    atomic_write(path, json.dumps(snap, indent=1).encode("utf-8"))
    prune(cfg)
    return "written"


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def prune(cfg, keep=KEEP_FILES, now=None):
    """Keep the newest `keep` snapshot files, siblings included; a removed file takes its
    marker along. Temp files a crash stranded and orphaned markers go too."""
    d = snapshots_dir(cfg)
    now = time.time() if now is None else now
    try:
        names = os.listdir(d)
    except OSError:
        return
    snaps = []
    for n in names:
        p = os.path.join(d, n)
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        if SNAP_RE.match(n):
            snaps.append((mtime, n))
        elif n.startswith(".tmp-") and now - mtime > TMP_MAX_AGE_S:
            _unlink(p)
    snaps.sort(reverse=True)
    for _, n in snaps[keep:]:
        _unlink(os.path.join(d, n))
    kept = {n for _, n in snaps[:keep]}
    for n in names:
        for suffix in MARKER_SUFFIXES:
            if n.endswith(suffix) and n[:-len(suffix)] not in kept:
                _unlink(os.path.join(d, n))


def load_entry(path):
    """{"path", "snap", "sibling"} for one snapshot file, None when unreadable."""
    snap = read_snapshot(path)
    if snap is None:
        return None
    return {"path": path, "snap": snap, "sibling": ".before-shrink-" in os.path.basename(path)}


def list_snapshots(cfg):
    """Readable snapshot files, newest taken_at first."""
    d = snapshots_dir(cfg)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for n in names:
        if SNAP_RE.match(n):
            e = load_entry(os.path.join(d, n))
            if e is not None:
                out.append(e)
    out.sort(key=lambda e: e["snap"]["taken_at"], reverse=True)
    return out


def previous(cfg, current, entries=None):
    """The newest snapshot not of the current instance, None when there is none. A shrink
    sibling is never picked: it is the older state of its own main file, opened by name."""
    for e in list_snapshots(cfg) if entries is None else entries:
        if not e["sibling"] and not same_instance(e["snap"], current):
            return e
    return None


def next_instance_start(cfg, snap, current, entries=None):
    """When the iTerm2 instance after this snapshot's started, None when none did. Whatever
    began or ended after that is not something this instance's death took down."""
    starts = {e["snap"].get("iterm_start") for e in (list_snapshots(cfg) if entries is None else entries)}
    if current:
        starts.add(current["start"])
    later = [s for s in starts if s and s > (snap.get("iterm_start") or 0)]
    return min(later) if later else None


def marker_path(path):
    return path + MARKER_SUFFIX


def consumed(path):
    return os.path.exists(marker_path(path))


def mark_consumed(path):
    atomic_write(marker_path(path), ("%s\n" % time.time()).encode("ascii"))


def flagged(path, suffix):
    return os.path.exists(path + suffix)


def set_flag(path, suffix):
    atomic_write(path + suffix, ("%s\n" % time.time()).encode("ascii"))


def lock_path(cfg):
    return os.path.join(snapshots_dir(cfg), "restore.lock")


def restore_lock(cfg):
    """The exclusive restore lock (close the file to release), None when another run holds
    it. Post-hoc binding cannot dedupe two runs that listed GUIDs before either bound."""
    os.makedirs(snapshots_dir(cfg), exist_ok=True)
    fh = open(lock_path(cfg), "a+")
    for i in range(LOCK_TRIES):
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if i + 1 < LOCK_TRIES:
                time.sleep(LOCK_RETRY_S)
    fh.close()
    return None


def restore_running(cfg):
    """True while a restore holds the lock."""
    try:
        fh = open(lock_path(cfg))
    except OSError:
        return False
    try:
        fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        fh.close()


def clean_title(title):
    """A title safe to put inside an OSC sequence: printable characters only."""
    return "".join(c for c in str(title or "") if c.isprintable())


def when(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
